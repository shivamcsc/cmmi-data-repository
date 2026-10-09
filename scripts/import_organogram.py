"""
Load the vertical organogram.

    python scripts/import_organogram.py --file "<path to the benchmark sheet>"
    python scripts/import_organogram.py --from-share          (pulls it from Nextcloud)

Reads the 'Project Name / Vertical Division / Domain / Business manager /
Technical Manager / Vertical Head / Engineers' table and stores one row per
project, after cleaning it:

  * trailing and doubled spaces removed ('Mayank Rawat ', 'Anupam Singh  & ...')
  * '&' and ',' separated name lists normalised to ', '
  * blank and placeholder cells ('-', 'NA', 'Vaccant') stored as nothing
  * unfilled posts ('Replacement -01', 'Replacement - 06') dropped -- they are
    vacancies, not people
  * each row matched to a seeded Project where the names correspond

Re-running replaces the previous import.
"""
import argparse
import base64
import re
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import delete, select

from app.db import SessionLocal
from app.models import OrganogramEntry, Project

SHARE_PATH = ("/public.php/webdav/Updated_Final%20Approach_Benchmark"
              "%2015062026%20%28SH%2CDM%2CEngineers%29%20%284%29.xlsx")

#: source column heading -> database column
COLUMNS = {
    "project name": "project_name",
    "vertical division": "vertical_division",
    "domain": "domain",
    "scope": "scope",
    "business manager": "business_manager",
    "statehead sme": "state_head",
    "distict manager": "district_manager",
    "district manager": "district_manager",
    "sdm": "sdm",
    "technical manager": "technical_manager",
    "vertical head": "vertical_head",
    "engineers": "engineers",
}

#: source project name -> seeded project code, where the two differ
PROJECT_ALIASES = {
    "li gi": "insurance",
    "bc management": "psb",
    "csc safar irctc": "csc_safar_irctc",
    "bill payment": "dsp_electricity",
    "e district": "dsp_edistrict",
    "bureau cibil": "cibil",
    "csc safar air red bus": "csc_safar_air",
    "nfdp fisheries": "nfdp_fisheries",
    "crm": "salesforce_crm",
    "digipay": "digipay",
    "cctns tripura": "cctns",
}

PLACEHOLDERS = {"", "-", "--", "na", "n a", "nil", "none", "tbd", "vaccant", "vacant"}

#: unfilled posts written as 'Replacement - 01', 'Replacement-06', 'Replacement 2'.
#: These are vacancies rather than people, so they are not stored as names.
#: a leading '1)' or '2.' list number, which these sheets use
_LIST_NUMBER = re.compile(r"^\s*\d+\s*[).]\s*")
_VACANCY = re.compile(r"^(replacement|vacancy|to be hired|new resource)\s*[-–—]?\s*\d*$",
                      re.IGNORECASE)


def _is_vacancy(text):
    return bool(_VACANCY.match(_LIST_NUMBER.sub("", text).strip()))


def key(text):
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()


def clean(value):
    """Collapse whitespace, normalise name separators, drop placeholders."""
    if value is None:
        return None
    text = re.sub(r"\s+", " ", str(value).replace("\n", " ")).strip()
    if key(text) in PLACEHOLDERS or _is_vacancy(text):
        return None
    # a name list may carry a vacancy alongside real people: drop just that part
    parts = [x.strip() for x in re.split(r"\s*(?:,|&)\s*", text) if x.strip()]
    kept = [x for x in parts if not _is_vacancy(x) and key(x) not in PLACEHOLDERS]
    if len(kept) != len(parts):
        text = ", ".join(kept)
    text = re.sub(r"\s*&\s*", " & ", text)
    text = re.sub(r"\s*,\s*", ", ", text)
    return text or None


def fetch_from_share(token, password, target):
    url = "https://docs.csccloud.in" + SHARE_PATH
    req = urllib.request.Request(url)
    req.add_header("Authorization", "Basic " +
                   base64.b64encode(("%s:%s" % (token, password)).encode()).decode())
    with urllib.request.urlopen(req, timeout=120) as response, open(target, "wb") as handle:
        handle.write(response.read())
    return target


def find_table(worksheet):
    """Locate the header row and map its columns; the table does not start at row 1."""
    rows = [list(r) for r in worksheet.iter_rows(values_only=True)]
    for index, row in enumerate(rows):
        mapping = {}
        for position, cell in enumerate(row):
            column = COLUMNS.get(key(cell))
            if column and column not in mapping:
                mapping[position] = column
        if len(set(mapping.values())) >= 4:
            return index, mapping, rows
    return None, {}, rows


def main():
    parser = argparse.ArgumentParser(description="Import the vertical organogram.")
    parser.add_argument("--file", help="local copy of the benchmark sheet")
    parser.add_argument("--from-share", action="store_true", help="download it first")
    parser.add_argument("--token", default="uyINe2gd5xontf2")
    parser.add_argument("--password", default="Cmmi@123")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    import openpyxl

    path = args.file
    if args.from_share or not path:
        path = fetch_from_share(args.token, args.password,
                                Path(__file__).resolve().parent.parent
                                / "storage" / "organogram.xlsx")
        print("downloaded %s" % path)

    book = openpyxl.load_workbook(path, read_only=True, data_only=True)
    session = SessionLocal()
    try:
        projects = list(session.scalars(select(Project)))
        by_code = {p.code: p for p in projects}
        by_name = {key(p.name): p for p in projects}

        entries = []
        for sheet_name in book.sheetnames:
            header_index, mapping, rows = find_table(book[sheet_name])
            if header_index is None:
                continue
            print("\\nreading '%s', headers on row %d" % (sheet_name, header_index + 1))
            for row in rows[header_index + 1:]:
                values = {column: clean(row[position] if position < len(row) else None)
                          for position, column in mapping.items()}
                if not values.get("project_name") and not values.get("vertical_division"):
                    continue

                name_key = key(values.get("project_name"))
                project = (by_code.get(PROJECT_ALIASES.get(name_key, ""))
                           or by_name.get(name_key))
                values["project_id"] = project.id if project else None
                values["source_file"] = Path(path).name
                entries.append(values)
                print("   %-26s %-26s -> %-22s head: %s" % (
                    (values.get("project_name") or "?")[:26],
                    (values.get("vertical_division") or "?")[:26],
                    project.name[:22] if project else "(no project match)",
                    values.get("vertical_head") or "-"))
            break
        book.close()

        if args.dry_run:
            print("\\ndry run: %d row(s) would be stored" % len(entries))
            return

        session.execute(delete(OrganogramEntry))
        for values in entries:
            session.add(OrganogramEntry(**values))
        session.commit()
        matched = sum(1 for e in entries if e["project_id"])
        print("\\nstored %d row(s); %d matched to a project, %d not matched"
              % (len(entries), matched, len(entries) - matched))
    finally:
        session.close()


if __name__ == "__main__":
    main()
