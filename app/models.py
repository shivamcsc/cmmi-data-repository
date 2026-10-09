"""
Tabular schema for CMMI data collection sheets.

Reference : Category -> Project, ReportingPeriod

Display order is not stored. Categories come back in the order seed.py defines
them, projects and periods are ordered by name and start date. Anything a
query can derive -- the year or quarter of a period, a sort key -- is derived
rather than stored, so there is nothing to drift out of step.
Load trail: UploadBatch -> SheetTab
Data      : DevelopmentRecord, ServicesRecord -- one row per sheet row, with
            the sheet's columns as real database columns

Each record carries `upload_name` (the file it came from) and its own name
column -- `service_name` for Development, `request_id` for Services. Together
those identify a row, and both are indexed.

Not stored: 'S.No.' (a row counter) and the four 'FOR MQAS USE' columns
(Cycle Time, Effort Variance, Productivity, Defect Removal Effectiveness),
which MQAS fills in rather than the project teams.
"""
import datetime as dt

from sqlalchemy import (
    JSON, Boolean, Column, Date, DateTime, Float, ForeignKey, Index, Integer,
    String, Table, Text, UniqueConstraint,
)
from sqlalchemy.orm import relationship

from app.db import Base


def _now():
    return dt.datetime.now()


# --------------------------------------------------------------------------
# Reference data
# --------------------------------------------------------------------------
class Category(Base):
    """CMMI track: Development (ML 5.0) or Services (ML 3.0)."""

    __tablename__ = "category"

    id = Column(Integer, primary_key=True)
    code = Column(String(32), nullable=False, unique=True)
    name = Column(String(128), nullable=False)
    cmmi_level = Column(String(32))

    projects = relationship("Project", back_populates="category", order_by="Project.name")


class Project(Base):
    __tablename__ = "project"
    __table_args__ = (UniqueConstraint("category_id", "code", name="uq_project_cat_code"),)

    id = Column(Integer, primary_key=True)
    category_id = Column(Integer, ForeignKey("category.id"), nullable=False)
    code = Column(String(64), nullable=False)
    name = Column(String(255), nullable=False)

    category = relationship("Category", back_populates="projects")


class ReportingPeriod(Base):
    """The period a sheet covers: 'Cycle 3 (Apr-Jun 2026)', 'Jun 2026'."""

    __tablename__ = "reporting_period"
    __table_args__ = (UniqueConstraint("label", name="uq_period_label"),)

    id = Column(Integer, primary_key=True)
    label = Column(String(128), nullable=False)
    period_type = Column(String(16), default="custom")  # monthly|quarterly|yearly|cycle|custom
    start_date = Column(Date, index=True)               # periods sort by this
    end_date = Column(Date)

    def __repr__(self):
        return "<ReportingPeriod %s>" % self.label




# --------------------------------------------------------------------------
# People and access
# --------------------------------------------------------------------------
#: which projects a vertical head signs off; unused for the other roles
user_project = Table(
    "user_project", Base.metadata,
    Column("user_id", Integer, ForeignKey("user.id", ondelete="CASCADE"), primary_key=True),
    Column("project_id", Integer, ForeignKey("project.id", ondelete="CASCADE"), primary_key=True),
)


class User(Base):
    """
    A sign-in. `role` is one of uploader / vertical_head / cto / admin --
    see app.auth for what each may do.
    """

    __tablename__ = "user"

    id = Column(Integer, primary_key=True)
    username = Column(String(64), nullable=False, unique=True)   # the login id
    full_name = Column(String(255), nullable=False)
    email = Column(String(255))
    designation = Column(String(128))                            # e.g. 'Vertical Head, Fisheries'
    password_hash = Column(String(255), nullable=False)
    role = Column(String(32), nullable=False, default="uploader")

    is_active = Column(Boolean, default=True, nullable=False)
    must_change_password = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=_now)
    created_by_id = Column(Integer, ForeignKey("user.id"))
    last_login_at = Column(DateTime)
    password_changed_at = Column(DateTime)

    projects = relationship("Project", secondary=user_project, lazy="selectin")
    created_by = relationship("User", remote_side=[id])

    def __repr__(self):
        return "<User %s (%s)>" % (self.username, self.role)


