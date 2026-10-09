"""FastAPI application: upload data collection sheets and browse what was stored."""
import csv
import datetime as dt
import io
import os
import re
from pathlib import Path
from typing import List, Optional

from fastapi import Depends, FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import case, delete, func, or_, select
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from app import activity, auth, config, metrics_catalog as mc, service
from app.db import Base, SessionLocal, create_database_if_missing, engine, get_session
from app.ingest import DuplicateUpload, IngestError
from app.models import (
    RECORD_MODELS, Category, OrganogramEntry, OrgPerson, Project, ReportingPeriod,
    SheetTab, UploadBatch, User,
)
from app.seed import seed_all, seed_first_admin

app = FastAPI(title="CMMI Data Collection Repository")

BASE_DIR = config.BASE_DIR

#: reachable without signing in
OPEN_PATHS = ("/login", "/static", "/favicon.ico")
#: reachable while a password change is outstanding
PASSWORD_PATHS = ("/change-password", "/logout")
templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "app" / "static")), name="static")

PREVIEW_ROWS = 25
BROWSE_ROWS = 300


@app.on_event("startup")
def startup():
    create_database_if_missing()
    Base.metadata.create_all(engine)
    session = SessionLocal()
    try:
        seed_all(session)
        created = seed_first_admin(session)
        if created:
            print("\n  Created the first administrator:")
            print("    login id: %s" % created[0])
            print("    password: %s" % created[1])
            print("  You will be asked to change it at first sign-in.\n")
    finally:
        session.close()
    config.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


@app.middleware("http")
async def require_sign_in(request: Request, call_next):
    """
    Everything needs a signed-in person except the sign-in page itself, and an
    outstanding password change blocks the rest of the site until it is done.
    """
    path = request.url.path
    if path.startswith(OPEN_PATHS):
        return await call_next(request)
    if not request.session.get("user_id"):
        return auth.redirect_to_login(request)
    if request.session.get("must_change_password") and not path.startswith(PASSWORD_PATHS):
        return RedirectResponse("/change-password", status_code=303)
    return await call_next(request)


# Registered after require_sign_in so that it wraps it: Starlette runs the
# most recently added middleware outermost, and the guard reads request.session.
app.add_middleware(SessionMiddleware, secret_key=auth.session_secret(),
                   session_cookie="cmmi_session", max_age=8 * 60 * 60,
                   same_site="lax", https_only=False)


def current_user(request: Request, db: Session = Depends(get_session)):
    """The signed-in person, loaded in the request's own session."""
    return auth.load_user(request, db)


def render(request, name, **context):
    """
    Render a page with the signed-in person always in context.

    Some pages -- Browse data, Columns -- do not need the user for their own
    work, so they never loaded one, and the header lost the name, Change
    password and Sign out links. Loading here rather than in each route means
    the header stays put on every page, including ones added later.
    """
    context.setdefault("request", request)
    user = getattr(request.state, "user", None)
    if user is None and request.session.get("user_id"):
        session = SessionLocal()
        try:
            user = auth.load_user(request, session)
        finally:
            session.close()
    context.setdefault("user", user)
    context.setdefault("auth", auth)
    return templates.TemplateResponse(name, context)


def deny(request, message):
    return render(request, "denied.html", message=message)


#: the stage each role signs off, used for their queue
QUEUE_STAGE = {auth.VERTICAL_HEAD: auth.PENDING_VERTICAL,
               auth.ADMIN: auth.PENDING_ADMIN,
               auth.CTO: auth.PENDING_CTO}


def pending_for(db, user):
    """How many sheets are waiting on this person's own approval."""
    stage = QUEUE_STAGE.get(user.role) if user else None
    if stage is None:
        return 0
    query = auth.scope_uploads(
        select(func.count(UploadBatch.id)).where(UploadBatch.stage == stage),
        UploadBatch, user)
    return db.scalar(query) or 0


def as_id(value):
    """
    Filter dropdowns submit an empty string for 'All', so these query parameters
    arrive as text and are converted here rather than being declared as ints,
    which would reject '' outright.
    """
    if value is None:
        return None
    value = str(value).strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def show(value):
    """One cell as the tables should display it."""
    if value is None:
        return ""
    if isinstance(value, (dt.date, dt.datetime)):
        return value.strftime("%d %b %Y")
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else ("%.2f" % value)
    return str(value)


def used_columns(db, model, family, **filters):
    """
    Only show columns that actually hold data for the current selection -- a
    43-column table is unreadable when half of it is empty.
    """
    query = select(*[func.count(getattr(model, c)) for c in mc.COLUMNS[family]])
    for field, value in filters.items():
        if value is not None:
            query = query.where(getattr(model, field) == value)
    counts = db.execute(query).one()
    return [c for c, n in zip(mc.COLUMNS[family], counts) if n]


