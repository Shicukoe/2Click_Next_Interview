"""Every SQL statement the web pages use, in one place.

The handoff assistant's lookups are not here on purpose: they live in its Toolbox, so the agent's only
access to the database stays visible in one class.
"""

ACTIVITY_TYPES = ("call", "email", "meeting", "note", "task")  # the documented list in data/README.md
# ponytail: the brief assumes one user with no login (ASSIGNMENT.md:83); record real users once login exists
AUTHOR = "crm user"
RESULT_LIMIT = 50
FOLLOW_UP_LIMIT = 200

ENTRY_COLUMNS = """
    a.entry_id, a.occurred_at, a.activity_type, a.details, a.author, a.completed,
    a.follow_up_on, a.follow_up_done_at, a.follow_up_on < current_date AS overdue
"""


def _contains(text):
    """A LIKE pattern matching text anywhere, with the user's own % and _ treated literally."""
    return "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


# ── Search ────────────────────────────────────────────────────────────────────

def search(conn, text):
    """Companies and contacts matching a name, an email or an exact code. Uses the trigram indexes."""
    params = {"pattern": _contains(text), "code": text.upper(), "limit": RESULT_LIMIT}
    companies = conn.execute("""
        SELECT c.company_code, c.company_name, c.region, c.sales_rep, count(o.opportunity_code) AS enquiries
        FROM companies c
        LEFT JOIN opportunities o ON o.company_code = c.company_code
        WHERE c.company_name ILIKE %(pattern)s OR c.company_code = %(code)s
        GROUP BY c.company_code
        ORDER BY c.company_name, c.company_code
        LIMIT %(limit)s
    """, params).fetchall()
    contacts = conn.execute("""
        SELECT t.contact_code, t.first_name, t.last_name, t.email, t.phone, c.company_code, c.company_name
        FROM contacts t
        JOIN companies c ON c.company_code = t.company_code
        WHERE t.first_name || ' ' || t.last_name ILIKE %(pattern)s
           OR t.email ILIKE %(pattern)s
           OR t.contact_code = %(code)s
        ORDER BY t.last_name, t.first_name, t.contact_code
        LIMIT %(limit)s
    """, params).fetchall()
    return companies, contacts


def opportunity_exists(conn, code):
    return conn.execute("SELECT 1 FROM opportunities WHERE opportunity_code = %s", (code,)).fetchone() is not None


# ── Companies ─────────────────────────────────────────────────────────────────

def company(conn, code):
    return conn.execute("SELECT * FROM companies WHERE company_code = %s", (code,)).fetchone()


def contacts_of(conn, company_code):
    return conn.execute(
        "SELECT * FROM contacts WHERE company_code = %s ORDER BY contact_code", (company_code,)
    ).fetchall()


def opportunities_of(conn, company_code):
    """A company's enquiries, newest fair edition first, each with its latest handoff outcome."""
    return conn.execute("""
        SELECT o.opportunity_code, o.description, o.status, o.amount_eur, o.client_budget_eur,
               e.fair_edition_code, e.fair_name, e.city, e.starts_on, e.ends_on, r.outcome AS latest_outcome
        FROM opportunities o
        JOIN fair_editions e ON e.fair_edition_code = o.fair_edition_code
        LEFT JOIN LATERAL (
            SELECT outcome FROM assistant_runs
            WHERE opportunity_code = o.opportunity_code
            ORDER BY run_number DESC LIMIT 1
        ) r ON true
        WHERE o.company_code = %s
        ORDER BY e.starts_on DESC, o.opportunity_code
    """, (company_code,)).fetchall()


def company_level_entries(conn, company_code):
    """Entries about the company in general, not tied to any enquiry."""
    return conn.execute(f"""
        SELECT {ENTRY_COLUMNS} FROM activities a
        WHERE a.company_code = %s AND a.opportunity_code IS NULL
        ORDER BY a.occurred_at DESC, a.entry_id DESC
    """, (company_code,)).fetchall()


# ── Opportunities ─────────────────────────────────────────────────────────────

def opportunity(conn, code):
    """One enquiry with its company, contact and fair edition.

    last_change is the latest edit or logged entry, used to tell whether the last assistant run is out of date.
    """
    return conn.execute("""
        SELECT o.*, c.company_name, c.region, c.sales_rep,
               t.first_name || ' ' || t.last_name AS contact_name, t.email AS contact_email, t.phone AS contact_phone,
               e.fair_name, e.city, e.venue, e.starts_on, e.ends_on, e.max_stand_height_m,
               greatest(o.updated_at,
                        (SELECT max(occurred_at) FROM activities a WHERE a.opportunity_code = o.opportunity_code)
               ) AS last_change
        FROM opportunities o
        JOIN companies c ON c.company_code = o.company_code
        JOIN fair_editions e ON e.fair_edition_code = o.fair_edition_code
        LEFT JOIN contacts t ON t.contact_code = o.contact_code
        WHERE o.opportunity_code = %s
    """, (code,)).fetchone()


def entries_of_opportunity(conn, code):
    """Only this enquiry's entries, so another edition's conversations never appear."""
    return conn.execute(f"""
        SELECT {ENTRY_COLUMNS} FROM activities a
        WHERE a.opportunity_code = %s
        ORDER BY a.occurred_at DESC, a.entry_id DESC
    """, (code,)).fetchall()


