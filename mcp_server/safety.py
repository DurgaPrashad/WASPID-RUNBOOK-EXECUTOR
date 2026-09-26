"""WASPID safety layer.

Risk metadata is enforced HERE, at the tool boundary — not by the LLM.
Every destructive tool call is physically blocked unless a matching,
unconsumed, explicitly-granted approval token exists.
"""
from __future__ import annotations

import enum
import hashlib
import json
import os
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional, Tuple


class Risk(str, enum.Enum):
    READ_ONLY = "read_only"
    SAFE = "safe"            # reversible, e.g. sandbox execution
    DESTRUCTIVE = "destructive"


@dataclass(frozen=True)
class ToolSpec:
    name: str
    risk: Risk
    description: str
    # JSON-schema properties, in call order. The first one is the target and is
    # passed positionally (that is what approval tokens are bound to).
    params: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    optional: Tuple[str, ...] = ()

    @property
    def requires_approval(self) -> bool:
        return self.risk is Risk.DESTRUCTIVE

    def metadata(self) -> Dict[str, Any]:
        return {"risk": self.risk.value, "requires_approval": self.requires_approval}


class ApprovalRequired(Exception):
    """Raised when a destructive tool is invoked without a valid approval token."""

    def __init__(self, request: "ApprovalRequest"):
        self.request = request
        super().__init__(
            f"APPROVAL REQUIRED: '{request.action}' on target '{request.target}' "
            f"is destructive and was blocked. Present the approval card to the human. "
            f"approval_id={request.approval_id}"
        )


class ApprovalRejected(Exception):
    """Raised when the human rejected the request. The runbook MUST stop."""


@dataclass
class ApprovalRequest:
    action: str
    target: str
    reason: str
    risk_summary: str
    expected_effect: str
    next_step: str
    approval_id: str = field(default_factory=lambda: f"apr_{secrets.token_hex(6)}")
    status: str = "waiting_for_approval"  # waiting_for_approval | approved | rejected
    created_at: float = field(default_factory=time.time)
    decided_at: Optional[float] = None
    decided_by: Optional[str] = None
    consumed: bool = False

    def fingerprint(self) -> str:
        return hashlib.sha256(f"{self.action}::{self.target}".encode()).hexdigest()[:16]

    def to_card(self) -> Dict[str, Any]:
        return {
            "approval_id": self.approval_id,
            "action": self.action,
            "target": self.target,
            "reason": self.reason,
            "risk": self.risk_summary,
            "expected_effect": self.expected_effect,
            "next_step": self.next_step,
            "status": self.status,
        }


class ApprovalGate:
    """Single source of truth for human approvals. LLM cannot mint tokens."""

    def __init__(self) -> None:
        self._requests: Dict[str, ApprovalRequest] = {}

    def request(self, **kwargs: Any) -> ApprovalRequest:
        req = ApprovalRequest(**kwargs)
        self._requests[req.approval_id] = req
        return req

    def all(self) -> list[ApprovalRequest]:
        return list(self._requests.values())

    def pending(self) -> list[ApprovalRequest]:
        return [r for r in self._requests.values() if r.status == "waiting_for_approval"]

    def get(self, approval_id: str) -> ApprovalRequest:
        return self._requests[approval_id]

    def decide(self, approval_id: str, approved: bool, decided_by: str = "human") -> ApprovalRequest:
        req = self._requests[approval_id]
        if req.status != "waiting_for_approval":
            raise ValueError(f"approval {approval_id} already decided: {req.status}")
        req.status = "approved" if approved else "rejected"
        req.decided_at = time.time()
        req.decided_by = decided_by
        return req

    def consume(self, approval_id: str, action: str, target: str) -> ApprovalRequest:
        """Validate + single-use consume an approval for a specific action/target."""
        req = self._requests.get(approval_id)
        if req is None:
            raise ApprovalRequired(self._missing(action, target))
        if req.status == "rejected":
            raise ApprovalRejected(f"Human rejected '{action}' on '{target}'. STOP RUNBOOK.")
        if req.status != "approved" or req.consumed:
            raise ApprovalRequired(req)
        if req.action != action or req.target != target:
            # Token cannot be reused for a different action/target.
            raise ApprovalRequired(self._missing(action, target))
        req.consumed = True
        return req

    def _missing(self, action: str, target: str) -> ApprovalRequest:
        return self.request(
            action=action,
            target=target,
            reason="Destructive tool invoked without valid approval",
            risk_summary="Irreversible / production-impacting operation",
            expected_effect=f"'{action}' would be executed against '{target}'",
            next_step="Human must approve or reject",
        )


def guarded(spec: ToolSpec, gate: ApprovalGate, fn: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap a tool. Destructive tools demand a consumable approval token."""

    def wrapper(*args: Any, approval_id: Optional[str] = None, **kwargs: Any) -> Any:
        if spec.requires_approval:
            target = kwargs.get("target") or (args[0] if args else "<unknown>")
            gate.consume(approval_id or "", spec.name, str(target))
        return fn(*args, **kwargs)

    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = (fn.__doc__ or "") + f"\n\nRISK METADATA: {json.dumps(spec.metadata())}"
    wrapper.tool_spec = spec  # type: ignore[attr-defined]
    return wrapper
