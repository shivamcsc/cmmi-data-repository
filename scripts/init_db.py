"""
Create the database, all tables, and the reference data. Safe to re-run.

    python scripts/init_db.py            create anything missing
    python scripts/init_db.py --reset    drop every table first (destroys data)
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import Base, SessionLocal, create_database_if_missing, engine
from app.seed import seed_all
import app.models  # noqa: F401  -- registers the tables on Base


def main():
    parser = argparse.ArgumentParser(description="Set up the CMMI database.")
    parser.add_argument("--reset", action="store_true",
                        help="drop all tables first -- this deletes every stored sheet")
    args = parser.parse_args()

    create_database_if_missing()
    if args.reset:
        confirm = input("This deletes all stored data. Type 'yes' to continue: ")
        if confirm.strip().lower() != "yes":
            print("cancelled")
            return
        Base.metadata.drop_all(engine)
        print("dropped all tables")
    Base.metadata.create_all(engine)
    print("tables ready: %s" % ", ".join(sorted(Base.metadata.tables)))
    session = SessionLocal()
    try:
        refs, metrics = seed_all(session)
        print("seeded %d categories/projects, %d metrics" % (refs, metrics))
    finally:
        session.close()


if __name__ == "__main__":
    main()
