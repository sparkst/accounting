"""Shared FastAPI dependencies.

Use ``Depends(get_db)`` in route functions to obtain a SQLAlchemy session that
is automatically closed after the response is sent (or on error). The session
is not committed automatically — routes must call ``session.commit()`` before
returning.

Example::

    from src.api.deps import get_db

    @router.get("/things")
    def list_things(session: Session = Depends(get_db)):
        return session.query(Thing).all()
"""

from collections.abc import Generator

from sqlalchemy.orm import Session

from src.db.connection import SessionLocal


def get_db() -> Generator[Session, None, None]:
    """Yield a database session and close it after the request completes.

    The session is never committed by this dependency — each route is
    responsible for calling ``session.commit()`` before returning.
    On exception the session is rolled back automatically when closed.
    """
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
