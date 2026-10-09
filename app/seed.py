"""Reference data: the two CMMI tracks, their projects, and the metric catalogue."""
from sqlalchemy import select

from app.models import Category, Project

# Order matters: categories are listed in the order they should appear.
CATEGORIES = [
    ("development", "CMMI ML 5.0 (Development)", "ML 5.0"),
    ("services", "CMMI ML 3.0 (Services)", "ML 3.0"),
    # further ML 3.0 views of the model
    ("acquisition", "Supplier Management / Acquisition (CMMI-ACQ)", "ML 3.0"),
    ("data_management", "Data Management (CMMI Data)", "ML 3.0"),
    ("people_management", "People Management (CMMI People)", "ML 3.0"),
    ("safety_security", "Safety and Security", "ML 3.0"),
    ("virtual_environments", "Virtual Environments", "ML 3.0"),
]

# Project names match the 'Current' folders in the Nextcloud share.
# 'ML 3 Artefacts of DSP' is deliberately excluded -- it holds test-case and
# tracking artefacts, not data collection sheets.
PROJECTS = {
    "development": [
        ("cctns", "CCTNS"),
        ("cibil", "CIBIL"),
        ("csc_safar_air", "CSC Safar (Air Tourism & Travel)"),
        ("digipay", "Digipay"),
        ("nfdp_fisheries", "NFDP (Fisheries)"),
        ("salesforce_crm", "Sales Force CRM"),
    ],
    "services": [
        ("csc_safar_irctc", "CSC Safar (IRCTC)"),
        ("dsp_edistrict", "DSP e-district"),
        ("dsp_electricity", "DSP electricity"),
        ("insurance", "Insurance"),
        ("psb", "Public Sector Banking"),
    ],
    "acquisition": [],
    "data_management": [],
    "people_management": [],
    "safety_security": [],
    "virtual_environments": [],
}


def seed_categories_and_projects(session):
    created = 0
    for code, name, level in CATEGORIES:
        category = session.scalar(select(Category).where(Category.code == code))
        if category is None:
            category = Category(code=code, name=name, cmmi_level=level)
            session.add(category)
            session.flush()
            created += 1
        for pcode, pname in PROJECTS[code]:
            exists = session.scalar(
                select(Project).where(Project.category_id == category.id, Project.code == pcode)
            )
            if exists is None:
                session.add(Project(category_id=category.id, code=pcode, name=pname))
                created += 1
    session.commit()
    return created


def seed_all(session):
    """The column catalogue lives in metrics_catalog.py, so only tracks and
    projects need seeding."""
    return seed_categories_and_projects(session)


def seed_first_admin(session):
    """
    Create one administrator the first time the app runs, so there is a way in.
    Returns (login_id, password) when it creates one, else None. The password
    must be changed at first sign-in.
    """
    import os

    from app import auth
    from app.models import User

    if session.scalar(select(User).limit(1)) is not None:
        return None

    login_id = (os.getenv("ADMIN_USERNAME") or "admin").strip().lower()
    password = os.getenv("ADMIN_PASSWORD") or auth.temporary_password()
    session.add(User(
        username=login_id,
        full_name=os.getenv("ADMIN_NAME") or "Administrator",
        role=auth.ADMIN,
        password_hash=auth.hash_password(password),
        must_change_password=True,
    ))
    session.commit()
    return login_id, password
