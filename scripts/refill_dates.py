"""
Re-read the date columns of every stored workbook and fill in what the parser
used to drop.

A date typed into a cell Excel treats as text stays text, and the teams type
them several ways, so `to_date` used to return None for roughly one date cell
in five -- the column was then stored NULL and the row looked like it had no
such date. Widening the format list fixes new uploads; this fills in the ones
already loaded, reading each upload's original file back off disk.

Only date columns are touched, and only where the stored value is NULL and the
sheet has something that now parses: nothing already loaded is overwritten, so
a correctly parsed date cannot be changed by a re-run.

    python scripts/refill_dates.py --dry-run     say what would change
    python scripts/refill_dates.py               apply it
    python scripts/refill_dates.py --project cibil
"""
import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select

from app import ingest
from app.db import SessionLocal
from app.models import RECORD_MODELS, Project, UploadBatch


def date_columns():
    """{family: [column names that hold a date]}"""
    return {family: [c for c, (kind, _limit) in ingest.column_types(model).items() if kind == "date"]
            for family, model in RECORD_MODELS.items()}


def rows_by_position(session, model, upload_id):
    """{(sheet_name, row_index): record} for one upload."""
    records = session.scalars(select(model).where(model.upload_id == upload_id))
    return {(r.sheet_name, r.row_index): r for r in records}


def main():
    parser = argparse.ArgumentParser(description="Backfill dates the old parser dropped.")
    parser.add_argument("--dry-run", action="store_true", help="report without writing")
    parser.add_argument("--project", help="limit to one project code, e.g. cibil")
    args = parser.parse_args()

    session = SessionLocal()
    dates = date_columns()
    filled = Counter()
    per_upload = Counter()
    missing_files = []
    unreadable = []

    try:
        query = select(UploadBatch).order_by(UploadBatch.id)
        if args.project:
            project = session.scalar(select(Project).where(Project.code == args.project))
            if project is None:
                sys.exit("no project with code %r" % args.project)
            query = query.where(UploadBatch.project_id == project.id)

        for upload in session.scalars(query):
            path = Path(upload.stored_path)
            if not path.is_absolute():
                path = Path(__file__).resolve().parent.parent / path
            if not path.exists():
                missing_files.append(upload.original_filename)
                continue
            try:
                sheets = ingest.parse_workbook(str(path))
            except Exception as exc:                      # a workbook that will not open
                unreadable.append("%s: %s" % (upload.original_filename, exc))
                continue

            for sheet in sheets:
                family = sheet.get("family")
                if not sheet.get("parsed") or family not in RECORD_MODELS:
                    continue
                model = RECORD_MODELS[family]
                wanted = set(dates[family])
                # where each date column sits in this sheet
                positions = [(i, column) for i, (column, _raw, _ignored)
                             in enumerate(sheet["columns"]) if column in wanted]
                if not positions:
                    continue

                stored = rows_by_position(session, model, upload.id)
                for row in sheet["rows"]:
                    record = stored.get((sheet["sheet_name"], row["row_index"]))
                    if record is None:
                        continue
                    for index, column in positions:
                        if index >= len(row["values"]):
                            continue
                        if getattr(record, column) is not None:
                            continue                      # never overwrite what is already there
                        value = ingest.to_date(row["values"][index])
                        if value is None:
                            continue
                        setattr(record, column, value)
                        filled[column] += 1
                        per_upload[upload.original_filename] += 1

        total = sum(filled.values())
        print("=== dates filled in ===")
        for column, count in filled.most_common():
            print("  %6d  %s" % (count, column))
        print("\n=== by file ===")
        for name, count in per_upload.most_common(15):
            print("  %6d  %s" % (count, name[:66]))
        if missing_files:
            print("\n%d upload(s) have no file on disk, skipped:" % len(missing_files))
            for name in missing_files[:10]:
                print("   ", name)
        if unreadable:
            print("\n%d workbook(s) would not open:" % len(unreadable))
            for line in unreadable[:10]:
                print("   ", line)

        if args.dry_run:
            session.rollback()
            print("\ndry run -- %d value(s) would be filled, nothing written" % total)
        else:
            session.commit()
            print("\nfilled %d value(s)" % total)
    finally:
        session.close()


if __name__ == "__main__":
    main()
