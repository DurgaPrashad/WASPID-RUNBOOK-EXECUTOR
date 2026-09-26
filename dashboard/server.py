"""WASPID operator dashboard — live backend (stdlib HTTP + Server-Sent Events).

Drives the runbook engine against the real Docker daemon (or the in-memory fake
with WASPID_FAKE_DOCKER=1) and streams every state change to the browser.

Anyone who can reach the page gets a read-only live view. Start / approve /
reject / reset require the operator token (header X-Operator-Token). Set
WASPID_OPERATOR_TOKEN, or one is generated at startup and printed as a sign-in
link. Approvals must name the exact approval_id the operator is looking at.

Run:  python dashboard/server.py            (real Docker)
      WASPID_FAKE_DOCKER=1 python dashboard/server.py
"""
from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

import sys as _sys
from pathlib import Path as _P
_sys.path.insert(0, str(_P(__file__).resolve().parent.parent))
import _bootstrap  # noqa: E402,F401 — makes `waspid.*` importable

from waspid.engine.audit import AuditLog  # noqa: E402
from waspid.engine.runbook import RunbookEngine, load_runbook  # noqa: E402
from waspid.mcp_server.docker_engine import FakeDockerEngine, RealDockerEngine  # noqa: E402
from waspid.mcp_server.safety import ApprovalGate  # noqa: E402
from waspid.mcp_server.tools import build_tools  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
INDEX = Path(__file__).resolve().parent / "index.html"
AUDIT_DIR = ROOT / "audit"
HOST = os.environ.get("WASPID_DASHBOARD_HOST", "127.0.0.1")
PORT = int(os.environ.get("WASPID_DASHBOARD_PORT", "8787"))
TOKEN_FROM_ENV = bool(os.environ.get("WASPID_OPERATOR_TOKEN"))
OPERATOR_TOKEN = os.environ.get("WASPID_OPERATOR_TOKEN") or secrets.token_urlsafe(18)
MAX_STREAMS = 64
RESULT_PREVIEW_CHARS = 8000
STATS_HISTORY = 40

# The cleanup step removes this "previous release" image; it is rebuilt from the
# demo API Dockerfile whenever it's missing so the runbook can be replayed.
OBSOLETE_IMAGE = ("waspid/api:1.4.1", ROOT / "demo_infra" / "api", {"WASPID_VERSION": "1.4.1"})
SANDBOX_IMAGE = "python:3.12-slim"
ROLE_ORDER = {"api": 0, "worker": 1, "db": 2}


def _runbook_file() -> Path:
    name = os.environ.get("WASPID_RUNBOOK")
    if name:
        return ROOT / "runbooks" / name
    try:
        import yaml  # noqa: F401
        return ROOT / "runbooks" / "production_deployment.yaml"
    except ImportError:
        return ROOT / "runbooks" / "production_deployment.json"


RUNBOOK_FILE = _runbook_file()


def _parse_time(value: Any) -> Optional[float]:
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str) or value.startswith("0001-"):
        return None
    try:
        value = re.sub(r"(\.\d{6})\d+", r"\1", value).replace("Z", "+00:00")
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return None


def _preview(result: Any) -> Any:
    if result is None:
        return None
    text = result if isinstance(result, str) else json.dumps(result, default=str)
    if len(text) <= RESULT_PREVIEW_CHARS:
        return result
    return text[:RESULT_PREVIEW_CHARS] + "\n… (truncated)"


