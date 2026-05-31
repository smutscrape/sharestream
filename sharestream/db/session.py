"""Database engine, session factory, and the ``get_db`` FastAPI dependency.

Routers obtain a session via ``db: Session = Depends(get_db)`` so the session
lifecycle (and eventual swap to a different pool/engine) is owned here rather
than scattered across routes.
"""
from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy import event as _sa_event
from sqlalchemy.orm import Session, sessionmaker

from sharestream.config import DATABASE_URL

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})


# We write (hit counters) on ordinary page views, so the default rollback-journal
# mode (single coarse lock, "database is locked" under concurrency) is the first
# thing that bites under real traffic. WAL lets readers and the writer proceed
# concurrently; busy_timeout makes brief write contention wait instead of erroring;
# synchronous=NORMAL is the safe, faster companion to WAL. Applied per-connection
# (journal_mode=WAL is persistent on the file; the rest are per-connection).
@_sa_event.listens_for(engine, "connect")
def _set_sqlite_pragmas(dbapi_conn, _connection_record):
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA busy_timeout=5000")
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.close()


SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db():
    """Yield a request-scoped SQLAlchemy session, always closed afterwards."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