# --------------------------------------------------------------------------
# Load trail
# --------------------------------------------------------------------------
class UploadBatch(Base):
    """One uploaded workbook, for one project and one reporting period."""

    __tablename__ = "upload_batch"

    id = Column(Integer, primary_key=True)
    category_id = Column(Integer, ForeignKey("category.id"), nullable=False)
    project_id = Column(Integer, ForeignKey("project.id"), nullable=False)
    period_id = Column(Integer, ForeignKey("reporting_period.id"), nullable=False)

    original_filename = Column(String(512), nullable=False)
    stored_path = Column(String(1024), nullable=False)
    file_hash = Column(String(64), index=True)
    file_size = Column(Integer)

    uploaded_by_id = Column(Integer, ForeignKey("user.id"), index=True)
    uploaded_by = Column(String(255))                          # name as recorded at the time
    uploaded_at = Column(DateTime, default=_now, index=True)   # when it entered this system
    source_folder = Column(String(512))                        # full path, e.g. 'Development/Current/Digipay/Cycle 3'
    source_modified_at = Column(DateTime)                      # when it was last changed where it came from
    notes = Column(Text)

    status = Column(String(16), default="pending")      # how the file read: pending|success|partial|failed
    error_message = Column(Text)

    # --- three approvals: vertical head -> admin -> CTO ---
    stage = Column(String(24), default="pending_vertical", index=True)

    vertical_approved_by_id = Column(Integer, ForeignKey("user.id"))
    vertical_approved_at = Column(DateTime)
    vertical_note = Column(Text)

    admin_approved_by_id = Column(Integer, ForeignKey("user.id"))
    admin_approved_at = Column(DateTime)
    admin_note = Column(Text)

    cto_approved_by_id = Column(Integer, ForeignKey("user.id"))
    cto_approved_at = Column(DateTime)
    cto_note = Column(Text)

    rejected_by_id = Column(Integer, ForeignKey("user.id"))
    rejected_at = Column(DateTime)
    rejected_stage = Column(String(24))                 # which stage turned it down
    reject_reason = Column(Text)

    sheet_count = Column(Integer, default=0)
    row_count = Column(Integer, default=0)
    is_superseded = Column(Boolean, default=False)

    category = relationship("Category")
    project = relationship("Project")
    period = relationship("ReportingPeriod")
    sheets = relationship("SheetTab", back_populates="upload", cascade="all, delete-orphan")

    uploader = relationship("User", foreign_keys=[uploaded_by_id])
    vertical_approver = relationship("User", foreign_keys=[vertical_approved_by_id])
    admin_approver = relationship("User", foreign_keys=[admin_approved_by_id])
    cto_approver = relationship("User", foreign_keys=[cto_approved_by_id])
    rejecter = relationship("User", foreign_keys=[rejected_by_id])

    @property
    def is_approved(self):
        return self.stage == "approved"


class SheetTab(Base):
    """A worksheet inside an uploaded workbook."""

    __tablename__ = "sheet_tab"

    id = Column(Integer, primary_key=True)
    upload_id = Column(Integer, ForeignKey("upload_batch.id", ondelete="CASCADE"), nullable=False)
    sheet_name = Column(String(255), nullable=False)
    sheet_index = Column(Integer, default=0)
    sheet_kind = Column(String(32), default="other")    # development|services|other
    header_row = Column(Integer)
    data_start_row = Column(Integer)
    row_count = Column(Integer, default=0)
    column_count = Column(Integer, default=0)
    was_parsed = Column(Boolean, default=False)
    skip_reason = Column(String(255))
    #: {database column -> spreadsheet column letter}, so a value on screen can
    #: be traced straight back to its cell in the original file
    column_map = Column(JSON)

    upload = relationship("UploadBatch", back_populates="sheets")


