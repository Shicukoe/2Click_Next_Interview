"""Handoff policy: when an enquiry may go to the technical team.

Rules only: no wording and no database access. Every assistant role reads the same rules from here.
"""

VERSION = "1"

READY = "ready"              # hand over for full technical review
PARTIAL = "partial"          # hand over part of the brief for a feasibility check; some details are still missing
BLOCKED = "blocked"          # keep with sales


def sales_director_view(facts):
    """What the sales director asks for: a named fair and a budget are enough to hand over."""
    return READY if facts["client_budget_eur"] is not None else BLOCKED


def evaluate(facts):
    """Findings against the fair's rules, the technical team's needs and the sales minimum.

    kind: gap (information missing), conflict (request breaks a fair rule), inactive (nothing to hand over).
    blocks: True keeps the enquiry with sales; a non-blocking gap still allows a partial handoff.
    """
    found = []
    if facts["status"] == "lost":
        found.append({"code": "OPPORTUNITY_LOST", "kind": "inactive", "blocks": True, "field": "status"})
    if facts["ends_on"] < facts["today"]:
        found.append({"code": "FAIR_EDITION_ENDED", "kind": "inactive", "blocks": True, "field": "ends_on"})
    if facts["client_budget_eur"] is None:
        found.append({"code": "MISSING_BUDGET", "kind": "gap", "blocks": True, "field": "client_budget_eur"})
    if facts["requested_height_m"] is None:
        found.append({"code": "MISSING_HEIGHT", "kind": "gap", "blocks": False, "field": "requested_height_m"})
    elif facts["requested_height_m"] > facts["max_stand_height_m"]:
        found.append({"code": "HEIGHT_OVER_LIMIT", "kind": "conflict", "blocks": True, "field": "requested_height_m"})
    if facts["stand_area_sqm"] is None:
        found.append({"code": "MISSING_AREA", "kind": "gap", "blocks": False, "field": "stand_area_sqm"})
    return sorted(found, key=lambda f: f["code"])


def required_action(findings):
    """The agreed policy: blocked if anything blocks, partial if only details are missing, otherwise ready."""
    if any(f["blocks"] for f in findings):
        return BLOCKED
    if findings:
        return PARTIAL
    return READY
