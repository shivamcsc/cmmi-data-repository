"""
Column catalogue: maps the many spellings used across the sheets onto one
fixed set of database columns.

Each entry is (column_name, label, group, data_type, unit, [aliases]) and the
column_name is literally the column in `development_record` / `services_record`,
so what you see on screen is what you query in SQL.

Deliberately excluded:
  * 'S.No.' -- a row counter, carries no information
  * the four 'FOR MQAS USE' columns (Cycle Time, Effort Variance, Productivity,
    Defect Removal Effectiveness) -- filled in by MQAS, not by the project teams

Matching runs in three passes (see resolve_headers):
  1. exact normalised header
  2. 'base' header, with any trailing parenthetical unit stripped
  3. unrecognised -- kept in the record's `extra_columns` JSON, never dropped
"""
import re

# (column, label, group, data_type, unit, [aliases])
DEVELOPMENT = [
    # --- identity -------------------------------------------------------
    ("department", "Department", "identity", "text", None, ["dept"]),
    ("project_name", "Project Name", "identity", "text", None, ["project"]),
    ("service_name", "Service / Module", "identity", "text", None,
     ["module cr use case", "service module", "module", "use case", "module name",
      "component", "service", "service name"]),

    # --- size -----------------------------------------------------------
    ("size", "Size", "size", "number", "size points", []),
    ("size_old", "Size (old calculation)", "size", "number", "size points",
     ["size old cal", "size old calculation", "size old"]),
    ("size_new", "Size (estimation model)", "size", "number", "size points",
     ["size new as per model develp", "size new as per model development", "size new",
      "size updated", "updated size",          # both word orders appear in the sheets
      "size as per model", "size acc to estimation model",
      "size updated acc to estimation model"]),

    # --- effort ---------------------------------------------------------
    ("planned_effort", "Planned Effort", "effort", "number", "person hours", []),
    ("actual_effort", "Actual Effort", "effort", "number", "person hours", []),
    ("ra_actual_effort", "RA Actual Effort", "effort", "number", "person hours", []),
    ("design_actual_effort", "Design Actual Effort", "effort", "number", "person hours", []),
    ("coding_actual_effort", "Coding Actual Effort", "effort", "number", "person hours", []),
    ("crut_effort", "Code Review & Unit Testing Effort", "effort", "number", "person hours",
     ["code review and unit testing effort", "crut effort"]),
    ("testing_actual_effort", "Testing Actual Effort", "effort", "number", "person hours", []),

    # --- schedule -------------------------------------------------------
    ("planned_start", "Planned Start Date", "schedule", "date", None, []),
    ("planned_end", "Planned End Date", "schedule", "date", None, []),
    ("actual_start", "Actual Start Date", "schedule", "date", None, []),
    ("actual_end", "Actual End Date", "schedule", "date", None, []),
    ("ra_start", "RA Start Date", "schedule", "date", None, []),
    ("ra_end", "RA End Date", "schedule", "date", None, []),
    ("design_start", "Design Start Date", "schedule", "date", None, []),
    ("design_end", "Design End Date", "schedule", "date", None, []),
    ("cut_start", "CUT Start Date", "schedule", "date", None,
     ["coding ut start date", "coding and ut start date", "coding unit testing start date"]),
    ("cut_end", "CUT End Date", "schedule", "date", None,
     ["coding ut end date", "coding and ut end date", "coding unit testing end date"]),
    ("crut_start", "Code Review & UT Start Date", "schedule", "date", None,
     ["code review and unit testing start date"]),
    ("crut_end", "Code Review & UT End Date", "schedule", "date", None,
     ["code review and unit testing end date"]),
    ("testing_start", "Testing Start Date", "schedule", "date", None,
     ["internal testing start date", "internaltesting start date"]),
    ("testing_end", "Testing End Date", "schedule", "date", None,
     ["internal testing end date", "internaltesting end date"]),
    ("uat_start", "UAT Start Date", "schedule", "date", None, []),
    ("uat_end", "UAT End Date", "schedule", "date", None, []),
    ("golive_date", "Migration / Go-Live", "schedule", "date", None,
     ["migration go live", "go live", "golive", "migration golive"]),

    # --- duration -------------------------------------------------------
    ("ra_duration", "RA Duration", "duration", "number", "days/size", []),
    ("design_duration", "Design Duration", "duration", "number", "days/size", []),
    ("cut_duration", "CUT Duration", "duration", "number", "days/size", []),
    ("testing_duration", "Testing Duration", "duration", "number", "days/size", []),

    # --- rates ----------------------------------------------------------
    ("coding_rate", "Coding Rate", "rate", "number", "size/effort", []),
    ("crut_rate", "CRUT Rate", "rate", "number", "size/effort", []),
    ("testing_rate", "Testing Rate", "rate", "number", "size/effort", []),

    # --- quality --------------------------------------------------------
    ("test_cases", "No. of Test Cases", "quality", "number", "count",
     ["no of test cases", "number of test cases"]),
    ("req_review_defects", "Requirement Review Defects", "quality", "number", "count",
     ["no of requirement review defects", "number of requirement review defects"]),
    ("design_review_defects", "Design Review Defects", "quality", "number", "count",
     ["no of design review defects", "number of design review defects"]),
    ("code_review_defects", "Code Review Defects", "quality", "number", "count",
     ["no of code review defects", "number of code review defects"]),
    ("internal_testing_defects", "Internal Testing Defects", "quality", "number", "count",
     ["no of internal testing defects", "number of internal testing defects"]),
    ("external_defects", "External Defects (UAT / Production)", "quality", "number", "count",
     ["external defects uat production", "total defects after release",
      "defects after release", "post release defects", "production defects"]),
]