# --------------------------------------------------------------------------
# Who runs what
# --------------------------------------------------------------------------
class OrganogramEntry(Base):
    """
    One row of the vertical organogram: which division a project belongs to and
    who runs it. Imported from the organisation's benchmark sheet, cleaned of
    stray spacing and matched to a Project where the names line up.

    This is what tells you which vertical head signs off which project.
    """

    __tablename__ = "organogram_entry"
    __table_args__ = (Index("ix_org_division", "vertical_division"),)

    id = Column(Integer, primary_key=True)
    project_id = Column(Integer, ForeignKey("project.id"), index=True)   # null when unmatched
    project_name = Column(String(255))                 # as written in the source sheet
    vertical_division = Column(String(128))
    domain = Column(String(128))                       # SVC | Dev-CRs (Small/Medium) | Dev-CRs (Large)
    scope = Column(Text)

    business_manager = Column(String(255))
    state_head = Column(String(255))
    district_manager = Column(String(255))
    sdm = Column(String(255))
    technical_manager = Column(String(255))
    vertical_head = Column(String(255))
    engineers = Column(String(512))

    source_file = Column(String(512))
    imported_at = Column(DateTime, default=_now)

    project = relationship("Project")


class OrgPerson(Base):
    """
    One box in a vertical's organogram: a person, or a team where the chart
    names a team rather than an individual.

    `reports_to_id` builds the tree. Where the source states a reporting
    manager outright (the Fisheries team sheet) the line is exact; where it
    only draws boxes (the Insurance and Banking charts) the parent is inferred
    from the box's position, and `inferred` is set so the page can say so.
    """

    __tablename__ = "org_person"
    __table_args__ = (Index("ix_org_person_vertical", "vertical"),)

    id = Column(Integer, primary_key=True)
    vertical = Column(String(64), nullable=False)      # technology | banking | fisheries | insurance
    name = Column(String(255))
    role = Column(String(512))
    level = Column(String(32))                         # 'Level 7' where the chart gives one
    sub_department = Column(String(512))               # the area this box covers
    headcount = Column(Integer)                        # '(15)' on a team box
    to_be_hired = Column(Integer)                      # posts the chart marks 'Replacement - NN'
    is_unit = Column(Boolean, default=False)           # a team rather than a named person
    counted = Column(Boolean, default=True)            # False = shown for context only (the MD)

    reports_to_id = Column(Integer, ForeignKey("org_person.id"))
    reports_to_name = Column(String(255))              # as written, before matching
    inferred = Column(Boolean, default=False)          # parent came from chart position

    source_file = Column(String(512))
    sort_order = Column(Integer, default=0)
    imported_at = Column(DateTime, default=_now)

    reports_to = relationship("OrgPerson", remote_side=[id], backref="reports")


# --------------------------------------------------------------------------
# Shared record columns
# --------------------------------------------------------------------------
class RecordBase:
    """Provenance carried on every data row, so a single table answers most questions."""

    @property
    def key(self):
        return (self.upload_name, getattr(self, self.KEY_COLUMN))

    id = Column(Integer, primary_key=True)
    upload_id = Column(Integer, ForeignKey("upload_batch.id", ondelete="CASCADE"), nullable=False)
    sheet_id = Column(Integer, ForeignKey("sheet_tab.id", ondelete="CASCADE"), nullable=False)
    category_id = Column(Integer, ForeignKey("category.id"), nullable=False)
    project_id = Column(Integer, ForeignKey("project.id"), nullable=False)
    period_id = Column(Integer, ForeignKey("reporting_period.id"), nullable=False)

    upload_name = Column(String(512), nullable=False)   # the file it was uploaded as
    period_label = Column(String(128))                  # denormalised for easy querying
    sheet_name = Column(String(255))
    row_index = Column(Integer)                         # 1-based row in the worksheet
    extra_columns = Column(JSON)                        # columns not in the catalogue


