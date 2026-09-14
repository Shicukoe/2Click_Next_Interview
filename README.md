# Exhibition sales CRM

Read [the assignment](ASSIGNMENT.md) first.

Create your own public Git repository for the work. If you received this starter as a ZIP, initialise Git in the extracted project and commit the starter before making changes. Submit by email using the required format in [What to send us](ASSIGNMENT.md#what-to-send-us).

The starter runs PostgreSQL. Add your application services to `compose.yml` and put your code wherever suits your stack. The export is in `data/`; its [README](data/README.md) describes the files and field formats.

Run `./dev.sh` to start and `docker compose down` to stop. `./reset.sh` removes the project's data. Once you've added the application, `./verify.sh` checks that it responds on port 3000. Until then, the HTTP check will fail.

## Submission notes

### My understanding of the situation

A company that builds exhibition stands has six sales people and an old system that data remains only as an export: 10,000 companies, 20,000 contacts, 15,000 stand enquiries and 40,000 logged conversations. The account managers and the sales coordinator need one clear place for each exhibitor and its enquiries, while the sales director and the technical coordinator disagree on when an enquiry goes to technical. The handoff assistant is where that conflict is resolved.

### Stack and versions

- **Python 3.13.15** (`docker.io/library/python:3.13.15-slim-trixie`) with **uv 0.12.13** (`ghcr.io/astral-sh/uv`); dependencies are pinned in the committed `uv.lock` and installed with `uv sync --locked`.
- **Flask 3.1.3** with Jinja2 templates, served by **gunicorn 26.2.0**.
- **psycopg 3.3.5** with plain parameterised SQL, no ORM.
- **PostgreSQL 17.6** (`postgres:17.6-alpine3.22`) with `pg_trgm` for search.
- Everything runs through `compose.yml`, built locally from the Dockerfile, for `linux/amd64` and `linux/arm64`.

Python code for the importer, the web app and the assistant, so everything shares one language. Plain SQL instead of an ORM keeps the table design and indexes visible. Server-rendered Flask pages are enough for a one-user sales tool, with no JavaScript build step to maintain.


### Time spent

About **[20] hours [03] minutes** from the initial commmit

### Running it

The provided scripts work as described in the assignment, and I added two more written the same way, with only Docker needed on the host.

`./test.sh` runs the reviewers' routine (reset, start, verify, restart, reset again) and checks the import, every CRM screen, the assistant and index use along the way. Note that it resets this project's data.

`./analyze.sh` shows how I decided which table each column belongs to. For example, every company has the same fax, or none, on all of its contact rows, and each fax number belongs to only one company, so `fax` is stored on `companies` rather than `contacts`.

### Import decisions

Every row is imported. Two columns are left out: `legacy_row_id`, the export's row number, because `contact_code` already identifies the row, and `legacy_print_layout`, which is obsolete presentation metadata. The data goes into five tables (companies, contacts, fair_editions, opportunities and activities), keyed by their legacy codes.

Empty values become NULL, and only columns documented as optional allow NULL, so a missing required value is rejected instead of loaded. `legacy_status` is trimmed and lower-cased, turning 13 spellings into 5 statuses, and the original value is kept.

Six date-times fall inside a clock change, so their Rome time either never existed or happened twice. I use the UTC offset in force before the change: `30/03/2025 02:30` never existed, is stored as 01:30 UTC and shows as 03:30, while `26/10/2025 02:19` happened twice and is stored as its first occurrence.

The archive never records a follow-up as done, so every imported follow-up starts open. The whole import runs in one transaction: a row that breaks the documented format stops it and leaves no tables behind, so the next start retries cleanly.

### How I handled the team's requests

Account managers want everything about an exhibitor in one place. Each exhibitor has one company record, and its contacts are entered once and reused for every fair. The company page lists all of its enquiries, grouped by fair edition.

The sales coordinator wants this year's enquiry to show only this edition's conversations. An enquiry page shows only the conversations and follow-ups logged against that enquiry, so last year's agreement never appears on this year's page.

The sales director and the technical coordinator disagree about when to hand over. A simple yes or no can't satisfy both, so I design the assistant to gives one of three answers:

- **Ready:** the budget, stand area and requested height are known, and the height is within the edition's limit.
- **Partial:** the fair and budget are known, but the area or height is still missing. The enquiry goes to technical now for a feasibility check, and the assistant says what to ask the customer.
- **Blocked:** the height is over the limit, there is no budget yet, or there is nothing to hand over because the opportunity was lost or the fair edition has ended.

Account managers also need to know who to call and what they're waiting for. A conversation logged with a follow-up date becomes a follow-up. The Follow-ups page opens on today's list, shows who to call, what they're waiting for and a **Mark done** button, and can be filtered by account owner or day.

To keep searches practical at 100,000 contacts, name and email searches use trigram indexes, open follow-ups use a partial index, and long lists are capped.

### Trying the assistant

Start the app with `./dev.sh`, open http://localhost:3000, type an opportunity code into the search box, then press **Prepare technical handoff**. Each pass of Preparer, Checker and Coordinator is one loop, shown as a Round on the run page.

For a complete enquiry, try `OP000001`: it is **ready** after 1 loop.

For an incomplete enquiry, try `OP000003`: it is **partial** after 2 loops. In the first loop the Preparer proposes a full handoff and the Checker objects because the area and height are missing; in the second loop the Preparer proposes a partial handoff and the Coordinator stops. Then save a stand area of `80` and a height of `4,50` and run it again: run #2 is **ready**, and run #1 stays unchanged.

For a conflicting request, try `OP000005`: it is **blocked**, because 6.00 m is requested against a 5.00 m limit.

`OP000003` and `OP000005` are for a fair that ends on 23/10/2026, so after that date both are blocked because the fair edition has ended. `OP000086`, a 2027 enquiry with the height missing, works at any time: it is partial, then ready once a height of `4,00` is saved.

Each run page shows every loop's Preparer, Checker and Coordinator output and every lookup the assistant made. The wording comes from `app/stand_in_model.py`, a deterministic local stand-in labelled as such, with no API keys, model calls or downloads.

### Unfinished work

I haven't tested the app at 100,000 contacts. The assignment asks to plan for the archive growing to that size, with its companies, opportunities and activities, but I haven't loaded or timed a dataset that large. At that size, searching contacts and joining them to their companies, enquiries and activities would get slow if each query had to read whole tables, so my plan is indexing.


