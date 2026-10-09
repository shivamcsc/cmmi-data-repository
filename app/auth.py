"""
Authentication, roles and the approval chain.

Roles, least to most senior:

    uploader       upload sheets and see their own. Cannot approve or delete.
    vertical_head  FIRST approval, and only for the projects assigned to them.
    admin          SECOND approval, manages sign-ins, and the ONLY role that deletes.
    cto            THIRD and final approval. Cannot delete.

Seniority runs uploader < vertical_head < admin < cto. Each approval stage
belongs to exactly one role, so no one can give two approvals in a row and
the three sign-offs stay independent.

A sheet moves

    pending_vertical -> pending_admin -> pending_cto -> approved

and only a fully approved sheet appears in Browse and the CSV exports. Whoever
may approve a stage may also reject at it, which records who rejected it and why.

What each role sees:

    uploader       the sheets they uploaded
    vertical_head  every sheet for their assigned projects, whoever uploaded it
    admin / cto    everything

Passwords are stored as PBKDF2-HMAC-SHA256 with a per-password salt. A new
account is created with must_change_password set, so the first sign-in forces
the person to replace whatever the administrator handed them.
"""
import base64
import hashlib
import hmac
import os
import secrets

from fastapi import Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select

# --- roles ----------------------------------------------------------------
UPLOADER = "uploader"
VERTICAL_HEAD = "vertical_head"
CTO = "cto"
ADMIN = "admin"

ROLES = [UPLOADER, VERTICAL_HEAD, ADMIN, CTO]

#: seniority, low to high. Deleting does not follow this -- see can_delete.
ROLE_RANK = {UPLOADER: 1, VERTICAL_HEAD: 2, ADMIN: 3, CTO: 4}
ROLE_LABELS = {
    UPLOADER: "Uploader",
    VERTICAL_HEAD: "Vertical Head",
    CTO: "CTO",
    ADMIN: "Administrator",
}
ROLE_HELP = {
    UPLOADER: "Uploads sheets. Cannot approve or delete.",
    VERTICAL_HEAD: "First approval, for their assigned projects.",
    ADMIN: "First approval for any project, manages sign-ins, and the only role that can delete.",
    CTO: "Final approval across all projects. Cannot delete.",
}

# --- approval stages ------------------------------------------------------
PENDING_VERTICAL = "pending_vertical"
PENDING_ADMIN = "pending_admin"
PENDING_CTO = "pending_cto"
APPROVED = "approved"
REJECTED = "rejected"

STAGE_LABELS = {
    PENDING_VERTICAL: "Awaiting Vertical Head",
    PENDING_ADMIN: "Awaiting Admin",
    PENDING_CTO: "Awaiting CTO",
    APPROVED: "Approved",
    REJECTED: "Rejected",
}

#: which role approves which stage, and what the sheet becomes once they do
STAGE_APPROVER = {
    PENDING_VERTICAL: VERTICAL_HEAD,
    PENDING_ADMIN: ADMIN,
    PENDING_CTO: CTO,
}
NEXT_STAGE = {
    PENDING_VERTICAL: PENDING_ADMIN,
    PENDING_ADMIN: PENDING_CTO,
    PENDING_CTO: APPROVED,
}

ITERATIONS = 200_000


# --------------------------------------------------------------------------
# Passwords
# --------------------------------------------------------------------------
def hash_password(password, salt=None):
    """'pbkdf2_sha256$<iterations>$<salt>$<hash>'."""
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), ITERATIONS)
    return "pbkdf2_sha256$%d$%s$%s" % (ITERATIONS, salt,
                                       base64.b64encode(digest).decode())


def verify_password(password, stored):
    if not stored or stored.count("$") != 3:
        return False
    _algorithm, iterations, salt, expected = stored.split("$")
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), int(iterations))
    return hmac.compare_digest(base64.b64encode(digest).decode(), expected)


def password_problem(password, confirm=None):
    """Return a message when the password is unacceptable, else None."""
    if confirm is not None and password != confirm:
        return "The two passwords do not match."
    if len(password or "") < 8:
        return "Choose a password of at least 8 characters."
    if password.lower() in ("password", "12345678", "cmmi@123", "changeme"):
        return "That password is too easy to guess."
    return None