# --------------------------------------------------------------------------
# Signing in
# --------------------------------------------------------------------------
@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: Optional[str] = None):
    if request.session.get("user_id"):
        return RedirectResponse(next or "/", status_code=303)
    return render(request, "login.html", error=None, next=next or "", username="")


@app.post("/login", response_class=HTMLResponse)
def login_submit(request: Request, username: str = Form(...), password: str = Form(...),
                 next: Optional[str] = Form(None), db: Session = Depends(get_session)):
    account = db.scalar(select(User).where(User.username == username.strip().lower()))
    if account is None or not auth.verify_password(password, account.password_hash):
        # the same message either way, so the page cannot be used to find valid login ids
        return render(request, "login.html", error="Login id or password is not correct.",
                      next=next or "", username=username)
    if not account.is_active:
        return render(request, "login.html", error="This account has been disabled.",
                      next=next or "", username=username)

    request.session.clear()
    request.session["user_id"] = account.id
    request.session["must_change_password"] = bool(account.must_change_password)
    account.last_login_at = dt.datetime.now()
    db.commit()

    if account.must_change_password:
        return RedirectResponse("/change-password", status_code=303)
    target = next or "/"
    return RedirectResponse(target if target.startswith("/") else "/", status_code=303)


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.get("/change-password", response_class=HTMLResponse)
def change_password_form(request: Request, db: Session = Depends(get_session)):
    user = auth.load_user(request, db)
    return render(request, "change_password.html", error=None, done=False,
                  forced=bool(request.session.get("must_change_password")), user=user)


@app.post("/change-password", response_class=HTMLResponse)
def change_password_submit(request: Request, current_password: str = Form(...),
                           new_password: str = Form(...), confirm_password: str = Form(...),
                           db: Session = Depends(get_session)):
    user = auth.load_user(request, db)
    forced = bool(request.session.get("must_change_password"))

    def fail(message):
        return render(request, "change_password.html", error=message, done=False,
                      forced=forced, user=user)

    if not auth.verify_password(current_password, user.password_hash):
        return fail("Your current password is not correct.")
    problem = auth.password_problem(new_password, confirm_password)
    if problem:
        return fail(problem)
    if auth.verify_password(new_password, user.password_hash):
        return fail("Choose a password different from the current one.")

    user.password_hash = auth.hash_password(new_password)
    user.must_change_password = False
    user.password_changed_at = dt.datetime.now()
    db.commit()
    request.session["must_change_password"] = False
    return render(request, "change_password.html", error=None, done=True, forced=False, user=user)


# --------------------------------------------------------------------------
# Managing people (administrators only)
# --------------------------------------------------------------------------
@app.get("/users", response_class=HTMLResponse)
def users_page(request: Request, db: Session = Depends(get_session)):
    user = auth.load_user(request, db)
    if not auth.can_manage_users(user):
        return deny(request, "Only an administrator can manage sign-ins.")
    return render(request, "users.html",
                  people=list(db.scalars(select(User).order_by(User.role, User.full_name))),
                  categories=service.categories_with_projects(db),
                  new_account=None, error=None)


@app.post("/users", response_class=HTMLResponse)
def users_create(request: Request, username: str = Form(...), full_name: str = Form(...),
                 role: str = Form(...), email: Optional[str] = Form(None),
                 designation: Optional[str] = Form(None),
                 project_ids: Optional[List[int]] = Form(None),
                 db: Session = Depends(get_session)):
    user = auth.load_user(request, db)
    if not auth.can_manage_users(user):
        return deny(request, "Only an administrator can manage sign-ins.")

    def back(error=None, new_account=None):
        return render(request, "users.html",
                      people=list(db.scalars(select(User).order_by(User.role, User.full_name))),
                      categories=service.categories_with_projects(db),
                      new_account=new_account, error=error)

    login_id = (username or "").strip().lower()
    if not login_id or not (full_name or "").strip():
        return back("A login id and a name are both needed.")
    if role not in auth.ROLES:
        return back("Pick a valid role.")
    if db.scalar(select(User).where(User.username == login_id)):
        return back("The login id '%s' is already taken." % login_id)

    password = auth.temporary_password()
    account = User(
        username=login_id, full_name=full_name.strip(),
        email=(email or "").strip() or None,
        designation=(designation or "").strip() or None,
        role=role, password_hash=auth.hash_password(password),
        must_change_password=True, created_by_id=user.id,
    )
    if role == auth.VERTICAL_HEAD and project_ids:
        account.projects = list(db.scalars(select(Project).where(Project.id.in_(project_ids))))
    db.add(account)
    db.commit()
    return back(new_account={"username": login_id, "password": password,
                             "full_name": account.full_name})


@app.post("/users/{user_id}/projects")
def users_set_projects(user_id: int, request: Request,
                       project_ids: Optional[List[int]] = Form(None),
                       db: Session = Depends(get_session)):
    user = auth.load_user(request, db)
    if not auth.can_manage_users(user):
        return deny(request, "Only an administrator can manage sign-ins.")
    account = db.get(User, user_id)
    if account is not None:
        account.projects = (list(db.scalars(select(Project).where(Project.id.in_(project_ids))))
                            if project_ids else [])
        db.commit()
    return RedirectResponse("/users", status_code=303)


