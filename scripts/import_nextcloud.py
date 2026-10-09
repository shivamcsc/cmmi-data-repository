"""
Import every data collection sheet from the CSC Nextcloud share.

    python scripts/import_nextcloud.py --dry-run
    python scripts/import_nextcloud.py --token <share> --password <pw>

Walks 'CMMI ML 5.0 (Development)/Current' and 'CMMI ML 3.0(Services)/Current',
matches each project folder to a seeded project, reads the reporting period
from the sub-folder and file name, and records for every sheet:

  * the name exactly as it appears in the share
  * the full folder path, with slashes
  * the date the share last modified it (source_modified_at)

'ML 3 Artefacts of DSP' is skipped -- test cases and tracking artefacts, not
data collection sheets. Imports land as 'pending_review' for confirmation.
"""
import argparse
import base64
import datetime as dt
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select

from app import config
from app.db import SessionLocal
from app.ingest import DuplicateUpload
from app.models import Category, Project, User
from app.service import create_upload, get_or_create_period, store_file, suggest_period_label

BASE = "https://docs.csccloud.in"
DAV = "{DAV:}"
TREES = [
    ("development", "CMMI ML 5.0 (Development)/Current"),
    ("services", "CMMI ML 3.0(Services)/Current"),
]
SKIP_FOLDERS = ("ml 3 artefacts",)


def quote(path):
    return urllib.parse.quote(path, safe="/")


class Share(object):
    def __init__(self, token, password):
        self.auth = base64.b64encode(("%s:%s" % (token, password)).encode()).decode()

    def _request(self, href, method="GET", depth=None, attempts=4):
        """The share returns an occasional 405/5xx under load, so retry with backoff."""
        last = None
        for attempt in range(attempts):
            req = urllib.request.Request(BASE + href, method=method)
            req.add_header("Authorization", "Basic " + self.auth)
            if depth is not None:
                req.add_header("Depth", depth)
            try:
                return urllib.request.urlopen(req, timeout=120)
            except Exception as exc:                        # noqa: BLE001
                last = exc
                if attempt < attempts - 1:
                    time.sleep(2 * (attempt + 1))
        raise last

    def listdir(self, href):
        with self._request(href, "PROPFIND", "1") as response:
            body = response.read()
        out = []
        for entry in ET.fromstring(body).findall(DAV + "response"):
            child = entry.find(DAV + "href").text
            # compare decoded paths: the server may return the collection itself
            # with different percent-encoding than we asked for
            if (urllib.parse.unquote(child).rstrip("/")
                    == urllib.parse.unquote(href).rstrip("/")):
                continue
            modified = entry.find(".//" + DAV + "getlastmodified")
            out.append({
                "href": child,
                "name": urllib.parse.unquote(child.rstrip("/").split("/")[-1]),
                "is_dir": entry.find(".//" + DAV + "collection") is not None,
                "modified": modified.text if modified is not None else None,
            })
        return sorted(out, key=lambda e: e["name"].lower())

    def download(self, href, target):
        with self._request(href) as response, open(target, "wb") as handle:
            handle.write(response.read())


def parse_http_date(value):
    if not value:
        return None
    try:
        return dt.datetime.strptime(value, "%a, %d %b %Y %H:%M:%S %Z")
    except ValueError:
        return None


def key(text):
    return re.sub(r"[^a-z0-9]+", "", str(text).lower())


def match_project(projects, folder_name):
    target = key(folder_name)
    for project in projects:
        if key(project.name) == target:
            return project
    for project in projects:
        if target and (target in key(project.name) or key(project.name) in target):
            return project
    return None


def collect(share, href, rel, depth=0, limit=3):
    """[(file_entry, folder_path_with_slashes)] for every sheet below href."""
    found = []
    for entry in share.listdir(href):
        child_rel = rel + "/" + entry["name"]
        if entry["is_dir"]:
            if depth < limit:
                found.extend(collect(share, entry["href"], child_rel, depth + 1, limit))
        elif (Path(entry["name"]).suffix.lower() in config.ALLOWED_EXTENSIONS
              and not entry["name"].startswith("~$")):
            found.append((entry, rel))
    return found