def temporary_password():
    """A readable one-off password for a new account."""
    return "Cmmi-" + secrets.token_urlsafe(6)


def session_secret():
    """Stable across restarts when SECRET_KEY is set, random otherwise."""
    return os.getenv("SECRET_KEY") or secrets.token_hex(32)


# --------------------------------------------------------------------------
# Request helpers
# --------------------------------------------------------------------------
def load_user(request, db):
    """The signed-in User, or None. Cached on the request."""
    from app.models import User

    if getattr(request.state, "_user_loaded", False):
        return request.state.user
    user_id = request.session.get("user_id")
    user = db.get(User, user_id) if user_id else None
    if user is not None and not user.is_active:
        user = None
    request.state.user = user
    request.state._user_loaded = True
    return user


class NotAuthenticated(Exception):
    """Raised so the caller can redirect to the sign-in page."""


class NotPermitted(Exception):
    def __init__(self, message="You do not have access to that."):
        self.message = message
        super().__init__(message)


def redirect_to_login(request):
    target = request.url.path
    if request.url.query:
        target += "?" + request.url.query
    return RedirectResponse("/login?next=" + target, status_code=303)


# --------------------------------------------------------------------------
# Permissions
# --------------------------------------------------------------------------
def can_upload(user):
    return user is not None


def can_delete(user):
    """Deleting is the administrator's alone -- the CTO is senior but cannot delete."""
    return user is not None and user.role == ADMIN


def can_manage_users(user):
    return user is not None and user.role == ADMIN


def can_see_activity(user):
    """
    Who may read the trail of who did what.

    The administrator keeps track of the chain; the CTO signs the last approval
    and sees every project anyway, so withholding it from them would protect
    nothing. A vertical head's own queue is on Uploads already.
    """
    return user is not None and user.role in (ADMIN, CTO)


def approves_stage(user, stage):
    """
    True when this person signs off the given stage.

    Each stage belongs to one role only, so an administrator cannot also give
    the vertical head's approval and thereby sign off twice.
    """
    if user is None or stage not in STAGE_APPROVER:
        return False
    return user.role == STAGE_APPROVER[stage]


def covers_project(user, project_id):
    """
    Vertical heads only act on the projects assigned to them. A vertical head
    with no assignments yet covers none, so access is granted deliberately
    rather than by omission.
    """
    if user is None:
        return False
    if user.role in (ADMIN, CTO):
        return True
    if user.role == VERTICAL_HEAD:
        return project_id in {p.id for p in user.projects}
    return False


def can_approve(user, upload):
    """Whether this person can act on this upload right now."""
    if upload is None or upload.stage not in STAGE_APPROVER:
        return False
    if not approves_stage(user, upload.stage):
        return False
    if upload.stage == PENDING_VERTICAL and user.role == VERTICAL_HEAD:
        return covers_project(user, upload.project_id)
    return True


def scope_uploads(query, model, user):
    """
    Narrow a query over uploads to what this person is allowed to see:
    an uploader sees their own, a vertical head sees their projects, and
    an administrator or the CTO sees everything.
    """
    if user is None:
        return query.where(model.id.is_(None))
    if user.role == UPLOADER:
        return query.where(model.uploaded_by_id == user.id)
    if user.role == VERTICAL_HEAD:
        ids = [p.id for p in user.projects]
        if not ids:
            return query.where(model.id.is_(None))
        return query.where(model.project_id.in_(ids))
    return query


def may_see_upload(user, upload):
    if user is None or upload is None:
        return False
    if user.role == UPLOADER:
        return upload.uploaded_by_id == user.id
    if user.role == VERTICAL_HEAD:
        return covers_project(user, upload.project_id)
    return True


def visible_projects(db, user):
    """Projects this person may file sheets against."""
    from app.models import Project

    if user is None:
        return []
    if user.role == VERTICAL_HEAD and user.projects:
        return sorted(user.projects, key=lambda p: p.name)
    return list(db.scalars(select(Project).order_by(Project.name)))