@app.post("/users/{user_id}/active")
def users_toggle_active(user_id: int, request: Request, db: Session = Depends(get_session)):
    user = auth.load_user(request, db)
    if not auth.can_manage_users(user):
        return deny(request, "Only an administrator can manage sign-ins.")
    account = db.get(User, user_id)
    if account is not None and account.id != user.id:      # never lock yourself out
        account.is_active = not account.is_active
        db.commit()
    return RedirectResponse("/users", status_code=303)


@app.post("/users/{user_id}/reset-password", response_class=HTMLResponse)
def users_reset_password(user_id: int, request: Request, db: Session = Depends(get_session)):
    user = auth.load_user(request, db)
    if not auth.can_manage_users(user):
        return deny(request, "Only an administrator can manage sign-ins.")
    account = db.get(User, user_id)
    new_account = None
    if account is not None:
        password = auth.temporary_password()
        account.password_hash = auth.hash_password(password)
        account.must_change_password = True
        db.commit()
        new_account = {"username": account.username, "password": password,
                       "full_name": account.full_name, "reset": True}
    return render(request, "users.html",
                  people=list(db.scalars(select(User).order_by(User.role, User.full_name))),
                  categories=service.categories_with_projects(db),
                  new_account=new_account, error=None)


# --------------------------------------------------------------------------
# Dashboard
# --------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_session)):
    user = auth.load_user(request, db)
    query = auth.scope_uploads(
        select(UploadBatch).order_by(UploadBatch.uploaded_at.desc()), UploadBatch, user)
    return render(request, "dashboard.html",
                  counts=service.dashboard_counts(db),
                  summary=service.project_summary(db),
                  pending_count=pending_for(db, user),
                  rejected_count=db.scalar(auth.scope_uploads(
                      select(func.count(UploadBatch.id))
                      .where(UploadBatch.stage == auth.REJECTED),
                      UploadBatch, user)) or 0,
                  recent=list(db.scalars(query.limit(10))))


# --------------------------------------------------------------------------
# Upload
# --------------------------------------------------------------------------
@app.get("/upload", response_class=HTMLResponse)
def upload_form(request: Request, db: Session = Depends(get_session)):
    user = auth.load_user(request, db)
    known = list(db.scalars(select(ReportingPeriod).order_by(ReportingPeriod.start_date.desc())))
    return render(request, "upload.html",
                  categories=service.categories_with_projects(db),
                  allowed_ids={p.id for p in auth.visible_projects(db, user)},
                  known_periods=known, error=None)


@app.post("/upload", response_class=HTMLResponse)
def upload_submit(
    request: Request,
    project_id: int = Form(...),
    period_label: str = Form(...),
    period_start: Optional[str] = Form(None),
    period_end: Optional[str] = Form(None),
    source_folder: Optional[str] = Form(None),
    notes: Optional[str] = Form(None),
    allow_duplicate: Optional[str] = Form(None),
    sheet: UploadFile = File(...),
    db: Session = Depends(get_session),
):
    user = auth.load_user(request, db)

    def fail(message):
        known = list(db.scalars(select(ReportingPeriod).order_by(ReportingPeriod.start_date.desc())))
        return render(request, "upload.html",
                      categories=service.categories_with_projects(db),
                      allowed_ids={p.id for p in auth.visible_projects(db, user)},
                      known_periods=known, error=message)

    project = db.get(Project, project_id)
    if project is None:
        return fail("Pick a project.")
    if project.id not in {p.id for p in auth.visible_projects(db, user)}:
        return fail("You are not assigned to that project.")

    extension = os.path.splitext(sheet.filename or "")[1].lower()
    if extension not in config.ALLOWED_EXTENSIONS:
        return fail("Only %s files can be uploaded." % ", ".join(sorted(config.ALLOWED_EXTENSIONS)))

    label = (period_label or "").strip()
    if not label:
        return fail("Enter the reporting period this sheet covers.")

    def as_date(value):
        try:
            return dt.datetime.strptime(value, "%Y-%m-%d").date() if value else None
        except ValueError:
            return None

    try:
        period = service.get_or_create_period(db, label, as_date(period_start), as_date(period_end))
        stored = service.store_file(sheet.filename, sheet.file, project.code, period.label)
    except ValueError as exc:
        return fail(str(exc))

    size_mb = os.path.getsize(stored) / (1024.0 * 1024.0)
    if size_mb > config.MAX_UPLOAD_MB:
        os.remove(stored)
        return fail("File is %.1f MB; the limit is %d MB." % (size_mb, config.MAX_UPLOAD_MB))

    try:
        upload, _sheets = service.create_upload(
            db, project, period, sheet.filename, stored,
            uploaded_by=user.full_name, uploaded_by_id=user.id,
            notes=notes, source_folder=source_folder,
            allow_duplicate=bool(allow_duplicate),
        )
    except DuplicateUpload as exc:
        db.rollback()
        os.remove(stored)
        return fail("%s Tick 'load anyway' to store it a second time." % exc)
    except IngestError as exc:
        db.rollback()
        return fail("Could not read the workbook: %s" % exc)
    except Exception as exc:                      # noqa: BLE001 - surfaced to the user
        db.rollback()
        return fail("Could not read the workbook: %s" % exc)

    return RedirectResponse("/uploads/%d?new=1" % upload.id, status_code=303)


