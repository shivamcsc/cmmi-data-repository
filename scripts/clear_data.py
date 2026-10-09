"""
Remove every uploaded sheet and everything read from it, leaving the reference
data (tracks and projects) in place.

    python scripts/clear_data.py           ask first
    python scripts/clear_data.py --yes     no prompt

Deletes: development and services rows, sheet tabs, upload batches,
reporting periods, and the stored files under storage/uploads/.
"""
import argparse
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import delete, func, select, text

from app import config
from app.db import SessionLocal, engine
from app.models import (
    DevelopmentRecord, ReportingPeriod, ServicesRecord, SheetTab, UploadBatch,
)

# child tables first so foreign keys stay satisfied
ORDER = [
    ("development rows", DevelopmentRecord),
    ("services rows", ServicesRecord),
    ("sheet tabs", SheetTab),
    ("upload batches", UploadBatch),
    ("reporting periods", ReportingPeriod),
]


def main():
    parser = argparse.ArgumentParser(description="Delete all uploaded sheet data.")
    parser.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    args = parser.parse_args()

    session = SessionLocal()
    try:
        counts = {label: session.scalar(select(func.count()).select_from(model))
                  for label, model in ORDER}
        files = [p for p in config.UPLOAD_DIR.rglob("*") if p.is_file() and p.name != ".gitkeep"]

        print("About to delete:")
        for label, _model in ORDER:
            print("  %-20s %d" % (label, counts[label]))
        print("  %-20s %d" % ("stored files", len(files)))

        if not args.yes:
            if input("\nType 'yes' to delete: ").strip().lower() != "yes":
                print("cancelled")
                return

        for label, model in ORDER:
            session.execute(delete(model))

        session.commit()

        with engine.connect() as conn:
            for table in ("development_record", "services_record", "sheet_tab",
                          "upload_batch", "reporting_period"):
                conn.execute(text("ALTER TABLE `%s` AUTO_INCREMENT = 1" % table))
            conn.commit()

        for child in config.UPLOAD_DIR.iterdir():
            if child.is_dir():
                shutil.rmtree(child)
            elif child.name != ".gitkeep":
                child.unlink()

        print("\nDeleted %d rows across %d tables and %d file(s)."
              % (sum(counts.values()), len(ORDER), len(files)))
        print("Tracks and projects are untouched.")
    finally:
        session.close()


if __name__ == "__main__":
    main()