SERVICES = [
    # --- ticket identity ------------------------------------------------
    ("request_id", "Request ID", "ticket", "text", None,
     ["request id", "ticket id", "ticket no", "request no", "case number"]),
    ("client", "Client", "ticket", "text", None, ["customer", "raised by"]),
    ("description", "Description", "ticket", "text", None, ["issue description", "subject"]),
    ("ticket_category", "Category", "ticket", "text", None, ["category", "request type", "type"]),
    ("priority", "Priority", "ticket", "text", None, ["severity"]),
    ("kedb", "KEDB Referred", "ticket", "text", None, ["kedb", "kedb refered", "kedb referred"]),
    ("assigned_to", "Assigned To", "ticket", "text", None, ["owner", "assignee"]),
    ("status", "Status", "ticket", "text", None, ["current status"]),
    ("reopen", "Reopen", "ticket", "text", None, ["reopened"]),
    ("rejections_complaints", "Rejections / Complaints", "ticket", "text", None,
     ["rejections complaints", "complaints", "rejection complaint"]),
    ("remarks", "Remarks", "ticket", "text", None, ["comment", "comments", "remark"]),

    # --- SLA: response leg ----------------------------------------------
    ("logging_datetime", "Logging Date/Time", "sla", "date", None,
     ["logging date time", "logged date time", "logging date", "created date",
      "date time opened"]),          # Salesforce case export
    ("response_datetime", "Response Date/Time", "sla", "date", None,
     ["response date time", "first response date"]),
    ("response_time", "Response Time", "sla", "number", "hrs/days",
     ["response time hrs", "response time days"]),
    ("response_defined_sla", "Defined SLA (Response)", "sla", "text", None, []),
    ("response_sla_compliance", "SLA Compliance (Response)", "sla", "text", None, []),

    # --- SLA: resolution leg --------------------------------------------
    ("resolution_datetime", "Resolution Date/Time", "sla", "date", None,
     ["resolution date time", "closure date time", "closed date",
      "date time closed"]),          # Salesforce case export
    ("resolution_time", "Resolution Time", "sla", "number", "days",
     ["resolution time days", "resolution time hrs"]),
    ("resolution_defined_sla", "Defined SLA (Resolution)", "sla", "text", None, []),
    ("resolution_sla_compliance", "SLA Compliance (Resolution)", "sla", "text", None, []),
    ("closure_date", "Date", "sla", "date", None, []),
]

CATALOGS = {"development": DEVELOPMENT, "services": SERVICES}

#: ordered column names per family, used for display and export
COLUMNS = {name: [c[0] for c in cat] for name, cat in CATALOGS.items()}

#: column -> (label, group, data_type, unit)
META = {
    family: {c[0]: {"label": c[1], "group": c[2], "type": c[3], "unit": c[4]} for c in cat}
    for family, cat in CATALOGS.items()
}

#: the column that names each row, used as the business key alongside the file name
KEY_COLUMN = {"development": "service_name", "services": "request_id"}

#: headers that identify each family; whichever scores higher wins
FAMILY_SIGNALS = {
    "development": {"department", "project_name", "service_name", "planned_effort",
                    "actual_effort", "size", "size_old", "size_new",
                    "coding_actual_effort", "testing_actual_effort", "external_defects"},
    "services": {"request_id", "priority", "logging_datetime", "response_datetime",
                 "resolution_datetime", "response_time", "resolution_time", "status",
                 "ticket_category"},
}

