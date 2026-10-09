"""
Build the reporting hierarchy for each vertical from the organogram documents.

    python scripts/import_hierarchy.py              reads storage/organogram/
    python scripts/import_hierarchy.py --dry-run

Four verticals, from three kinds of source:

  fisheries   the NFDP team sheet, which states a Reporting Manager per person,
              so the lines are exact.
  insurance   the organogram sheet's drawing: each box reads 'Role [Name]' and
  banking     carries a position, so the parent is the nearest box one level up.
              Those lines are marked inferred.
  technology  the chart PDF. Each box carries a numbered list of its team in
              the same column; those are read off by position and placed under
              their lead. Entries reading 'Replacement - NN' are posts still to
              be hired rather than people, so they are counted instead of being
              listed -- which is why a team shows fewer names than its
              sanctioned headcount.

A 'Replacement - NN' on the chart is a post still to be hired, not a person, so
it is counted rather than stored as a name. A headcount is the sanctioned
figure and so covers both the people in post and the posts still to be filled.
Re-running replaces what was there.
"""
import argparse
import re
import sys
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import delete, select, update

from app import config
from app.db import SessionLocal
from app.models import OrgPerson

A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
XDR = "{http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing}"

VACANT = re.compile(r"^(replacement|vacancy|vaccant|vacant|new|tbd|to be hired)\s*[-–—]?\s*\d*$",
                    re.IGNORECASE)
LIST_NUM = re.compile(r"^\s*\d+\s*[).]\s*")
HEADCOUNT = re.compile(r"\((\d+)\)")
#: 'Manager- GI [Gourav Gahlot]' and 'Senior Executive [Sangita] Bank of India'
#: -- the name is in brackets anywhere, with role before and area after
ROLE_NAME = re.compile(r"^(.*?)[\[(]\s*([^\[\]()]*?)\s*[\])]\s*(.*)$", re.S)


def tidy(text):
    return re.sub(r"\s+", " ", str(text or "").replace("\n", " ")).strip(" -–—,")


def is_vacant(text):
    return not text or bool(VACANT.match(LIST_NUM.sub("", tidy(text))))


def norm(name):
    return re.sub(r"[^a-z]+", "", (name or "").lower())


def find_person(lookup, vertical, written):
    """
    Match a reporting manager to a person. The sheets spell names loosely --
    'Subosh Mishra' for Subodh, 'Pankaj Sharma' for Pankaj Kumar Sharma -- so
    an exact match is tried first, then one name containing the other, then a
    close spelling.
    """
    import difflib

    key = norm(written)
    if not key:
        return None
    if (vertical, key) in lookup:
        return lookup[(vertical, key)]

    same = {k[1]: v for k, v in lookup.items() if k[0] == vertical}
    contained = [v for k, v in same.items() if k and (k in key or key in k)]
    if len(contained) == 1:
        return contained[0]
    close = difflib.get_close_matches(key, list(same), n=1, cutoff=0.85)
    return same[close[0]] if close else None


# --------------------------------------------------------------------------
# fisheries: an explicit Reporting Manager column
# --------------------------------------------------------------------------
def read_fisheries(path):
    import openpyxl

    book = openpyxl.load_workbook(path, read_only=True, data_only=True)
    if "Sheet2" not in book.sheetnames:
        return []
    rows = [list(r) for r in book["Sheet2"].iter_rows(values_only=True)]
    book.close()

    header_index = next((i for i, r in enumerate(rows)
                         if any("name of resource" in str(c or "").lower() for c in r)), None)
    if header_index is None:
        return []
    header = {str(c).strip().lower(): i for i, c in enumerate(rows[header_index]) if c}
    name_at = header.get("name of resource")
    role_at = header.get("project role")
    mgr_at = header.get("reporting manager")

    out = []
    for order, row in enumerate(rows[header_index + 1:]):
        name = tidy(row[name_at]) if name_at is not None and name_at < len(row) else ""
        if not name or is_vacant(name):
            continue
        out.append({
            "vertical": "fisheries", "name": name,
            "role": tidy(row[role_at]) if role_at is not None and role_at < len(row) else None,
            "reports_to_name": tidy(row[mgr_at]) if mgr_at is not None and mgr_at < len(row) else None,
            "inferred": False, "sort_order": order, "source_file": Path(path).name,
        })
    return out


