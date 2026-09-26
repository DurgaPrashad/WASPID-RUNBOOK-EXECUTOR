"""WASPID CLI demo driver (stdlib only).

Runs the production deployment runbook and prompts a human for approval
at every destructive boundary. Set WASPID_FAKE_DOCKER=1 to run without
Docker; set WASPID_AUTO_DECISION=approve|reject for non-interactive runs.
"""
from __future__ import annotations

import os
import secrets
import sys
import time
from pathlib import Path

import sys as _sys
from pathlib import Path as _P
_sys.path.insert(0, str(_P(__file__).resolve().parent))
import _bootstrap  # noqa: E402,F401 — makes `waspid.*` importable

from waspid.engine.audit import AuditLog
from waspid.engine.runbook import RunbookEngine, load_runbook
from waspid.mcp_server.docker_engine import FakeDockerEngine, RealDockerEngine
from waspid.mcp_server.safety import ApprovalGate
from waspid.mcp_server.tools import build_tools

ROOT = Path(__file__).resolve().parent
STATUS_ICON = {
    "successful": "\u2713", "failed": "\u2717", "rejected": "\u2717",
    "waiting_for_approval": "\u26a0", "stopped": "\u26a0",
    "planned": "\u25cb", "running": "\u2026",
}


def render_approval_box(card: dict) -> str:
    rows = [
        ("Action", card["action"]), ("Target", card["target"]),
        ("Reason", card["reason"]), ("Risk", card["risk"]),
        ("Expected effect", card["expected_effect"]), ("Next step", card["next_step"]),
    ]
    lines = [f"{k+':':<17}{v}" for k, v in rows]
    width = max(len(l) for l in lines + ["APPROVAL REQUIRED"]) + 2
    out = ["+" + "=" * width + "+",
           "|" + "APPROVAL REQUIRED".center(width) + "|",
           "+" + "-" * width + "+"]
    out += ["| " + l.ljust(width - 1) + "|" for l in lines]
    out.append("+" + "=" * width + "+")
    return "\n".join(out)


def decide() -> str:
    auto = os.environ.get("WASPID_AUTO_DECISION", "").strip().lower()
    if auto in ("approve", "reject"):
        print(f"(auto decision: {auto})")
        return auto
    while True:
        ans = input("[a]pprove / [r]eject ? ").strip().lower()
        if ans in ("a", "approve"):
            return "approve"
        if ans in ("r", "reject"):
            return "reject"


def main() -> int:
    use_fake = os.environ.get("WASPID_FAKE_DOCKER") == "1"
    engine_impl = FakeDockerEngine() if use_fake else RealDockerEngine()
    print(f"WASPID Runbook Executor — engine: {'FakeDockerEngine' if use_fake else 'RealDockerEngine'}")

    runbook = load_runbook(ROOT / "runbooks" / "production_deployment.json")
    gate = ApprovalGate()
    tools = build_tools(engine_impl, gate)
    run_id = time.strftime("run_%Y%m%d_%H%M%S", time.gmtime()) + f"_{secrets.token_hex(2)}"
    audit_dir = ROOT / "audit"
    audit_dir.mkdir(exist_ok=True)
    audit = AuditLog(run_id, path=audit_dir / f"{run_id}.jsonl")
    engine = RunbookEngine(runbook, tools, gate, audit)

    print(f"Runbook: {runbook.name} ({len(runbook.steps)} steps)\n")
    status = engine.run()
    while status == "waiting_for_approval":
        step = engine.pending_approval_step
        card = gate.get(step.approval_id).to_card()
        print()
        print(render_approval_box(card))
        status = engine.approve() if decide() == "approve" else engine.reject()

    print("\n=== Execution timeline ===")
    for s in engine.timeline():
        icon = STATUS_ICON.get(s["status"], "?")
        print(f" {icon} [{s['status']:>10}] {s['id']:<10} {s['description']} (risk={s['risk']})")

    print()
    if status == "completed":
        print("RUNBOOK COMPLETED")
    elif status == "rejected":
        print("STOP RUNBOOK")
        print("(human rejected a destructive step — remaining steps stopped)")
    else:
        print(f"RUNBOOK ENDED: {status.upper()}")
        if status == "failed":
            print("STOP RUNBOOK")

    print(f"\nAudit log: {audit.path} ({len(audit.events)} events)")
    return 0 if status == "completed" else 1


if __name__ == "__main__":
    sys.exit(main())