class DevelopmentRecord(Base, RecordBase):
    """One module / CR / use case from a Development data collection sheet."""

    __tablename__ = "development_record"
    __table_args__ = (
        # prefix lengths: two full varchar(512) columns exceed MySQL's
        # 3072-byte index limit under utf8mb4
        Index("ix_dev_key", "upload_name", "service_name",
              mysql_length={"upload_name": 191, "service_name": 191}),
        Index("ix_dev_project_period", "project_id", "period_id"),
    )
    KEY_COLUMN = "service_name"

    # identity
    department = Column(String(255))
    project_name = Column(String(255))
    service_name = Column(String(512))

    # size
    size = Column(Float)
    size_old = Column(Float)
    size_new = Column(Float)

    # effort (person hours)
    planned_effort = Column(Float)
    actual_effort = Column(Float)
    ra_actual_effort = Column(Float)
    design_actual_effort = Column(Float)
    coding_actual_effort = Column(Float)
    crut_effort = Column(Float)
    testing_actual_effort = Column(Float)

    # schedule
    planned_start = Column(Date)
    planned_end = Column(Date)
    actual_start = Column(Date)
    actual_end = Column(Date)
    ra_start = Column(Date)
    ra_end = Column(Date)
    design_start = Column(Date)
    design_end = Column(Date)
    cut_start = Column(Date)
    cut_end = Column(Date)
    crut_start = Column(Date)
    crut_end = Column(Date)
    testing_start = Column(Date)
    testing_end = Column(Date)
    uat_start = Column(Date)
    uat_end = Column(Date)
    golive_date = Column(Date)

    # duration (days/size)
    ra_duration = Column(Float)
    design_duration = Column(Float)
    cut_duration = Column(Float)
    testing_duration = Column(Float)

    # rates (size/effort)
    coding_rate = Column(Float)
    crut_rate = Column(Float)
    testing_rate = Column(Float)

    # quality
    test_cases = Column(Float)
    req_review_defects = Column(Float)
    design_review_defects = Column(Float)
    code_review_defects = Column(Float)
    internal_testing_defects = Column(Float)
    external_defects = Column(Float)

    upload = relationship("UploadBatch")
    project = relationship("Project")
    period = relationship("ReportingPeriod")


class ServicesRecord(Base, RecordBase):
    """One ticket / service request from a Services log."""

    __tablename__ = "services_record"
    __table_args__ = (
        Index("ix_svc_key", "upload_name", "request_id",
              mysql_length={"upload_name": 191}),
        Index("ix_svc_project_period", "project_id", "period_id"),
    )
    KEY_COLUMN = "request_id"

    # ticket identity
    request_id = Column(String(64))
    client = Column(String(255))
    description = Column(Text)
    ticket_category = Column(String(128))
    priority = Column(String(64))
    kedb = Column(String(64))
    assigned_to = Column(String(255))
    status = Column(String(64))
    reopen = Column(String(64))
    rejections_complaints = Column(String(255))
    remarks = Column(Text)

    # SLA: response leg
    logging_datetime = Column(Date)
    response_datetime = Column(Date)
    response_time = Column(Float)
    response_defined_sla = Column(String(128))
    response_sla_compliance = Column(String(64))

    # SLA: resolution leg
    resolution_datetime = Column(Date)
    resolution_time = Column(Float)
    resolution_defined_sla = Column(String(128))
    resolution_sla_compliance = Column(String(64))
    closure_date = Column(Date)

    upload = relationship("UploadBatch")
    project = relationship("Project")
    period = relationship("ReportingPeriod")


RECORD_MODELS = {"development": DevelopmentRecord, "services": ServicesRecord}
