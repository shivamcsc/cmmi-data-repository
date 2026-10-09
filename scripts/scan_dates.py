"""
List date cells in the stored workbooks that hold something but do not parse.

The teams type dates by hand into cells Excel is treating as text, so the same
column arrives spelled several ways and a format the parser does not know is
stored as NULL -- silently, because an empty cell and an unreadable one look
the same once loaded. This prints what is being dropped, grouped by shape, so
a missing format can be added to ingest._DATE_FORMATS and the rest confirmed
as genuinely not dates.

    ./.venv/bin/python scripts/scan_dates.py
    ./.venv/bin/python scripts/scan_dates.py --project cibil

What remains after a clean run should be only words in a date column
('Post UAT Signoff', 'Ongoing', 'NA') and real mistakes in the sheets
('4/31/2025' -- April has thirty days).
"""
import argparse
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import ingest
from app.models import RECORD_MODELS

UPLOADS = Path(__file__).resolve().parent.parent / "storage" / "uploads"


def shape(text):
    """A signature that groups like spellings: '06-01-2026' -> '99-99-9999'."""
    return re.sub(r"\d", "9", text)[:34]


def main():
    parser = argparse.ArgumentParser(description="Find date cells that fail to parse.")
    parser.add_argument("--project", help="limit to one project folder, e.g. cibil")
    args = parser.parse_args()

    types = {family: ingest.column_types(model) for family, model in RECORD_MODELS.items()}
    missed, examples = Counter(), {}
    by_column, by_file = Counter(), Counter()

    pattern = "%s/*/*.xls*" % (args.project or "*")
    for path in sorted(UPLOADS.glob(pattern)):
        try:
            sheets = ingest.parse_workbook(str(path))
        except Exception as exc:
            print("!! %s: %s" % (path.name, exc))
            continue

        for sheet in sheets:
            family = sheet.get("family")
            if not sheet.get("parsed") or family not in types:
                continue
            kinds = types[family]
            positions = [(i, c) for i, (c, _raw, _ignored) in enumerate(sheet["columns"])
                         if c and kinds.get(c, (None,))[0] == "date"]
            for row in sheet["rows"]:
                for index, column in positions:
                    if index >= len(row["values"]):
                        continue
                    raw = row["values"][index]
                    if ingest.is_blank(raw) or ingest.to_date(raw) is not None:
                        continue
                    text = str(raw).strip()
                    signature = shape(text)
                    missed[signature] += 1
                    examples.setdefault(signature, text)
                    by_column[column] += 1
                    by_file[path.name.split("__")[-1][:60]] += 1

    print("=== date cells holding something that does not parse ===")
    for signature, count in missed.most_common(25):
        print("  %6d  %-34s e.g. %s" % (count, signature, examples[signature]))
    print("\n=== by column ===")
    for column, count in by_column.most_common(20):
        print("  %6d  %s" % (count, column))
    print("\n=== by file ===")
    for name, count in by_file.most_common(15):
        print("  %6d  %s" % (count, name))
    print("\ntotal: %d" % sum(missed.values()))


if __name__ == "__main__":
    main()
