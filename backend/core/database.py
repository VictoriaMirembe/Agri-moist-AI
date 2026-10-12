# Database engine, session and the advice_visible_to_farmer view (see docs/API_CONTRACT.md).
from __future__ import annotations

from pathlib import Path
from typing import Iterator

from sqlalchemy import inspect
from sqlmodel import Session, SQLModel, create_engine

from backend.core.config import settings
from backend.db.models import Officer

if settings.database_url.startswith("sqlite"):
    db_path = settings.database_url.split("///")[-1]
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)

connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, echo=False, connect_args=connect_args)


def init_db() -> None:
    """Create tables and the farmer-visible advice view. Safe to call every start."""
    SQLModel.metadata.create_all(engine)
    with engine.begin() as conn:
        officer_columns = {column["name"] for column in inspect(conn).get_columns(Officer.__tablename__)}
        if "login_username" not in officer_columns:
            conn.exec_driver_sql(f"ALTER TABLE {Officer.__tablename__} ADD COLUMN login_username VARCHAR")
        if "password_hash" not in officer_columns:
            conn.exec_driver_sql(f"ALTER TABLE {Officer.__tablename__} ADD COLUMN password_hash VARCHAR")
        conn.exec_driver_sql(
            """
            CREATE VIEW IF NOT EXISTS advice_visible_to_farmer AS
            SELECT * FROM advice WHERE status IN ('approved', 'edited_and_approved')
            """
        )


def get_session() -> Iterator[Session]:
    with Session(engine) as session:
        yield session


def session_scope() -> Session:
    """For code outside FastAPI's dependency injection (the agent, seed script)."""
    return Session(engine)
