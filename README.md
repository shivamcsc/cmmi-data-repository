# CMMI Data Collection Repository

Upload CMMI data collection sheets and store them in MySQL in a shape that
stays usable when the modelling work starts.

Covers both tracks:

| Track | Projects |
|---|---|
| **CMMI ML 5.0 (Development)** | CCTNS, CIBIL, CSC Safar (Air Tourism & Travel), Digipay, NFDP (Fisheries), Sales Force CRM |
| **CMMI ML 3.0 (Services)** | CSC Safar (IRCTC), DSP e-district, DSP electricity, Insurance, Public Sector Banking |

`ML 3 Artefacts of DSP` is deliberately not a project — it holds test cases and
tracking artefacts, not data collection sheets.

## Setup

```bash
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
cp .env.example .env          # then set DB_PASSWORD
./.venv/bin/python scripts/init_db.py
./run.sh
```

Open http://127.0.0.1:8000.

## How a sheet gets in

1. **Upload** — choose the track, the project, and the file. The reporting
   period is read from the file name (`Cycle 3 (April-June)`, `Apr-May26`,
   `July -Sep 2025`) and shown as an editable default, so a misread name never
   silently mislabels data.
2. **Review** — the upload page shows every column that was accepted and the
   rows as they were stored, plus any column it did not recognise and any
   worksheet it skipped.
3. **Confirm** — nothing counts as accepted data until someone confirms it.
   Browse and CSV export show confirmed uploads only. Rejecting removes the
   parsed rows but keeps the record and the original file.

Re-uploading the identical file for the same project and period is refused
unless you tick *load anyway*. A newer sheet for a project and period marks the
previous one **superseded** rather than deleting it.

### Bulk import

For a folder laid out like the Nextcloud share:

```bash
./.venv/bin/python scripts/bulk_import.py --category development \
    --root "/path/to/CMMI ML 5.0 (Development)/Current" --dry-run
```

Drop `--dry-run` to load. Imports land as *pending review*.

## The database

Plain tables. One database row per spreadsheet row, with the sheet's columns as
real columns — so you can query it with ordinary SQL and no joins.

```
category ─< project ─┐
reporting_period ────┼─< upload_batch ─< sheet_tab
                     └──< development_record
                     └──< services_record
```

| Table | Holds |
|---|---|
| `category`, `project` | the two tracks and their projects |
| `reporting_period` | `Cycle 3 (Apr-Jun 2026)` with real start/end dates; periods sort by `start_date` |
| `upload_batch` | one uploaded workbook: who, when, file hash, status, review decision |
| `sheet_tab` | each worksheet, where its header row was, or why it was skipped |
| **`development_record`** | one row per module / CR / use case — 43 data columns |
| **`services_record`** | one row per ticket — 21 data columns |

Each data row carries `upload_name` (the file it came from), `period_label`,
`sheet_name` and `row_index`, plus the usual foreign keys. A row is identified
by **the file it was uploaded as together with its own name** — `service_name`
for Development, `request_id` for Services — and both pairs are indexed.

Note these are indexes, not unique constraints: Request IDs repeat in the
Services logs (five do in the e-district June 2026 sheet), so a unique key
would reject real rows.

### Columns are fixed, headings are not

The sheets do not agree with each other. Column counts range from 19 to 76,
the header row sits on row 1, 3 or 5 depending on the project, and the same
measure is written several ways. Each heading is matched to one fixed column:

| Seen in the sheets | Stored in column |
|---|---|
| `No. of requirement review defects`, `No- of requirement review defects` | `req_review_defects` |
| `CUT Start Date`, `Coding & UT Start Date` | `cut_start` |
| `External defects (UAT/ production)`, `Total Defects after release` | `external_defects` |
| `Module/CR/Use case`, `Service/Module` | `service_name` |
| `Size(old cal.)` / `Size New (as per model develp.)` / `Size Updated July 2026` | `size_old` / `size_new` |

An unrecognised heading is kept in the row's `extra_columns` JSON field and
flagged on the upload page, so nothing is lost and the table stays stable.

Services logs repeat `Defined SLA` and `SLA Compliance` twice, once after the
response columns and once after the resolution columns. They are told apart by
position and stored as `response_defined_sla` and `resolution_defined_sla`.

### Dates arrive in several spellings

A date typed into a cell Excel is treating as text stays text, and each sheet is
filled in by hand, so one column holds several spellings at once. CIBIL writes
the whole stamp with a colon before the time — `06-01-2026:10:00:00` — which is
why those columns look like dates on screen but are strings underneath. Others
use `31-Mar-26`, `15-06-2026, 2:33 pm`, `18/08/2025, 2:20pm`.

Every spelling found in the share is listed in `ingest._DATE_FORMATS`, day
first throughout — these are Indian sheets, so `06-01-2026` is 6 January. An
unreadable date is stored NULL, which looks exactly like an empty cell, so two
scripts keep that honest:

```bash
./.venv/bin/python scripts/scan_dates.py              # what still fails to parse
./.venv/bin/python scripts/refill_dates.py --dry-run  # fill blanks in loaded rows
```

