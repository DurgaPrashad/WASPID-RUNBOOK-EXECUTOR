"""WASPID tool registry: binds engine methods to risk-classified, gated tools."""
from __future__ import annotations

from typing import Any, Callable, Dict, Iterable, Optional

from .docker_engine import DockerEngine
from .safety import ApprovalGate, ApprovalRejected, ApprovalRequired, Risk, ToolSpec, guarded


def arg(description: str, type_: str = "string", **extra: Any) -> Dict[str, Any]:
    """One JSON-schema property for ToolSpec.params."""
    return {"type": type_, "description": description, **extra}


CONTAINER = {"container": arg("Container name, e.g. waspid-api")}

TOOL_SPECS = {
    # read-only
    "list_containers":   ToolSpec("list_containers",   Risk.READ_ONLY, "List all Docker containers"),
    "inspect_container": ToolSpec("inspect_container", Risk.READ_ONLY, "Inspect a container (no secret values exposed)", CONTAINER),
    "container_logs":    ToolSpec("container_logs",    Risk.READ_ONLY, "Fetch recent container logs",
                                  {**CONTAINER, "tail": arg("Number of log lines", "integer")}, ("tail",)),
    "container_health":  ToolSpec("container_health",  Risk.READ_ONLY, "Container health status", CONTAINER),
    "list_images":       ToolSpec("list_images",       Risk.READ_ONLY, "List Docker images"),
    "inspect_network":   ToolSpec("inspect_network",   Risk.READ_ONLY, "Inspect a Docker network",
                                  {"network": arg("Network name (default: bridge)")}, ("network",)),
    # sandbox (safe: isolated, no network, disposable)
    "run_sandbox":       ToolSpec("run_sandbox",       Risk.SAFE, "Execute generated code inside an isolated, network-less, disposable container", {
        "image": arg("Container image, e.g. python:3.12-slim"),
        "command": arg("Command to run inside /work", "array", items={"type": "string"}),
        "files": arg("Files to write into /work: filename -> content", "object", additionalProperties={"type": "string"}),
        "timeout": arg("Timeout in seconds", "integer"),
    }, ("timeout",)),
    # destructive — approval enforced at this layer
    "restart_container": ToolSpec("restart_container", Risk.DESTRUCTIVE, "Restart a container (service interruption)", CONTAINER),
    "stop_container":    ToolSpec("stop_container",    Risk.DESTRUCTIVE, "Stop a container", CONTAINER),
    "remove_container":  ToolSpec("remove_container",  Risk.DESTRUCTIVE, "Remove a container (irreversible)", CONTAINER),
    "remove_image":      ToolSpec("remove_image",      Risk.DESTRUCTIVE, "Remove an image (irreversible)",
                                  {"image": arg("Image tag, e.g. waspid/api:1.4.1")}),
    "remove_volume":     ToolSpec("remove_volume",     Risk.DESTRUCTIVE, "Remove a volume (DATA LOSS)",
                                  {"volume": arg("Volume name")}),
}


def invoke(tools: Dict[str, Callable[..., Any]], name: str, *args: Any,
           approval_id: Optional[str] = None, **kwargs: Any) -> Dict[str, Any]:
    """Call a gated tool and shape the outcome the way agents see it (MCP and LLM agent alike)."""
    fn = tools.get(name)
    if fn is None:
        return {"ok": False, "status": "error", "message": f"unknown or disabled tool: {name}"}
    spec = fn.tool_spec
    try:
        if spec.requires_approval:
            result = fn(*args, approval_id=approval_id, **kwargs)
        else:
            result = fn(*args, **kwargs)
        return {"ok": True, "risk": spec.risk.value, "result": result}
    except ApprovalRejected as e:
        return {"ok": False, "status": "rejected", "message": str(e), "directive": "STOP RUNBOOK"}
    except ApprovalRequired as e:
        return {"ok": False, "status": "waiting_for_approval", "approval_card": e.request.to_card()}
    except Exception as e:  # noqa: BLE001 — reported to the agent, never hidden
        return {"ok": False, "status": "error", "message": str(e)}


def build_tools(engine: DockerEngine, gate: ApprovalGate,
                connectors: Iterable[Any] = ()) -> Dict[str, Callable[..., Any]]:
    """Gate every Docker tool, plus the tools of each connector (AWS, WASPID API/DB, …).

    A connector is any object with a TOOL_SPECS dict and a method per spec name;
    its destructive tools go through the same ApprovalGate as Docker's.
    """
    tools: Dict[str, Callable[..., Any]] = {}
    for provider, specs in [(engine, TOOL_SPECS)] + [(c, c.TOOL_SPECS) for c in connectors]:
        for name, spec in specs.items():
            if name in tools:
                raise ValueError(f"duplicate tool name: {name}")
            tools[name] = guarded(spec, gate, getattr(provider, name))
    return tools
