-- NULL is allowed exactly where data/README.md says a value can be missing.

CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE companies (
    company_code   text PRIMARY KEY,
    company_name   text NOT NULL,
    -- ponytail: region depends on province_code; kept here because both are read-only legacy labels
    province_code  text NOT NULL,
    region         text NOT NULL,
    sales_rep      text NOT NULL,
    fax            text  -- one number per company, repeated on every contact row in the export
);

CREATE TABLE contacts (
    contact_code   text PRIMARY KEY,
    company_code   text NOT NULL REFERENCES companies,
    first_name     text NOT NULL,
    last_name      text NOT NULL,
    email          text,
    phone          text,
    UNIQUE (company_code, contact_code)
);

CREATE TABLE fair_editions (
    fair_edition_code   text PRIMARY KEY,
    fair_name           text NOT NULL,
    city                text NOT NULL,
    venue               text NOT NULL,
    starts_on           date NOT NULL,
    ends_on             date NOT NULL,
    max_stand_height_m  numeric(5,2) NOT NULL
);

CREATE TABLE opportunities (
    opportunity_code          text PRIMARY KEY,
    company_code              text NOT NULL REFERENCES companies,
    contact_code              text,
    fair_edition_code         text NOT NULL REFERENCES fair_editions,
    description               text NOT NULL,
    amount_eur                numeric(12,2) NOT NULL,  -- sales' recorded value, not the customer's budget
    status                    text NOT NULL,           -- legacy_status trimmed and lower-cased
    legacy_status             text NOT NULL,           -- as exported
    opened_on                 date NOT NULL,
    expected_close_on         date,
    historical_campaign_code  text,
    stand_area_sqm            numeric(8,2),
    client_budget_eur         numeric(12,2),
    requested_height_m        numeric(5,2),            -- may exceed the edition limit
    brief_notes               text NOT NULL,
    updated_at                timestamptz,             -- NULL until the brief is edited in the app
    UNIQUE (company_code, opportunity_code),
    -- the contact, when known, must work for the same company
    FOREIGN KEY (company_code, contact_code) REFERENCES contacts (company_code, contact_code)
);

-- Entries logged in the app get their own prefix, so they can never collide with imported AC… codes.
CREATE SEQUENCE app_entry_number;

CREATE TABLE activities (
    entry_id          text PRIMARY KEY DEFAULT 'APP' || lpad(nextval('app_entry_number')::text, 7, '0'),
    company_code      text NOT NULL REFERENCES companies,
    opportunity_code  text,                                -- empty for company-level entries
    activity_type     text NOT NULL CHECK (activity_type IN ('call', 'email', 'meeting', 'note', 'task')),
    occurred_at       timestamptz NOT NULL,
    details           text NOT NULL,
    follow_up_on      date,
    follow_up_done_at timestamptz,                         -- NULL = still open; the archive records no follow-up as done
    completed         boolean,                             -- NULL means not applicable (notes)
    author            text NOT NULL,
    -- the opportunity, when set, must belong to the same company
    FOREIGN KEY (company_code, opportunity_code) REFERENCES opportunities (company_code, opportunity_code)
);

-- Every handoff-assistant run, kept forever: re-running adds a run, it never overwrites one.
CREATE TABLE assistant_runs (
    opportunity_code  text NOT NULL REFERENCES opportunities,
    run_number        int NOT NULL,
    created_at        timestamptz NOT NULL DEFAULT now(),
    policy_version    text NOT NULL,
    input_snapshot    jsonb NOT NULL,  -- every tool call the Preparer made, with what it returned
    rounds            jsonb NOT NULL,  -- per round: the Preparer's brief, the Checker's findings, the Coordinator's decision
    outcome           text NOT NULL CHECK (outcome IN ('ready', 'partial', 'blocked')),
    reason            text NOT NULL,
    PRIMARY KEY (opportunity_code, run_number)
);

CREATE TABLE import_state (
    imported_at             timestamptz NOT NULL DEFAULT now(),
    archive_reference_time  timestamptz  -- when the export was taken (manifest.json); NULL if the manifest doesn't say
);

-- Search that stays fast at 100,000 contacts.
CREATE INDEX companies_name_trgm ON companies USING gin (company_name gin_trgm_ops);
CREATE INDEX contacts_name_trgm ON contacts USING gin ((first_name || ' ' || last_name) gin_trgm_ops);
CREATE INDEX contacts_email_trgm ON contacts USING gin (email gin_trgm_ops);

-- One enquiry's timeline, and a company's own entries.
CREATE INDEX activities_opportunity ON activities (opportunity_code, occurred_at DESC);
CREATE INDEX activities_company ON activities (company_code, occurred_at DESC);

-- Open follow-ups by date: exactly the rows the follow-ups page lists.
CREATE INDEX activities_open_follow_up ON activities (follow_up_on)
    WHERE follow_up_on IS NOT NULL AND follow_up_done_at IS NULL;
