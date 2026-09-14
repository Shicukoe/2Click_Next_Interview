"""End-to-end tests of ASSIGNMENT.md, following the application's workflow. Run by ./test.sh in the app container.

  data         archive → importer → Postgres: every value, normalisation, relationships, a failed import
  fingerprint  prints a checksum of every table (taken before a restart)
  kept         after `docker compose down` + start: every table identical, nothing imported twice
  web          page → request → backend → database → page, for each CRM requirement
  agent        Toolbox → Preparer → Checker → Coordinator → saved run
  scale        the everyday queries are served by indexes

Each line names the layer it checks: [file] [importer] [database] [page] [request] [backend]
[tool] [preparer] [checker] [coordinator] [orchestrator] [stand-in] [decision].
Nothing is tied to one record: test data is chosen by query, expected counts come from manifest.json.

Usage: python - <phase>
"""

import copy
import csv
import hashlib
import inspect
import json
import os
import shutil
import socket
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta
from decimal import Decimal
from html.parser import HTMLParser
from pathlib import Path
from urllib import error, parse, request
from zoneinfo import ZoneInfo

import psycopg
from markupsafe import escape

from app import assistant, importer, policy, queries, stand_in_model
from app.db import connect

BASE = "http://localhost:3000"
DATA = Path("data")
ROME = ZoneInfo("Europe/Rome")
MARK = "test.sh check"  # every text these tests write starts with this
TABLES = {"companies": "companies", "contacts": "contacts", "fair_editions": "fair_editions",
          "opportunities": "opportunities", "activities": "activity_log_entries"}
ROLES = ("preparer_write_brief", "checker_review", "coordinator_decide")


# ── Helpers ───────────────────────────────────────────────────────────────────

def ok(layer, text):
    print(f"  ok  [{layer}] {text}")


def section(title):
    print(f"\n  {title}")


