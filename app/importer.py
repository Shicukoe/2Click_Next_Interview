"""Create the schema and load data/ on first start. Later starts change nothing."""

import csv
import os
from pathlib import Path

import psycopg

from app.parse import blank, completion, decimal_comma, dmy_date, dmy_datetime_rome, status

APP_DIR = Path(__file__).resolve().parent
DATA_DIR = APP_DIR.parent / "data"


def read(filename, columns):
    with open(DATA_DIR / filename, encoding="utf-8-sig", newline="") as f:
        rows = csv.DictReader(f, delimiter=";")
        missing = set(columns) - set(rows.fieldnames or [])
        if missing:
            raise SystemExit(f"{filename}: missing columns {sorted(missing)}")
        yield from rows


def copy(cur, table, columns, rows):
    with cur.copy(f"COPY {table} ({', '.join(columns)}) FROM STDIN") as out:
        for row in rows:
            out.write_row(row)


def load(cur):
    companies, contacts = {}, []
    for r in read("companies_and_contacts.csv", [
        "company_code", "company_name", "province_code", "region", "sales_rep",
        "contact_code", "contact_first_name", "contact_last_name", "email", "phone", "fax",
    ]):
        # Company details repeat on every contact row; they are identical, so the first copy is kept.
        companies.setdefault(r["company_code"], (
            blank(r["company_code"]), blank(r["company_name"]), blank(r["province_code"]),
            blank(r["region"]), blank(r["sales_rep"]), blank(r["fax"]),
        ))
        contacts.append((
            blank(r["contact_code"]), blank(r["company_code"]), blank(r["contact_first_name"]),
            blank(r["contact_last_name"]), blank(r["email"]), blank(r["phone"]),
        ))
    copy(cur, "companies",
         ["company_code", "company_name", "province_code", "region", "sales_rep", "fax"],
         companies.values())
    copy(cur, "contacts",
         ["contact_code", "company_code", "first_name", "last_name", "email", "phone"],
         contacts)

    editions = ["fair_edition_code", "fair_name", "city", "venue", "starts_on", "ends_on", "max_stand_height_m"]
    copy(cur, "fair_editions", editions, (
        (blank(r["fair_edition_code"]), blank(r["fair_name"]), blank(r["city"]), blank(r["venue"]),
         dmy_date(r["starts_on"]), dmy_date(r["ends_on"]), decimal_comma(r["max_stand_height_m"]))
        for r in read("fair_editions.csv", editions)
    ))

    source = [
        "opportunity_code", "company_code", "contact_code", "fair_edition_code", "description", "amount_eur",
        "legacy_status", "opened_on", "expected_close_on", "historical_campaign_code",
        "stand_area_sqm", "client_budget_eur", "requested_height_m", "brief_notes",
    ]
    copy(cur, "opportunities", source[:6] + ["status"] + source[6:], (
        (blank(r["opportunity_code"]), blank(r["company_code"]), blank(r["contact_code"]),
         blank(r["fair_edition_code"]), blank(r["description"]), decimal_comma(r["amount_eur"]),
         status(r["legacy_status"]), blank(r["legacy_status"]), dmy_date(r["opened_on"]),
         dmy_date(r["expected_close_on"]), blank(r["historical_campaign_code"]),
         decimal_comma(r["stand_area_sqm"]), decimal_comma(r["client_budget_eur"]),
         decimal_comma(r["requested_height_m"]), blank(r["brief_notes"]))
        for r in read("opportunities.csv", source)
    ))

    source = [
        "entry_id", "company_code", "opportunity_code", "activity_type", "occurred_at",
        "details", "follow_up_on", "completion_marker", "legacy_author",
    ]
    copy(cur, "activities", [
        "entry_id", "company_code", "opportunity_code", "activity_type", "occurred_at",
        "details", "follow_up_on", "completed", "author",
    ], (
        (blank(r["entry_id"]), blank(r["company_code"]), blank(r["opportunity_code"]),
         blank(r["activity_type"]), dmy_datetime_rome(r["occurred_at"]), blank(r["details"]),
         dmy_date(r["follow_up_on"]), completion(r["completion_marker"]), blank(r["legacy_author"]))
        for r in read("activity_log.csv", source)
    ))


def main():
    # One transaction: if anything fails, nothing is kept and the next start tries again from scratch.
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn, conn.cursor() as cur:
        if cur.execute("SELECT to_regclass('import_state')").fetchone()[0]:
            print("import: already done, keeping existing data")
            return
        cur.execute((APP_DIR / "schema.sql").read_text())
        load(cur)
        cur.execute("INSERT INTO import_state DEFAULT VALUES")
        for table in ["companies", "contacts", "fair_editions", "opportunities", "activities"]:
            count = cur.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            print(f"import: {table} {count}")
        print("import: not imported legacy_row_id (export row number), legacy_print_layout (obsolete)")


if __name__ == "__main__":
    main()