`scan_dates.py` groups failures by shape, so a missing format shows up as a
count rather than as quietly absent data. What it still reports is meant to
fail: words in a date column (`Post UAT Signoff`, `Ongoing`, `NA`) and real
mistakes in the sheets (`4/31/2025` — April has thirty days).

`refill_dates.py` re-reads each upload's original file off disk and fills date
columns that are NULL where the sheet now parses. It never overwrites a value
already stored, so it is safe to re-run.

### Deliberately not stored

- **`S.No.`** — a row counter, carries no information.
- **The `FOR MQAS USE` block** — `Cycle Time`, `Effort Variance`, `Productivity`
  and `Defect Removal Effectiveness`. MQAS fills these in, not the project
  teams, and they are derivable from the stored columns anyway.

These headings are recognised and skipped, so they are not treated as unknown
columns. The `/columns` page lists every stored column and both exclusions.

### Sheets that are not loaded

Only worksheets that look like data collection sheets are parsed — a sheet must
carry enough recognised columns *and* an anchor column (an effort column for
Development, a logging/response/resolution timestamp for Services). This keeps
the estimation and size tabs bundled into the CCTNS and CIBIL workbooks out of
the data tables. Skipped tabs are listed on the upload page with the reason;
the original file always keeps them.

## Who did what

`/activity` — administrator and CTO only — is the trail, newest first: one row
per dated action, with who did it and what they did it to.

| When | Who | Did what | Project | Period | Sheet / account | Note |
|---|---|---|---|---|---|---|
| 08 Oct 2026 15:00 | Nirmal Kumar · CTO | Approved — CTO (final) | CCTNS | Sep 2026 | CCTNS … Sept 2026.xlsx | |
| 04 Apr 2026 11:12 | Subodh Mishra · Vertical Head | Turned down | Digipay | Cycle 3 | Digipay Q1.xlsx | at the Vertical Head stage: effort column blank |

Filter by project, person, kind of action, and a date range; **Download CSV**
gives every matching row, not just the first few hundred shown.

Nothing new is recorded for this. Each action already leaves its mark on the
row it acted on — `uploaded_by`/`uploaded_at`, then one approved-by/approved-at
pair per stage, then the rejection — and `app/activity.py` reads those columns
back out, turning one upload into up to five dated events so they can be read
in time order. The columns stay the record of truth; a separate audit table
could drift from them.

Two limits worth knowing:

- Only the **most recent** sign-in is kept per account, not every sign-in.
- **Deleting** an upload deletes its row, and so removes it from this trail.
  Deletion is the administrator's alone.

## Getting the data out

`/data` switches between the two tracks and filters by project and period, and
only shows columns that actually hold data. **Download all as CSV** gives the
table as a flat file — the same columns, one row per sheet row.

Straight into pandas — no reshaping needed:

```python
import pandas as pd
df = pd.read_csv("cmmi_development.csv")
df.groupby("period_label")["actual_effort"].mean()
```

Or query MySQL directly:

```sql
SELECT period_label,
       COUNT(*)                                            AS rows_n,
       ROUND(AVG(size_new / actual_effort), 4)             AS productivity,
       ROUND(AVG((actual_effort - planned_effort)
                 / planned_effort * 100), 1)               AS effort_variance_pct
FROM development_record
WHERE actual_effort > 0
GROUP BY period_label;
```

Sheets vary in which size column they use, so for modelling take the first one
present:

```sql
SELECT service_name, COALESCE(size, size_new, size_old) AS size_pts
FROM development_record;
```

To restrict to approved data, join `upload_batch` and filter
`review_status = 'confirmed'` — that is what the web pages do by default.

## Layout

```
app/
  config.py          settings from .env
  db.py              engine and session
  models.py          the schema
  metrics_catalog.py the fixed column catalogue and heading matching
  periods.py         reads periods out of folder and file names
  ingest.py          workbook parsing and loading
  service.py         shared operations
  activity.py        who did what, read back out of the approval columns
  main.py            FastAPI routes
  templates/         pages
scripts/
  init_db.py         create tables and reference data (--reset to wipe)
  clear_data.py      delete all uploaded sheet data, keep tracks and projects
  bulk_import.py     load a whole folder tree
  scan_dates.py      date cells that do not parse, grouped by shape
  refill_dates.py    backfill dates a narrower parser once dropped
storage/uploads/     every uploaded file, kept as sent
```

## What is not stored

Anything a query can work out is left out of the schema rather than kept in
step by hand:

- **No `sort_order` columns.** Categories come back in the order `seed.py`
  lists them; projects and periods are ordered by name and `start_date`.
- **No `year` / `quarter` / `month` on a period.** `YEAR(start_date)` and
  `QUARTER(start_date)` give the same answer and cannot drift.
- **No `sort_key`.** It was `start_date` reformatted, so `ORDER BY start_date`
  replaces it. Periods without dates (`Cycle 1`) sort last.

## Notes

- Uploaded files are kept on disk under `storage/uploads/<project>/<period>/`,
  so the original is always recoverable.
- `.env` holds the database password and is git-ignored.
- The app binds to `127.0.0.1` and has no login. Put it behind a reverse proxy
  with authentication before exposing it beyond this machine.
