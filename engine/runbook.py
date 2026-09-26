"""Runbook model + sequential execution engine.

The engine — not the LLM — enforces:
  * strict step order (no arbitrary skipping),
  * halting at destructive steps until the ApprovalGate says 'approved',
  * full stop on rejection or on step failure,
  * audit trail for every transition.
"""
from __future__ import annotations

import enum
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from waspid.engine.audit import AuditLog
from waspid.mcp_server.safety import ApprovalGate, ApprovalRejected, ApprovalRequired, Risk


class StepStatus(str, enum.Enum):
    PLANNED = "planned"
    RUNNING = "running"
    SUCCESSFUL = "successful"
    FAILED = "failed"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    REJECTED = "rejected"
    SKIPPED_STOPPED = "stopped"


@dataclass
class Step:
    id: str
    description: str
    action: str
    risk: str
    target: str = ""
    requires_approval: bool = False
    params: Dict[str, Any] = field(default_factory=dict)
    verify: Optional[Dict[str, Any]] = None
    status: StepStatus = StepStatus.PLANNED
    result: Any = None
    approval_id: Optional[str] = None
    started_at: Optional[float] = None
    finished_at: Optional[float] = None


def load_runbook(path: Path) -> "Runbook":
    text = Path(path).read_text()
    if str(path).endswith((".yaml", ".yml")):
        import yaml  # runtime dependency
        data = yaml.safe_load(text)
    else:
        data = json.loads(text)
    return Runbook.from_dict(data)


@dataclass
class Runbook:
    name: str
    steps: List[Step]

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> "Runbook":
        steps = [Step(**{**s, "requires_approval": s.get("requires_approval", s.get("risk") == "destructive")})
                 for s in data["steps"]]
        # Safety invariant: destructive steps ALWAYS require approval, even if
        # the runbook author forgot to say so.
        for s in steps:
            if s.risk == Risk.DESTRUCTIVE.value:
                s.requires_approval = True
        return Runbook(name=data["name"], steps=steps)


