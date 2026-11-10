"""Optional semantic code search (FAISS).

* Disabled unless ``FAISS_ENABLED=true`` and the optional ``faiss`` extra is installed.
* Embeddings are computed **locally** with fastembed (ONNX); repository source is never sent
  to a hosted embedding provider by this adapter.
* Indexes are per repository *and* commit, versioned, persisted under ``FAISS_INDEX_PATH``.
* Results are filtered to the requesting repository/commit and then re-checked against the
  pinned tree by the investigator: similarity search never grants access to anything.
* When unavailable, the investigator falls back to deterministic search only.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.core.logging import get_logger
from app.security.input_sanitization import normalize_repo_path

logger = get_logger(__name__)

INDEX_VERSION = 1
CHUNK_LINES = 60
CHUNK_OVERLAP = 10
MAX_FILE_BYTES = 200_000
MAX_CHUNKS = 20_000
INDEXED_EXTENSIONS = {".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".rb", ".java", ".kt", ".rs", ".php",
                      ".cs", ".md"}
DEFAULT_EMBEDDING_MODEL = "BAAI/bge-small-en-v1.5"


def faiss_available() -> tuple[bool, str]:
    try:
        import faiss  # noqa: F401
        import fastembed  # noqa: F401
        import numpy  # noqa: F401
    except ImportError:
        return False, "Install the optional extra: pip install -e '.[faiss]'"
    return True, "faiss and fastembed are installed"


@dataclass
class Chunk:
    path: str
    start_line: int
    end_line: int
    text: str
    digest: str


def chunk_file(path: str, text: str) -> list[Chunk]:
    lines = text.splitlines()
    chunks: list[Chunk] = []
    step = CHUNK_LINES - CHUNK_OVERLAP
    for start in range(0, max(1, len(lines)), step):
        window = lines[start:start + CHUNK_LINES]
        if not any(line.strip() for line in window):
            continue
        body = "\n".join(window)
        chunks.append(Chunk(path, start + 1, start + len(window), body,
                            hashlib.sha256(body.encode()).hexdigest()[:16]))
        if start + CHUNK_LINES >= len(lines):
            break
    return chunks


def collect_chunks(root: Path, excluded: list[str] | None = None) -> list[Chunk]:
    from app.services.repository_service import is_sensitive

    seen: set[str] = set()
    chunks: list[Chunk] = []
    for file in sorted(root.rglob("*")):
        if not file.is_file() or file.is_symlink() or file.suffix not in INDEXED_EXTENSIONS:
            continue
        rel = file.relative_to(root).as_posix()
        if rel.startswith((".git/", "node_modules/", ".sandbox-")) or "/node_modules/" in rel:
            continue
        if is_sensitive(rel) or (excluded and rel in excluded) or file.stat().st_size > MAX_FILE_BYTES:
            continue
        try:
            text = file.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for chunk in chunk_file(rel, text):
            if chunk.digest in seen:  # dedupe repeated chunks (vendored copies, generated code)
                continue
            seen.add(chunk.digest)
            chunks.append(chunk)
            if len(chunks) >= MAX_CHUNKS:
                return chunks
    return chunks


class SemanticIndex:
    def __init__(self, base_path: Path, embedding_model: str | None) -> None:
        self.base_path = base_path
        self.model_name = embedding_model or DEFAULT_EMBEDDING_MODEL
        self._embedder: Any = None

    def _dir(self, repository_id: int, sha: str) -> Path:
        return self.base_path / f"repo-{int(repository_id)}" / sha[:40]

    def _embed(self, texts: list[str]) -> Any:
        import numpy as np
        from fastembed import TextEmbedding

        if self._embedder is None:
            self._embedder = TextEmbedding(model_name=self.model_name)
        vectors = np.array(list(self._embedder.embed(texts)), dtype="float32")
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        return vectors / np.maximum(norms, 1e-12)

    def exists(self, repository_id: int, sha: str) -> bool:
        meta = self._dir(repository_id, sha) / "meta.json"
        if not meta.is_file():
            return False
        try:
            data = json.loads(meta.read_text(encoding="utf-8"))
        except ValueError:
            return False
        return data.get("version") == INDEX_VERSION and data.get("model") == self.model_name

    def build(self, repository_id: int, sha: str, root: Path) -> int:
        import faiss

        chunks = collect_chunks(root)
        if not chunks:
            return 0
        vectors = self._embed([f"{c.path}\n{c.text}" for c in chunks])
        index = faiss.IndexFlatIP(vectors.shape[1])
        index.add(vectors)
        target = self._dir(repository_id, sha)
        target.mkdir(parents=True, exist_ok=True)
        faiss.write_index(index, str(target / "index.faiss"))
        (target / "meta.json").write_text(json.dumps({
            "version": INDEX_VERSION, "model": self.model_name, "repository_id": repository_id, "sha": sha,
            "chunks": [c.__dict__ for c in chunks],
        }), encoding="utf-8")
        return len(chunks)

    def delete(self, repository_id: int, sha: str | None = None) -> None:
        target = self._dir(repository_id, sha) if sha else self.base_path / f"repo-{int(repository_id)}"
        if target.exists() and target.resolve().is_relative_to(self.base_path.resolve()):
            shutil.rmtree(target)

    def search(self, repository_id: int, sha: str, query: str, k: int = 5,
               keyword_boost: list[str] | None = None) -> list[dict[str, Any]]:
        import faiss

        if not self.exists(repository_id, sha):
            return []
        target = self._dir(repository_id, sha)
        meta = json.loads((target / "meta.json").read_text(encoding="utf-8"))
        if meta.get("repository_id") != repository_id or meta.get("sha") != sha:
            return []
        index = faiss.read_index(str(target / "index.faiss"))
        scores, ids = index.search(self._embed([query]), min(k * 3, index.ntotal))
        results: list[dict[str, Any]] = []
        for score, idx in zip(scores[0], ids[0], strict=False):
            if idx < 0:
                continue
            chunk = meta["chunks"][int(idx)]
            try:
                normalize_repo_path(chunk["path"])
            except Exception:  # noqa: S112
                continue
            bonus = sum(0.05 for word in (keyword_boost or []) if word and word in chunk["text"])
            results.append({**chunk, "score": float(score) + bonus})
        results.sort(key=lambda r: r["score"], reverse=True)
        return results[:k]
