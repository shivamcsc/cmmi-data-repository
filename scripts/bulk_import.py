"""
Load many sheets at once from a folder laid out like the Nextcloud share:

    <root>/<Project>/<period folder>/<sheet>.xlsx
    <root>/<Project>/<sheet>.xlsx                 (period read from the file name)

Usage
    ./.venv/bin/python scripts/bulk_import.py --category development --root "/path/to/Current"
    ./.venv/bin/python scripts/bulk_import.py --category services --root "..." --dry-run

Projects are matched to the seeded list by name, ignoring case, spacing and
punctuation, so 'CSC Safar( IRCTC)' and 'NFDP(Fisheries)' both land correctly.
Imported sheets are left as 'pending_review' -- confirm them in the web app.
"""
import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select

from app import config
from app.db import SessionLocal
from app.ingest import DuplicateUpload
from app.models import Category, Project
from app.service import create_upload, get_or_create_period, store_file, suggest_period_label

SKIP_NAMES = ("ml 3 artefacts",)   # not data collection sheets


def key(text):
    return re.sub(r"[^a-z0-9]+", "", str(text).lower())


def match_project(projects, folder_name):
    target = key(folder_name)
    for project in projects:
        if key(project.name) == target:
            return project
    for project in projects:                     # fall back to containment
        if target and (target in key(project.name) or key(project.name) in target):
            return project
    return None


def main():
    parser = argparse.ArgumentParser(description="Bulk import CMMI data collection sheets.")
    parser.add_argument("--category", required=True, choices=["development", "services"])
    parser.add_argument("--root", required=True, help="folder holding the project folders")
    parser.add_argument("--uploaded-by", default="bulk-import")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    root = Path(args.root).expanduser()
    if not root.is_dir():
        parser.error("%s is not a folder" % root)

    session = SessionLocal()
    try:
        category = session.scalar(select(Category).where(Category.code == args.category))
        projects = list(session.scalars(select(Project).where(Project.category_id == category.id)))

        loaded = skipped = failed = 0
        for project_dir in sorted(p for p in root.iterdir() if p.is_dir()):
            if any(s in project_dir.name.lower() for s in SKIP_NAMES):
                print("-- skipping %s (not data collection sheets)" % project_dir.name)
                continue
            project = match_project(projects, project_dir.name)
            if project is None:
                print("-- no project matches folder %r; skipping" % project_dir.name)
                continue

            print("\n== %s -> %s" % (project_dir.name, project.name))
            for path in sorted(project_dir.rglob("*")):
                if not path.is_file() or path.suffix.lower() not in config.ALLOWED_EXTENSIONS:
                    continue
                if path.name.startswith("~$"):
                    continue
                folder_hint = path.parent.name if path.parent != project_dir else ""
                label = suggest_period_label(path.name, folder_hint)["label"]
                if not label:
                    print("   ?? %-56s no period in name -- upload by hand" % path.name[:56])
                    skipped += 1
                    continue
                if args.dry_run:
                    print("   .. %-56s -> %s" % (path.name[:56], label))
                    continue
                try:
                    period = get_or_create_period(session, label)
                    with open(path, "rb") as handle:
                        stored = store_file(path.name, handle, project.code, period.label)
                    upload, _ = create_upload(
                        session, project, period, path.name, stored,
                        uploaded_by=args.uploaded_by,
                        source_folder=str(path.parent.relative_to(root)),
                    )
                    print("   ok %-56s %-22s %s rows" % (path.name[:56], label, upload.row_count))
                    loaded += 1
                except DuplicateUpload:
                    session.rollback()
                    print("   == %-56s already loaded" % path.name[:56])
                    skipped += 1
                except Exception as exc:                 # noqa: BLE001
                    session.rollback()
                    print("   !! %-56s %s" % (path.name[:56], str(exc)[:70]))
                    failed += 1

        print("\nloaded %d, skipped %d, failed %d" % (loaded, skipped, failed))
        if loaded:
            print("All imports are pending review -- confirm them at /uploads")
    finally:
        session.close()


if __name__ == "__main__":
    main()
