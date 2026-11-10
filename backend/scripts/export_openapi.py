"""Write the OpenAPI schema used to generate the frontend's TypeScript types."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from app.config import REPO_ROOT
from app.main import create_app


def main() -> None:
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else REPO_ROOT / "frontend" / "openapi.json"
    schema = create_app(start_workers=False).openapi()
    target.write_text(json.dumps(schema, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {target}")


if __name__ == "__main__":
    main()
