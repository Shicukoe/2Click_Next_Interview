import json
from decimal import Decimal, InvalidOperation

from flask import Flask, abort, redirect, render_template, request, url_for

from app import assistant
from app.db import connect

app = Flask(__name__)

# Brief fields sales can edit, with the largest value each column can hold.
EDITABLE = {
    "client_budget_eur": Decimal("9999999999.99"),
    "stand_area_sqm": Decimal("999999.99"),
    "requested_height_m": Decimal("999.99"),
}

DETAIL_SQL = """
SELECT o.*, c.company_name, c.region, t.first_name || ' ' || t.last_name AS contact_name,
       e.fair_name, e.city, e.starts_on, e.ends_on, e.max_stand_height_m
FROM opportunities o
JOIN companies c ON c.company_code = o.company_code
JOIN fair_editions e ON e.fair_edition_code = o.fair_edition_code
LEFT JOIN contacts t ON t.contact_code = o.contact_code
WHERE o.opportunity_code = %s
"""


def parse_amount(raw, largest):
    """Empty means unknown. Otherwise a positive number, with a decimal comma or point."""
    raw = raw.strip().replace(",", ".")
    if not raw:
        return None
    value = Decimal(raw)
    if not 0 < value <= largest:
        raise ValueError(raw)
    return value.quantize(Decimal("0.01"))


def opportunity_page(code, error=None, status=200):
    with connect() as conn:
        opp = conn.execute(DETAIL_SQL, (code,)).fetchone()
        if opp is None:
            abort(404)
        activity = conn.execute(
            "SELECT occurred_at, activity_type, details, follow_up_on FROM activities"
            " WHERE opportunity_code = %s ORDER BY occurred_at DESC, entry_id DESC", (code,)
        ).fetchall()
        runs = conn.execute(
            "SELECT run_number, created_at, outcome, reason FROM assistant_runs"
            " WHERE opportunity_code = %s ORDER BY run_number DESC", (code,)
        ).fetchall()
    stale = bool(runs) and opp["updated_at"] is not None and opp["updated_at"] > runs[0]["created_at"]
    return render_template("opportunity.html", opp=opp, activity=activity, runs=runs, stale=stale, error=error), status


@app.get("/")
def index():
    code = request.args.get("code", "").strip().upper()
    if code:
        return redirect(url_for("opportunity", code=code))
    return render_template("index.html")


@app.get("/opportunities/<code>")
def opportunity(code):
    return opportunity_page(code)


@app.post("/opportunities/<code>")
def update_brief(code):
    try:
        values = {field: parse_amount(request.form.get(field, ""), largest) for field, largest in EDITABLE.items()}
    except (ValueError, InvalidOperation):
        return opportunity_page(code, "Budget, area and height must be positive numbers, or empty when unknown.", 400)
    notes = request.form.get("brief_notes", "").strip()
    if not notes:
        return opportunity_page(code, "Brief notes cannot be empty.", 400)
    with connect() as conn:
        updated = conn.execute(
            "UPDATE opportunities SET client_budget_eur = %s, stand_area_sqm = %s, requested_height_m = %s,"
            " brief_notes = %s, updated_at = now() WHERE opportunity_code = %s",
            (values["client_budget_eur"], values["stand_area_sqm"], values["requested_height_m"], notes, code),
        ).rowcount
    if not updated:
        abort(404)
    return redirect(url_for("opportunity", code=code))


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
        run = conn.execute(
            "SELECT * FROM assistant_runs WHERE opportunity_code = %s AND run_number = %s", (code, number)
        ).fetchone()
    if run is None:
        abort(404)
    snapshot = json.dumps(run["input_snapshot"], indent=2, ensure_ascii=False)
    return render_template("run.html", run=run, snapshot=snapshot)
