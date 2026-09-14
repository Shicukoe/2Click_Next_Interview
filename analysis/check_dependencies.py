"""Check which columns of companies_and_contacts.csv really belong to the company.

Question 1: is fax a company attribute or a personal one?
Question 2: does province_code determine region (so region would need a provinces table)?

Run from the repository root: ./analyze.sh  (runs this file in Docker; no Python needed on the host)
"""

import csv
import sys
from collections import defaultdict
from pathlib import Path

data = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent / "data"
with open(data / "companies_and_contacts.csv", encoding="utf-8", newline="") as f:
    rows = list(csv.DictReader(f, delimiter=";"))


def report(title, key, value, skip_empty_keys=False):
    """Does key -> value hold? i.e. does every key appear with only one distinct value?"""
    seen = defaultdict(set)
    for r in rows:
        if skip_empty_keys and r[key] == "":
            continue
        seen[r[key]].add(r[value])
    conflicts = {k: v for k, v in seen.items() if len(v) > 1}
    verdict = "HOLDS" if not conflicts else "BROKEN"
    print(f"{title}\n  {key} -> {value}: {verdict}  ({len(seen)} distinct {key}, {len(conflicts)} with more than one {value})")
    for k, v in list(conflicts.items())[:3]:
        shown = sorted(v)[:5]
        print(f"    e.g. {key}={k!r} has {shown}{' ...' if len(v) > 5 else ''}")
    return not conflicts


print(f"{len(rows)} contact rows, {len({r['company_code'] for r in rows})} companies\n")

print("=== Question 1: fax ===")
# Empty counts as a value here, so "some contacts have the fax, others don't" also shows up as a conflict.
same_within_company = report("Do all contacts of a company carry the same fax (empty included)?", "company_code", "fax")
# An empty fax isn't a number, so rows without one are left out of this check.
single_company_per_fax = report("Does each fax number belong to only one company?", "fax", "company_code", skip_empty_keys=True)
companies_with_fax = {r["company_code"] for r in rows if r["fax"]}
fax_equals_phone = sum(1 for r in rows if r["fax"] and r["fax"] == r["phone"])
print(f"  companies with a fax: {len(companies_with_fax)}; contact rows with a fax: {sum(1 for r in rows if r['fax'])}")
print(f"  rows where fax equals that person's phone: {fax_equals_phone}")
example = next(c for c in sorted(companies_with_fax) if sum(r["company_code"] == c for r in rows) > 1)
print(f"  example company {example}:")
for r in rows:
    if r["company_code"] == example:
        print(f"    {r['contact_code']}  phone={r['phone'] or '-':16} fax={r['fax'] or '-'}")
print("  => fax is a COMPANY attribute" if same_within_company and single_company_per_fax
      else "  => fax is NOT purely a company attribute")

print("\n=== Question 2: province and region ===")
company_location_stable = report("Does each company always have the same province?", "company_code", "province_code")
province_determines_region = report("Does each province_code map to exactly one region?", "province_code", "region")
region_determines_province = report("Does each region map to exactly one province_code?", "region", "province_code")
pairs = sorted({(r["province_code"], r["region"]) for r in rows})
print("  all pairs: " + ", ".join(f"{p}={g}" for p, g in pairs))
if province_determines_region:
    print("  => province_code determines region: strict 3NF would move region into a provinces table (6 tables).")
    print("     Keeping region on companies (5 tables) is a deliberate denormalisation.")
else:
    print("  => province_code does NOT determine region: no provinces table is justified, 5 tables.")
if province_determines_region and region_determines_province:
    print("     The mapping is one-to-one in this data, so each column can be derived from the other.")
