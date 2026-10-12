# Database engine, session and the advice_visible_to_farmer view (see docs/API_CONTRACT.md).
from __future__ import annotations

from pathlib import Path
from typing import Iterator

from sqlmodel import Session, SQLModel, create_engine

from backend.core.config import settings

if settings.database_url.startswith("sqlite"):
    db_path = settings.database_url.split("///")[-1]
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)

connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, echo=False, connect_args=connect_args)


def init_db() -> None:
    """Create tables and the farmer-visible advice view. Safe to call every start."""
    import backend.db.models  # noqa: F401  (register models with SQLModel.metadata)

    SQLModel.metadata.create_all(engine)
    with engine.begin() as conn:
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
