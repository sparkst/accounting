"""REQ-HM-015: invoicing PDF/HTML deps must be DECLARED so a fresh
`pip install -e ".[dev]"` on the Hetzner box installs them."""
import tomllib
from pathlib import Path


def test_pyproject_declares_fpdf2_and_jinja2() -> None:
    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    deps = tomllib.loads(pyproject.read_text())["project"]["dependencies"]
    joined = " ".join(deps)
    assert "fpdf2" in joined, "fpdf2 must be declared (pdf_renderer imports `from fpdf import FPDF`)"
    assert "jinja2" in joined, "jinja2 must be declared (render_html needs it)"


def test_render_html_jinja2_is_available() -> None:
    from src.invoicing import pdf_renderer
    assert pdf_renderer._JINJA2_AVAILABLE is True