# --------------------------------------------------------------------------
# Uploads
# --------------------------------------------------------------------------
@app.get("/uploads", response_class=HTMLResponse)
def uploads_list(request: Request, category: Optional[str] = None,
                 project: Optional[str] = None, period: Optional[str] = None,
                 review: Optional[str] = None, mine: Optional[str] = None,
                 db: Session = Depends(get_session)):
    user = auth.load_user(request, db)
    category, project, period = as_id(category), as_id(project), as_id(period)
    review = (review or "").strip() or None

    # what needs attention comes first: your own queue, then anything turned
    # down (which somebody has to correct), then the rest by date
    my_stage = QUEUE_STAGE.get(user.role)
    priority = case(
        (UploadBatch.stage == (my_stage or "~none~"), 0),
        (UploadBatch.stage == auth.REJECTED, 1),
        else_=2,
    )
    ordering = [priority, UploadBatch.uploaded_at.desc()]

    query = auth.scope_uploads(select(UploadBatch).order_by(*ordering), UploadBatch, user)
    if (mine or "").strip():
        query = query.where(UploadBatch.uploaded_by_id == user.id)
    if category:
        query = query.where(UploadBatch.category_id == category)
    if project:
        query = query.where(UploadBatch.project_id == project)
    if period:
        query = query.where(UploadBatch.period_id == period)
    if review:
        query = query.where(UploadBatch.stage == review)

    rows = list(db.scalars(query.limit(300)))
    return render(request, "uploads.html",
                  uploads=rows,
                  approvable={u.id for u in rows if auth.can_approve(user, u)},
                  categories=service.categories_with_projects(db),
                  periods=list(db.scalars(select(ReportingPeriod).order_by(ReportingPeriod.start_date.desc()))),
                  stages=auth.STAGE_LABELS,
                  pending_count=pending_for(db, user),
                  my_stage=my_stage,
                  my_stage_label=auth.STAGE_LABELS.get(my_stage, ""),
                  rejected_count=db.scalar(auth.scope_uploads(
                      select(func.count(UploadBatch.id))
                      .where(UploadBatch.stage == auth.REJECTED),
                      UploadBatch, user)) or 0,
                  selected={"category": category, "project": project,
                            "period": period, "review": review,
                            "mine": (mine or "").strip()})


def build_sheet_previews(db, upload, limit=PREVIEW_ROWS):
    """
    Per worksheet: the columns that were filled and the first rows as stored.
    Built from the database, not the file, so the reviewer checks what landed.
    """
    previews = []
    for tab in db.scalars(
        select(SheetTab).where(SheetTab.upload_id == upload.id).order_by(SheetTab.sheet_index)
    ):
        entry = {"tab": tab, "columns": [], "labels": [], "rows": [],
                 "shown": 0, "extras": [], "cells": [], "units": []}
        if tab.was_parsed and tab.sheet_kind in RECORD_MODELS:
            family = tab.sheet_kind
            model = RECORD_MODELS[family]
            columns = used_columns(db, model, family, sheet_id=tab.id)
            entry["columns"] = columns
            entry["labels"] = [mc.META[family][c]["label"] for c in columns]
            entry["units"] = [mc.META[family][c]["unit"] for c in columns]
            cells = tab.column_map or {}
            entry["cells"] = [cells.get(c, "") for c in columns]

            records = list(db.scalars(
                select(model).where(model.sheet_id == tab.id)
                .order_by(model.row_index).limit(limit)
            ))
            seen_extras = set()
            for record in records:
                entry["rows"].append({
                    "row_index": record.row_index,
                    "cells": [show(getattr(record, c)) for c in columns],
                })
                for key in (record.extra_columns or {}):
                    seen_extras.add(key)
            entry["extras"] = sorted(seen_extras)
            entry["shown"] = len(records)
        previews.append(entry)
    return previews


@app.get("/uploads/{upload_id}", response_class=HTMLResponse)
def upload_detail(upload_id: int, request: Request, new: Optional[str] = None,
                  db: Session = Depends(get_session)):
    user = auth.load_user(request, db)
    upload = db.get(UploadBatch, upload_id)
    if upload is None:
        return render(request, "missing.html", what="upload #%d" % upload_id)
    if not auth.may_see_upload(user, upload):
        return deny(request, "That sheet is not one you have access to.")
    return render(request, "upload_detail.html", upload=upload,
                  previews=build_sheet_previews(db, upload),
                  may_approve=auth.can_approve(user, upload),
                  may_delete=auth.can_delete(user),
                  is_new=bool((new or "").strip()))


