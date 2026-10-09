"""
Reporting-period parsing.

Periods appear in two places in the Nextcloud share:

  folder names  'Data collection sheet Cycle 1'
                'Data collection sheet Cycle 2 (Jan-Mar)'
                'Data Collection Sheet Cycle 3 (April-June)'
                'Updated Data collection Sheet ( Sep 2026)'

  file names    'CMMI data eDistrcit_Apr-May26'
                'CMMI data eDistrcit_Dec25 - feb26'
                'Ticket sheet Insurance crm (July -Sep 2025)'
                'TICKET DATA FROM 01-APR TO 15-JUNE-26'

suggest() turns any of those into a normalised label plus start/end dates, and
the upload form shows it as an editable default -- the person confirms or
overrides it, so a misread filename never silently mislabels data.
"""
import calendar
import datetime as dt
import re

MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
MONTH_ABBR = {v: k.capitalize() for k, v in MONTHS.items()}

# A month name, optionally glued or spaced to a 2- or 4-digit year:
# 'Dec25', 'June-26', 'Sep 2026'. The leading guard is (?<![a-z]) rather than
# \b because these names sit against underscores ('eDistrcit_Apr-May26'), where
# \b does not apply. Month names are spelled out in full instead of 'mar[a-z]*'
# so that 'Marathon' and 'Decision' are not read as months.
_MONTH_TOKEN = re.compile(
    r"(?<![a-z])"
    r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?"
    r"|aug(?:ust)?|sept(?:ember)?|sep|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
    r"(?![a-z])"
    r"\.?\s*[-_,]?\s*"
    # an optional day, but only when a four-digit year follows it, so
    # 'april 01, 2026' reads 2026 as the year while 'Dec25' still reads 25
    r"(?:\d{1,2}(?:st|nd|rd|th)?\s*[,-]?\s*(?=(?:19|20)\d{2}))?"
    r"((?:19|20)\d{2}|\d{2})?(?!\d)",
    re.IGNORECASE,
)
_FULL_YEAR = re.compile(r"\b((?:19|20)\d{2})\b")
# (?<![a-z]) rather than \b, and [\s_-]* between, because these appear as
# 'Cycle_1' and 'Sheet_cycle 1' where the underscore defeats a word boundary
_CYCLE = re.compile(r"(?<![a-z])cycle[\s_.-]*(\d+)", re.IGNORECASE)


def _expand_year(raw):
    """'26' -> 2026, '2026' -> 2026, None -> None."""
    if not raw:
        return None
    value = int(raw)
    return value if value > 100 else 2000 + value


def _last_day(year, month):
    return calendar.monthrange(year, month)[1]


def _month_tokens(text):
    """[(month_number, year_or_None), ...] in the order they appear."""
    found = []
    for match in _MONTH_TOKEN.finditer(text):
        month = MONTHS[match.group(1)[:3].lower()]
        found.append((month, _expand_year(match.group(2))))
    return found


def _resolve_years(tokens, fallback_year):
    """Fill in years for month tokens written without one."""
    years = [year for _m, year in tokens if year]
    resolved = []
    for index, (month, year) in enumerate(tokens):
        if year:
            resolved.append((month, year))
            continue
        # borrow the next stated year, else the previous, else the fallback
        later = next((y for _m, y in tokens[index + 1:] if y), None)
        earlier = next((y for _m, y in reversed(tokens[:index]) if y), None)
        resolved.append((month, later or earlier or fallback_year or dt.date.today().year))
    # a range written 'Dec25 - feb26' must not go backwards
    if len(resolved) >= 2 and years:
        (m0, y0), (m1, y1) = resolved[0], resolved[-1]
        if (y1, m1) < (y0, m0) and resolved[-1][1] == resolved[0][1]:
            resolved[-1] = (m1, y1 + 1)
    return resolved


def _label(start, end, cycle):
    if start is None:
        return "Cycle %d" % cycle if cycle else ""
    if (start.year, start.month) == (end.year, end.month):
        span = "%s %d" % (MONTH_ABBR[start.month], start.year)
    elif start.year == end.year:
        span = "%s-%s %d" % (MONTH_ABBR[start.month], MONTH_ABBR[end.month], start.year)
    else:
        span = "%s %d - %s %d" % (MONTH_ABBR[start.month], start.year,
                                  MONTH_ABBR[end.month], end.year)
    return "Cycle %d (%s)" % (cycle, span) if cycle else span


def suggest(*sources, **kwargs):
    """
    Read a period out of any number of strings (folder name, then file name).
    Returns the dict ReportingPeriod is built from; label is '' when nothing
    recognisable was found, and the caller must then ask for it.
    """
    fallback_year = kwargs.get("fallback_year")
    text = " ".join(s for s in sources if s)

    cycle_match = _CYCLE.search(text)
    cycle = int(cycle_match.group(1)) if cycle_match else None

    stated_year = _FULL_YEAR.search(text)
    fallback_year = fallback_year or (int(stated_year.group(1)) if stated_year else None)

    tokens = _resolve_years(_month_tokens(text), fallback_year)

    start = end = None
    if tokens:
        first_month, first_year = tokens[0]
        last_month, last_year = tokens[-1]
        start = dt.date(first_year, first_month, 1)
        end = dt.date(last_year, last_month, _last_day(last_year, last_month))
    elif fallback_year:
        start = dt.date(fallback_year, 1, 1)
        end = dt.date(fallback_year, 12, 31)

    if start is None and cycle is None:
        return {"label": "", "period_type": "custom", "start_date": None, "end_date": None}

    span_months = 0
    if start and end:
        span_months = (end.year - start.year) * 12 + (end.month - start.month) + 1

    if start is None:
        period_type = "cycle"
    elif span_months == 1:
        period_type = "monthly"
    elif span_months == 3:
        period_type = "quarterly"
    elif span_months >= 12:
        period_type = "yearly"
    else:
        period_type = "range"

    return {
        "label": _label(start, end, cycle),
        "period_type": period_type,
        "start_date": start,
        "end_date": end,
    }