class RunbookEngine:
    def __init__(self, runbook: Runbook, tools: Dict[str, Callable[..., Any]],
                 gate: ApprovalGate, audit: AuditLog) -> None:
        self.runbook = runbook
        self.tools = tools
        self.gate = gate
        self.audit = audit
        self.cursor = 0
        self.stopped = False
        self.pending_approval_step: Optional[Step] = None

    # ---- public API -----------------------------------------------------
    def run(self) -> str:
        """Execute steps in order until done, failure, or an approval boundary."""
        while not self.stopped and self.cursor < len(self.runbook.steps):
            step = self.runbook.steps[self.cursor]
            outcome = self._execute(step)
            if outcome == "waiting_for_approval":
                return "waiting_for_approval"
            if outcome in ("failed", "rejected"):
                self._stop_remaining()
                return outcome
            self.cursor += 1
        return "completed" if not self.stopped else "stopped"

    def approve(self, decided_by: str = "human") -> str:
        step = self._require_pending()
        self.gate.decide(step.approval_id, approved=True, decided_by=decided_by)
        self.audit.record(step=step.id, action=step.action, tool=step.action, target=step.target,
                          risk=step.risk, approval_required=True, approval_status="approved",
                          result="approval_granted",
                          detail={"decided_by": decided_by, "approval_id": step.approval_id})
        self.pending_approval_step = None
        return self.run()

    def reject(self, decided_by: str = "human") -> str:
        step = self._require_pending()
        self.gate.decide(step.approval_id, approved=False, decided_by=decided_by)
        step.status = StepStatus.REJECTED
        step.finished_at = time.time()
        self.audit.record(step=step.id, action=step.action, tool=step.action, target=step.target,
                          risk=step.risk, approval_required=True, approval_status="rejected",
                          result="STOP RUNBOOK",
                          detail={"decided_by": decided_by, "approval_id": step.approval_id})
        self.pending_approval_step = None
        self._stop_remaining()
        return "rejected"

    def timeline(self) -> List[Dict[str, Any]]:
        return [{"id": s.id, "description": s.description, "status": s.status.value,
                 "risk": s.risk, "action": s.action, "target": s.target,
                 "requires_approval": s.requires_approval, "approval_id": s.approval_id,
                 "started_at": s.started_at, "finished_at": s.finished_at}
                for s in self.runbook.steps]

    # ---- internals ------------------------------------------------------
    def _require_pending(self) -> Step:
        if not self.pending_approval_step:
            raise ValueError("no step is waiting for approval")
        return self.pending_approval_step

    def _stop_remaining(self) -> None:
        self.stopped = True
        for s in self.runbook.steps:
            if s.status is StepStatus.PLANNED:
                s.status = StepStatus.SKIPPED_STOPPED

    def _execute(self, step: Step) -> str:
        tool = self.tools.get(step.action)
        if tool is None:
            step.status = StepStatus.FAILED
            step.result = f"unknown tool: {step.action}"
            self.audit.record(step=step.id, action=step.action, tool=step.action, target=step.target,
                              risk=step.risk, result="failed", detail=step.result)
            return "failed"

        # Destructive boundary: request approval BEFORE any execution attempt.
        if step.requires_approval and step.approval_id is None:
            req = self.gate.request(
                action=step.action, target=step.target,
                reason=f"Required by runbook '{self.runbook.name}', step '{step.id}'",
                risk_summary="Production service interruption / irreversible change",
                expected_effect=step.description,
                next_step=self._next_desc(step),
            )
            step.approval_id = req.approval_id
            step.status = StepStatus.WAITING_FOR_APPROVAL
            self.pending_approval_step = step
            self.audit.record(step=step.id, action=step.action, tool=step.action, target=step.target,
                              risk=step.risk, approval_required=True,
                              approval_status="waiting_for_approval", result="halted")
            return "waiting_for_approval"

        step.status = StepStatus.RUNNING
        step.started_at = time.time()
        try:
            kwargs = dict(step.params)
            if step.target and step.action != "run_sandbox":
                result = tool(step.target, approval_id=step.approval_id, **kwargs) \
                    if step.requires_approval else tool(step.target, **kwargs)
            else:
                result = tool(**kwargs)
            step.result = result
            ok, why = self._verify(step, result)
            step.status = StepStatus.SUCCESSFUL if ok else StepStatus.FAILED
            step.finished_at = time.time()
            self.audit.record(step=step.id, action=step.action, tool=step.action, target=step.target,
                              risk=step.risk, approval_required=step.requires_approval,
                              approval_status="approved" if step.requires_approval else None,
                              result="success" if ok else "failed",
                              detail=result if ok else {"result": result, "reason": why})
            return "success" if ok else "failed"
        except ApprovalRejected as e:
            step.status = StepStatus.REJECTED
            step.finished_at = time.time()
            self.audit.record(step=step.id, action=step.action, tool=step.action, target=step.target,
                              risk=step.risk, approval_required=True, approval_status="rejected",
                              result="STOP RUNBOOK", detail=str(e))
            return "rejected"
        except (ApprovalRequired, Exception) as e:  # noqa: BLE001 — recorded, never hidden
            step.status = StepStatus.FAILED
            step.result = str(e)
            step.finished_at = time.time()
            self.audit.record(step=step.id, action=step.action, tool=step.action, target=step.target,
                              risk=step.risk, result="failed", detail=str(e))
            return "failed"

    def _verify(self, step: Step, result: Any) -> tuple[bool, str]:
        """Independent, tool-evidence-based verification. Never claim blind success."""
        v = step.verify or {}
        if step.action == "run_sandbox":
            ec = result.get("exit_code", -1)
            return (ec == 0, f"sandbox exit_code={ec}")
        if v.get("expect_health") is not None:
            # re-query health via tool — do not trust the action's own response.
            # A freshly (re)started container reports 'starting' until its first
            # healthcheck passes, so poll until healthy, unhealthy, or timeout.
            target = step.target or v.get("target")
            deadline = time.monotonic() + float(v.get("timeout", 60))
            while True:
                fresh = self.tools["container_health"](target)
                health, status = fresh.get("health"), fresh.get("status")
                ok = health in (v["expect_health"], "none") and status == "running"
                if ok or health == "unhealthy" or time.monotonic() >= deadline:
                    return (ok, f"health={health} status={status}")
                time.sleep(1)
        if v.get("expect_status"):
            ok = isinstance(result, dict) and result.get("status") == v["expect_status"]
            return (ok, f"status={result.get('status') if isinstance(result, dict) else result}")
        if isinstance(result, dict) and result.get("exit_code") not in (None, 0):
            return (False, f"exit_code={result['exit_code']}")
        return (True, "tool returned without error")

    def _next_desc(self, step: Step) -> str:
        i = self.runbook.steps.index(step)
        return self.runbook.steps[i + 1].description if i + 1 < len(self.runbook.steps) else "Runbook complete"