@app.post("/uploads/{upload_id}/approve")
def upload_approve(upload_id: int, request: Request, note: Optional[str] = Form(None),
                   db: Session = Depends(get_session)):
    """
    Sign off the stage this sheet is waiting at and move it to the next one:
    vertical head -> admin -> CTO. The CTO's approval makes it count as data.
    """
    user = auth.load_user(request, db)
    upload = db.get(UploadBatch, upload_id)
    if upload is None:
        return render(request, "missing.html", what="upload #%d" % upload_id)
    if not auth.can_approve(user, upload):
        return deny(request, "You cannot approve this sheet at its current stage.")

    now = dt.datetime.now()
    comment = (note or "").strip() or None

    # record the sign-off against the stage being approved, then advance
    field = {auth.PENDING_VERTICAL: "vertical", auth.PENDING_ADMIN: "admin",
             auth.PENDING_CTO: "cto"}[upload.stage]
    setattr(upload, "%s_approved_by_id" % field, user.id)
    setattr(upload, "%s_approved_at" % field, now)
    setattr(upload, "%s_note" % field, comment)
    upload.stage = auth.NEXT_STAGE[upload.stage]

    if upload.stage == auth.APPROVED:
        # a fully approved sheet replaces whatever was approved before it
        for prior in db.scalars(
            select(UploadBatch).where(
                UploadBatch.project_id == upload.project_id,
                UploadBatch.period_id == upload.period_id,
                UploadBatch.id != upload.id,
                UploadBatch.is_superseded.is_(False),
            )
        ):
            prior.is_superseded = True
    db.commit()
    return RedirectResponse("/uploads/%d" % upload_id, status_code=303)


@app.post("/uploads/{upload_id}/reject")
def upload_reject(upload_id: int, request: Request, note: Optional[str] = Form(None),
                  db: Session = Depends(get_session)):
    """
    Turn a sheet down. The rows stay in place but the sheet never counts as
    data, and the reason is kept so the uploader knows what to fix.
    """
    user = auth.load_user(request, db)
    upload = db.get(UploadBatch, upload_id)
    if upload is None:
        return render(request, "missing.html", what="upload #%d" % upload_id)
    if not auth.can_approve(user, upload):
        return deny(request, "You cannot reject this sheet at its current stage.")
    reason = (note or "").strip()
    if not reason:
        return deny(request, "Give a reason when turning a sheet down, so it can be corrected.")

    upload.rejected_by_id = user.id
    upload.rejected_at = dt.datetime.now()
    upload.rejected_stage = upload.stage
    upload.reject_reason = reason
    upload.stage = auth.REJECTED
    upload.is_superseded = True
    db.commit()
    return RedirectResponse("/uploads/%d" % upload_id, status_code=303)


@app.post("/uploads/{upload_id}/delete")
def upload_delete(upload_id: int, request: Request, db: Session = Depends(get_session)):
    """Remove an upload and its rows. Administrators only."""
    user = auth.load_user(request, db)
    if not auth.can_delete(user):
        return deny(request, "Only an administrator can delete an upload.")
    upload = db.get(UploadBatch, upload_id)
    if upload is None:
        return render(request, "missing.html", what="upload #%d" % upload_id)
    for model in RECORD_MODELS.values():
        db.execute(delete(model).where(model.upload_id == upload_id))
    stored = Path(upload.stored_path)
    db.delete(upload)
    db.commit()
    try:
        if stored.is_file():
            stored.unlink()
    except OSError:
        pass
    return RedirectResponse("/uploads", status_code=303)


# --------------------------------------------------------------------------
# Browse the stored data
# --------------------------------------------------------------------------
def _record_query(db, model, project, period, include_unreviewed):
    query = select(model)
    if project:
        query = query.where(model.project_id == project)
    if period:
        query = query.where(model.period_id == period)
    if not include_unreviewed:
        query = query.join(UploadBatch, UploadBatch.id == model.upload_id).where(
            UploadBatch.stage == auth.APPROVED
        )
    return query


