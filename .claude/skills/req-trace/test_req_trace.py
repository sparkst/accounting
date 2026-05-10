"""Tests for req-trace skill."""
from __future__ import annotations

from pathlib import Path

import pytest
from req_trace import build_matrix, format_matrix, main


def write(p: Path, body: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """A miniature requirements/src/test tree.

    REQ-001: has both impl and test references — covered
    REQ-002: has impl only — orphan impl (no tests)
    REQ-003: has test only — orphan test (no impl)
    REQ-004: declared, mentioned nowhere else — bare
    """
    write(
        tmp_path / "requirements" / "current.md",
        "## REQ-001: Foo\n## REQ-002: Bar\n## REQ-003: Baz\n## REQ-004: Quux\n",
    )
    write(
        tmp_path / "src" / "alpha" / "feature.py",
        "# implements REQ-001 and REQ-002\n",
    )
    write(
        tmp_path / "src" / "alpha" / "test_feature.py",
        "# tests REQ-001 and REQ-003\n",
    )
    return tmp_path


def test_matrix_keys_match_declared_reqs(tree: Path) -> None:
    matrix = build_matrix(tree / "requirements" / "current.md", tree / "src")
    assert set(matrix.keys()) == {"REQ-001", "REQ-002", "REQ-003", "REQ-004"}


def test_matrix_classifies_each_req(tree: Path) -> None:
    m = build_matrix(tree / "requirements" / "current.md", tree / "src")
    assert m["REQ-001"]["tests"] and m["REQ-001"]["impl"]
    assert not m["REQ-002"]["tests"] and m["REQ-002"]["impl"]
    assert m["REQ-003"]["tests"] and not m["REQ-003"]["impl"]
    assert not m["REQ-004"]["tests"] and not m["REQ-004"]["impl"]


def test_test_files_partition_correctly(tree: Path) -> None:
    """test_*.py files appear in tests bucket and never in impl bucket."""
    m = build_matrix(tree / "requirements" / "current.md", tree / "src")
    test_path = "test_feature.py"
    feature_path = "feature.py"
    assert any(test_path in f for f in m["REQ-001"]["tests"])
    assert any(test_path in f for f in m["REQ-003"]["tests"])
    assert all(test_path not in f for f in m["REQ-001"]["impl"])
    assert all(test_path not in f for f in m["REQ-003"]["impl"])
    assert any(feature_path in f and test_path not in f for f in m["REQ-001"]["impl"])


def test_subrequirements_are_detected(tmp_path: Path) -> None:
    write(tmp_path / "requirements" / "current.md", "### REQ-005a: Sub\n### REQ-005b: Sub2\n")
    write(tmp_path / "src" / "x.py", "# REQ-005a\n")
    m = build_matrix(tmp_path / "requirements" / "current.md", tmp_path / "src")
    assert "REQ-005a" in m and "REQ-005b" in m


def test_parent_with_subreqs_is_marked_as_parent(tmp_path: Path) -> None:
    """REQ-005 (heading) with REQ-005a/b sub-headings should not show as 'bare'.

    The parent is a structural heading; coverage lives on the sub-reqs.
    """
    write(
        tmp_path / "requirements" / "current.md",
        "## REQ-005: Brokerage\n### REQ-005a: A\n### REQ-005b: B\n",
    )
    write(tmp_path / "src" / "x.py", "# REQ-005a\n")
    write(tmp_path / "src" / "test_x.py", "# REQ-005a\n")
    m = build_matrix(tmp_path / "requirements" / "current.md", tmp_path / "src")
    out = format_matrix(m)
    # Parent heading should be flagged as a parent, not as 'bare'
    assert "REQ-005 — parent" in out
    # Sub-req should still classify normally
    assert "REQ-005a — covered" in out


def test_parent_label_persists_when_parent_has_direct_mentions(tmp_path: Path) -> None:
    """If a parent REQ is cited directly AND has sub-reqs, it stays labeled as parent.

    Real-world case: src/db/test_brokerage_migration.py mentions REQ-005 directly
    while REQ-005a/b/... carry the sub-req coverage.
    """
    write(
        tmp_path / "requirements" / "current.md",
        "## REQ-005: Brokerage\n### REQ-005a: A\n",
    )
    write(tmp_path / "src" / "test_migration.py", "# verifies REQ-005 schema\n")
    write(tmp_path / "src" / "feature.py", "# implements REQ-005a\n")
    m = build_matrix(tmp_path / "requirements" / "current.md", tmp_path / "src")
    out = format_matrix(m)
    assert "REQ-005 — parent" in out
    # Direct mention should still surface for traceability
    assert "test_migration.py" in out


def test_format_matrix_flags_each_state(tree: Path) -> None:
    m = build_matrix(tree / "requirements" / "current.md", tree / "src")
    out = format_matrix(m)
    for rid in ("REQ-001", "REQ-002", "REQ-003", "REQ-004"):
        assert rid in out
    assert "covered" in out.lower()
    assert "no tests" in out.lower()
    assert "no impl" in out.lower()
    assert "bare" in out.lower()


def test_format_matrix_renders_files_as_markdown_bullets(tree: Path) -> None:
    """File lists should not appear as Python repr."""
    m = build_matrix(tree / "requirements" / "current.md", tree / "src")
    out = format_matrix(m)
    assert "['" not in out
    assert "', '" not in out
    # File paths should appear inside backticks as bullet items
    assert "  - `" in out


def test_file_lists_are_sorted_for_stable_output(tmp_path: Path) -> None:
    write(tmp_path / "requirements" / "current.md", "## REQ-100: Multi\n")
    write(tmp_path / "src" / "z.py", "# REQ-100\n")
    write(tmp_path / "src" / "a.py", "# REQ-100\n")
    write(tmp_path / "src" / "m.py", "# REQ-100\n")
    m = build_matrix(tmp_path / "requirements" / "current.md", tmp_path / "src")
    impl = m["REQ-100"]["impl"]
    assert impl == sorted(impl)


def test_non_utf8_requirements_file_does_not_crash(tmp_path: Path) -> None:
    """Latin-1 bytes in requirements should not raise; errors='replace' protects us."""
    req = tmp_path / "requirements" / "current.md"
    req.parent.mkdir(parents=True)
    req.write_bytes(b"## REQ-001: \xe9\xe8\n## REQ-002: ok\n")
    (tmp_path / "src").mkdir()
    m = build_matrix(req, tmp_path / "src")
    assert "REQ-001" in m and "REQ-002" in m


def test_main_returns_2_when_requirements_missing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    src = tmp_path / "src"
    src.mkdir()
    rc = main(["req_trace.py", "--requirements", str(tmp_path / "nope.md"), "--src", str(src)])
    assert rc == 2
    captured = capsys.readouterr()
    assert "not found" in captured.err


def test_main_returns_2_when_src_missing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    req = tmp_path / "requirements" / "current.md"
    req.parent.mkdir(parents=True)
    req.write_text("## REQ-001: x\n")
    rc = main(["req_trace.py", "--requirements", str(req), "--src", str(tmp_path / "nope")])
    assert rc == 2
    captured = capsys.readouterr()
    assert "not found" in captured.err


def test_main_happy_path_prints_matrix(tree: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = main([
        "req_trace.py",
        "--requirements", str(tree / "requirements" / "current.md"),
        "--src", str(tree / "src"),
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "REQ Coverage Matrix" in out
    assert "REQ-001" in out
