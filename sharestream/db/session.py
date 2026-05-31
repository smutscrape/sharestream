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

# Pool sizing matters because routes that proxy media return a StreamingResponse,
# and a request's DB connection isn't released until that response *finishes* —
# i.e. for the whole duration of a video download. Even though we now release the
# session before streaming begins (the media routes use short SessionLocal()
# blocks), a generous, fast-failing pool is the safety net: pool_timeout=10 fails
# a starved request quickly instead of stalling 30s behind Cloudflare's edge
# timeout (which surfaces as a 524). SQLite in WAL mode handles many concurrent
# connections fine, so a larger pool is safe here. pool_pre_ping drops dead
# connections; pool_recycle keeps them fresh.
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
    pool_size=20,
    max_overflow=40,
    pool_timeout=10,
    pool_recycle=1800,
    pool_pre_ping=True,
)


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
