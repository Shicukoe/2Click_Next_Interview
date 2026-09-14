"""Web pages: one function per URL. Validation happens here; SQL lives in queries.py."""

from datetime import date
from decimal import Decimal, InvalidOperation
from itertools import groupby
import json
from urllib.parse import urlsplit

from flask import Flask, abort, redirect, render_template, request, url_for

from app import assistant, queries
from app.db import connect

app = Flask(__name__)

# Brief fields sales can edit, with the largest value each column can hold.
AMOUNT_LIMITS = {
    "client_budget_eur": Decimal("9999999999.99"),
    "stand_area_sqm": Decimal("999999.99"),
    "requested_height_m": Decimal("999.99"),
}
MAX_DETAILS = 2000


def parse_amount(raw, largest):
    """Empty means unknown. Otherwise a positive number, with a decimal comma or point."""
    raw = raw.strip().replace(",", ".")
    if not raw:
        return None
    value = Decimal(raw)
    if not 0 < value <= largest:
        raise ValueError(raw)
    return value.quantize(Decimal("0.01"))


def same_site(path, fallback):
    """Only redirect back to a path on this site.

    urlsplit drops tabs and newlines the way browsers do, so "/\\t/evil.example" is seen as the other site it is.
    """
    parts = urlsplit(path)
    local = path.startswith("/") and not parts.scheme and not parts.netloc and "\\" not in path
    return path if local else fallback


# ── Search and companies ──────────────────────────────────────────────────────

@app.get("/")
def search():
    text = request.args.get("q", "").strip()
    companies = contacts = message = None
    if text:
        with connect() as conn:
            if queries.opportunity_exists(conn, text.upper()):
                return redirect(url_for("opportunity", code=text.upper()))
            if len(text) < 3:
                message = "Type at least 3 characters, or a full code."
            else:
                companies, contacts = queries.search(conn, text)
    return render_template("search.html", q=text, companies=companies, contacts=contacts, message=message,
                           limit=queries.RESULT_LIMIT)


@app.get("/companies/<code>")
def company(code):
    with connect() as conn:
        found = queries.company(conn, code)
        if found is None:
            abort(404)
        contacts = queries.contacts_of(conn, code)
        opportunities = queries.opportunities_of(conn, code)
        entries = queries.company_level_entries(conn, code)
    editions = [list(group) for _, group in groupby(opportunities, key=lambda o: o["fair_edition_code"])]
    return render_template("company.html", company=found, contacts=contacts, editions=editions, entries=entries)


# ── Opportunities ─────────────────────────────────────────────────────────────

def render_opportunity(code, error=None, status=200):
    with connect() as conn:
        opp = queries.opportunity(conn, code)
        if opp is None:
            abort(404)
        entries = queries.entries_of_opportunity(conn, code)
        company_entries = queries.company_level_entries(conn, opp["company_code"])
        runs = queries.runs_of(conn, code)
        statuses = queries.statuses(conn)
    # The last run is out of date if the brief was edited or an entry was logged after it.
    stale = bool(runs) and opp["last_change"] is not None and opp["last_change"] > runs[0]["created_at"]
    return render_template("opportunity.html", opp=opp, entries=entries, company_entries=company_entries,
                           runs=runs, statuses=statuses, stale=stale, error=error,
                           activity_types=queries.ACTIVITY_TYPES), status


@app.get("/opportunities/<code>")
def opportunity(code):
    return render_opportunity(code)


@app.post("/opportunities/<code>")
def update_opportunity(code):
    try:
        amounts = {field: parse_amount(request.form.get(field, ""), largest) for field, largest in AMOUNT_LIMITS.items()}
    except (ValueError, InvalidOperation):
        return render_opportunity(code, "Budget, area and height must be positive numbers, or empty when unknown.", 400)
    notes = request.form.get("brief_notes", "").strip()
    if not notes:
        return render_opportunity(code, "Brief notes cannot be empty.", 400)
    status = request.form.get("status", "")
    with connect() as conn:
        if status not in queries.statuses(conn):
            return render_opportunity(code, "Choose one of the listed statuses.", 400)
        if not queries.update_opportunity(conn, code, status, notes, amounts):
            abort(404)
    return redirect(url_for("opportunity", code=code))


@app.post("/opportunities/<code>/entries")
def record_entry(code):
    activity_type = request.form.get("activity_type", "")
    details = request.form.get("details", "").strip()
    if activity_type not in queries.ACTIVITY_TYPES or not details or len(details) > MAX_DETAILS:
        return render_opportunity(code, f"Choose a type and describe what happened (up to {MAX_DETAILS:,} characters).", 400)
    raw_date = request.form.get("follow_up_on", "").strip()
    try:
        follow_up_on = date.fromisoformat(raw_date) if raw_date else None
    except ValueError:
        return render_opportunity(code, "The follow-up date is not a valid date.", 400)
    with connect() as conn:
        if not queries.add_entry(conn, code, activity_type, details, follow_up_on):
            abort(404)
    return redirect(url_for("opportunity", code=code) + "#entries")


# ── Follow-ups ────────────────────────────────────────────────────────────────

@app.get("/follow-ups")
def follow_ups():
    tab = request.args.get("tab", "today")
    if tab not in queries.FOLLOW_UP_TABS:
        tab = "today"
    rep = request.args.get("rep", "")
    try:  # "Due on": one day's follow-ups, so any date is findable even when its tab is past the row limit
        day = date.fromisoformat(request.args.get("day", ""))
    except ValueError:
        day = None
    with connect() as conn:
        reps = queries.sales_reps(conn)
        if rep not in reps:
            rep = ""
        rows, counts = queries.open_follow_ups(conn, tab, rep, day)
        export_date = queries.export_date(conn)
    return render_template("follow_ups.html", rows=rows, counts=counts, tab=tab, rep=rep, reps=reps, day=day,
                           export_date=export_date, limit=queries.FOLLOW_UP_LIMIT)


@app.post("/entries/<entry_id>/done")
def mark_entry_done(entry_id):
    with connect() as conn:
        if not queries.mark_done(conn, entry_id):
            abort(404)
    return redirect(same_site(request.form.get("back", ""), url_for("follow_ups")))


# ── Handoff assistant ─────────────────────────────────────────────────────────

@app.post("/opportunities/<code>/handoff")
def run_handoff_assistant(code):
    with connect() as conn:
        try:
            number = assistant.run_and_save(conn, code)
        except LookupError:
            abort(404)
    return redirect(url_for("show_assistant_run", code=code, number=number))


@app.get("/opportunities/<code>/runs/<int:number>")
def show_assistant_run(code, number):
    with connect() as conn:
        found = queries.run(conn, code, number)
    if found is None:
        abort(404)
    snapshot = json.dumps(found["input_snapshot"], indent=2, ensure_ascii=False)
    return render_template("run.html", run=found, snapshot=snapshot)
