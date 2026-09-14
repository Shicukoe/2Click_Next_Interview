"""DETERMINISTIC STAND-IN FOR A LANGUAGE MODEL. NOT A MODEL CALL.

Every sentence the assistant's roles produce comes from the fixed templates in this file, filled with
structured CRM data. The same input always gives the same text. No network, no API key, no model weights.
Decisions are never made here: they come from policy.py.
"""

from app import policy

OUTCOME_LABEL = {
    policy.READY: "hand over for full technical review",
    policy.PARTIAL: "hand over part of the brief",
    policy.BLOCKED: "keep with sales",
}
def _amount(value, unit):
    return f"{value:,.2f} {unit}" if value is not None else "not recorded"


# Proposals and reasons reuse each finding's short summary, written by the Checker with the facts in hand.
def _missing(findings):
    return " and ".join(f["summary"] for f in findings if not f["blocks"])


def _blocking(findings):
    return "; ".join(f["summary"] for f in findings if f["blocks"])


def brief_summary(facts, activity):
    lines = [
        f"{facts['company_name']} wants a stand at {facts['fair_name']} ({facts['fair_edition_code']}, {facts['city']}, "
        f"{facts['starts_on']:%d/%m/%Y}–{facts['ends_on']:%d/%m/%Y}).",
        f"Contact: {facts['contact_name'] or 'not recorded'}. Deal status: {facts['status']}.",
        f"Client budget: {_amount(facts['client_budget_eur'], 'EUR')}. Stand area: {_amount(facts['stand_area_sqm'], 'm²')}. "
        f"Requested height: {_amount(facts['requested_height_m'], 'm')} (edition limit {facts['max_stand_height_m']:.2f} m).",
        f"Sales notes: {facts['brief_notes']}",
    ]
    return lines + [f"{a['occurred_at']:%d/%m/%Y} {a['activity_type']}: {a['details']}" for a in activity]


def finding_text(finding, facts):
    """The full explanation of a finding, shown once in the Checker's output."""
    code, f = finding["code"], facts
    if code == "DEAL_LOST":
        return (f"{f['company_name']} did not go ahead with {f['opportunity_code']} for {f['fair_name']} "
                f"{f['starts_on'].year}: its sales status is lost, so there is no stand to build.")
    if code == "EDITION_OVER":
        return (f"{f['fair_name']} {f['starts_on'].year} ({f['fair_edition_code']}) ended on {f['ends_on']:%d/%m/%Y}, "
                f"so a stand for it can no longer be built.")
    if code == "MISSING_BUDGET":
        return "The client has not stated a budget, so even the sales director's minimum for a handoff is not met."
    if code == "MISSING_HEIGHT":
        return f"The requested height is not recorded, so it cannot be checked against the {f['max_stand_height_m']:.2f} m limit."
    if code == "HEIGHT_OVER_LIMIT":
        return (f"The requested height of {f['requested_height_m']:.2f} m exceeds the {f['max_stand_height_m']:.2f} m "
                f"limit for {f['fair_edition_code']}, and no exception is recorded.")
    return "The stand area is not recorded."  # MISSING_AREA


def finding_summary(finding, facts):
    """A short, named form of a finding, reused in the Preparer's proposal and the Coordinator's reason."""
    code, f = finding["code"], facts
    if code == "DEAL_LOST":
        return f"{f['company_name']} did not go ahead with {f['opportunity_code']} (status lost)"
    if code == "EDITION_OVER":
        return f"{f['fair_name']} {f['starts_on'].year} already ended on {f['ends_on']:%d/%m/%Y}"
    if code == "MISSING_BUDGET":
        return f"{f['company_name']} has not stated a budget"
    if code == "HEIGHT_OVER_LIMIT":
        return (f"the requested {f['requested_height_m']:.2f} m is over the {f['max_stand_height_m']:.2f} m limit "
                f"for {f['fair_name']} {f['starts_on'].year}")
    if code == "MISSING_HEIGHT":
        return "requested height"
    return "stand area"  # MISSING_AREA


def proposal_text(outcome, findings):
    if outcome == policy.READY:
        return "Hand over to the technical team for full review."
    if outcome == policy.PARTIAL:
        return f"Hand over the partial brief for a feasibility check, and ask the customer for the {_missing(findings)}."
    if not findings:
        return "Keep with sales until the client states a budget."
    return f"Keep with sales: {_blocking(findings)}."


def objection_text(proposed, required):
    return f"The proposal is to {OUTCOME_LABEL[proposed]}, but the findings require: {OUTCOME_LABEL[required]}."


def reason_text(outcome, findings):
    if outcome == policy.READY:
        return "Budget, stand area and requested height are recorded, and the height is within the edition limit."
    if outcome == policy.PARTIAL:
        verb = "is" if sum(not f["blocks"] for f in findings) == 1 else "are"
        return f"The fair and budget are known; technical may check feasibility while the {_missing(findings)} {verb} confirmed."
    return f"Stays with sales: {_blocking(findings)}."


def no_agreement_text(rounds):
    return f"The preparer and checker did not agree within {rounds} rounds, so the enquiry stays with sales."
