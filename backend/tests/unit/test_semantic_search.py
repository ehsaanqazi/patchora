"""Optional FAISS semantic search. Skipped when the 'faiss' extra is not installed."""

from __future__ import annotations

import hashlib
import shutil

import pytest

from app.demo.providers import FIXTURE_ROOT
from app.services.search_service import SemanticIndex, chunk_file, collect_chunks, faiss_available

pytestmark = pytest.mark.faiss
if not faiss_available()[0]:
    pytest.skip("optional faiss extra not installed", allow_module_level=True)

np = pytest.importorskip("numpy")


class HashingIndex(SemanticIndex):
    """Deterministic bag-of-words embeddings so the index logic is tested offline."""

    def _embed(self, texts):  # type: ignore[override]
        vectors = np.zeros((len(texts), 256), dtype="float32")
        for row, text in enumerate(texts):
            for word in text.replace("(", " ").replace(")", " ").replace(".", " ").split():
                vectors[row, int(hashlib.md5(word.encode()).hexdigest(), 16) % 256] += 1.0  # noqa: S324
        return vectors / np.maximum(np.linalg.norm(vectors, axis=1, keepdims=True), 1e-12)


def test_chunking_preserves_line_ranges_and_dedupes(tmp_path):
    chunks = chunk_file("a.py", "\n".join(f"line {i}" for i in range(1, 131)))
    assert chunks[0].start_line == 1 and chunks[0].end_line == 60 and chunks[1].start_line == 51
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "a.py").write_text("def f():\n    return 1\n")
    (root / "pkg" / "b.py").write_text("def f():\n    return 1\n")  # duplicate content
    (root / ".env").write_text("SECRET=1\n")
    found = collect_chunks(root)
    assert len(found) == 1 and found[0].path in {"pkg/a.py", "pkg/b.py"}


def test_index_build_search_filter_and_delete(tmp_path):
    index = HashingIndex(tmp_path / "idx", "hashing-test")
    sha = "a" * 40
    assert index.build(1, sha, FIXTURE_ROOT) > 0
    assert index.exists(1, sha)
    hits = index.search(1, sha, "average_order_value orders len division", k=3)
    assert "shop/pricing.py" in [h["path"] for h in hits]  # toy embedder: membership, not rank
    assert all("start_line" in h and "end_line" in h for h in hits)
    # Results are scoped to the repository and commit that was indexed.
    assert index.search(2, sha, "average_order_value") == []
    assert index.search(1, "b" * 40, "average_order_value") == []
    # A different embedding model invalidates the index (versioning).
    assert not HashingIndex(tmp_path / "idx", "other-model").exists(1, sha)
    index.delete(1, sha)
    assert not index.exists(1, sha)


def test_real_local_embedding_model(tmp_path):
    """Uses fastembed's default local model (downloaded once). Skipped if it cannot be fetched."""
    index = SemanticIndex(tmp_path / "idx", None)
    root = tmp_path / "repo"
    shutil.copytree(FIXTURE_ROOT, root)
    try:
        count = index.build(1, "c" * 40, root)
    except Exception as exc:  # offline machine
        pytest.skip(f"embedding model unavailable: {type(exc).__name__}")
    assert count > 0
    hits = index.search(1, "c" * 40, "ZeroDivisionError dividing the order total by the number of orders", k=2)
    assert hits[0]["path"] == "shop/pricing.py"
