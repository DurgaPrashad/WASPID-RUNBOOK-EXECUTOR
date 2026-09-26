"""WASPID platform connector: the WASPID API (HTTP) and the WASPID database (Postgres).

Both tools are read-only and take no arguments: the endpoints come from the
environment, never from the model, so an agent can't point them anywhere else.

  WASPID_API_URL        default http://127.0.0.1:8080
  WASPID_DB_CONTAINER   default waspid-db   (probed with pg_isready + psql inside it)
  WASPID_DB_USER / WASPID_DB_NAME   default waspid / waspid
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Tuple

from waspid.mcp_server.safety import Risk, ToolSpec


class WaspidPlatform:
    TOOL_SPECS = {
        "waspid_api_health": ToolSpec("waspid_api_health", Risk.READ_ONLY,
                                      "HTTP health check of the WASPID API: status, version, latency"),
        "waspid_db_health": ToolSpec("waspid_db_health", Risk.READ_ONLY,
                                     "WASPID Postgres database: accepting connections, server version, "
                                     "size and active connections"),
    }

    def __init__(self, docker: Any, api_url: str = "", db_container: str = "") -> None:
        self._docker = docker
        self.api_url = (api_url or os.environ.get("WASPID_API_URL", "http://127.0.0.1:8080")).rstrip("/")
        self.db_container = db_container or os.environ.get("WASPID_DB_CONTAINER", "waspid-db")
        self.db_user = os.environ.get("WASPID_DB_USER", "waspid")
        self.db_name = os.environ.get("WASPID_DB_NAME", "waspid")

    def _fetch(self, path: str) -> Tuple[int, Dict[str, Any]]:
        req = urllib.request.Request(self.api_url + path, headers={"Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                code, raw = r.status, r.read()
        except urllib.error.HTTPError as e:
            code, raw = e.code, e.read()
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            body = {}
        return code, body if isinstance(body, dict) else {}

    def waspid_api_health(self) -> Dict[str, Any]:
        started = time.monotonic()
        out: Dict[str, Any] = {"url": f"{self.api_url}/health"}
        try:
            code, body = self._fetch("/health")
        except OSError as e:  # connection refused, DNS, timeout
            return {**out, "status": "down", "detail": str(e)}
        ok = code == 200 and body.get("status") == "ok"
        return {**out, "status": "ok" if ok else "down", "http_status": code, "version": body.get("version"),
                "latency_ms": round((time.monotonic() - started) * 1000, 1)}

    def waspid_db_health(self) -> Dict[str, Any]:
        return self._docker.postgres_status(self.db_container, self.db_user, self.db_name)

    def status(self) -> List[Dict[str, Any]]:
        api, db = self.waspid_api_health(), self.waspid_db_health()
        api_ok, db_ok = api["status"] == "ok", db["status"] == "ok"
        return [
            {"key": "api", "name": "WASPID API", "ok": api_ok,
             "detail": f"v{api['version']} · {api['latency_ms']} ms · {self.api_url}" if api_ok
             else api.get("detail") or f"HTTP {api.get('http_status')}"},
            {"key": "db", "name": "WASPID DB", "ok": db_ok,
             "detail": f"Postgres {db['server_version']} · {db['connections']} conn · {db['size_bytes'] / 1048576:.1f} MB"
             if db_ok else db.get("detail", "")},
        ]


class FakeWaspidPlatform(WaspidPlatform):
    """Same tools, answered from FakeDockerEngine state (tests / WASPID_FAKE_DOCKER=1)."""

    def __init__(self, docker: Any) -> None:
        super().__init__(docker, api_url="http://waspid-api.simulated")

    def _fetch(self, path: str) -> Tuple[int, Dict[str, Any]]:
        c = self._docker.containers.get("waspid-api")
        if not c or c["status"] != "running":
            raise ConnectionRefusedError(f"{self.api_url}{path}: connection refused")
        healthy = c["health"] == "healthy"
        return (200 if healthy else 503), {"status": "ok" if healthy else "unhealthy",
                                           "version": c["image"].rsplit(":", 1)[-1]}
