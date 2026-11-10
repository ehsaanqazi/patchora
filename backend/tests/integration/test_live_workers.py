"""The API stays responsive while the in-process worker pool executes runs.

Regression test: a request holding an SQLite write lock while workers wrote on the event loop
used to deadlock the server. Requests must complete quickly while a run progresses.
"""

from __future__ import annotations

import time

from fastapi.testclient import TestClient

from app.main import create_app

H = {"X-Requested-With": "patchora"}


def test_requests_and_workers_progress_concurrently(settings):
    app = create_app(settings, start_workers=True, run_migrations=False)
    with TestClient(app) as client:
        assert client.get("/api/v1/ready").json()["checks"]["workers"]["running"] is True
        run_id = client.post("/api/v1/demo/runs", json={"scenario": "zero-division"}, headers=H).json()["run_id"]
        deadline = time.monotonic() + 60
        status = ""
        while time.monotonic() < deadline:
            started = time.monotonic()
            assert client.get("/api/v1/diagnostics").status_code == 200
            assert client.get(f"/api/v1/runs/{run_id}").status_code == 200
            assert time.monotonic() - started < 5, "API blocked by worker activity"
            status = client.get(f"/api/v1/runs/{run_id}").json()["status"]
            if status == "WAITING_FOR_APPROVAL":
                break
            time.sleep(0.2)
        assert status == "WAITING_FOR_APPROVAL"
