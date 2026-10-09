"""
Who did what, when -- one flat list of dated actions.

Nothing new is recorded here. Every action already leaves its mark on the row
it acted on: an upload carries who loaded it and the time, then one
approved-by/approved-at pair per stage, then the rejection if it was turned
down. A sign-in carries who created it and when. This module reads those
columns back out and turns one upload into up to five dated events, so the
trail can be read in time order instead of a column at a time.

Written this way rather than as an audit table because the columns are already
the record of truth -- a separate log could drift from them.
"""
import datetime as dt

from sqlalchemy import select

from app import auth
from app.models import UploadBatch, User

# --- what an action is called, in the order a sheet moves through them ------
UPLOADED = "uploaded"
APPROVED_VERTICAL = "approved_vertical"
APPROVED_ADMIN = "approved_admin"
APPROVED_CTO = "approved_cto"
REJECTED = "rejected"
ACCOUNT_CREATED = "account_created"
PASSWORD_CHANGED = "password_changed"
LAST_SIGN_IN = "last_sign_in"

ACTION_LABELS = {
    UPLOADED: "Uploaded a sheet",
    APPROVED_VERTICAL: "Approved — Vertical Head",
    APPROVED_ADMIN: "Approved — Admin",
    APPROVED_CTO: "Approved — CTO (final)",
    REJECTED: "Turned down",
    ACCOUNT_CREATED: "Created a sign-in",
    PASSWORD_CHANGED: "Changed their password",
    LAST_SIGN_IN: "Last signed in",
}

#: actions that belong to a sheet, as opposed to a sign-in
SHEET_ACTIONS = [UPLOADED, APPROVED_VERTICAL, APPROVED_ADMIN, APPROVED_CTO, REJECTED]
ACCOUNT_ACTIONS = [ACCOUNT_CREATED, PASSWORD_CHANGED, LAST_SIGN_IN]


def _event(at, action, actor, actor_name, subject, note=None, upload=None):
    """
    One line of the trail.

    `actor` is the User row where one was recorded; bulk imports predate the
    sign-ins and only left a name, so `actor_name` is what gets displayed and
    the row is kept either way rather than dropped for want of an id.
    """
    return {
        "at": at,
        "action": action,
        "action_label": ACTION_LABELS[action],
        "actor": actor,
        "actor_id": actor.id if actor is not None else None,
        "actor_name": (actor.full_name if actor is not None else actor_name) or "—",
        "actor_role": actor.role if actor is not None else None,
        "subject": subject,
        "note": note,
        "upload": upload,
    }


def upload_events(upload):
    """Every dated action recorded against one uploaded workbook."""
    events = []
    name = upload.original_filename

    if upload.uploaded_at:
        events.append(_event(upload.uploaded_at, UPLOADED, upload.uploader,
                             upload.uploaded_by, name, upload.notes, upload))

    for action, at, by, note in (
        (APPROVED_VERTICAL, upload.vertical_approved_at, upload.vertical_approver, upload.vertical_note),
        (APPROVED_ADMIN, upload.admin_approved_at, upload.admin_approver, upload.admin_note),
        (APPROVED_CTO, upload.cto_approved_at, upload.cto_approver, upload.cto_note),
    ):
        if at:
            events.append(_event(at, action, by, None, name, note, upload))

    if upload.rejected_at:
        stage = auth.STAGE_LABELS.get(upload.rejected_stage, "")
        note = upload.reject_reason
        if stage:
            note = "at the %s stage%s" % (stage.replace("Awaiting ", ""),
                                          ": " + note if note else "")
        events.append(_event(upload.rejected_at, REJECTED, upload.rejecter,
                             None, name, note, upload))

    return events


def account_events(person, by_id):
    """Dated actions recorded against one sign-in, attributed to whoever did them."""
    events = []
    if person.created_at:
        maker = by_id.get(person.created_by_id)
        events.append(_event(person.created_at, ACCOUNT_CREATED, maker, "Administrator",
                             "%s (%s)" % (person.full_name, auth.ROLE_LABELS.get(person.role, person.role))))
    if person.password_changed_at:
        events.append(_event(person.password_changed_at, PASSWORD_CHANGED, person,
                             None, person.full_name))
    if person.last_login_at:
        events.append(_event(person.last_login_at, LAST_SIGN_IN, person,
                             None, person.full_name))
    return events


def collect(db, viewer, project=None, actor=None, action=None,
            date_from=None, date_to=None, include_accounts=True):
    """
    The trail, newest first.

    Uploads are narrowed in SQL to what the viewer may see; the per-event
    filters are applied in Python because each kind of action keeps its date in
    its own column, so there is no one column to filter on.
    """
    query = auth.scope_uploads(select(UploadBatch), UploadBatch, viewer)
    if project:
        query = query.where(UploadBatch.project_id == project)

    events = []
    for upload in db.scalars(query):
        events.extend(upload_events(upload))

    # an account action has no project, so it drops out as soon as one is chosen
    if include_accounts and not project:
        people = list(db.scalars(select(User)))
        by_id = {p.id: p for p in people}
        for person in people:
            events.extend(account_events(person, by_id))

    if action:
        events = [e for e in events if e["action"] == action]
    if actor:
        events = [e for e in events if e["actor_id"] == actor]
    if date_from:
        events = [e for e in events if e["at"] and e["at"].date() >= date_from]
    if date_to:
        events = [e for e in events if e["at"] and e["at"].date() <= date_to]

    events.sort(key=lambda e: e["at"] or dt.datetime.min, reverse=True)
    return events


def by_day(events):
    """
    The trail cut into days, newest first.

    A flat list of eighty timestamps is hard to read; the question being asked
    of this page is nearly always 'what happened on this date', so the day is
    made the heading and the rows beneath it carry only the time.
    """
    days = []
    for event in events:                      # already sorted newest first
        day = event["at"].date() if event["at"] else None
        if not days or days[-1][0] != day:
            days.append((day, []))
        days[-1][1].append(event)
    return days


def tally(events):
    """The counts worth seeing above the table."""
    return {
        "total": len(events),
        "uploads": sum(1 for e in events if e["action"] == UPLOADED),
        "approvals": sum(1 for e in events if e["action"].startswith("approved_")),
        "rejected": sum(1 for e in events if e["action"] == REJECTED),
        "people": len({e["actor_name"] for e in events if e["actor_name"] != "—"}),
    }


def as_date(value):
    """A date filter arrives as text and is blank for 'any'."""
    value = (value or "").strip()
    if not value:
        return None
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        return None