def main():
    parser = argparse.ArgumentParser(description="Import sheets from the Nextcloud share.")
    parser.add_argument("--token", default="uyINe2gd5xontf2", help="public share token")
    parser.add_argument("--password", default="Cmmi@123")
    parser.add_argument("--as-user", default="admin",
                        help="login id to record as the uploader")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    share = Share(args.token, args.password)
    session = SessionLocal()
    actor = session.scalar(select(User).where(User.username == args.as_user.strip().lower()))
    if actor is None:
        parser.error("no sign-in with login id %r -- create one first" % args.as_user)
    loaded = skipped = failed = 0
    try:
        for category_code, tree in TREES:
            category = session.scalar(select(Category).where(Category.code == category_code))
            projects = list(session.scalars(
                select(Project).where(Project.category_id == category.id)
            ))
            root = "/public.php/webdav/" + quote(tree) + "/"
            print("\n" + "=" * 78)
            print(tree)
            print("=" * 78)

            for folder in share.listdir(root):
                if not folder["is_dir"]:
                    continue
                if any(s in folder["name"].lower() for s in SKIP_FOLDERS):
                    print("\n-- %s: skipped (not data collection sheets)" % folder["name"])
                    continue
                project = match_project(projects, folder["name"])
                if project is None:
                    print("\n-- %s: no matching project, skipped" % folder["name"])
                    continue

                print("\n%s  ->  %s" % (folder["name"], project.name))
                try:
                    sheets = collect(share, folder["href"], tree + "/" + folder["name"])
                except Exception as exc:                    # noqa: BLE001
                    print("   !! could not list this folder: %s" % str(exc)[:60])
                    failed += 1
                    continue
                for entry, folder_path in sheets:
                    subfolder = folder_path.split("/" + folder["name"], 1)[-1].lstrip("/")
                    modified = parse_http_date(entry["modified"])
                    label = suggest_period_label(entry["name"], subfolder)["label"]
                    note = None
                    if not label and modified:
                        label = modified.strftime("%b %Y")
                        note = ("Period not stated in the file or folder name; taken from the "
                                "share's last-modified date (%s). Please confirm."
                                % modified.strftime("%d %b %Y"))
                    if not label:
                        print("   ?? %-52s no period, skipped" % entry["name"][:52])
                        skipped += 1
                        continue

                    if args.dry_run:
                        print("   .. %-52s %-22s %s" % (
                            entry["name"][:52], label,
                            modified.strftime("%d %b %Y") if modified else "?"))
                        continue

                    try:
                        period = get_or_create_period(session, label)
                        target = config.UPLOAD_DIR / "_incoming"
                        target.mkdir(parents=True, exist_ok=True)
                        temp = target / entry["name"]
                        share.download(entry["href"], temp)
                        with open(temp, "rb") as handle:
                            stored = store_file(entry["name"], handle,
                                                project.code, period.label)
                        temp.unlink()

                        upload, _ = create_upload(
                            session, project, period, entry["name"], stored,
                            uploaded_by=actor.full_name,
                            uploaded_by_id=actor.id,
                            source_folder=folder_path,
                            source_modified_at=modified,
                            notes=note,
                        )
                        print("   ok %-52s %-22s %-12s %s rows" % (
                            entry["name"][:52], label,
                            modified.strftime("%d %b %Y") if modified else "?",
                            upload.row_count))
                        loaded += 1
                    except DuplicateUpload:
                        session.rollback()
                        print("   == %-52s already loaded" % entry["name"][:52])
                        skipped += 1
                    except Exception as exc:                     # noqa: BLE001
                        session.rollback()
                        print("   !! %-52s %s" % (entry["name"][:52], str(exc)[:60]))
                        failed += 1

        print("\n" + "=" * 78)
        print("loaded %d, skipped %d, failed %d" % (loaded, skipped, failed))
        if loaded:
            print("All imports are pending review -- confirm them at /uploads")
    finally:
        session.close()


if __name__ == "__main__":
    main()