class AppState:
    """Engine + Docker backend + cached container telemetry, behind one lock."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        if os.environ.get("WASPID_FAKE_DOCKER") == "1":
            self.docker, self.mode = FakeDockerEngine(), "simulated"
        else:
            self.docker, self.mode = RealDockerEngine(), "live"
        self.engine_info = self.docker.describe()
        self.runbook_source = RUNBOOK_FILE.read_text()
        self.containers: List[Dict[str, Any]] = []
        self.containers_at: Optional[float] = None
        self.docker_error: Optional[str] = None
        self.stats: Dict[str, Dict[str, Any]] = {}
        self.history: Dict[str, Dict[str, deque]] = {}
        self.preparing: Optional[str] = None
        self._new_run()
        self._start_prepare()
        threading.Thread(target=self._poll_containers, daemon=True).start()
        if self.mode == "live":
            threading.Thread(target=self._poll_stats, daemon=True).start()

    # ---- run lifecycle ---------------------------------------------------
    def _new_run(self) -> None:
        self.gate = ApprovalGate()
        self.tools = build_tools(self.docker, self.gate)
        self.runbook = load_runbook(RUNBOOK_FILE)
        self.run_id: Optional[str] = None
        self.audit = AuditLog("pending")
        self.engine = RunbookEngine(self.runbook, self.tools, self.gate, self.audit)
        self.status = "idle"
        self.busy = False
        self.started_at: Optional[float] = None
        self.finished_at: Optional[float] = None

    def _start_prepare(self) -> None:
        if self.mode != "live":
            return
        self.preparing = "checking images"  # set before the thread so Start can't race it
        threading.Thread(target=self._prepare_environment, daemon=True).start()

    def _prepare_environment(self) -> None:
        """Live mode: make sure the sandbox image and the obsolete release exist."""
        try:
            if not self.docker.image_exists(SANDBOX_IMAGE):
                with self.lock:
                    self.preparing = f"pulling sandbox image {SANDBOX_IMAGE}"
                self.docker.pull_image(SANDBOX_IMAGE)
            tag, path, args = OBSOLETE_IMAGE
            if not self.docker.image_exists(tag):
                with self.lock:
                    self.preparing = f"building previous release {tag}"
                self.docker.build_image(str(path), tag, args)
                print(f"[waspid] built {tag} (previous release for the cleanup step)", flush=True)
        except Exception as e:  # noqa: BLE001 — surfaced in the UI
            print(f"[waspid] environment preparation failed: {e}", flush=True)
        finally:
            with self.lock:
                self.preparing = None

    def _run_bg(self, fn) -> None:
        self.busy = True
        self.status = "running"

        def worker() -> None:
            result = "failed"
            try:
                result = fn()
            finally:
                with self.lock:
                    self.status = result
                    self.busy = False
                    if result in ("completed", "rejected", "failed", "stopped"):
                        self.finished_at = time.time()

        threading.Thread(target=worker, daemon=True).start()

    def start(self) -> None:
        with self.lock:
            if self.busy or self.status != "idle":
                raise ValueError("A run has already been started — reset first.")
            if self.preparing:
                raise ValueError(f"Environment is still preparing ({self.preparing}).")
            self.run_id = time.strftime("run_%Y%m%d_%H%M%S", time.gmtime()) + f"_{secrets.token_hex(2)}"
            AUDIT_DIR.mkdir(exist_ok=True)
            self.audit.run_id = self.run_id
            self.audit.path = AUDIT_DIR / f"{self.run_id}.jsonl"
            self.started_at = time.time()
            self._run_bg(self.engine.run)

    def _pending_step(self, approval_id: str):
        step = self.engine.pending_approval_step
        if self.busy or step is None:
            raise ValueError("No step is waiting for approval.")
        if not approval_id or not hmac.compare_digest(step.approval_id, approval_id):
            raise ValueError("Stale approval card — refresh and review the current request.")
        return step

    def approve(self, approval_id: str, who: str) -> None:
        with self.lock:
            self._pending_step(approval_id)
            self._run_bg(lambda: self.engine.approve(decided_by=who))

    def reject(self, approval_id: str, who: str) -> None:
        with self.lock:
            self._pending_step(approval_id)
            self._run_bg(lambda: self.engine.reject(decided_by=who))

    def reset(self, who: str) -> None:
        with self.lock:
            if self.busy:
                raise ValueError("A step is executing — wait for it to finish.")
            if self.engine.pending_approval_step is not None:
                # Abandoning a pending destructive step is a rejection, and is audited as one.
                self.engine.reject(decided_by=f"{who} (reset)")
            self._new_run()
            self._start_prepare()

    # ---- telemetry -------------------------------------------------------
    def _poll_containers(self) -> None:
        while True:
            try:
                rows = []
                for c in self.docker.list_containers():
                    name = c.get("name") or ""
                    if not name.startswith("waspid-"):
                        continue  # never expose unrelated containers on this host
                    labels = c.get("labels") or {}
                    row = {"name": name, "image": c.get("image"), "status": c.get("status"),
                           "id": c.get("id"),
                           "role": labels.get("waspid.role") or name.split("-", 1)[-1],
                           "sandbox": labels.get("waspid.sandbox") == "true" or name.startswith("waspid-sandbox"),
                           "health": "none"}
                    try:
                        row.update({k: v for k, v in self.docker.container_health(name).items() if k != "name"})
                        info = self.docker.inspect_container(name)
                        row["started_at"] = _parse_time(info.get("started_at"))
                        row["restart_count"] = info.get("restart_count", 0)
                    except Exception:  # noqa: BLE001 — container may vanish mid-poll
                        pass
                    rows.append(row)
                rows.sort(key=lambda r: (r["sandbox"], ROLE_ORDER.get(r["role"], 9), r["name"]))
                with self.lock:
                    for r in rows:
                        r["stats"] = self.stats.get(r["name"])
                        h = self.history.get(r["name"])
                        r["history"] = {k: list(v) for k, v in h.items()} if h else None
                    self.containers, self.containers_at, self.docker_error = rows, time.time(), None
            except Exception as e:  # noqa: BLE001
                with self.lock:
                    self.docker_error = str(e)
            time.sleep(1.5)

    def _poll_stats(self) -> None:
        pool = ThreadPoolExecutor(max_workers=4)
        while True:
            with self.lock:
                names = [c["name"] for c in self.containers
                         if c.get("status") == "running" and not c.get("sandbox")]

            def one(name: str):
                try:
                    return name, self.docker.container_stats(name)
                except Exception:  # noqa: BLE001
                    return name, None

            for name, st in pool.map(one, names):
                if st is None:
                    continue
                with self.lock:
                    self.stats[name] = st
                    h = self.history.setdefault(name, {"cpu": deque(maxlen=STATS_HISTORY),
                                                       "mem": deque(maxlen=STATS_HISTORY)})
                    h["cpu"].append(st["cpu_percent"])
                    h["mem"].append(st["mem_bytes"])
            time.sleep(2)

    # ---- views -----------------------------------------------------------
    def _metrics(self) -> Dict[str, Any]:
        reqs = self.gate.all()
        steps = self.runbook.steps
        return {
            "steps_total": len(steps),
            "steps_done": sum(s.status.value == "successful" for s in steps),
            "destructive_gated": len(reqs),
            "approved": sum(r.status == "approved" for r in reqs),
            "rejected": sum(r.status == "rejected" for r in reqs),
            "tokens_consumed": sum(r.consumed for r in reqs),
            "sandbox_runs": sum(s.action == "run_sandbox" and s.started_at is not None for s in steps),
        }

    def snapshot(self) -> Dict[str, Any]:
        with self.lock:
            pending = None
            step = self.engine.pending_approval_step
            if step is not None and step.approval_id:
                req = self.gate.get(step.approval_id)
                pending = {**req.to_card(), "step": step.id, "requested_at": req.created_at,
                           "fingerprint": req.fingerprint()}
            timeline = self.engine.timeline()
            for t, s in zip(timeline, self.runbook.steps):
                t["result"] = _preview(s.result)
            return {
                "mode": self.mode,
                "engine": self.engine_info,
                "docker_error": self.docker_error,
                "preparing": self.preparing,
                "runbook": {"name": self.runbook.name, "file": RUNBOOK_FILE.name},
                "run": {"id": self.run_id, "status": self.status, "busy": self.busy,
                        "started_at": self.started_at, "finished_at": self.finished_at},
                "timeline": timeline,
                "pending_approval": pending,
                "audit": list(self.audit.events),
                "containers": self.containers,
                "containers_at": self.containers_at,
                "metrics": self._metrics(),
            }

    def container_names(self) -> List[str]:
        with self.lock:
            return [c["name"] for c in self.containers]


def list_runs(limit: int = 30) -> List[Dict[str, Any]]:
    runs = []
    if not AUDIT_DIR.exists():
        return runs
    for path in sorted(AUDIT_DIR.glob("run_*.jsonl"), reverse=True)[:limit]:
        events = []
        for line in path.read_text().splitlines():
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        if not events:
            continue
        results = [e.get("result") for e in events]
        if "STOP RUNBOOK" in results:
            outcome = "rejected"
        elif "failed" in results:
            outcome = "failed"
        elif events[-1].get("step") == STATE.runbook.steps[-1].id and results[-1] == "success":
            outcome = "completed"
        else:
            outcome = "incomplete"
        runs.append({"run_id": path.stem, "started": events[0].get("timestamp"),
                     "ended": events[-1].get("timestamp"), "events": len(events),
                     "outcome": outcome,
                     "approvals": sum(e.get("approval_status") == "approved" and e.get("result") == "approval_granted"
                                      for e in events)})
    return runs


STATE: AppState  # set in main()
_streams = threading.BoundedSemaphore(MAX_STREAMS)
RUN_ID_RE = re.compile(r"^run_[A-Za-z0-9_]+$")
NAME_RE = re.compile(r"[^\w .@-]")


class Handler(BaseHTTPRequestHandler):
    server_version = "WASPID"
    sys_version = ""

    # ---- helpers ---------------------------------------------------------
    def _headers(self, code: int, ctype: str, length: Optional[int] = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        if length is not None:
            self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")  # no clickjacking the approve button
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                         "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                         "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        self.end_headers()

    def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
        self._headers(code, ctype, len(body))
        self.wfile.write(body)

    def _json(self, obj: Any, code: int = 200) -> None:
        self._send(code, json.dumps(obj, default=str).encode())

    def _is_operator(self) -> bool:
        token = self.headers.get("X-Operator-Token", "")
        return bool(token) and hmac.compare_digest(token, OPERATOR_TOKEN)

    def _operator_name(self) -> str:
        name = NAME_RE.sub("", self.headers.get("X-Operator-Name", ""))[:40].strip()
        return f"{name or 'operator'} (dashboard)"

    def _body(self) -> Dict[str, Any]:
        length = min(int(self.headers.get("Content-Length") or 0), 4096)
        if not length:
            return {}
        try:
            data = json.loads(self.rfile.read(length))
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}

    # ---- routes ----------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        path, query = url.path, parse_qs(url.query)
        if path in ("/", "/index.html"):
            self._send(200, INDEX.read_bytes(), "text/html; charset=utf-8")
        elif path == "/api/state":
            self._json({**STATE.snapshot(), "server_time": time.time()})
        elif path == "/api/stream":
            self._stream()
        elif path == "/api/runbook":
            self._json({"name": STATE.runbook.name, "file": RUNBOOK_FILE.name, "source": STATE.runbook_source})
        elif path == "/api/runs":
            self._json(list_runs())
        elif path.startswith("/api/runs/") and path.endswith(".jsonl"):
            run_id = path[len("/api/runs/"):-len(".jsonl")]
            file = AUDIT_DIR / f"{run_id}.jsonl"
            if not RUN_ID_RE.match(run_id) or not file.exists():
                self._json({"error": "not found"}, 404)
                return
            self._send(200, file.read_bytes(), "application/x-ndjson")
        elif path == "/api/logs":
            name = (query.get("container") or [""])[0]
            tail = max(10, min(int((query.get("tail") or ["200"])[0] or 200), 1000))
            if name not in STATE.container_names():
                self._json({"error": "unknown container"}, 404)
                return
            try:
                self._json({"container": name, "logs": STATE.docker.container_logs(name, tail=tail)})
            except Exception as e:  # noqa: BLE001
                self._json({"error": str(e)}, 502)
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/api/auth":
            self._json({"ok": self._is_operator()}, 200 if self._is_operator() else 401)
            return
        if path not in ("/api/start", "/api/approve", "/api/reject", "/api/reset"):
            self._json({"error": "not found"}, 404)
            return
        if not self._is_operator():
            self._json({"ok": False, "error": "Operator sign-in required."}, 401)
            return
        body, who = self._body(), self._operator_name()
        try:
            if path == "/api/start":
                STATE.start()
            elif path == "/api/approve":
                STATE.approve(str(body.get("approval_id", "")), who)
            elif path == "/api/reject":
                STATE.reject(str(body.get("approval_id", "")), who)
            else:
                STATE.reset(who)
            self._json({"ok": True})
        except ValueError as e:
            self._json({"ok": False, "error": str(e)}, 409)

    def _stream(self) -> None:
        if not _streams.acquire(blocking=False):
            self._json({"error": "too many live viewers — falling back to polling"}, 503)
            return
        try:
            self._headers(200, "text/event-stream")
            last_key, last_write = None, 0.0
            while True:
                snap = STATE.snapshot()
                key = json.dumps(snap, sort_keys=True, default=str)
                now = time.time()
                if key != last_key:
                    payload = json.dumps({**snap, "server_time": now}, default=str)
                    self.wfile.write(f"event: state\ndata: {payload}\n\n".encode())
                    self.wfile.flush()
                    last_key, last_write = key, now
                elif now - last_write > 15:
                    self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
                    last_write = now
                time.sleep(0.4)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
        finally:
            _streams.release()

    def log_message(self, fmt: str, *args) -> None:
        pass


def main() -> None:
    global STATE
    try:
        STATE = AppState()
    except Exception as e:  # noqa: BLE001
        raise SystemExit(
            f"Could not connect to Docker ({e}).\n"
            "Start your Docker runtime (e.g. `colima start`) or run with WASPID_FAKE_DOCKER=1."
        )
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.daemon_threads = True
    engine = STATE.engine_info
    label = f"live Docker {engine.get('version')}" if STATE.mode == "live" else "simulated engine"
    print(f"WASPID dashboard ({label}) → http://{'localhost' if HOST in ('127.0.0.1', '0.0.0.0') else HOST}:{PORT}", flush=True)
    if TOKEN_FROM_ENV:
        print("Operator token: from WASPID_OPERATOR_TOKEN", flush=True)
    else:
        print(f"Operator sign-in link: http://localhost:{PORT}/#operator={OPERATOR_TOKEN}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