# --------------------------------------------------------------------------
# insurance / banking: boxes drawn on the sheet
# --------------------------------------------------------------------------
def read_chart_sheet(path, vertical):
    """Each drawing shape is a box; its row gives the level, its column the order."""
    archive = zipfile.ZipFile(path)
    shapes = []
    for entry in archive.namelist():
        if not re.match(r"xl/drawings/drawing\d+\.xml$", entry):
            continue
        for anchor in list(ET.fromstring(archive.read(entry))):
            start = anchor.find(XDR + "from")
            if start is None:
                continue
            text = " ".join(t.text.strip() for t in anchor.iter(A + "t")
                            if t.text and t.text.strip())
            if not text:
                continue
            shapes.append({"row": int(start.find(XDR + "row").text),
                           "col": int(start.find(XDR + "col").text),
                           "text": tidy(text)})
    archive.close()
    if not shapes:
        return []

    # boxes within a few rows of each other sit on the same level of the chart
    shapes.sort(key=lambda s: (s["row"], s["col"]))
    # the single highest box is the head of the vertical, on a level of its own
    levels, current = [[shapes[0]]], []
    for shape in shapes[1:]:
        if current and shape["row"] - current[-1]["row"] <= 2:
            current.append(shape)
        else:
            if current:
                levels.append(current)
            current = [shape]
    if current:
        levels.append(current)

    out = []
    for depth, level in enumerate(levels):
        for order, shape in enumerate(level):
            match = ROLE_NAME.match(shape["text"])
            if match:
                role, name, area = (tidy(match.group(1)), tidy(match.group(2)),
                                    tidy(match.group(3)))
            else:
                role, name, area = shape["text"], "", ""
            if is_vacant(name):
                name = ""                      # an unfilled post: keep the role, drop the name
            out.append({
                "vertical": vertical, "name": name or None, "role": role or None,
                "sub_department": area or None,
                "is_unit": not name, "inferred": depth > 0,
                "_depth": depth, "_col": shape["col"],
                "sort_order": depth * 100 + order, "source_file": Path(path).name,
            })

    # parent is the nearest box on the level above, by column
    for entry in out:
        if entry["_depth"] == 0:
            continue
        above = [e for e in out if e["_depth"] == entry["_depth"] - 1]
        if above:
            entry["_parent"] = min(above, key=lambda e: abs(e["_col"] - entry["_col"]))
    return out


