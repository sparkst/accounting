"""REQ-HM-011/015: every third-party package imported at runtime under src/
must be DECLARED in pyproject.toml so a fresh `pip install -e ".[dev]"` on the
Hetzner box installs it. These were present in the Mac venv (installed at some
point) but undeclared, so the box would have been missing them — breaking Stripe
income ingestion, invoice email/calendar parsing, xlsx imports, and bank CSV.
"""
import tomllib
from pathlib import Path

# Third-party packages imported by src/ that are NOT transitively guaranteed by
# the other declared deps. Discovered via a clean-venv `pytest --co` sweep.
_REQUIRED = [
    "fastapi",
    "uvicorn",
    "sqlalchemy",
    "anthropic",
    "pydantic",
    "httpx",
    "yfinance",
    "plaid-python",
    "cryptography",
    "fpdf2",
    "jinja2",
    "stripe",
    "openpyxl",
    "chardet",
    "icalendar",
    "resend",
    "alembic",
    "python-multipart",  # FastAPI File()/Form() routes (attachments, brokerage-csv upload)
]


def test_all_runtime_deps_declared():
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    deps = tomllib.loads(pyproject.read_text())["project"]["dependencies"]
    joined = " ".join(deps)
    missing = [pkg for pkg in _REQUIRED if pkg not in joined]
    assert not missing, f"undeclared runtime deps (box pip install would miss): {missing}"
