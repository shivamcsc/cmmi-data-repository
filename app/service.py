"""Operations shared by the web routes and the bulk-import script."""
import datetime as dt
import re
import shutil
import unicodedata
from pathlib import Path

from sqlalchemy import func, select

from app import config, periods
from app.ingest import load_upload
from app.models import (
    Category, DevelopmentRecord, Project, ReportingPeriod, ServicesRecord, UploadBatch,
)


def slugify(text, limit=60):
    text = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode()
    text = re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("_")
    return (text or "file")[:limit]


def get_or_create_period(session, label, start_date=None, end_date=None, period_type=None):
    """Periods are shared across projects, so 'Apr-Jun 2026' is one row."""
    label = (label or "").strip()
    if not label:
        raise ValueError("A reporting period is required.")
    period = session.scalar(select(ReportingPeriod).where(ReportingPeriod.label == label))
    if period is not None:
        return period

    parsed = periods.suggest(label)
    start_date = start_date or parsed["start_date"]
    end_date = end_date or parsed["end_date"]
    period = ReportingPeriod(
        label=label,
        period_type=period_type or parsed["period_type"],
        start_date=start_date,
        end_date=end_date,
    )
    session.add(period)
    session.flush()
    return period


def store_file(upload_filename, source, project_code, period_label):
    """Copy the uploaded workbook under storage/uploads/<project>/<period>/."""
    folder = config.UPLOAD_DIR / slugify(project_code, 40) / slugify(period_label, 40)
    folder.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    target = folder / ("%s__%s" % (stamp, slugify(upload_filename, 120)))
    with open(target, "wb") as handle:
        shutil.copyfileobj(source, handle)
    return target


def create_upload(session, project, period, filename, stored_path, uploaded_by=None,
                  uploaded_by_id=None, notes=None, source_folder=None,
                  source_modified_at=None, allow_duplicate=False):
    """Register the upload then parse it. Status ends up success/partial/failed."""
    upload = UploadBatch(
        category_id=project.category_id,
        project_id=project.id,
        period_id=period.id,
        original_filename=filename[:512],
        stored_path=str(stored_path),
        file_size=Path(stored_path).stat().st_size,
        uploaded_by=(uploaded_by or "").strip()[:255] or None,
        uploaded_by_id=uploaded_by_id,
        notes=(notes or "").strip() or None,
        source_folder=(source_folder or "").strip()[:512] or None,
        source_modified_at=source_modified_at,
        status="pending",
    )
    session.add(upload)
    session.flush()
    sheets = load_upload(session, upload, stored_path, allow_duplicate=allow_duplicate)
    return upload, sheets


def suggest_period_label(filename, folder_hint=None):
    """
    Work out the period, preferring the folder.

    The folder is what groups a cycle ('Data collection sheet Cycle 2 (Jan)'),
    while the file name usually carries the date it was saved
    ('CCTNS CMMI Data_10Feb2026'). Reading both at once merged the two and
    produced periods like 'Cycle 2 (Jan-Feb 2026)', so the folder wins whenever
    it states a period and the file name is only the fallback.
    """
    if folder_hint:
        from_folder = periods.suggest(folder_hint)
        if from_folder["label"]:
            return from_folder
    return periods.suggest(filename)


# --------------------------------------------------------------------------
# Read helpers for the pages
# --------------------------------------------------------------------------
def dashboard_counts(session):
    return {
        "uploads": session.scalar(select(func.count(UploadBatch.id))) or 0,
        "dev_rows": session.scalar(select(func.count(DevelopmentRecord.id))) or 0,
        "svc_rows": session.scalar(select(func.count(ServicesRecord.id))) or 0,
        "periods": session.scalar(select(func.count(ReportingPeriod.id))) or 0,
    }


def categories_with_projects(session):
    return list(session.scalars(select(Category).order_by(Category.id)))


def project_summary(session):
    """Per project: upload count, row count, and the latest period loaded."""
    rows = session.execute(
        select(
            Project.id, Project.name, Category.name, Category.code,
            func.count(func.distinct(UploadBatch.id)),
            func.coalesce(func.sum(UploadBatch.row_count), 0),
            func.max(UploadBatch.uploaded_at),
        )
        .select_from(Project)
        .join(Category, Category.id == Project.category_id)
        .outerjoin(UploadBatch, UploadBatch.project_id == Project.id)
        .group_by(Project.id, Project.name, Category.name, Category.code)
        .order_by(Category.id, Project.name)
    ).all()
    return [
        {"id": r[0], "project": r[1], "category": r[2], "category_code": r[3],
         "uploads": r[4], "rows": int(r[5] or 0), "last_upload": r[6]}
        for r in rows
    ]
