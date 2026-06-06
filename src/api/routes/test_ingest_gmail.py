"""REQ-HM-019: POST /api/ingest/gmail.

n8n uploads a Gmail receipt (one JSON object + optional attachment binaries) to
the box. The endpoint stages them into the gmail drop dir in the exact layout
the GmailN8nAdapter expects (``<id>.json`` holding a one-element array, plus
``<id>_<name>`` attachment siblings), then triggers a gmail ingest pass.

Covers auth, JSON-array wrapping, single-object-or-array acceptance, attachment
staging, path-traversal hardening (id + attachment filename), and that the
ingest pass is invoked for the gmail source.
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

import src.api.routes.ingest as ingest_mod

_AK = "a" * 32
_IK = "i" * 32

RECEIPT = {
    "id": "19578f6fd72939df",
    "filename": "2025-03-09_Anthropic_19578f6fd72939df",
    "date": "2025-03-09T03:33:26.000Z",
    "from": "Anthropic, PBC <invoice@mail.anthropic.com>",
    "subject": "Your receipt #2355-2148",
    "body_text": "Total $20.00",
    "body_html": "<p>Total $20.00</p>",
}


def _client(monkeypatch, staging: Path) -> TestClient:
    monkeypatch.setenv("API_KEY", _AK)
    monkeypatch.setenv("INGEST_API_KEY", _IK)
    monkeypatch.setenv("GMAIL_N8N_DIRS", str(staging))
    from src.api.main import app

    return TestClient(app)


def _stub_ingest(monkeypatch) -> dict:
    """Replace the real ingest pass with a hermetic stub (no classifier/LLM)."""
    summary = ingest_mod.IngestSummary(
        ingested_count=1,
        classified_count=1,
        needs_review_count=1,
        adapter_results=[],
        warnings=[],
        errors=[],
    )
    called: dict = {}

    def _fake(source):  # noqa: ANN001, ANN202
        called["source"] = source
        return summary

    monkeypatch.setattr(ingest_mod, "_run_ingest_locked", _fake)
    return called


def test_requires_auth(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    r = c.post("/api/ingest/gmail", data={"receipt": json.dumps(RECEIPT)})
    assert r.status_code == 401


def test_stages_receipt_and_attachment_and_ingests(monkeypatch, tmp_path):
    called = _stub_ingest(monkeypatch)
    c = _client(monkeypatch, tmp_path)
    r = c.post(
        "/api/ingest/gmail",
        headers={"X-Api-Key": _IK},
        data={"receipt": json.dumps(RECEIPT)},
        files=[("attachments", ("Receipt-2355.pdf", b"%PDF-1.4 fake", "application/pdf"))],
    )
    assert r.status_code == 200, r.text
    jpath = tmp_path / "19578f6fd72939df.json"
    assert jpath.exists()
    arr = json.loads(jpath.read_text())
    assert isinstance(arr, list) and len(arr) == 1
    assert arr[0]["id"] == "19578f6fd72939df"
    assert (tmp_path / "19578f6fd72939df_Receipt-2355.pdf").read_bytes() == b"%PDF-1.4 fake"
    assert called["source"] == ingest_mod.Source.GMAIL_N8N
    body = r.json()
    assert body["receipt_id"] == "19578f6fd72939df"
    assert body["attachments_staged"] == 1
    assert body["ingest"]["ingested_count"] == 1


def test_accepts_single_object_or_array(monkeypatch, tmp_path):
    _stub_ingest(monkeypatch)
    c = _client(monkeypatch, tmp_path)
    r = c.post(
        "/api/ingest/gmail",
        headers={"X-Api-Key": _IK},
        data={"receipt": json.dumps([RECEIPT])},
    )
    assert r.status_code == 200, r.text
    arr = json.loads((tmp_path / "19578f6fd72939df.json").read_text())
    assert len(arr) == 1


def test_rejects_path_traversal_in_id(monkeypatch, tmp_path):
    _stub_ingest(monkeypatch)
    c = _client(monkeypatch, tmp_path)
    bad = {**RECEIPT, "id": "../../../etc/evil"}
    r = c.post("/api/ingest/gmail", headers={"X-Api-Key": _IK}, data={"receipt": json.dumps(bad)})
    assert r.status_code == 422
    assert not list(tmp_path.glob("*.json"))


def test_attachment_filename_is_basename_only(monkeypatch, tmp_path):
    _stub_ingest(monkeypatch)
    c = _client(monkeypatch, tmp_path)
    r = c.post(
        "/api/ingest/gmail",
        headers={"X-Api-Key": _IK},
        data={"receipt": json.dumps(RECEIPT)},
        files=[("attachments", ("../../evil.pdf", b"x", "application/pdf"))],
    )
    assert r.status_code == 200, r.text
    assert (tmp_path / "19578f6fd72939df_evil.pdf").exists()
    assert not (tmp_path.parent / "evil.pdf").exists()


def test_missing_id_rejected(monkeypatch, tmp_path):
    _stub_ingest(monkeypatch)
    c = _client(monkeypatch, tmp_path)
    bad = {k: v for k, v in RECEIPT.items() if k != "id"}
    r = c.post("/api/ingest/gmail", headers={"X-Api-Key": _IK}, data={"receipt": json.dumps(bad)})
    assert r.status_code == 422


def test_malformed_receipt_json_rejected(monkeypatch, tmp_path):
    _stub_ingest(monkeypatch)
    c = _client(monkeypatch, tmp_path)
    r = c.post("/api/ingest/gmail", headers={"X-Api-Key": _IK}, data={"receipt": "{not json"})
    assert r.status_code == 422
