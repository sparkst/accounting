"""Tests for SHA-256 dedup utilities.

REQ-ID: DEDUP-001  compute_source_hash produces deterministic, unique hashes.
REQ-ID: DEDUP-002  compute_file_hash reflects file contents, not path.
"""

import hashlib
from pathlib import Path

import pytest

from src.utils.dedup import compute_file_hash, compute_source_hash


class TestComputeSourceHash:
    def test_returns_64_char_hex(self) -> None:
        h = compute_source_hash("gmail_n8n", "19578f6fd72939df")
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)

    def test_deterministic(self) -> None:
        h1 = compute_source_hash("gmail_n8n", "19578f6fd72939df")
        h2 = compute_source_hash("gmail_n8n", "19578f6fd72939df")
        assert h1 == h2

    def test_different_source_id_produces_different_hash(self) -> None:
        h1 = compute_source_hash("gmail_n8n", "id_aaa")
        h2 = compute_source_hash("gmail_n8n", "id_bbb")
        assert h1 != h2

    def test_different_source_type_produces_different_hash(self) -> None:
        h1 = compute_source_hash("gmail_n8n", "abc123")
        h2 = compute_source_hash("stripe", "abc123")
        assert h1 != h2

    def test_matches_manual_sha256(self) -> None:
        # Payload format: "<len(source_type)>:<source_type>:<source_id>"
        expected = hashlib.sha256(b"9:gmail_n8n:abc123").hexdigest()
        assert compute_source_hash("gmail_n8n", "abc123") == expected

    def test_colon_in_source_id_is_valid(self) -> None:
        # source IDs from Stripe look like "ch_abc:suffix" — must not collide
        h1 = compute_source_hash("stripe", "abc:def")
        h2 = compute_source_hash("stripe:abc", "def")
        # These two must differ because the separator is inside source_type.
        assert h1 != h2


class TestComputeFileHash:
    def test_returns_64_char_hex(self, tmp_path: Path) -> None:
        f = tmp_path / "sample.json"
        f.write_bytes(b'[{"id":"abc"}]')
        h = compute_file_hash(f)
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)

    def test_deterministic(self, tmp_path: Path) -> None:
        f = tmp_path / "sample.json"
        f.write_bytes(b'[{"id":"abc"}]')
        assert compute_file_hash(f) == compute_file_hash(f)

    def test_different_content_different_hash(self, tmp_path: Path) -> None:
        f1 = tmp_path / "a.json"
        f2 = tmp_path / "b.json"
        f1.write_bytes(b'[{"id":"aaa"}]')
        f2.write_bytes(b'[{"id":"bbb"}]')
        assert compute_file_hash(f1) != compute_file_hash(f2)

    def test_same_content_same_hash_regardless_of_filename(
        self, tmp_path: Path
    ) -> None:
        content = b'[{"id":"same"}]'
        f1 = tmp_path / "orig.json"
        f2 = tmp_path / "copy_renamed.json"
        f1.write_bytes(content)
        f2.write_bytes(content)
        assert compute_file_hash(f1) == compute_file_hash(f2)

    def test_matches_manual_sha256(self, tmp_path: Path) -> None:
        content = b'[{"id":"manual"}]'
        f = tmp_path / "test.json"
        f.write_bytes(content)
        expected = hashlib.sha256(content).hexdigest()
        assert compute_file_hash(f) == expected

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            compute_file_hash(tmp_path / "does_not_exist.json")

    def test_large_file_handled_in_chunks(self, tmp_path: Path) -> None:
        # Write >64 KB to exercise the chunk loop
        content = b"x" * (200 * 1024)
        f = tmp_path / "big.bin"
        f.write_bytes(content)
        expected = hashlib.sha256(content).hexdigest()
        assert compute_file_hash(f) == expected
