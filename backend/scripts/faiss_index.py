"""Build or delete the optional FAISS index for a repository revision.

    python backend/scripts/faiss_index.py build --repository-id 1 --sha <commit>
    python backend/scripts/faiss_index.py delete --repository-id 1 [--sha <commit>]

Embeddings are computed locally (fastembed). Requires: pip install -e ".[faiss]".
"""

from __future__ import annotations

import argparse
import asyncio
import tempfile
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.db.migrate import upgrade_to_head
from app.db.models import Repository
from app.db.session import init_engine, session_scope
from app.providers.factory import ProviderFactory
from app.security.authorization import ensure_repository_allowed
from app.services.search_service import SemanticIndex, faiss_available


async def build(repository_id: int, sha: str) -> None:
    settings = get_settings()
    factory = ProviderFactory(settings)
    host: Any
    with session_scope() as session:
        repo = ensure_repository_allowed(session.get(Repository, repository_id))
        if repo.is_demo:
            from app.demo.providers import DemoCodeHost

            host = DemoCodeHost()
        else:
            host = factory.github(session, repo)
        full_name = repo.full_name
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "src"
        await host.download_archive(full_name, sha, root)
        count = SemanticIndex(settings.faiss_index_path, settings.embedding_model).build(repository_id, sha, root)
    print(f"Indexed {count} chunks for {full_name}@{sha[:12]}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["build", "delete"])
    parser.add_argument("--repository-id", type=int, required=True)
    parser.add_argument("--sha")
    args = parser.parse_args()
    ok, reason = faiss_available()
    if not ok:
        raise SystemExit(reason)
    settings = get_settings()
    upgrade_to_head(settings.database_url)
    init_engine(settings.database_url)
    if args.action == "build":
        if not args.sha:
            raise SystemExit("--sha is required for build")
        asyncio.run(build(args.repository_id, args.sha))
    else:
        SemanticIndex(settings.faiss_index_path, settings.embedding_model).delete(args.repository_id, args.sha)
        print("Index deleted")


if __name__ == "__main__":
    main()
