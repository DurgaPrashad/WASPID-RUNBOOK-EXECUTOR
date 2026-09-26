"""Connectors: platforms beyond Docker that WASPID can observe and act on.

A connector is a class with a TOOL_SPECS dict (risk-classified ToolSpecs), one
method per tool, and a status() for the operator console. build_tools() gates
its destructive tools with the same ApprovalGate as Docker's, so a new cloud or
monitoring platform inherits the approval model, audit log and STOP RUNBOOK
semantics without touching the safety layer.
"""
from __future__ import annotations

import os
from typing import Any, List

from waspid.mcp_server.docker_engine import FakeDockerEngine

from .aws import AWSConnector, FakeAWSConnector
from .waspid_platform import FakeWaspidPlatform, WaspidPlatform

__all__ = ["AWSConnector", "FakeAWSConnector", "WaspidPlatform", "FakeWaspidPlatform", "default_connectors"]


def default_connectors(docker: Any) -> List[Any]:
    """WASPID API + DB always; AWS only when explicitly enabled."""
    connectors: List[Any] = [FakeWaspidPlatform(docker) if isinstance(docker, FakeDockerEngine)
                             else WaspidPlatform(docker)]
    if os.environ.get("WASPID_FAKE_AWS") == "1":
        connectors.append(FakeAWSConnector())
    elif os.environ.get("WASPID_ENABLE_AWS") == "1":
        connectors.append(AWSConnector())
    return connectors