# --------------------------------------------------------------------------
# technology: units and their leads from the chart PDF
# --------------------------------------------------------------------------
#: (name, role, area, headcount, reports to, anchor, vacancies).
#: The anchor is the text to find in the PDF so the team listed in that box's
#: column can be read off. vacancies, when given, overrides what the chart
#: shows -- used where the box's own post is unfilled.
TECHNOLOGY = [
    ('Nirmal Kumar', 'CTO', 'UIDAI & Project Management', 120, 'Akhil Kumar', 'Nirmal Kumar', None),
    ('Rajkishore Kumar', 'VP, CRM', 'Application Tech Support, Maintenance and Backend Monitoring', 15, 'Nirmal Kumar', 'Rajkishore Kumar(VP)', None),
    ('Rajesh Kumar', 'VP', 'Data Lake / ASA / AUA / CCTNS / CSC Academy - Digital Library / Training / Upgrade / Transport Sewa', 16, 'Nirmal Kumar', 'Rajesh Kumar (VP)', None),
    ('Shambhu Prasad', 'CISO', 'Security', 12, 'Nirmal Kumar', 'Shambhu Prasad (CISO)', None),
    ('Asitabha Panda', 'Head', 'Governance, Risk and Compliance', None, 'Shambhu Prasad', 'Asitabha Panda', None),
    ('Pankaj Dang / Raju More', 'Head of IT Infrastructure', 'IT Infrastructure - Technology', 10, 'Nirmal Kumar', 'Pankaj Dang/ Raju More (10)', None),
    ('Tapash Saha', 'Lead', 'Digital Seva Portal Connect, Wallet, Biller, Broker, Service Desk Back Office, Recharge, Topup, Electricity', 7, 'Nirmal Kumar', 'Tapash Saha', None),
    ('Raju More', 'Deployment / Operations / DevOps / DBA', 'Deployment and Operations', 9, 'Pankaj Dang / Raju More', 'Raju More', None),
    ('Chandresh Kumar', 'Lead', 'Front-end / UI / UX Developers', 5, 'Nirmal Kumar', 'Chandresh Kumar', None),
    ('Hariom Pal', 'Lead', 'Diginame, Chattisgarh Bima, Scholarship, Telelaw-Web, e-District Services, Mobile Development', 8, 'Nirmal Kumar', 'Hariom Pal', None),
    ('Gunwant Saini', 'Lead', 'Project Management (NCS)', 7, 'Rajesh Kumar', 'Gunwant Saini', None),
    ('Omprakash Singh', 'Lead', 'Insurance, Multi-Core', 7, 'Nirmal Kumar', 'Omprakash Singh', None),
    ('Harsh Mishra', 'Business Analyst', 'CSCPAY VLE Management', 5, 'Nirmal Kumar', 'Harsh Mishra', None),
    ('Shiv Charan Sharma', 'Lead', 'Grameen Store / CSC Academy / Skill / Education', 6, 'Nirmal Kumar', 'Shiv Charan Sharma', None),
    ('Sonal Kharbanda', 'Lead', 'VLE Management', 3, 'Nirmal Kumar', 'Sonal Kharbanda', None),
    ('Raju Kumar', 'Lead', 'DigiPay, BBPS & Recharge', 8, 'Nirmal Kumar', 'Raju Kumar', None),
    (None, 'Team', 'PPI', None, 'Omprakash Singh', 'PPI', None),
    (None, 'Team', 'Quality Assurance', 5, 'Asitabha Panda', 'Quality Assurance', None),
    (None, 'Team', 'CTO Office', 3, 'Nirmal Kumar', 'CTO Office', None),
    (None, 'Team', 'Security and Compliance', 4, 'Asitabha Panda', 'Security and Compliance', None),
    (None, 'Team', 'Infrastructure Operations (AIPL)', None, 'Pankaj Dang / Raju More', None, None),
    (None, 'Team', 'Technical Ticket Support', 3, 'Rajkishore Kumar', 'Technical Ticket Support', None),
    (None, 'Team', 'Business Application Integration & Development (Connect & Wallet)', 5, 'Rajkishore Kumar', 'Integration& Developement', None),
    (None, 'Team', 'Database management & cron job monitoring across Paysheet, Recovery, Refund, GR/Reports, VLEMIS, Vigilance, Data Archival and Card91, with Recon Maker/Checker synchronisation', 6, 'Rajkishore Kumar', 'Database management and cron job', None),
    (None, 'Team', 'Data Protection Officer (CSC Pay)', 1, 'Asitabha Panda', 'Data Protection Officer (CSC', None),
    (None, 'Team', 'AI Research & Innovation', 2, 'Nirmal Kumar', 'AI Research', None),
    (None, 'Team', 'Private Banking (NPS, FastTag, Loan Bazar, CIBIL)', 4, 'Travel / Banking', 'Private Banking', None),
    (None, 'Team Lead (post unfilled)', 'Travel / Banking', 11, 'Nirmal Kumar', None, 1),
    (None, 'Team', 'IRCTC, Air, Bus, Hotel', 6, 'Travel / Banking', 'Irctc', None),
    (None, 'Team', 'Mobile Developers', 4, 'Hariom Pal', 'Mobile Developers', None),
]


#: '1) Ravi Kumar', '2)Prashant Pal' (no space), '6 Replacement - 14'.
#: The name must start with a letter, so a bare year or figure is not a list entry.
NUMBERED = re.compile(r"^(\d+)\s*[).]?\s*([A-Za-z].*)$")


