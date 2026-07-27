"""Root conftest — model registration + a stray shared-cache-file guard.

P2-005: every ``sqlite+pysqlite:///file:<name>?mode=memory&cache=shared&uri=true``
test module previously built its engine URL as
``"sqlite+pysqlite:///" + uri.replace("file:", "")`` — stripping the ``file:``
scheme meant sqlite3 (uri=True requires the ``file:`` prefix to recognize a
URI) silently fell back to treating the WHOLE query string as a literal
on-disk filename (e.g. a file named ``accounting_test?mode=memory&cache=shared``
in the repo root), rather than a true named in-memory shared-cache database.
Two concurrent ``pytest`` invocations in the same checkout then stepped on
the SAME physical file, and this hook's ``os.remove()`` sweep — run at
collection time — would yank the file out from under a still-running sibling
process, producing ``sqlite3.OperationalError: attempt to write a readonly
database`` failures that had nothing to do with the code under test.

That has been fixed at the source (every test module now keeps the ``file:``
scheme, so the databases are genuinely in-memory and never touch disk). This
hook no longer deletes anything — a destructive collection-time sweep is not
safe to run automatically when a second ``pytest`` process might be alive in
the same tree. It only WARNS if a stray file matching the old broken pattern
reappears, which would mean the URI-scheme bug regressed somewhere.
"""

import glob
import os
import sys

# Register every model on the shared Base BEFORE any test module runs its
# import-time create_all(). Without this, a module whose create_all() ran
# before some other module imported src.planning.models gets a shared-cache
# DB missing planning_runs, and its per-test cleanup (which iterates the
# now-larger Base.metadata) fails with "no such table: planning_runs".
import src.models  # noqa: E402, F401
import src.planning.models  # noqa: E402, F401


def pytest_configure(config):  # noqa: ARG001
    """Warn (do not delete) if a stray on-disk shared-cache file reappears.

    Deleting here is what caused P2-005's cross-run corruption — a second,
    concurrently-running pytest process could have that same file open. If
    this ever fires, the `file:` URI scheme was dropped again somewhere and
    needs a real fix at the engine-construction call site, not a cleanup hack.
    """
    root = os.path.dirname(__file__)
    stray: list[str] = []
    for pattern in ("*test*cache=shared*", "*test*mode=memory*"):
        stray.extend(glob.glob(os.path.join(root, pattern)))
    if stray:
        print(
            "WARNING: stray on-disk shared-cache test DB file(s) found — this "
            "means a test module dropped the 'file:' URI scheme again (P2-005): "
            f"{stray}",
            file=sys.stderr,
        )


def pytest_addoption(parser):  # noqa: ANN001, ANN201 — pytest hook signature
    """--golden-update: regenerate the classification golden fixture
    (src/classification/test_golden_oracle.py) instead of asserting against it."""
    parser.addoption(
        "--golden-update",
        action="store_true",
        default=False,
        help="Rewrite golden fixtures from current classifier output.",
    )