@app.get("/data", response_class=HTMLResponse)
def browse(request: Request, family: Optional[str] = None, project: Optional[str] = None,
           period: Optional[str] = None, unreviewed: Optional[str] = None,
           db: Session = Depends(get_session)):
    family = (family or "").strip() or "development"
    if family not in RECORD_MODELS:
        family = "development"
    project, period = as_id(project), as_id(period)
    include = bool((unreviewed or "").strip())
    model = RECORD_MODELS[family]

    query = _record_query(db, model, project, period, include)
    records = list(db.scalars(query.order_by(model.id).limit(BROWSE_ROWS)))
    total = db.scalar(select(func.count()).select_from(query.subquery()))

    filters = {}
    if project:
        filters["project_id"] = project
    if period:
        filters["period_id"] = period
    columns = used_columns(db, model, family, **filters) if total else []

    rows = [{
        "upload_name": r.upload_name, "period_label": r.period_label,
        "row_index": r.row_index,
        "cells": [show(getattr(r, c)) for c in columns],
    } for r in records]

    return render(request, "browse.html", family=family, rows=rows, total=total,
                  columns=columns,
                  labels=[mc.META[family][c]["label"] for c in columns],
                  units=[mc.META[family][c]["unit"] for c in columns],
                  key_column=mc.KEY_COLUMN[family],
                  include_unreviewed=include,
                  categories=service.categories_with_projects(db),
                  periods=list(db.scalars(select(ReportingPeriod).order_by(ReportingPeriod.start_date.desc()))),
                  selected={"project": project, "period": period})


@app.get("/export/records.csv")
def export_csv(family: Optional[str] = None, project: Optional[str] = None,
               period: Optional[str] = None, unreviewed: Optional[str] = None,
               db: Session = Depends(get_session)):
    """The table as a flat CSV -- one row per sheet row, ready for modelling."""
    family = (family or "").strip() or "development"
    if family not in RECORD_MODELS:
        family = "development"
    model = RECORD_MODELS[family]
    query = _record_query(db, model, as_id(project), as_id(period),
                          bool((unreviewed or "").strip()))
    records = list(db.scalars(query.order_by(model.id)))

    lead = ["upload_name", "project", "period_label", "sheet_name", "row_index"]
    columns = mc.COLUMNS[family]

    return _csv_response(
        lead + columns,
        [[record.upload_name, record.project.name, record.period_label,
          record.sheet_name, record.row_index]
         + [getattr(record, c) if getattr(record, c) is not None else "" for c in columns]
         for record in records],
        "cmmi_%s.csv" % family)


@app.get("/columns", response_class=HTMLResponse)
def columns_page(request: Request, db: Session = Depends(get_session)):
    """Reference: the fixed columns each sheet is mapped onto."""
    families = {}
    for family, model in RECORD_MODELS.items():
        filled = set(used_columns(db, model, family))
        families[family] = [
            {"column": c, "filled": c in filled, **mc.META[family][c]}
            for c in mc.COLUMNS[family]
        ]
    return render(request, "columns.html", families=families,
                  ignored=sorted(mc.IGNORED_HEADERS), key_column=mc.KEY_COLUMN)


# --------------------------------------------------------------------------
# Who did what
# --------------------------------------------------------------------------
ACTIVITY_ROWS = 400


def _activity_filters(project, person, action, date_from, date_to):
    """The five query parameters, cleaned, as collect() wants them."""
    return {
        "project": as_id(project),
        "actor": as_id(person),
        "action": (action or "").strip() or None,
        "date_from": activity.as_date(date_from),
        "date_to": activity.as_date(date_to),
    }


@app.get("/activity", response_class=HTMLResponse)
def activity_page(request: Request, project: Optional[str] = None,
                  person: Optional[str] = None, action: Optional[str] = None,
                  date_from: Optional[str] = None, date_to: Optional[str] = None,
                  db: Session = Depends(get_session)):
    """Every dated action -- uploads, the three approvals, rejections, sign-ins."""
    user = auth.load_user(request, db)
    if not auth.can_see_activity(user):
        return deny(request, "Only an administrator or the CTO can see the activity trail.")

    chosen = _activity_filters(project, person, action, date_from, date_to)
    events = activity.collect(db, user, **chosen)
    return render(request, "activity.html",
                  days=activity.by_day(events[:ACTIVITY_ROWS]),
                  tally=activity.tally(events),
                  total=len(events),
                  shown_limit=ACTIVITY_ROWS,
                  today=dt.date.today(),
                  week_ago=dt.date.today() - dt.timedelta(days=7),
                  month_ago=dt.date.today() - dt.timedelta(days=30),
                  categories=service.categories_with_projects(db),
                  people=list(db.scalars(select(User).order_by(User.full_name))),
                  sheet_actions=activity.SHEET_ACTIONS,
                  account_actions=activity.ACCOUNT_ACTIONS,
                  action_labels=activity.ACTION_LABELS,
                  pending_count=pending_for(db, user),
                  selected={"project": chosen["project"], "person": chosen["actor"],
                            "action": chosen["action"],
                            "date_from": (date_from or "").strip(),
                            "date_to": (date_to or "").strip()})