def _pdf_items(path):
    """
    [(x, y, text)] for the chart, with runs on the same line joined up.

    The PDF breaks some labels mid-word -- 'Omprakash Singh' arrives as 'O'
    then 'mprakash Singh' -- so runs that sit on the same line with only a
    small gap are joined before anything tries to match them.
    """
    import collections

    from pypdf import PdfReader

    runs = []

    def visit(text, cm, tm, font, size):
        if text.strip():
            runs.append((round(tm[4]), round(tm[5]), text))

    PdfReader(path).pages[0].extract_text(visitor_text=visit)

    by_line = collections.defaultdict(list)
    for x, y, text in runs:
        by_line[y].append((x, text))

    out = []
    for y, parts in by_line.items():
        parts.sort()
        start, joined = parts[0]
        for x, text in parts[1:]:
            if x - start < 30:                 # same word or phrase, keep joining
                joined += text
            else:
                out.append((start, y, joined.strip()))
                start, joined = x, text
        out.append((start, y, joined.strip()))
    return [(x, y, t) for x, y, t in out if t]


def _find_anchor(items, anchor):
    """
    Locate a box by its label, allowing for the chart's loose spacing and
    punctuation. Exact text first, then a line that contains the label.
    """
    key = re.sub(r"[^a-z0-9]+", "", anchor.lower())
    if not key:
        return None
    for x, y, text in items:
        if text.strip() == anchor:
            return (x, y)
    best = None
    for x, y, text in items:
        flat = re.sub(r"[^a-z0-9]+", "", text.lower())
        if key and (key in flat or (len(flat) > 6 and flat in key)):
            if best is None or len(flat) < len(best[2]):
                best = (x, y, flat)
    return (best[0], best[1]) if best else None


#: The MD sits above every vertical. He is shown at the head of Technology for
#: context but does not count toward its strength.
MANAGING_DIRECTOR = {
    "vertical": "technology", "name": "Akhil Kumar", "role": "MD",
    "sub_department": "Above all verticals", "is_unit": False, "inferred": False,
    "counted": False, "sort_order": -1,
}


def read_technology(path):
    """Units and leads, each with the team listed in its column on the chart."""
    items = _pdf_items(path)
    numbered = [(x, y, NUMBERED.match(t).group(2).strip())
                for x, y, t in items if NUMBERED.match(t)]
    source_name = Path(path).name

    out = [dict(MANAGING_DIRECTOR, source_file=source_name)]
    anchored = []
    for order, (name, role, area, count, parent, anchor, vacancies) in enumerate(TECHNOLOGY):
        box = {
            "vertical": "technology", "name": name, "role": role,
            "sub_department": area, "headcount": count, "reports_to_name": parent,
            "is_unit": name is None, "inferred": False,
            "to_be_hired": vacancies, "_fixed_vacancies": vacancies is not None,
            "sort_order": order * 100, "source_file": source_name,
        }
        out.append(box)
        if not anchor:
            continue
        spot = _find_anchor(items, anchor)
        if spot is not None:
            anchored.append((spot[0], spot[1], order, box))

    # Assign each listed person to one box only -- the nearest column whose box
    # sits just below them. Matching every box within range put people who sit
    # between two adjacent columns under both of them.
    placed = {}
    for x, y, member in numbered:
        candidates = [(abs(x - bx), y - by, order, box)
                      for bx, by, order, box in anchored
                      if abs(x - bx) < 70 and by < y and y - by < 260]
        if not candidates:
            continue
        _dx, _dy, order, box = min(candidates, key=lambda c: (c[0], c[1]))
        if is_vacant(member):
            # a post still to be hired: count it, do not invent a person
            if box.get("_fixed_vacancies"):
                continue                       # the box states its own figure
            box["to_be_hired"] = (box.get("to_be_hired") or 0) + 1
            continue
        placed.setdefault((order, id(box)), []).append((y, member, box))

    # a box's label, lead name and area all count as claiming that chart text,
    # otherwise every wrapped line of a long label looks unaccounted for
    claimed = set()
    for name, role, area, _count, _parent, anchor, _vac in TECHNOLOGY:
        for text in (name, area, anchor):
            if text:
                claimed.add(re.sub(r"[^a-z0-9]+", "", text.lower()))
    unclaimed = []
    for x, y, text in items:
        if NUMBERED.match(text) or len(text) < 6:
            continue
        flat = re.sub(r"[^a-z0-9]+", "", text.lower())
        if flat and len(flat) > 8 and not any(c in flat or flat in c for c in claimed):
            unclaimed.append(text)
    if unclaimed:
        print("\n   chart text not claimed by any box (%d):" % len(unclaimed))
        for text in unclaimed[:30]:
            print("      %s" % text[:72])

    for (order, _box_id), rows in placed.items():
        for position, (_y, member, box) in enumerate(sorted(rows, reverse=True)):
            out.append({
                "vertical": "technology", "name": tidy(member), "role": "Team member",
                "is_unit": False, "inferred": False, "_parent": box,
                "sort_order": order * 100 + position + 1, "source_file": source_name,
            })
    return out


