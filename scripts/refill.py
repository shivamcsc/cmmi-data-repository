"""
Re-read every stored workbook and fill in the columns the parser used to drop.

Two things left columns empty that should not have been:

  Dates. A date typed into a cell Excel treats as text stays text, and the
  teams type them several ways, so `to_date` returned None for roughly one
  date cell in five and the row looked like it had no such date.

  Merged and carried-down cells. Excel keeps a merged block's value in its
  top-left cell only, so a module merged down its sub-module rows was read
  once and then came back empty; and 'NFDP' is typed once at the top of
  Project Name with the cells below simply left blank.

Fixing the reader fixes new uploads; this fills in the ones already loaded,
reading each upload's original file back off disk.

Only a column that is NULL now and has a value in the sheet is written:
nothing already loaded is overwritten, so a re-run cannot change a value that
parsed correctly the first time.

    python scripts/refill.py --dry-run     say what would change
    python scripts/refill.py               apply it
    python scripts/refill.py --project nfdp_fisheries
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


def fillable_columns():
    """
    {family: [columns worth re-reading]} -- every date, plus the identity
    columns a merge or a carried-down value used to leave empty.
    """
    out = {}
    for family, model in RECORD_MODELS.items():
        dates = [c for c, (kind, _limit) in ingest.column_types(model).items() if kind == "date"]
        out[family] = list(dict.fromkeys(dates + list(ingest.CARRY_DOWN.get(family, ()))))
    return out


def rows_by_position(session, model, upload_id):
    """{(sheet_name, row_index): record} for one upload."""
    records = session.scalars(select(model).where(model.upload_id == upload_id))
    return {(r.sheet_name, r.row_index): r for r in records}


def main():
    parser = argparse.ArgumentParser(description="Backfill columns the old reader dropped.")
    parser.add_argument("--dry-run", action="store_true", help="report without writing")
    parser.add_argument("--project", help="limit to one project code, e.g. cibil")
    args = parser.parse_args()

    session = SessionLocal()
    fillable = fillable_columns()
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
                wanted = set(fillable[family])
                types = ingest.column_types(model)
                # where each fillable column sits in this sheet
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
                        cell = row["values"][index]
                        if ingest.is_blank(cell):
                            continue
                        # coerce the way the loader would -- a date column
                        # wants a date, a name column wants trimmed text
                        kind, limit = types.get(column, ("text", None))
                        value = ingest.coerce(cell, kind, limit)
                        if value is None:
                            continue
                        setattr(record, column, value)
                        filled[column] += 1
                        per_upload[upload.original_filename] += 1

        total = sum(filled.values())
        print("=== values filled in ===")
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