@app.get("/activity.csv")
def activity_csv(request: Request, project: Optional[str] = None,
                 person: Optional[str] = None, action: Optional[str] = None,
                 date_from: Optional[str] = None, date_to: Optional[str] = None,
                 db: Session = Depends(get_session)):
    """The same trail as a file -- every matching row, not just the page's first few hundred."""
    user = auth.load_user(request, db)
    if not auth.can_see_activity(user):
        return deny(request, "Only an administrator or the CTO can see the activity trail.")

    events = activity.collect(db, user, **_activity_filters(project, person, action, date_from, date_to))
    rows = []
    for e in events:
        upload = e["upload"]
        rows.append([
            e["at"].strftime("%Y-%m-%d %H:%M") if e["at"] else "",
            e["actor_name"],
            auth.ROLE_LABELS.get(e["actor_role"], ""),
            e["action_label"],
            upload.project.name if upload else "",
            upload.category.name if upload else "",
            upload.period.label if upload else "",
            e["subject"] or "",
            upload.row_count if upload else "",
            e["note"] or "",
        ])
    return _csv_response(
        ["when", "who", "role", "action", "project", "track", "period",
         "file_or_account", "rows", "note"],
        rows, "cmmi_activity.csv")


# --------------------------------------------------------------------------
# Downloads
# --------------------------------------------------------------------------
def _csv_response(header, rows, filename):
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(header)
    writer.writerows(rows)
    buffer.seek(0)
    return StreamingResponse(
        iter([buffer.getvalue()]), media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="%s"' % filename},
    )


@app.get("/uploads/{upload_id}/file")
def upload_original(upload_id: int, request: Request, db: Session = Depends(get_session)):
    """The workbook exactly as it was uploaded."""
    user = auth.load_user(request, db)
    upload = db.get(UploadBatch, upload_id)
    if upload is None:
        return render(request, "missing.html", what="upload #%d" % upload_id)
    if not auth.may_see_upload(user, upload):
        return deny(request, "That sheet is not one you have access to.")
    path = Path(upload.stored_path)
    if not path.is_file():
        return render(request, "missing.html",
                      what="stored copy of %r" % upload.original_filename)
    return FileResponse(path, filename=upload.original_filename)






# --------------------------------------------------------------------------
# Vertical organogram
# --------------------------------------------------------------------------
#: shown inline in the browser rather than downloaded
INLINE_TYPES = {".pdf": "application/pdf", ".png": "image/png",
                ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}


def organogram_files():
    """The source documents, newest name first, with a note of what each is."""
    if not config.ORGANOGRAM_DIR.is_dir():
        return []
    out = []
    for path in sorted(config.ORGANOGRAM_DIR.iterdir()):
        if not path.is_file() or path.name.startswith("."):
            continue
        out.append({
            "name": path.name,
            "size_kb": path.stat().st_size / 1024.0,
            "kind": path.suffix.lower().lstrip("."),
            "viewable": path.suffix.lower() in INLINE_TYPES,
        })
    return out


@app.get("/organogram", response_class=HTMLResponse)
def organogram_page(request: Request, db: Session = Depends(get_session)):
    entries = list(db.scalars(
        select(OrganogramEntry).order_by(OrganogramEntry.domain,
                                         OrganogramEntry.vertical_division)
    ))
    # vertical head -> the projects they cover, which drives approvals
    heads = {}
    for entry in entries:
        if not entry.vertical_head:
            continue
        heads.setdefault(entry.vertical_head, []).append(entry)

    existing = {u.full_name.strip().lower(): u
                for u in db.scalars(select(User).where(User.role == auth.VERTICAL_HEAD))}
    for name, rows in heads.items():
        account = existing.get(name.strip().lower())
        heads[name] = {
            "rows": rows,
            "projects": [r.project for r in rows if r.project],
            "account": account,
            "assigned": bool(account and account.projects),
        }
    return render(request, "organogram.html", entries=entries, heads=heads,
                  files=organogram_files(),
                  may_manage=auth.can_manage_users(auth.load_user(request, db)))



VERTICAL_LABELS = {"technology": "Technology", "banking": "Banking",
                   "fisheries": "Fisheries", "insurance": "Insurance"}

#: which organogram document each vertical was read from
VERTICAL_SOURCE = {
    "technology": "Organogram_CSCSPV_Technology.pdf",
    "banking": "PSB_Banking_Vertical_Organogram.xlsx",
    "fisheries": "Vertical_Benchmark_15062026.xlsx",
    "insurance": "Insurance_Resource_KRA_Organogram_FY2026-27.xlsx",
}


