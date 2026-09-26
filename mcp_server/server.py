"""WASPID Infrastructure MCP server (FastMCP, stdio).

TrueForge → MCP → this server → Docker Engine.

Risk metadata is embedded in every tool description AND enforced in code:
destructive tools raise ApprovalRequired unless a human-granted, single-use
approval token is supplied. The LLM cannot mint tokens — only the
approve_request/reject_request tools (wired to the human approval UI in
TrueForge) can decide them.
"""
from __future__ import annotations

import json
import os
from typing import Optional

from mcp.server.fastmcp import FastMCP

from .docker_engine import FakeDockerEngine, RealDockerEngine
from .safety import ApprovalGate, ApprovalRejected, ApprovalRequired
from .tools import TOOL_SPECS, build_tools

mcp = FastMCP("waspid-infrastructure")

if os.environ.get("WASPID_FAKE_DOCKER") == "1":
    engine = FakeDockerEngine()
else:
    engine = RealDockerEngine()

gate = ApprovalGate()
tools = build_tools(engine, gate)


def _call(name: str, *args, approval_id: Optional[str] = None, **kwargs) -> str:
    try:
        if TOOL_SPECS[name].requires_approval:
            result = tools[name](*args, approval_id=approval_id, **kwargs)
        else:
            result = tools[name](*args, **kwargs)
        return json.dumps({"ok": True, "risk": TOOL_SPECS[name].risk.value, "result": result}, default=str)
    except ApprovalRejected as e:
        return json.dumps({"ok": False, "status": "rejected", "message": str(e), "directive": "STOP RUNBOOK"})
    except ApprovalRequired as e:
        return json.dumps({"ok": False, "status": "waiting_for_approval", "approval_card": e.request.to_card()})
    except Exception as e:  # noqa: BLE001
        return json.dumps({"ok": False, "status": "error", "message": str(e)})


# ---- read-only ----------------------------------------------------------
@mcp.tool()
def list_containers() -> str:
    """List all Docker containers. RISK: read_only, requires_approval: false"""
    return _call("list_containers")

@mcp.tool()
def inspect_container(container: str) -> str:
    """Inspect a container (env values redacted). RISK: read_only"""
    return _call("inspect_container", container)

@mcp.tool()
def container_logs(container: str, tail: int = 100) -> str:
    """Fetch recent logs. RISK: read_only"""
    return _call("container_logs", container, tail=tail)

@mcp.tool()
def container_health(container: str) -> str:
    """Health status of a container. RISK: read_only"""
    return _call("container_health", container)

@mcp.tool()
def list_images() -> str:
    """List Docker images. RISK: read_only"""
    return _call("list_images")

@mcp.tool()
def inspect_network(network: str = "bridge") -> str:
    """Inspect a Docker network. RISK: read_only"""
    return _call("inspect_network", network)


# ---- sandbox ------------------------------------------------------------
@mcp.tool()
def run_sandbox_command(image: str, command: list[str], files_json: str = "{}",
                        timeout: int = 120) -> str:
    """Execute generated code inside an ISOLATED, NETWORK-LESS, disposable
    container. files_json maps filename -> content, written into /work.
    This is the ONLY permitted execution path for generated code.
    RISK: safe (sandboxed, reversible)."""
    files = json.loads(files_json)
    return _call("run_sandbox", image=image, command=command, files=files, timeout=timeout)


# ---- destructive (approval enforced in the safety layer) -----------------
@mcp.tool()
def restart_container(container: str, approval_id: str = "") -> str:
    """Restart a container. RISK: destructive, requires_approval: true.
    {"risk": "destructive", "requires_approval": true}"""
    return _call("restart_container", container, approval_id=approval_id)

@mcp.tool()
def stop_container(container: str, approval_id: str = "") -> str:
    """Stop a container. {"risk": "destructive", "requires_approval": true}"""
    return _call("stop_container", container, approval_id=approval_id)

@mcp.tool()
def remove_container(container: str, approval_id: str = "") -> str:
    """Remove a container. {"risk": "destructive", "requires_approval": true}"""
    return _call("remove_container", container, approval_id=approval_id)

@mcp.tool()
def remove_image(image: str, approval_id: str = "") -> str:
    """Remove an image. {"risk": "destructive", "requires_approval": true}"""
    return _call("remove_image", image, approval_id=approval_id)

@mcp.tool()
def remove_volume(volume: str, approval_id: str = "") -> str:
    """Remove a volume (DATA LOSS). {"risk": "destructive", "requires_approval": true}"""
    return _call("remove_volume", volume, approval_id=approval_id)


# ---- approval flow (wired to the human, NOT the model) --------------------
@mcp.tool()
def list_pending_approvals() -> str:
    """List approval requests waiting for a human decision. RISK: read_only"""
    return json.dumps([r.to_card() for r in gate.pending()])

@mcp.tool()
def approve_request(approval_id: str, decided_by: str) -> str:
    """HUMAN-ONLY: grant an approval. In TrueForge this tool is bound to the
    human-approval UI element; the model must never call it on its own."""
    req = gate.decide(approval_id, approved=True, decided_by=decided_by)
    return json.dumps(req.to_card())

@mcp.tool()
def reject_request(approval_id: str, decided_by: str) -> str:
    """HUMAN-ONLY: reject an approval. The runbook must STOP."""
    req = gate.decide(approval_id, approved=False, decided_by=decided_by)
    return json.dumps({**req.to_card(), "directive": "STOP RUNBOOK"})


if __name__ == "__main__":
    mcp.run(transport="stdio")