#: a sheet only counts as a data collection sheet if it carries one of these.
#: Without this the 'Size' and estimation tabs bundled into the CCTNS and CIBIL
#: workbooks match on 'Module'/'Size' alone and pollute the tables.
FAMILY_ANCHORS = {
    "development": {"planned_effort", "actual_effort", "ra_actual_effort",
                    "coding_actual_effort", "testing_actual_effort", "crut_effort"},
    "services": {"logging_datetime", "response_datetime", "resolution_datetime",
                 "response_time", "resolution_time"},
}

MIN_SIGNALS = {"development": 4, "services": 3}

#: ambiguous headers in services logs, resolved by which phase precedes them
PHASE_AMBIGUOUS = {
    "defined sla": {"response": "response_defined_sla", "resolution": "resolution_defined_sla"},
    "sla compliance": {"response": "response_sla_compliance", "resolution": "resolution_sla_compliance"},
}

#: headers to ignore outright -- recorded as skipped, not as extra columns
IGNORED_HEADERS = {
    "s no", "sr no", "serial no", "sno",                       # row counters
    "cycle time", "effort variance", "productivity",           # FOR MQAS USE
    "defect removal effectiveness", "defect removal efficiency", "dre",
}

_PAREN = re.compile(r"\([^)]*\)")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_TRAILING_MONTH = re.compile(
    r"\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*(\s+\d{2,4})?$")


def normalize(header):
    """'No- of  Code review defects ' -> 'no of code review defects'."""
    if header is None:
        return ""
    text = _NON_ALNUM.sub(" ", str(header).replace("\n", " ").lower())
    return " ".join(text.split())


def base_form(header):
    """Normalised form with any parenthetical unit removed.

    'Planned Effort (Person Hours)' -> 'planned effort'
    'Cycle Time(Days/size)'         -> 'cycle time'
    """
    if header is None:
        return ""
    return normalize(_PAREN.sub(" ", str(header).replace("\n", " ")))


def _trim_trailing_month(norm):
    """'size updated july 2026' -> 'size updated' so month-stamped headers match."""
    return _TRAILING_MONTH.sub("", norm).strip()


def _build_lookup(catalog):
    lookup = {}
    for column, label, _g, _d, _u, aliases in catalog:
        keys = [normalize(label), base_form(label), normalize(column.replace("_", " "))]
        for key in keys + list(aliases):
            if key and key not in lookup:
                lookup[key] = column
    return lookup


LOOKUPS = {name: _build_lookup(cat) for name, cat in CATALOGS.items()}


def is_ignored(header):
    """True for row counters and the FOR MQAS USE columns."""
    for key in (normalize(header), base_form(header)):
        if key in IGNORED_HEADERS:
            return True
    return False


def match_column(header, family="development"):
    """Resolve a spreadsheet header to a database column, or None."""
    if is_ignored(header):
        return None
    lookup = LOOKUPS.get(family, {})
    for key in (normalize(header), base_form(header), _trim_trailing_month(base_form(header))):
        if key and key in lookup:
            return lookup[key]
    return None


def detect_family(headers):
    """
    Pick the family a header row belongs to, or None if it is not a data
    collection sheet. A family needs both enough signal columns and at least
    one anchor column.
    """
    scores, qualified = {}, {}
    for family in CATALOGS:
        columns = {match_column(h, family) for h in headers}
        columns.discard(None)
        scores[family] = len(columns & FAMILY_SIGNALS[family])
        qualified[family] = (bool(columns & FAMILY_ANCHORS[family])
                             and scores[family] >= MIN_SIGNALS[family])
    eligible = [f for f in CATALOGS if qualified[f]]
    if not eligible:
        return None, scores
    return max(eligible, key=lambda f: scores[f]), scores


def resolve_headers(headers, family):
    """
    Map each header to a database column, in sheet order.

    Returns [(column_or_None, raw_header, ignored_flag)]. Services logs repeat
    'Defined SLA' and 'SLA Compliance' once after the response columns and once
    after the resolution columns; the phase seen so far tells them apart. A
    column already taken is left unmapped so a repeat never overwrites the first.
    """
    resolved, taken, phase = [], set(), None
    for raw in headers:
        norm = normalize(raw)
        if "response" in norm:
            phase = "response"
        elif "resolution" in norm or "closure" in norm:
            phase = "resolution"

        if is_ignored(raw):
            resolved.append((None, raw, True))
            continue

        column = None
        if family == "services" and norm in PHASE_AMBIGUOUS:
            column = PHASE_AMBIGUOUS[norm].get(phase or "response")
        if column is None:
            column = match_column(raw, family)
        if column is not None and column in taken:
            column = None                      # keep the first occurrence only
        if column is not None:
            taken.add(column)
        resolved.append((column, raw, False))
    return resolved