@app.get("/hierarchy", response_class=HTMLResponse)
def hierarchy_page(request: Request, vertical: Optional[str] = None,
                   db: Session = Depends(get_session)):
    """Who reports to whom, per vertical."""
    available = [v for (v,) in db.execute(
        select(OrgPerson.vertical).distinct().order_by(OrgPerson.vertical)).all()]
    chosen = (vertical or "").strip().lower()
    if chosen not in available:
        chosen = "technology" if "technology" in available else (available[0] if available else "")

    people = list(db.scalars(
        select(OrgPerson).where(OrgPerson.vertical == chosen)
        .order_by(OrgPerson.sort_order, OrgPerson.id)))

    children = {}
    for person in people:
        children.setdefault(person.reports_to_id, []).append(person)
    known = {p.id for p in people}
    # anyone whose manager sits outside this vertical shows at the top
    roots = [p for p in people if p.reports_to_id is None or p.reports_to_id not in known]

    # Roll each branch up, so a box that heads an organisation says how many
    # people sit beneath it in total rather than only its direct reports.
    rollup = {}

    def roll(person):
        """
        (in post, to be hired, sanctioned) for this box and everything under it.

        A headcount on the chart counts the lead as well -- 'Harsh Mishra (5)'
        is Harsh plus four others -- so the person themselves is included.
        """
        in_post = 1 if (person.name and person.counted) else 0
        to_hire = person.to_be_hired or 0
        beneath = 0
        for child in children.get(person.id, []):
            child_post, child_hire, child_sanctioned = roll(child)
            in_post += child_post
            to_hire += child_hire
            beneath += child_sanctioned
        sanctioned = person.headcount or max(
            beneath + (1 if person.name and person.counted else 0), in_post + to_hire)
        rollup[person.id] = (in_post, to_hire, sanctioned)
        return in_post, to_hire, sanctioned

    for root in roots:
        roll(root)

    totals = [sum(c) for c in zip(*[rollup[r.id] for r in roots])] if roots else [0, 0, 0]

    return render(request, "hierarchy.html",
                  rollup=rollup, totals=totals,
                  verticals=[(v, VERTICAL_LABELS.get(v, v.title())) for v in available],
                  chosen=chosen, label=VERTICAL_LABELS.get(chosen, chosen.title()),
                  roots=roots, children=children, total=len(people),
                  inferred_count=sum(1 for p in people if p.inferred),
                  source=VERTICAL_SOURCE.get(chosen))


@app.get("/organogram/file/{name}")
def organogram_file(name: str, request: Request):
    """Serve a source document. PDFs open in the browser, the rest download."""
    safe = Path(name).name                       # never escape the folder
    path = config.ORGANOGRAM_DIR / safe
    if not path.is_file():
        return render(request, "missing.html", what="document %r" % safe)
    suffix = path.suffix.lower()
    if suffix in INLINE_TYPES:
        return FileResponse(path, media_type=INLINE_TYPES[suffix],
                            headers={"Content-Disposition": 'inline; filename="%s"' % safe})
    return FileResponse(path, filename=safe)


@app.post("/organogram/create-heads", response_class=HTMLResponse)
def organogram_create_heads(request: Request, db: Session = Depends(get_session)):
    """
    Turn the organogram into sign-ins: for every vertical head named, create an
    account if there is not one already and assign the projects they cover.
    """
    user = auth.load_user(request, db)
    if not auth.can_manage_users(user):
        return deny(request, "Only an administrator can create sign-ins.")

    by_head = {}
    for entry in db.scalars(select(OrganogramEntry)):
        if entry.vertical_head and entry.project_id:
            by_head.setdefault(entry.vertical_head.strip(), set()).add(entry.project_id)

    existing = {u.full_name.strip().lower(): u for u in db.scalars(select(User))}
    taken = {u.username for u in db.scalars(select(User))}
    created = []

    for name, project_ids in sorted(by_head.items()):
        account = existing.get(name.lower())
        if account is None:
            base = re.sub(r"[^a-z0-9]+", ".", name.lower()).strip(".")[:50] or "head"
            login_id, n = base, 2
            while login_id in taken:
                login_id, n = "%s%d" % (base, n), n + 1
            taken.add(login_id)
            password = auth.temporary_password()
            account = User(username=login_id, full_name=name, role=auth.VERTICAL_HEAD,
                           designation="Vertical Head",
                           password_hash=auth.hash_password(password),
                           must_change_password=True, created_by_id=user.id)
            db.add(account)
            created.append({"username": login_id, "password": password, "full_name": name})
        account.projects = list(db.scalars(select(Project).where(Project.id.in_(project_ids))))
    db.commit()

    return render(request, "organogram.html",
                  entries=list(db.scalars(select(OrganogramEntry)
                                          .order_by(OrganogramEntry.domain,
                                                    OrganogramEntry.vertical_division))),
                  heads={}, files=organogram_files(), may_manage=True,
                  created=created, recheck=True)


# --------------------------------------------------------------------------
# Small JSON helpers used by the upload form
# --------------------------------------------------------------------------
@app.get("/api/projects")
def api_projects(category: int, db: Session = Depends(get_session)):
    projects = db.scalars(
        select(Project).where(Project.category_id == category).order_by(Project.name)
    )
    return [{"id": p.id, "name": p.name} for p in projects]


@app.get("/api/suggest-period")
def api_suggest_period(filename: str, folder: Optional[str] = None):
    parsed = service.suggest_period_label(filename, folder)
    return {
        "label": parsed["label"],
        "start_date": parsed["start_date"].isoformat() if parsed["start_date"] else "",
        "end_date": parsed["end_date"].isoformat() if parsed["end_date"] else "",
        "period_type": parsed["period_type"],
    }