class _Stay(request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def send(path, form=None, follow=True):
    """GET, or POST when form is given. Returns (status, final url or the redirect's Location, html)."""
    body = parse.urlencode(form).encode() if form is not None else None
    opener = request.build_opener() if follow else request.build_opener(_Stay)
    try:
        with opener.open(BASE + path, body) as r:
            return r.status, r.url, r.read().decode()
    except error.HTTPError as e:
        return e.code, e.headers.get("Location") or e.url, e.read().decode()


def page(path, form=None):
    status, url, html = send(path, form)
    assert status == 200, f"{path} answered {status}"
    return url, html


def shows(html, text):
    return str(escape(text)) in html


class Forms(HTMLParser):
    """Every form on a page: method, action, and each field's name with the choices a select offers."""

    def __init__(self, html):
        super().__init__()
        self.forms, self.form, self.select, self.text_option = [], None, None, False
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form":
            self.form = {"method": a.get("method", "get").lower(), "action": a.get("action"), "fields": {}}
            self.forms.append(self.form)
        elif self.form is not None and tag in ("input", "textarea", "select") and a.get("name"):
            self.form["fields"][a["name"]] = []
            self.select = a["name"] if tag == "select" else None
        elif self.form is not None and tag == "option" and self.select:
            if "value" in a:
                self.form["fields"][self.select].append(a["value"])
            else:
                self.text_option = True

    def handle_data(self, data):
        if self.text_option:
            self.form["fields"][self.select].append(data.strip())
            self.text_option = False

    def handle_endtag(self, tag):
        if tag == "form":
            self.form = None
        elif tag == "select":
            self.select = None


def form_on(html, action, method="post"):
    for form in Forms(html).forms:
        if form["action"] == action and form["method"] == method:
            return form
    raise AssertionError(f"the page has no {method} form sending to {action}")


def manifest():
    return json.loads((DATA / "manifest.json").read_text())


def csv_rows(name, folder=DATA):
    with open(folder / name, encoding="utf-8", newline="") as f:
        yield from csv.DictReader(f, delimiter=";")


def as_decimal(raw):
    return Decimal(raw.replace(",", ".")) if raw else None


def as_date(raw):
    return datetime.strptime(raw, "%d/%m/%Y").date() if raw else None


def one(conn, sql, params=()):
    return conn.execute(sql, params).fetchone()


def enquiry_under_test(conn):
    """An active enquiry for a future edition, whose company has a second enquiry, company-level notes
    and a contact with an email, so that no check runs against an empty list."""
    return one(conn, """
        SELECT o.opportunity_code AS code, o.status, o.company_code, c.company_name, c.sales_rep,
               s.opportunity_code AS sibling, t.first_name || ' ' || t.last_name AS contact_name, t.email,
               oc.first_name || ' ' || oc.last_name AS enquiry_contact, oc.phone AS enquiry_phone,
               oc.email AS enquiry_email
        FROM opportunities o
        JOIN fair_editions e ON e.fair_edition_code = o.fair_edition_code
        JOIN companies c ON c.company_code = o.company_code
        JOIN contacts oc ON oc.contact_code = o.contact_code
        JOIN contacts t ON t.company_code = o.company_code AND t.email IS NOT NULL
        JOIN opportunities s ON s.company_code = o.company_code AND s.opportunity_code <> o.opportunity_code
        WHERE o.status NOT IN ('lost', 'won') AND e.ends_on >= current_date
          AND EXISTS (SELECT 1 FROM activities n WHERE n.company_code = o.company_code AND n.opportunity_code IS NULL)
          AND (SELECT count(*) FROM activities a WHERE a.opportunity_code = o.opportunity_code) >= 1
        ORDER BY o.opportunity_code, t.contact_code, s.opportunity_code
        LIMIT 1
    """)


# ── DATA: archive → importer → Postgres ───────────────────────────────────────

def data():
    m = manifest()
    section("D1  The whole archive is imported, once")
    for name, meta in m["files"].items():
        assert hashlib.sha256((DATA / name).read_bytes()).hexdigest() == meta["sha256"], f"{name} was edited"
    ok("file", "data/ matches the SHA-256 checksums in manifest.json: source files unchanged")
    with connect() as conn:
        for table, entity in TABLES.items():
            n = one(conn, f"SELECT count(*) AS n FROM {table}")["n"]
            assert n == m["entities"][entity], f"{table}: {n} rows, manifest says {m['entities'][entity]}"
        ok("database", "row counts equal the manifest: " + ", ".join(f"{t} {m['entities'][e]:,}" for t, e in TABLES.items()))
        state = conn.execute("SELECT archive_reference_time FROM import_state").fetchall()
        assert [s["archive_reference_time"].timestamp() for s in state] == [
            datetime.fromisoformat(m["reference_time"]).timestamp()]
        assert one(conn, "SELECT count(*) AS n FROM assistant_runs")["n"] == 0
        ok("database", f"one import recorded in import_state, with the export time from manifest.json "
                       f"({m['reference_time']}); no assistant runs or app entries yet")
        columns = {r["column_name"] for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_schema = 'public'")}
        assert not {"legacy_row_id", "legacy_print_layout"} & columns
        ok("database", "only the two documented exclusions are left out: legacy_row_id, legacy_print_layout")

        section("D2  Every value converted by the documented rules")
        values_match_the_files(conn)

        section("D3  Empty means unknown: NULL exactly where data/README.md allows it")
        nullable = {f"{r['table_name']}.{r['column_name']}" for r in conn.execute(
            "SELECT table_name, column_name FROM information_schema.columns"
            " WHERE table_schema = 'public' AND is_nullable = 'YES'")}
        documented = {"companies.fax", "contacts.email", "contacts.phone", "opportunities.contact_code",
                      "opportunities.expected_close_on", "opportunities.historical_campaign_code",
                      "opportunities.stand_area_sqm", "opportunities.client_budget_eur",
                      "opportunities.requested_height_m", "activities.opportunity_code", "activities.follow_up_on",
                      "activities.completed"}
        app_only = {"opportunities.updated_at", "activities.follow_up_done_at",  # set by the app
                    "import_state.archive_reference_time"}  # from manifest.json, not from the CSV files
        assert nullable == documented | app_only, sorted(nullable ^ (documented | app_only))
        ok("database", f"the {len(documented)} columns documented as optional are nullable; every other imported column is NOT NULL")
        text_columns = conn.execute("SELECT table_name, column_name FROM information_schema.columns"
                                    " WHERE table_schema = 'public' AND data_type = 'text'").fetchall()
        for c in text_columns:
            empty = one(conn, f"SELECT count(*) AS n FROM {c['table_name']} WHERE {c['column_name']} = ''")["n"]
            assert empty == 0, f"{c['table_name']}.{c['column_name']} has {empty} empty strings"
        ok("database", f"no empty string in any of {len(text_columns)} text columns: an empty field is stored as NULL")

        section("D4  Relationships kept")
        assert one(conn, """SELECT count(*) AS n FROM opportunities o JOIN contacts t ON t.contact_code = o.contact_code
                            WHERE t.company_code <> o.company_code""")["n"] == 0
        assert one(conn, """SELECT count(*) AS n FROM activities a JOIN opportunities o USING (opportunity_code)
                            WHERE a.company_code <> o.company_code""")["n"] == 0
        ok("database", "every enquiry's contact and every entry's enquiry belong to the same company")
        split = one(conn, """SELECT count(*) AS n FROM (SELECT company_code FROM opportunities
                             GROUP BY company_code HAVING count(DISTINCT fair_edition_code) > 1) x""")["n"]
        assert split > 0
        ok("database", f"{split:,} exhibitors keep one company record across several fair editions (no re-entered contacts)")

    section("D5  A broken file fails loudly and leaves nothing half-loaded")
    broken_import_leaves_nothing()


def values_match_the_files(conn):
    companies = {}
    for r in csv_rows("companies_and_contacts.csv"):
        companies.setdefault(r["company_code"], set()).add(
            (r["company_name"], r["province_code"], r["region"], r["sales_rep"], r["fax"] or None))
    assert all(len(details) == 1 for details in companies.values()), "a company's details differ between rows"
    saved = {r["company_code"]: (r["company_name"], r["province_code"], r["region"], r["sales_rep"], r["fax"])
             for r in conn.execute("SELECT * FROM companies")}
    assert saved == {code: details.pop() for code, details in companies.items()}
    ok("importer", f"companies: {m_rows('companies_and_contacts.csv'):,} contact rows → {len(saved):,} companies; "
                   "details identical on every row, so deduplicated by company_code without guessing")

    saved = {r["contact_code"]: r for r in conn.execute("SELECT * FROM contacts")}
    for r in csv_rows("companies_and_contacts.csv"):
        s = saved[r["contact_code"]]
        assert (s["company_code"], s["first_name"], s["last_name"], s["email"], s["phone"]) == (
            r["company_code"], r["contact_first_name"], r["contact_last_name"], r["email"] or None, r["phone"] or None)
    ok("importer", "contacts: code, company, names, email and phone as in the file; empty email/phone → NULL")

    saved = {r["fair_edition_code"]: r for r in conn.execute("SELECT * FROM fair_editions")}
    for r in csv_rows("fair_editions.csv"):
        s = saved[r["fair_edition_code"]]
        assert (s["fair_name"], s["city"], s["venue"], s["starts_on"], s["ends_on"], s["max_stand_height_m"]) == (
            r["fair_name"], r["city"], r["venue"], as_date(r["starts_on"]), as_date(r["ends_on"]),
            as_decimal(r["max_stand_height_m"]))
    ok("importer", "fair editions: DD/MM/YYYY dates and decimal-comma height limits converted")

    saved = {r["opportunity_code"]: r for r in conn.execute("SELECT * FROM opportunities")}
    spellings = set()
    for r in csv_rows("opportunities.csv"):
        s = saved[r["opportunity_code"]]
        spellings.add(r["legacy_status"])
        assert s["status"] == r["legacy_status"].strip().lower() and s["legacy_status"] == r["legacy_status"]
        assert (s["company_code"], s["contact_code"], s["fair_edition_code"], s["description"], s["brief_notes"],
                s["opened_on"], s["expected_close_on"], s["historical_campaign_code"]) == (
            r["company_code"], r["contact_code"] or None, r["fair_edition_code"], r["description"], r["brief_notes"],
            as_date(r["opened_on"]), as_date(r["expected_close_on"]), r["historical_campaign_code"] or None)
        for column in ("amount_eur", "client_budget_eur", "stand_area_sqm", "requested_height_m"):
            assert s[column] == as_decimal(r[column]), (r["opportunity_code"], column)
        assert s["updated_at"] is None
    statuses = {s.strip().lower() for s in spellings}
    ok("importer", f"opportunities: {len(spellings)} status spellings → {len(statuses)} statuses {sorted(statuses)}, "
                   "original kept in legacy_status")
    ok("importer", "opportunities: amounts, area, budget, height from decimal commas; empty budget/area/height → NULL, never 0")

    saved = {r["entry_id"]: r for r in conn.execute("SELECT * FROM activities")}
    for r in csv_rows("activity_log.csv"):
        s = saved[r["entry_id"]]
        rome = datetime.strptime(r["occurred_at"], "%d/%m/%Y %H:%M").replace(tzinfo=ROME)  # fold=0 in a clock change
        assert s["occurred_at"].timestamp() == rome.timestamp(), (r["entry_id"], s["occurred_at"], r["occurred_at"])
        assert (s["company_code"], s["opportunity_code"], s["activity_type"], s["details"], s["follow_up_on"],
                s["completed"], s["author"], s["follow_up_done_at"]) == (
            r["company_code"], r["opportunity_code"] or None, r["activity_type"], r["details"],
            as_date(r["follow_up_on"]), {"Y": True, "N": False, "": None}[r["completion_marker"]],
            r["legacy_author"], None), r["entry_id"]
    ok("importer", "activities: date-times read as Europe/Rome (clock changes included), Y/N/empty → true/false/NULL, "
                   "embedded semicolons parsed, every imported follow-up starts open")


def m_rows(name):
    return manifest()["files"][name]["data_rows"]


def broken_import_leaves_nothing():
    url = os.environ["DATABASE_URL"]
    test_url = url.rsplit("/", 1)[0] + "/crm_import_test"
    with psycopg.connect(url, autocommit=True) as admin:
        admin.execute("DROP DATABASE IF EXISTS crm_import_test")
        admin.execute("CREATE DATABASE crm_import_test")
    folder = Path(tempfile.mkdtemp())
    for name in manifest()["files"]:
        shutil.copy(DATA / name, folder / name)
    row = next(csv_rows("activity_log.csv"))
    with open(folder / "activity_log.csv", "a", encoding="utf-8", newline="") as f:
        csv.writer(f, delimiter=";", lineterminator="\n").writerow(
            (row | {"entry_id": "AC9999999", "occurred_at": "31/02/2026 10:00"}).values())
    saved_dir, saved_url = importer.DATA_DIR, os.environ["DATABASE_URL"]
    importer.DATA_DIR, os.environ["DATABASE_URL"] = folder, test_url
    try:
        importer.main()
        failed = False
    except ValueError:
        failed = True
    finally:
        importer.DATA_DIR, os.environ["DATABASE_URL"] = saved_dir, saved_url
        shutil.rmtree(folder)
    with psycopg.connect(test_url) as conn:
        tables = conn.execute("SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'").fetchone()[0]
    with psycopg.connect(url, autocommit=True) as admin:
        admin.execute("DROP DATABASE crm_import_test")
    assert failed and tables == 0, (failed, tables)
    ok("importer", "the real files plus one row dated 31/02/2026, in a scratch database: the import stops "
                   "and leaves 0 tables, so the next start retries from scratch")


# ── DATA: idempotency across restarts ─────────────────────────────────────────

def table_checksums():
    with connect() as conn:
        return {t: one(conn, f"SELECT count(*) AS n, md5(string_agg(x::text, '|' ORDER BY x::text)) AS md5 FROM {t} x")
                for t in [*TABLES, "assistant_runs", "import_state"]}


def fingerprint():
    print(json.dumps(table_checksums()))


def kept():
    section("D6  docker compose down + ./dev.sh keeps user changes and does not import again")
    before, now = json.loads(os.environ["BEFORE"]), table_checksums()
    assert now == before, {t: (before[t], now[t]) for t in now if now[t] != before[t]}
    ok("database", "all 7 tables are byte-for-byte identical before and after the restart (count + md5 of every row)")
    m = manifest()
    with connect() as conn:
        app_entries = one(conn, "SELECT count(*) AS n FROM activities WHERE entry_id LIKE 'APP%%'")["n"]
        edited = one(conn, "SELECT count(*) AS n FROM opportunities WHERE updated_at IS NOT NULL")["n"]
        runs = one(conn, "SELECT count(*) AS n FROM assistant_runs")["n"]
        assert app_entries and edited and runs, (app_entries, edited, runs)
        for table, entity in TABLES.items():
            n = one(conn, f"SELECT count(*) AS n FROM {table}")["n"]
            assert n == m["entities"][entity] + (app_entries if table == "activities" else 0), (table, n)
    ok("importer", f"the {app_entries} logged entries, {edited} edited brief(s) and {runs} runs survived; "
                   "imported rows still equal the manifest, so nothing was imported twice")


# ── WEB: page → request → backend → database → page ───────────────────────────

def web():
    with connect() as conn:
        o = enquiry_under_test(conn)
    assert o, "no active enquiry for a future fair edition to test with"
    print(f"  (enquiry under test: {o['code']} of {o['company_code']}; its company's other enquiry: {o['sibling']})")
    find_exhibitor_or_contact(o)
    update_opportunity(o)
    entry_id = record_conversation(o)
    follow_up(o, entry_id)


def find_exhibitor_or_contact(o):
    section("W1  Find an exhibitor or contact")
    _, html = page("/")
    assert "q" in form_on(html, "/", "get")["fields"]
    ok("page", "Exhibitors has a search form sending q to /")

    with connect() as conn:
        truth = one(conn, "SELECT contact_code FROM contacts WHERE email = %s", (o["email"],))["contact_code"]
        searches = [("contact email", o["email"], lambda cs, ts: truth in [t["contact_code"] for t in ts]),
                    ("company name", o["company_name"], lambda cs, ts: o["company_code"] in [c["company_code"] for c in cs]),
                    ("company code", o["company_code"].lower(), lambda cs, ts: o["company_code"] in [c["company_code"] for c in cs]),
                    ("contact name in capitals", o["contact_name"].upper(),
                     lambda cs, ts: ts and all(o["contact_name"].lower() in f"{t['first_name']} {t['last_name']}".lower() for t in ts))]
        for label, text, found in searches:
            companies, contacts = queries.search(conn, text)
            assert found(companies, contacts), f"searching the {label} {text!r} did not return the right rows"
            _, html = page("/?" + parse.urlencode({"q": text}))
            for row in companies + contacts:
                assert row["company_code"] in html and (row.get("email") is None or shows(html, row["email"]))
            ok("backend→page", f"{label}: the query returns the right record and the page shows all "
                               f"{len(companies)} companies + {len(contacts)} contacts it returned")
        assert queries.search(conn, "%%%") == ([], [])
    _, html = page("/?q=%25%25%25")
    assert "No company matches." in html and "No contact matches." in html
    ok("backend", "a typed % is matched literally, not as a wildcard")
    _, html = page("/?q=ab")
    assert "at least 3 characters" in html
    ok("backend", "fewer than 3 characters asks for more instead of scanning")
    status, location, _ = send("/?q=" + o["code"].lower(), follow=False)
    assert status == 302 and location.endswith(f"/opportunities/{o['code']}"), (status, location)
    ok("request", "an opportunity code in any case redirects straight to that enquiry")

    section("W1  …and see the relevant opportunities and fair editions")
    with connect() as conn:
        truth = conn.execute("""SELECT o.opportunity_code, o.status, e.fair_edition_code, e.starts_on
                                FROM opportunities o JOIN fair_editions e USING (fair_edition_code)
                                WHERE o.company_code = %s""", (o["company_code"],)).fetchall()
        backend = queries.opportunities_of(conn, o["company_code"])
        contacts = queries.contacts_of(conn, o["company_code"])
        notes = queries.company_level_entries(conn, o["company_code"])
    assert {r["opportunity_code"] for r in backend} == {r["opportunity_code"] for r in truth}
    ok("database→backend", f"{o['company_code']} has {len(truth)} enquiries in the database; the page query returns all of them")
    _, html = page(f"/companies/{o['company_code']}")
    for r in truth:
        assert r["opportunity_code"] in html and r["fair_edition_code"] in html
    positions = [html.index(r["fair_edition_code"]) for r in sorted(truth, key=lambda r: r["starts_on"], reverse=True)]
    assert positions == sorted(positions)
    for t in contacts:
        assert t["contact_code"] in html
    for n in notes:
        assert shows(html, n["details"])
    ok("page", f"the company page shows every enquiry under its fair edition (newest first), all {len(contacts)} "
               f"contacts and {len(notes)} company-level notes")


def update_opportunity(o):
    section("W2  Update an opportunity")
    path = f"/opportunities/{o['code']}"
    _, html = page(path)
    form = form_on(html, path)
    with connect() as conn:
        statuses = queries.statuses(conn)
    assert {"status", "client_budget_eur", "stand_area_sqm", "requested_height_m", "brief_notes"} <= form["fields"].keys()
    assert form["fields"]["status"] == statuses
    ok("page", f"the enquiry page has a brief form posting to {path}; its status choices are the database's {statuses}")

    sent = {"status": o["status"], "client_budget_eur": "45000,50", "stand_area_sqm": "", "requested_height_m": "",
            "brief_notes": f"{MARK}: customer will confirm the floor area on Friday"}
    status, location, _ = send(path, sent, follow=False)
    assert status == 302 and location.endswith(path), (status, location)
    ok("request", "a valid brief is accepted and redirects back to the enquiry")
    read = ("SELECT status, client_budget_eur, stand_area_sqm, requested_height_m, brief_notes,"
            " updated_at > now() - interval '5 minutes' AS just_updated FROM opportunities WHERE opportunity_code = %s")
    with connect() as conn:
        saved = one(conn, read, (o["code"],))
    assert saved == {"status": o["status"], "client_budget_eur": Decimal("45000.50"), "stand_area_sqm": None,
                     "requested_height_m": None, "brief_notes": sent["brief_notes"], "just_updated": True}, saved
    ok("database", "saved as sent: '45000,50' → 45000.50, empty area and height → NULL (unknown), notes, updated_at = now")
    _, html = page(path)
    assert 'value="45000.50"' in html and shows(html, sent["brief_notes"])
    ok("page", "the reloaded enquiry shows the saved values")

    for bad in ({"client_budget_eur": "abc"}, {"stand_area_sqm": "-5"}, {"requested_height_m": "1000"},
                {"status": "approved"}, {"brief_notes": "  "}):
        status, _, html = send(path, sent | bad, follow=False)
        assert status == 400 and 'class="alert error"' in html, (bad, status)
    with connect() as conn:
        assert one(conn, read, (o["code"],)) == saved
    ok("backend", "text as a number, a negative area, a height too large, an unknown status and empty notes → 400 with a message")
    ok("database", "none of those rejected forms changed the row")
    assert send("/opportunities/NOPE", sent, follow=False)[0] == 404
    ok("backend", "an unknown enquiry → 404")


def record_conversation(o):
    section("W3  Record a customer conversation, shown only on its own enquiry")
    action = f"/opportunities/{o['code']}/entries"
    _, html = page(f"/opportunities/{o['code']}")
    form = form_on(html, action)
    assert form["fields"].keys() == {"activity_type", "follow_up_on", "details"}
    assert form["fields"]["activity_type"] == list(queries.ACTIVITY_TYPES)
    ok("page", f"the enquiry page has a log form posting to {action}: type {list(queries.ACTIVITY_TYPES)}, details, follow-up date")

    with connect() as conn:
        in_30_days = one(conn, "SELECT current_date + 30 AS d")["d"]
        count = one(conn, "SELECT count(*) AS n FROM activities")["n"]
    call = {"activity_type": "call", "follow_up_on": in_30_days.isoformat(),
            "details": f"{MARK}: <script>alert(1)</script> customer will confirm the floor area"}
    status, location, _ = send(action, call, follow=False)
    assert status == 302 and location.endswith(f"/opportunities/{o['code']}#entries"), (status, location)
    ok("request", "a valid entry is accepted and redirects back to the enquiry's conversations")
    with connect() as conn:
        entry = one(conn, "SELECT *, occurred_at > now() - interval '5 minutes' AS just_now FROM activities"
                          " WHERE details = %s", (call["details"],))
    assert entry["entry_id"].startswith("APP") and entry["just_now"]
    assert (entry["company_code"], entry["opportunity_code"], entry["activity_type"], entry["follow_up_on"],
            entry["follow_up_done_at"], entry["completed"], entry["author"]) == (
        o["company_code"], o["code"], "call", in_30_days, None, True, queries.AUTHOR), entry
    ok("database", f"saved as {entry['entry_id']}: linked to {o['code']} and, from the enquiry, to {o['company_code']}; "
                   "occurred now; a logged call counts as completed")
    _, html = page(f"/opportunities/{o['code']}")
    assert shows(html, call["details"]) and "<script>alert(1)</script>" not in html
    ok("page", "the enquiry lists the call, with the <script> shown as text")
    for other in (f"/opportunities/{o['sibling']}", f"/companies/{o['company_code']}"):
        assert MARK not in page(other)[1], f"the call leaked onto {other}"
    ok("page", f"the call is not on {o['sibling']} (same company, other enquiry) nor in the company-level notes")

    for kind, completed in (("note", None), ("task", False)):
        page(action, {"activity_type": kind, "follow_up_on": "", "details": f"{MARK}: a {kind}"})
        with connect() as conn:
            assert one(conn, "SELECT completed FROM activities WHERE details = %s", (f"{MARK}: a {kind}",))["completed"] is completed
    ok("database", "a note is saved with completed = NULL (not applicable), a task with completed = false (to do)")

    with connect() as conn:
        count = one(conn, "SELECT count(*) AS n FROM activities")["n"]
    for bad in ({"activity_type": "fax"}, {"details": "  "}, {"details": "x" * 2001}, {"follow_up_on": "2026-02-30"}):
        assert send(action, call | bad, follow=False)[0] == 400, bad
    assert send("/opportunities/NOPE/entries", call, follow=False)[0] == 404
    with connect() as conn:
        assert one(conn, "SELECT count(*) AS n FROM activities")["n"] == count
    ok("backend", "an unknown type, empty or over-long details and an impossible date → 400; unknown enquiry → 404")
    ok("database", "no row was added by any rejected entry")
    return entry["entry_id"]


TAB_LABELS = {"late": "Late", "today": "Today", "week": "Next 7 days", "later": "Later",
              "before_export": "Before the export"}


def expected_tab(due, today, export):
    """The tab a follow-up belongs to, written here independently of the SQL in queries.py."""
    if export and due < export:
        return "before_export"
    if due < today:
        return "late"
    if due == today:
        return "today"
    return "week" if due <= today + timedelta(days=7) else "later"


def row_of(html, entry_id):
    """The table row holding an entry's Mark done button."""
    return next(r for r in html.split("<tr>") if f"/entries/{entry_id}/done" in r)


def follow_up(o, entry_id):
    section("W4  Schedule a follow-up and find it again later: who to call and what they're waiting for")
    _, html = page("/follow-ups")
    form = form_on(html, "/follow-ups", "get")
    with connect() as conn:
        reps = queries.sales_reps(conn)
        entry = one(conn, "SELECT * FROM activities WHERE entry_id = %s", (entry_id,))
        today = one(conn, "SELECT current_date AS d")["d"]
    export = datetime.fromisoformat(manifest()["reference_time"]).date()
    assert {"tab", "rep", "day"} <= form["fields"].keys() and form["fields"]["rep"] == ["", *reps]
    assert all(f"{label} · " in html for label in TAB_LABELS.values()) and 'class="active">Today · ' in html
    ok("page", "Follow-ups opens on Today; tabs Late, Today, Next 7 days, Later, Before the export; "
               "filters for account owner (the database's 6) and due day")

    rep = o["sales_rep"]
    with connect() as conn:
        dates = [r["follow_up_on"] for r in conn.execute(
            """SELECT a.follow_up_on FROM activities a JOIN companies c USING (company_code)
               WHERE a.follow_up_on IS NOT NULL AND a.follow_up_done_at IS NULL AND c.sales_rep = %s""", (rep,))]
        _, counts = queries.open_follow_ups(conn, "today", rep)
    truth = {tab: sum(expected_tab(d, today, export) == tab for d in dates) for tab in TAB_LABELS}
    assert counts == truth, (counts, truth)
    _, html = page("/follow-ups?" + parse.urlencode({"rep": rep}))
    assert all(f"{TAB_LABELS[tab]} · {n}" in html for tab, n in truth.items())
    ok("database→backend→page", f"{rep}'s {len(dates)} open follow-ups split by date exactly as the rules say "
                                f"(export {export:%d/%m/%Y}): " + ", ".join(f"{TAB_LABELS[t]} {n}" for t, n in truth.items()))

    day = entry["follow_up_on"]
    listing = "/follow-ups?" + parse.urlencode({"rep": rep, "day": day.isoformat()})
    with connect() as conn:
        truth = {r["entry_id"] for r in conn.execute(
            """SELECT a.entry_id FROM activities a JOIN companies c USING (company_code)
               WHERE a.follow_up_on = %s AND a.follow_up_done_at IS NULL AND c.sales_rep = %s""", (day, rep))}
        rows, _ = queries.open_follow_ups(conn, "today", rep, day)
    assert {r["entry_id"] for r in rows} == truth and entry_id in truth
    ok("database→backend", f"Due on {day:%d/%m/%Y} ({expected_tab(day, today, export)} tab) for {rep}: "
                           f"the query returns exactly the {len(truth)} open follow-ups of that day, ours included")
    _, html = page(listing)
    buttons = {f["action"] for f in Forms(html).forms if f["action"].startswith("/entries/")}
    assert buttons == {f"/entries/{r['entry_id']}/done" for r in rows}
    row = row_of(html, entry_id)
    contact = o["enquiry_contact"], o["enquiry_phone"] or o["enquiry_email"] or "no phone or email"
    assert shows(row, contact[0]) and shows(row, contact[1]) and shows(row, entry["details"]) and o["code"] in row
    ok("page", f"each of the {len(rows)} rows has Mark done; ours shows who to call ({contact[0]}, {contact[1]}), "
               "the company and enquiry, and what they're waiting for")

    with connect() as conn:
        company_level = one(conn, """SELECT a.entry_id, a.follow_up_on, c.sales_rep FROM activities a
                                     JOIN companies c USING (company_code)
                                     WHERE a.opportunity_code IS NULL AND a.follow_up_on IS NOT NULL
                                       AND a.follow_up_done_at IS NULL ORDER BY a.entry_id LIMIT 1""")
    _, html = page("/follow-ups?" + parse.urlencode({"rep": company_level["sales_rep"],
                                                     "day": company_level["follow_up_on"].isoformat()}))
    row = row_of(html, company_level["entry_id"])
    assert "No contact recorded" in row and "company-level" in row
    ok("page", "a company-level follow-up says 'No contact recorded · company-level' instead of guessing a person")

    for due, text in ((today, "call back today"), (today - timedelta(days=1), "was due yesterday")):
        page(f"/opportunities/{o['code']}/entries", {"activity_type": "task", "follow_up_on": due.isoformat(),
                                                       "details": f"{MARK}: {text}"})
        with connect() as conn:
            new = one(conn, "SELECT entry_id FROM activities WHERE details = %s", (f"{MARK}: {text}",))["entry_id"]
            tab = expected_tab(due, today, export)
            listed, _ = queries.open_follow_ups(conn, tab, rep)
            elsewhere = [r["entry_id"] for t in TAB_LABELS if t != tab for r in queries.open_follow_ups(conn, t, rep)[0]]
        assert new in [r["entry_id"] for r in listed] and new not in elsewhere, (text, tab)
        _, html = page("/follow-ups?" + parse.urlencode({"tab": tab, "rep": rep}))
        row = row_of(html, new)
        assert ("overdue" in row) == (tab in ("late", "before_export"))
        ok("backend→page", f"a follow-up that {text.replace('call back today', 'is due today')} is on the "
                           f"{TAB_LABELS[tab]} tab only{', its date in red' if tab != 'today' else ''}")
        late = new

    with connect() as conn:
        old = one(conn, """SELECT a.entry_id FROM activities a JOIN companies c USING (company_code)
                           WHERE c.sales_rep = %s AND a.follow_up_on < %s AND a.follow_up_done_at IS NULL
                           ORDER BY a.follow_up_on DESC, a.entry_id LIMIT 1""", (rep, export))
    _, html = page("/follow-ups?" + parse.urlencode({"tab": "before_export", "rep": rep}))
    assert f"/entries/{old['entry_id']}/done" in html and "never recorded whether these were done" in html
    ok("page", "follow-ups dated before the export are still open, on their own tab, with the reason explained")

    status, location, _ = send(f"/entries/{entry_id}/done", {"back": listing}, follow=False)
    assert status == 302 and parse.unquote_plus(location) == parse.unquote_plus(listing), (status, location)
    ok("request", "Mark done redirects back to the list it was pressed on")
    with connect() as conn:
        done_at = one(conn, "SELECT follow_up_done_at FROM activities WHERE entry_id = %s", (entry_id,))["follow_up_done_at"]
        send(f"/entries/{entry_id}/done", {"back": listing})
        again = one(conn, "SELECT follow_up_done_at FROM activities WHERE entry_id = %s", (entry_id,))["follow_up_done_at"]
    assert done_at is not None and again == done_at
    ok("database", "follow_up_done_at is set; pressing Mark done again keeps the first time (safe to repeat)")
    _, html = page(listing)
    assert not shows(html, entry["details"])
    _, html = page(f"/opportunities/{o['code']}")
    assert f"Done {done_at:%d/%m/%Y}" in html
    ok("page", "it has left the follow-ups list and shows as 'Done' on its enquiry")

    page(f"/entries/{late}/done", {"back": "/follow-ups"})
    with connect() as conn:
        assert one(conn, "SELECT completed FROM activities WHERE entry_id = %s", (late,))["completed"] is True
    ok("database", "marking a task's follow-up done also completes the task")

    for back in ("//evil.example", "https://evil.example", "/\\evil.example", "/\t/evil.example", "javascript:alert(1)"):
        status, location, _ = send(f"/entries/{entry_id}/done", {"back": back}, follow=False)
        assert status == 302 and location.endswith("/follow-ups"), (back, location)
    assert send("/entries/NOPE/done", {"back": "/"}, follow=False)[0] == 404
    ok("backend", "a 'back' pointing to another site (5 tricks, incl. a tab) goes to /follow-ups instead; unknown entry → 404")


# ── AGENT: Toolbox → Preparer → Checker → Coordinator → saved run ─────────────

def agent():
    with connect() as conn:
        o = enquiry_under_test(conn)
        facts = tools_bring_the_data(conn, o)
    complete = facts | {"client_budget_eur": Decimal("50000.00"), "stand_area_sqm": Decimal("60.00"),
                        "requested_height_m": facts["max_stand_height_m"]}
    orchestration(complete)
    checker_covers_each_request(complete)
    decisions_through_the_web(o)


class Recorder:
    """Stands in for a connection and remembers every SQL statement sent through it."""

    def __init__(self, conn):
        self.conn, self.statements = conn, []

    def execute(self, sql, params=None):
        self.statements.append((sql, params))
        return self.conn.execute(sql, params)


def tools_bring_the_data(conn, o):
    section("A1  Tools: the CRM and fair data reaches the agent through the Toolbox, and only there")
    tools = assistant.Toolbox(conn)
    facts, activity = assistant.preparer_gather_facts(tools, o["code"])

    opp = one(conn, "SELECT * FROM opportunities WHERE opportunity_code = %s", (o["code"],))
    company = one(conn, "SELECT * FROM companies WHERE company_code = %s", (opp["company_code"],))
    contact = one(conn, "SELECT * FROM contacts WHERE contact_code = %s", (opp["contact_code"],))
    edition = one(conn, "SELECT * FROM fair_editions WHERE fair_edition_code = %s", (opp["fair_edition_code"],))
    expected = {k: opp[k] for k in ("opportunity_code", "status", "client_budget_eur", "stand_area_sqm",
                                    "requested_height_m", "brief_notes")}
    expected |= {k: edition[k] for k in ("fair_edition_code", "fair_name", "city", "starts_on", "ends_on",
                                         "max_stand_height_m")}
    expected |= {"company_name": company["company_name"],
                 "contact_name": f"{contact['first_name']} {contact['last_name']}" if contact else None,
                 "today": datetime.now(ROME).date()}
    assert facts == expected, {k: (facts.get(k), v) for k, v in expected.items() if facts.get(k) != v}
    ok("tool", "get_opportunity + get_today give the Preparer the brief, company, contact and fair edition "
               "(dates, height limit) exactly as stored in their 4 tables, and today's date in Rome")

    own = conn.execute("SELECT occurred_at, activity_type, details, follow_up_on FROM activities"
                       " WHERE opportunity_code = %s ORDER BY occurred_at DESC, entry_id DESC", (o["code"],)).fetchall()
    assert activity == own[:3] and len(own) >= 3
    ok("tool", f"get_recent_activity returns the 3 newest of {o['code']}'s {len(own)} entries, none from other enquiries")

    assert [c["tool"] for c in tools.log] == ["get_opportunity", "get_today", "get_recent_activity"]
    assert tools.log[2]["args"] == {"opportunity_code": o["code"], "limit": 3}
    ok("tool", "every call is logged with its arguments and result: this log becomes the run's input_snapshot")
    try:
        assistant.preparer_gather_facts(assistant.Toolbox(conn), "NOPE")
        raise AssertionError("an unknown enquiry was not refused")
    except LookupError:
        ok("tool", "an unknown enquiry is refused before any role runs")

    recorder = Recorder(conn)
    assistant.run_and_save(recorder, o["code"])
    conn.rollback()
    sql = [s for s, _ in recorder.statements]
    assert sql[:2] == [assistant.OPPORTUNITY_SQL, assistant.ACTIVITY_SQL] and len(sql) == 4
    assert "assistant_runs" in sql[2] and sql[3].startswith("INSERT INTO assistant_runs")
    ok("tool", "a whole run sends 4 statements: the 2 Toolbox reads, then numbering and saving the run; "
               "the Preparer's brief, the Checker and the Coordinator send none")
    return facts


@contextmanager
def roles_recorded():
    """Replace the three role functions with wrappers that note each call, then put them back."""
    calls, originals = [], {name: getattr(assistant, name) for name in ROLES}

    def wrap(name, role):
        def recorded(*args):
            calls.append(name.split("_")[0])
            return role(*args)
        return recorded

    for name, role in originals.items():
        setattr(assistant, name, wrap(name, role))
    try:
        yield calls
    finally:
        for name, role in originals.items():
            setattr(assistant, name, role)


def orchestration(complete):
    section("A2  Orchestration: one role prepares the brief, another checks it, a coordinator continues or stops")
    params = {name: list(inspect.signature(getattr(assistant, name)).parameters) for name in ROLES}
    assert params == {"preparer_write_brief": ["facts", "activity", "review"],
                      "checker_review": ["facts", "brief"],
                      "coordinator_decide": ["brief", "review", "round_no"]}, params
    ok("orchestrator", "no role receives the database: the Preparer and Checker get facts, "
                       "the Coordinator only the brief, the review and the round number")

    with roles_recorded() as calls:
        rounds, outcome, _ = assistant.orchestrator(complete, [])
    assert calls == ["preparer", "checker", "coordinator"] and (outcome, len(rounds)) == ("ready", 1)
    ok("orchestrator", "complete brief: Preparer → Checker → Coordinator once, stop, ready")

    incomplete = complete | {"stand_area_sqm": None}
    with roles_recorded() as calls:
        rounds, outcome, _ = assistant.orchestrator(incomplete, [])
    assert calls == ["preparer", "checker", "coordinator"] * 2, calls
    first, second = rounds
    assert first["preparer"]["proposed_outcome"] == policy.sales_director_view(incomplete) == "ready"
    ok("preparer", "round 1 proposes the sales director's view: a named fair and a budget → hand over (ready)")
    assert first["checker"]["objections"] and [f["code"] for f in first["checker"]["findings"]] == ["MISSING_AREA"]
    ok("checker", "round 1 finds the missing stand area and objects that the proposal ignores it")
    assert first["coordinator"]["decision"] == "continue"
    ok("coordinator", "an objection before round 3 → continue: the Preparer gets another round")
    assert second["preparer"]["proposed_outcome"] == "partial" and "revised" in second["preparer"]["basis"]
    assert not second["checker"]["objections"]
    assert second["coordinator"] == {"decision": "stop", "outcome": "partial", "reason": second["coordinator"]["reason"]}
    assert outcome == "partial"
    ok("preparer→checker→coordinator", "round 2: the Preparer revises to partial, the Checker has no objection, "
                                       "the Coordinator stops with that outcome")

    brief = assistant.preparer_write_brief(incomplete, [], None)
    untouched = copy.deepcopy(brief)
    assistant.checker_review(incomplete, brief)
    assert brief == untouched
    ok("checker", "only reviews: the brief it was given is unchanged afterwards")

    stubborn = assistant.preparer_write_brief
    over_limit = complete | {"requested_height_m": complete["max_stand_height_m"] + 1}
    assistant.preparer_write_brief = lambda facts, activity, review: stubborn(facts, activity, None)
    try:
        with roles_recorded() as calls:
            rounds, outcome, reason = assistant.orchestrator(over_limit, [])
    finally:
        assistant.preparer_write_brief = stubborn
    assert [r["coordinator"]["decision"] for r in rounds] == ["continue", "continue", "stop"]
    assert outcome == "blocked" and reason == stand_in_model.no_agreement_text(assistant.MAX_ROUNDS)
    ok("coordinator", f"a Preparer that never revises: the Coordinator stops after MAX_ROUNDS = {assistant.MAX_ROUNDS} "
                      "and keeps the enquiry with sales (the loop is bounded)")

    real_socket = socket.socket
    socket.socket = lambda *args, **kwargs: (_ for _ in ()).throw(OSError("network used"))
    try:
        runs = [json.dumps(assistant.orchestrator(incomplete, []), default=str) for _ in range(2)]
    finally:
        socket.socket = real_socket
    assert runs[0] == runs[1]
    assert stand_in_model.__doc__.startswith("DETERMINISTIC STAND-IN FOR A LANGUAGE MODEL. NOT A MODEL CALL.")
    ok("stand-in", "runs with the network switched off, gives identical output twice, and its module is labelled "
                   "'DETERMINISTIC STAND-IN FOR A LANGUAGE MODEL. NOT A MODEL CALL.'")


def checker_covers_each_request(complete):
    section("A3  Checker: the information each person asked for, per the handoff policy")
    brief = assistant.preparer_write_brief(complete, [], None)
    assert assistant.checker_review(complete, brief) == {"findings": [], "objections": []}
    ok("checker", "complete brief (budget, area, height = the limit, opportunity not lost, fair edition not ended): no findings")

    limit, ends = complete["max_stand_height_m"], complete["ends_on"]
    cases = [
        ("sales director", "the customer gives a budget", {"client_budget_eur": None}, "MISSING_BUDGET", True),
        ("technical coordinator", "the stand area is known", {"stand_area_sqm": None}, "MISSING_AREA", False),
        ("technical coordinator", "the requested height is known", {"requested_height_m": None}, "MISSING_HEIGHT", False),
        ("technical coordinator", "the height is checked against the fair's limit",
         {"requested_height_m": limit + Decimal("0.01")}, "HEIGHT_OVER_LIMIT", True),
        ("sales team", "the opportunity is not lost", {"status": "lost"}, "OPPORTUNITY_LOST", True),
        ("sales team", "the fair edition has not ended", {"today": ends + timedelta(days=1)}, "FAIR_EDITION_ENDED", True),
    ]
    for person, need, change, code, blocks in cases:
        facts = complete | change
        findings = assistant.checker_review(facts, brief)["findings"]
        assert [(f["code"], f["blocks"]) for f in findings] == [(code, blocks)], (code, findings)
        assert findings[0]["message"] and findings[0]["summary"]
        effect = "blocks → blocked" if blocks else "does not block → partial"
        ok("checker", f"{person} – {need}: without it → {code}, {effect}  «{findings[0]['summary']}»")

    with connect() as conn:
        fair_required = one(conn, "SELECT is_nullable FROM information_schema.columns WHERE table_name = 'opportunities'"
                                  " AND column_name = 'fair_edition_code'")["is_nullable"]
    assert fair_required == "NO"
    ok("database", "sales director – a named fair: every enquiry has a fair edition (NOT NULL foreign key), so it is always met")

    everything = complete | {"client_budget_eur": None, "stand_area_sqm": None, "requested_height_m": None, "status": "lost"}
    codes = [f["code"] for f in assistant.checker_review(everything, brief)["findings"]]
    assert codes == ["MISSING_AREA", "MISSING_BUDGET", "MISSING_HEIGHT", "OPPORTUNITY_LOST"], codes
    ok("checker", "several problems at once are all listed, in a fixed order")

    over = assistant.checker_review(complete | {"requested_height_m": Decimal("6.00"), "max_stand_height_m": Decimal("5.00")},
                                    brief)["findings"][0]
    assert "6.00 m" in over["message"] and "5.00 m" in over["message"]
    ok("checker", "a conflict names both values: «" + over["message"] + "»")


def decisions_through_the_web(o):
    section("A4  Decisions on real enquiries, through the web; every run saved and re-runnable")
    path = f"/opportunities/{o['code']}"
    _, html = page(path)
    form_on(html, f"{path}/handoff")
    ok("page", f"the enquiry page has a 'Prepare technical handoff' action posting to {path}/handoff")
    with connect() as conn:
        limit = one(conn, "SELECT max_stand_height_m FROM opportunities JOIN fair_editions USING (fair_edition_code)"
                          " WHERE opportunity_code = %s", (o["code"],))["max_stand_height_m"]
    complete = {"status": o["status"], "client_budget_eur": "50000", "stand_area_sqm": "60",
                "requested_height_m": str(limit), "brief_notes": f"{MARK}: complete brief"}

    def run(code, brief):
        page(f"/opportunities/{code}", brief)
        status, location, _ = send(f"/opportunities/{code}/handoff", {}, follow=False)
        assert status == 302 and "/runs/" in location, (status, location)
        number = int(location.rsplit("/", 1)[1])
        with connect() as conn:
            saved = one(conn, "SELECT * FROM assistant_runs WHERE opportunity_code = %s AND run_number = %s", (code, number))
        return saved, page(f"/opportunities/{code}/runs/{number}")[1]

    cases = [  # name, brief change, outcome, rounds, text in the reason
        ("complete enquiry", {}, "ready", 1, "within the edition limit"),
        ("incomplete: area and height unknown", {"stand_area_sqm": "", "requested_height_m": ""}, "partial", 2,
         "stand area and requested height"),
        ("conflict: height over the fair's limit", {"requested_height_m": str(limit + 1)}, "blocked", 2,
         f"{limit + 1:.2f} m is over the {limit:.2f} m limit"),
        ("sales minimum missing: no budget", {"client_budget_eur": ""}, "blocked", 1, "has not stated a budget"),
        ("nothing to hand over: opportunity lost", {"status": "lost"}, "blocked", 2, f"did not go ahead with {o['code']}"),
    ]
    first = None
    for name, change, outcome, n_rounds, reason in cases:
        brief = complete | change
        saved, html = run(o["code"], brief)
        assert (saved["outcome"], len(saved["rounds"])) == (outcome, n_rounds), (name, saved["outcome"], saved["reason"])
        assert reason in saved["reason"], (name, saved["reason"])
        stored = saved["input_snapshot"][0]["result"]
        sent_budget = str(Decimal(brief["client_budget_eur"]).quantize(Decimal("0.01"))) if brief["client_budget_eur"] else None
        assert stored["client_budget_eur"] == sent_budget and stored["status"] == brief["status"]
        assert all({"preparer", "checker", "coordinator"} <= r.keys() for r in saved["rounds"])
        assert shows(html, saved["reason"]) and f'badge {outcome}">{outcome}' in html
        assert html.count("Preparer proposes") == html.count("Checker finds") == html.count("Coordinator decides") == n_rounds
        assert "deterministic local stand-in" in html
        first = first or saved
        ok("decision", f"{name} → {outcome} in {n_rounds} round(s): «{saved['reason']}»")
    ok("database", "each run stores the brief as it was (input_snapshot), every round's three role outputs, the outcome and the reason")
    ok("page", "each run page shows the outcome, the reason, each round's Preparer / Checker / Coordinator, and the stand-in label")

    with connect() as conn:
        ended = one(conn, """SELECT o.opportunity_code AS code, o.status, e.max_stand_height_m AS limit_m
                             FROM opportunities o JOIN fair_editions e USING (fair_edition_code)
                             WHERE o.status <> 'lost' AND e.ends_on < current_date
                             ORDER BY o.opportunity_code LIMIT 1""")
    assert ended, "no enquiry for a past fair edition to test with"
    saved, _ = run(ended["code"], {"status": ended["status"], "client_budget_eur": "50000", "stand_area_sqm": "60",
                                   "requested_height_m": str(ended["limit_m"]), "brief_notes": f"{MARK}: past edition"})
    assert saved["outcome"] == "blocked" and "already ended on" in saved["reason"], saved["reason"]
    ok("decision", f"nothing to hand over: {ended['code']} is complete but its fair edition ended → blocked: «{saved['reason']}»")

    with connect() as conn:
        again = one(conn, "SELECT outcome, input_snapshot, rounds, reason FROM assistant_runs"
                          " WHERE opportunity_code = %s AND run_number = %s", (o["code"], first["run_number"]))
        runs = conn.execute("SELECT run_number FROM assistant_runs WHERE opportunity_code = %s", (o["code"],)).fetchall()
    assert again == {k: first[k] for k in again}
    _, html = page(path)
    assert all(f"/runs/{r['run_number']}" in html for r in runs)
    ok("database", f"run #{first['run_number']} is unchanged after 4 brief edits and re-runs; all {len(runs)} runs are listed on the enquiry")

    one_run, _ = run(o["code"], complete)
    two_run, _ = run(o["code"], complete)
    assert (one_run["rounds"], one_run["reason"]) == (two_run["rounds"], two_run["reason"])
    ok("stand-in", "the same brief run twice through the web gives identical role outputs")

    assert "changed after the last run" not in page(path)[1]
    page(path, complete | {"brief_notes": f"{MARK}: edited after the run"})
    assert "changed after the last run" in page(path)[1]
    ok("page", "editing the brief after a run shows 'changed after the last run' so sales re-runs it")
    assert send("/opportunities/NOPE/handoff", {}, follow=False)[0] == 404
    assert send(f"{path}/runs/999", follow=False)[0] == 404
    ok("backend", "unknown enquiry or run number → 404")


# ── SCALE: everyday queries use indexes ───────────────────────────────────────

def scale():
    section("S1  Everyday searches stay practical at 100,000 contacts")
    with connect() as conn:
        conn.execute("SET enable_seqscan = off")  # a full scan then only appears when no index can serve the query
        today = one(conn, "SELECT current_date AS d")["d"]
        o = enquiry_under_test(conn)
        checks = [("search by name/email", lambda c: queries.search(c, o["contact_name"][:4]), ("companies", "contacts")),
                  ("today's follow-ups", lambda c: queries.open_follow_ups(c, "today", ""), ("activities",)),
                  ("follow-ups due on a day", lambda c: queries.open_follow_ups(c, "today", "", today), ("activities",)),
                  ("an enquiry's conversations", lambda c: queries.entries_of_opportunity(c, o["code"]), ("activities",)),
                  ("a company's notes", lambda c: queries.company_level_entries(c, o["company_code"]), ("activities",))]
        for name, call, tables in checks:
            recorder = Recorder(conn)
            call(recorder)
            for sql, params in recorder.statements:
                plan = "\n".join(r["QUERY PLAN"] for r in conn.execute("EXPLAIN " + sql, params))
                for table in tables:
                    assert f"Seq Scan on {table}" not in plan, f"{name} scans all of {table}:\n{plan}"
            ok("database", f"{name}: served by an index on {', '.join(tables)}, never a full table scan")


if __name__ == "__main__":
    phases = {"data": data, "fingerprint": fingerprint, "kept": kept, "web": web, "agent": agent, "scale": scale}
    phases[sys.argv[1]]()
