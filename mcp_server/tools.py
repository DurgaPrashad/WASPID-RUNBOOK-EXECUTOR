"""WASPID tool registry: binds engine methods to risk-classified, gated tools."""
from __future__ import annotations

from typing import Any, Callable, Dict

from .docker_engine import DockerEngine
from .safety import ApprovalGate, Risk, ToolSpec, guarded

TOOL_SPECS = {
    # read-only
    "list_containers":   ToolSpec("list_containers",   Risk.READ_ONLY, "List all Docker containers"),
    "inspect_container": ToolSpec("inspect_container", Risk.READ_ONLY, "Inspect a container (no secret values exposed)"),
    "container_logs":    ToolSpec("container_logs",    Risk.READ_ONLY, "Fetch recent container logs"),
    "container_health":  ToolSpec("container_health",  Risk.READ_ONLY, "Container health status"),
    "list_images":       ToolSpec("list_images",       Risk.READ_ONLY, "List Docker images"),
    "inspect_network":   ToolSpec("inspect_network",   Risk.READ_ONLY, "Inspect a Docker network"),
    # sandbox (safe: isolated, no network, disposable)
    "run_sandbox":       ToolSpec("run_sandbox",       Risk.SAFE, "Execute generated code inside an isolated, network-less, disposable container"),
    # destructive — approval enforced at this layer
    "restart_container": ToolSpec("restart_container", Risk.DESTRUCTIVE, "Restart a container (service interruption)"),
    "stop_container":    ToolSpec("stop_container",    Risk.DESTRUCTIVE, "Stop a container"),
    "remove_container":  ToolSpec("remove_container",  Risk.DESTRUCTIVE, "Remove a container (irreversible)"),
    "remove_image":      ToolSpec("remove_image",      Risk.DESTRUCTIVE, "Remove an image (irreversible)"),
    "remove_volume":     ToolSpec("remove_volume",     Risk.DESTRUCTIVE, "Remove a volume (DATA LOSS)"),
}


def build_tools(engine: DockerEngine, gate: ApprovalGate) -> Dict[str, Callable[..., Any]]:
    tools: Dict[str, Callable[..., Any]] = {}
    for name, spec in TOOL_SPECS.items():
        fn = getattr(engine, name)
        tools[name] = guarded(spec, gate, fn)
    return tools
