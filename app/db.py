"""Engine and session handling."""
from sqlalchemy import create_engine, text
from sqlalchemy.orm import declarative_base, sessionmaker

from app import config

Base = declarative_base()

engine = create_engine(
    config.database_url(),
    pool_pre_ping=True,
    pool_recycle=3600,
    future=True,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


def get_session():
    """FastAPI dependency yielding a session that always closes."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def create_database_if_missing():
    """Create the schema itself, connecting to the server without a database selected."""
    server = create_engine(config.database_url(include_db=False), future=True)
    with server.connect() as conn:
        conn.execute(
            text(
                "CREATE DATABASE IF NOT EXISTS `%s` "
                "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci" % config.DB_NAME
            )
        )
        conn.commit()
    server.dispose()
