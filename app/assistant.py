"""Technical handoff assistant: Preparer, Checker and Coordinator in a bounded loop.

policy.py decides, stand_in_model.py writes the words, this file runs the roles and saves each run.
Only the Preparer reads the database, through the logged Toolbox; the Checker and Coordinator work only
on what they are handed, so every stored run shows exactly what each role saw and said.
"""

import json
from datetime import datetime
from zoneinfo import ZoneInfo

from psycopg.types.json import Jsonb

from app import policy, stand_in_model

ROME = ZoneInfo("Europe/Rome")
MAX_ROUNDS = 3


# ── Tools: the assistant's only access to the database ────────────────────────

OPPORTUNITY_SQL = """
SELECT o.opportunity_code, o.status, o.client_budget_eur, o.stand_area_sqm, o.requested_height_m, o.brief_notes,
       c.company_name, t.first_name || ' ' || t.last_name AS contact_name,
       e.fair_edition_code, e.fair_name, e.city, e.starts_on, e.ends_on, e.max_stand_height_m
FROM opportunities o
JOIN companies c ON c.company_code = o.company_code
JOIN fair_editions e ON e.fair_edition_code = o.fair_edition_code
LEFT JOIN contacts t ON t.contact_code = o.contact_code
WHERE o.opportunity_code = %s
"""

ACTIVITY_SQL = """
SELECT occurred_at, activity_type, details, follow_up_on
FROM activities
WHERE opportunity_code = %s
ORDER BY occurred_at DESC, entry_id DESC
LIMIT %s
"""


class Toolbox:
    """Read-only lookups for the Preparer. The call log becomes the run's input snapshot."""

    def __init__(self, conn):
        self.conn = conn
        self.log = []

    def _record(self, tool, args, result):
        self.log.append({"tool": tool, "args": args, "result": result})
        return result

    def get_opportunity(self, code):
        return self._record("get_opportunity", {"opportunity_code": code},
                            self.conn.execute(OPPORTUNITY_SQL, (code,)).fetchone())

    def get_recent_activity(self, code, limit=3):
        return self._record("get_recent_activity", {"opportunity_code": code, "limit": limit},
                            self.conn.execute(ACTIVITY_SQL, (code, limit)).fetchall())

    def get_today(self):
        # The date is an input like any other, so it is logged too.
        return self._record("get_today", {"time_zone": "Europe/Rome"}, datetime.now(ROME).date())


# ── Roles ─────────────────────────────────────────────────────────────────────

def preparer_gather_facts(tools, code):
    """Preparer: collect the CRM and fair facts through the tools."""
    facts = tools.get_opportunity(code)
    if facts is None:
        raise LookupError(code)
    return dict(facts, today=tools.get_today()), tools.get_recent_activity(code)


def preparer_write_brief(facts, activity, review):
    """Preparer: summarise the enquiry and propose an outcome.

    Round 1 proposes what the sales director asks for. Later rounds follow the Checker's findings.
    """
    if review is None:
        proposed, findings = policy.sales_director_view(facts), []
        basis = "round 1: what the sales director asks for, a named fair and a budget"
    else:
        findings = review["findings"]
        proposed, basis = policy.required_action(findings), "revised to follow the checker's findings"
    return {"summary": stand_in_model.brief_summary(facts, activity), "proposed_outcome": proposed,
            "proposal": stand_in_model.proposal_text(proposed, findings), "basis": basis}


def checker_review(facts, brief):
    """Checker: test the proposal against the policy. Lists findings and objects; decides nothing."""
    findings = [f | {"message": stand_in_model.finding_text(f, facts),
                     "summary": stand_in_model.finding_summary(f, facts)}
                for f in policy.evaluate(facts)]
    required = policy.required_action(findings)
    objections = [] if brief["proposed_outcome"] == required else [
        stand_in_model.objection_text(brief["proposed_outcome"], required)
    ]
    return {"findings": findings, "objections": objections}