def statuses(conn):
    return [row["status"] for row in conn.execute("SELECT DISTINCT status FROM opportunities ORDER BY status")]


def update_opportunity(conn, code, status, notes, amounts):
    return conn.execute("""
        UPDATE opportunities
        SET status = %s, brief_notes = %s, client_budget_eur = %s, stand_area_sqm = %s, requested_height_m = %s,
            updated_at = now()
        WHERE opportunity_code = %s
    """, (status, notes, amounts["client_budget_eur"], amounts["stand_area_sqm"], amounts["requested_height_m"],
          code)).rowcount


def add_entry(conn, opportunity_code, activity_type, details, follow_up_on):
    """Log a conversation, note or task against an enquiry. Returns 0 if the enquiry doesn't exist."""
    completed = {"task": False, "note": None}.get(activity_type, True)  # a logged call, email or meeting happened
    return conn.execute("""
        INSERT INTO activities (company_code, opportunity_code, activity_type, occurred_at, details,
                                follow_up_on, completed, author)
        SELECT company_code, opportunity_code, %s, now(), %s, %s, %s, %s
        FROM opportunities WHERE opportunity_code = %s
    """, (activity_type, details, follow_up_on, completed, AUTHOR, opportunity_code)).rowcount


# ── Follow-ups ────────────────────────────────────────────────────────────────

def mark_done(conn, entry_id):
    """Close an entry's follow-up, and complete it if it's a task. Safe to repeat."""
    return conn.execute("""
        UPDATE activities
        SET follow_up_done_at = coalesce(follow_up_done_at, CASE WHEN follow_up_on IS NOT NULL THEN now() END),
            completed = CASE WHEN activity_type = 'task' THEN true ELSE completed END
        WHERE entry_id = %s
    """, (entry_id,)).rowcount


def sales_reps(conn):
    return [row["sales_rep"] for row in conn.execute("SELECT DISTINCT sales_rep FROM companies ORDER BY sales_rep")]


FOLLOW_UP_TABS = ("late", "today", "week", "later", "before_export")

# Every open follow-up (has a date, not marked done) with who to call and the tab it belongs to.
# The tab is defined once, here, and used for both the counts and the list.
OPEN_FOLLOW_UPS = """
    SELECT a.entry_id, a.details, a.follow_up_on, a.opportunity_code,
           c.company_code, c.company_name, c.sales_rep,
           t.first_name || ' ' || t.last_name AS contact_name, t.phone AS contact_phone, t.email AS contact_email,
           CASE WHEN a.follow_up_on < s.export_date THEN 'before_export'  -- the old system never recorded these as done
                WHEN a.follow_up_on < current_date THEN 'late'
                WHEN a.follow_up_on = current_date THEN 'today'
                WHEN a.follow_up_on <= current_date + 7 THEN 'week'
                ELSE 'later' END AS tab
    FROM activities a
    JOIN companies c ON c.company_code = a.company_code
    LEFT JOIN opportunities o ON o.opportunity_code = a.opportunity_code
    LEFT JOIN contacts t ON t.contact_code = o.contact_code
    CROSS JOIN (SELECT coalesce(max(archive_reference_time)::date, '-infinity') AS export_date FROM import_state) s
    WHERE a.follow_up_on IS NOT NULL AND a.follow_up_done_at IS NULL
      AND (%(rep)s = '' OR c.sales_rep = %(rep)s)
"""


def export_date(conn):
    """The day the archive was exported, or None if the manifest didn't say."""
    return conn.execute("SELECT max(archive_reference_time)::date AS d FROM import_state").fetchone()["d"]


def open_follow_ups(conn, tab, rep, day=None):
    """Open follow-ups for one tab, or for one day whatever the tab. rep: an account owner, or '' for everyone.

    Tabs: late, today, week (the next 7 days), later, before_export. Returns (rows, count per tab).
    """
    params = {"tab": tab, "rep": rep, "day": day, "limit": FOLLOW_UP_LIMIT}
    found = {r["tab"]: r["n"] for r in conn.execute(
        f"SELECT tab, count(*) AS n FROM ({OPEN_FOLLOW_UPS}) f GROUP BY tab", params)}
    rows = conn.execute(f"""
        SELECT * FROM ({OPEN_FOLLOW_UPS}) f
        WHERE CASE WHEN %(day)s::date IS NOT NULL THEN follow_up_on = %(day)s::date ELSE tab = %(tab)s END
        ORDER BY CASE WHEN tab IN ('late', 'before_export') THEN follow_up_on END DESC,  -- most recent first
                 follow_up_on, entry_id
        LIMIT %(limit)s
    """, params).fetchall()
    return rows, {t: found.get(t, 0) for t in FOLLOW_UP_TABS}


# ── Assistant runs ────────────────────────────────────────────────────────────

def runs_of(conn, code):
    return conn.execute("""
        SELECT run_number, created_at, outcome, reason FROM assistant_runs
        WHERE opportunity_code = %s ORDER BY run_number DESC
    """, (code,)).fetchall()


def run(conn, code, number):
    return conn.execute(
        "SELECT * FROM assistant_runs WHERE opportunity_code = %s AND run_number = %s", (code, number)
    ).fetchone()
