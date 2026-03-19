"""Root conftest — clean up stale shared-cache test database files.

SQLite shared-cache URI test databases (e.g. ``file:accounting_test?mode=memory&cache=shared``)
are accidentally created as file-backed databases by ``sqlite+pysqlite:///`` URLs.
When the Transaction model schema changes (new columns), these stale files cause
``OperationalError: table transactions has no column named ...`` because SQLite's
``CREATE TABLE IF NOT EXISTS`` does not add new columns to existing tables.

This hook deletes any such files before test collection begins so that
``Base.metadata.create_all()`` in each test module creates fresh tables
with the current schema.
"""

import glob
import os


def pytest_configure(config):  # noqa: ARG001
    """Delete leftover shared-cache test database files before test collection."""
    root = os.path.dirname(__file__)
    for pattern in ("*test*cache=shared*", "*test*mode=memory*"):
        for path in glob.glob(os.path.join(root, pattern)):
            try:
                os.remove(path)
            except OSError:
                pass