# --------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Build the reporting hierarchy.")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    folder = config.ORGANOGRAM_DIR
    sources = {
        "fisheries": folder / "Vertical_Benchmark_15062026.xlsx",
        "insurance": folder / "Insurance_Resource_KRA_Organogram_FY2026-27.xlsx",
        "banking": folder / "PSB_Banking_Vertical_Organogram.xlsx",
        "technology": folder / "Organogram_CSCSPV_Technology.pdf",
    }

    entries = []
    if sources["fisheries"].is_file():
        entries += read_fisheries(sources["fisheries"])
    for vertical in ("insurance", "banking"):
        if sources[vertical].is_file():
            entries += read_chart_sheet(sources[vertical], vertical)
    if sources["technology"].is_file():
        entries += read_technology(sources["technology"])

    by_vertical = {}
    for e in entries:
        by_vertical.setdefault(e["vertical"], []).append(e)
    for vertical, rows in sorted(by_vertical.items()):
        print("\n%s: %d box(es)" % (vertical.upper(), len(rows)))
        for e in rows[:40]:
            parent = e.get("reports_to_name") or (
                (e.get("_parent") or {}).get("name") or (e.get("_parent") or {}).get("role"))
            print("   %-34s %-34s -> %s" % ((e.get("name") or "(team)")[:34],
                                            (e.get("role") or "")[:34],
                                            (parent or "(top)")[:30]))
    if args.dry_run:
        print("\ndry run: nothing written")
        return

    session = SessionLocal()
    try:
        # reports_to_id points at this same table, so the links go before the rows
        session.execute(update(OrgPerson).values(reports_to_id=None))
        session.flush()
        session.execute(delete(OrgPerson))
        session.flush()
        made = {}
        for e in entries:
            person = OrgPerson(**{k: v for k, v in e.items() if not k.startswith("_")})
            session.add(person)
            session.flush()
            made[id(e)] = person
            e["_person"] = person

        # positional parents first, then the ones named outright
        for e in entries:
            person = e["_person"]
            if e.get("_parent") is not None:
                person.reports_to_id = e["_parent"]["_person"].id
        # a box can be named after a person or, for a team, after its area --
        # both have to be findable, since either may be quoted as a parent
        lookup = {}
        for e in entries:
            person = e["_person"]
            for label in (person.name, None if person.name else person.sub_department):
                if not label:
                    continue
                key = (e["vertical"], norm(label))
                if key not in lookup:
                    lookup[key] = person
        for e in entries:
            person = e["_person"]
            if person.reports_to_id or not person.reports_to_name:
                continue
            parent = find_person(lookup, e["vertical"], person.reports_to_name)
            if parent is not None and parent.id != person.id:
                person.reports_to_id = parent.id
        session.commit()
        linked = sum(1 for e in entries if e["_person"].reports_to_id)
        print("\nstored %d box(es) across %d vertical(s); %d have a reporting line"
              % (len(entries), len(by_vertical), linked))
    finally:
        session.close()


if __name__ == "__main__":
    main()