def coordinator_decide(brief, review, round_no):
    """Coordinator: continue (another round) or stop, and on stopping, the outcome and why."""
    if review["objections"] and round_no < MAX_ROUNDS:
        return {"decision": "continue", "reason": "The checker objected, so the preparer revises the proposal."}
    if review["objections"]:
        return {"decision": "stop", "outcome": policy.BLOCKED,
                "reason": stand_in_model.no_agreement_text(MAX_ROUNDS)}
    outcome = brief["proposed_outcome"]
    return {"decision": "stop", "outcome": outcome,
            "reason": stand_in_model.reason_text(outcome, review["findings"])}


# ── Loop and saving ───────────────────────────────────────────────────────────

def orchestrator(facts, activity):
    """Call Preparer → Checker → Coordinator, round after round, until the Coordinator stops."""
    rounds, review = [], None
    for round_no in range(1, MAX_ROUNDS + 1):
        brief = preparer_write_brief(facts, activity, review)
        review = checker_review(facts, brief)
        decision = coordinator_decide(brief, review, round_no)
        rounds.append({"round": round_no, "preparer": brief, "checker": review, "coordinator": decision})
        if decision["decision"] == "stop":
            return rounds, decision["outcome"], decision["reason"]


def _json(value):
    return Jsonb(value, dumps=lambda v: json.dumps(v, default=str, ensure_ascii=False))


def run_and_save(conn, code):
    """Run the assistant on one opportunity and store the run. Returns the new run number."""
    tools = Toolbox(conn)
    facts, activity = preparer_gather_facts(tools, code)
    rounds, outcome, reason = orchestrator(facts, activity)
    number = conn.execute(
        "SELECT coalesce(max(run_number), 0) + 1 AS n FROM assistant_runs WHERE opportunity_code = %s", (code,)
    ).fetchone()["n"]
    conn.execute(
        "INSERT INTO assistant_runs (opportunity_code, run_number, policy_version, input_snapshot, rounds, outcome, reason)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (code, number, policy.VERSION, _json(tools.log), _json(rounds), outcome, reason),
    )
    return number


if __name__ == "__main__":
    from datetime import date
    from decimal import Decimal as D

    base = {"opportunity_code": "TEST", "status": "open", "client_budget_eur": D("50000"), "stand_area_sqm": D("80"),
            "requested_height_m": D("4.00"), "brief_notes": "Test.", "company_name": "Test S.r.l.", "contact_name": None,
            "fair_edition_code": "BEAUTY-2027", "fair_name": "Beauty Trade Forum", "city": "Bologna",
            "starts_on": date(2027, 6, 24), "ends_on": date(2027, 6, 27), "max_stand_height_m": D("4.50"),
            "today": date(2026, 9, 14)}
    cases = {  # change to the facts: (expected outcome, expected rounds)
        "complete": ({}, ("ready", 1)),
        "area and height unknown": ({"stand_area_sqm": None, "requested_height_m": None}, ("provisional", 2)),
        "height unknown": ({"requested_height_m": None}, ("provisional", 2)),
        "height over limit": ({"requested_height_m": D("6.00"), "max_stand_height_m": D("5.00")}, ("blocked", 2)),
        "no budget": ({"client_budget_eur": None}, ("blocked", 1)),
        "deal lost": ({"status": "lost"}, ("blocked", 2)),
        "edition over": ({"today": date(2027, 7, 1)}, ("blocked", 2)),
    }
    for name, (change, expected) in cases.items():
        facts = base | change
        rounds, outcome, reason = orchestrator(facts, [])
        assert (outcome, len(rounds)) == expected, (name, outcome, len(rounds))
        again = json.dumps(orchestrator(facts, []), default=str)
        assert again == json.dumps((rounds, outcome, reason), default=str), f"not deterministic: {name}"
    print("assistant: ok")
