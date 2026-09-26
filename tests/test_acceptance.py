"""Acceptance tests — mirror the hackathon acceptance scenario exactly.

Path A: run → halt at destructive step → REJECT → runbook stops, nothing destructive ran.
Path B: run → halt → APPROVE → restart executes → health verified → completed → audit full.
Plus: safety-layer bypass attempts, failure handling, token misuse.
"""
import json

import pytest

from waspid.engine.audit import AuditLog
from waspid.engine.runbook import Runbook, RunbookEngine, StepStatus
from waspid.mcp_server.docker_engine import FakeDockerEngine
from waspid.mcp_server.safety import ApprovalGate, ApprovalRejected, ApprovalRequired
from waspid.mcp_server.tools import build_tools

RUNBOOK = {
    "name": "Production Deployment",
    "steps": [
        {"id": "inspect", "description": "Inspect current infrastructure",
         "action": "list_containers", "risk": "read_only"},
        {"id": "health", "description": "Check application health", "action": "container_health",
         "target": "waspid-api", "risk": "read_only", "verify": {"expect_health": "healthy"}},
        {"id": "validate", "description": "Run validation tests in sandbox", "action": "run_sandbox",
         "risk": "safe", "params": {"image": "python:3.12-slim",
                                    "command": ["python", "validate_release.py"],
                                    "files": {"validate_release.py": "print('PASS')"}}},
        {"id": "restart", "description": "Restart production container", "action": "restart_container",
         "target": "waspid-api", "risk": "destructive", "verify": {"expect_health": "healthy"}},
        {"id": "verify", "description": "Verify production health", "action": "container_health",
         "target": "waspid-api", "risk": "read_only", "verify": {"expect_health": "healthy"}},
    ],
}


def make_engine(fake=None):
    fake = fake or FakeDockerEngine()
    gate = ApprovalGate()
    tools = build_tools(fake, gate)
    eng = RunbookEngine(Runbook.from_dict(RUNBOOK), tools, gate, AuditLog("run_test"))
    return fake, gate, eng


def test_reject_path_stops_everything():
    fake, gate, eng = make_engine()
    assert eng.run() == "waiting_for_approval"
    step = eng.pending_approval_step
    assert step.id == "restart" and step.status is StepStatus.WAITING_FOR_APPROVAL
    # nothing destructive happened yet
    assert not any(c[0] == "restart_container" for c in fake.calls)
    assert eng.reject() == "rejected"
    assert step.status is StepStatus.REJECTED
    assert eng.runbook.steps[-1].status is StepStatus.SKIPPED_STOPPED
    assert not any(c[0] == "restart_container" for c in fake.calls)
    # audit records the rejection with STOP RUNBOOK
    assert any(e["approval_status"] == "rejected" and e["result"] == "STOP RUNBOOK"
               for e in eng.audit.events)


def test_approve_path_completes_with_verification_and_audit():
    fake, gate, eng = make_engine()
    assert eng.run() == "waiting_for_approval"
    assert eng.approve(decided_by="oncall@waspid") == "completed"
    assert [s.status for s in eng.runbook.steps] == [StepStatus.SUCCESSFUL] * 5
    assert ("restart_container", "waspid-api") in fake.calls
    assert fake.containers["waspid-api"]["restart_count"] == 1
    # audit trail covers every step with risk + approval metadata
    ev = eng.audit.events
    restart_events = [e for e in ev if e["step"] == "restart"]
    assert {e["approval_status"] for e in restart_events} >= {"waiting_for_approval", "approved"}
    assert all("timestamp" in e and "risk" in e for e in ev)


def test_destructive_tool_blocked_without_token():
    fake, gate, _ = make_engine()
    tools = build_tools(fake, gate)
    with pytest.raises(ApprovalRequired):
        tools["restart_container"]("waspid-api")           # no token
    with pytest.raises(ApprovalRequired):
        tools["restart_container"]("waspid-api", approval_id="forged_by_llm")
    assert not any(c[0] == "restart_container" for c in fake.calls)


def test_token_is_single_use_and_target_bound():
    fake, gate, _ = make_engine()
    tools = build_tools(fake, gate)
    req = gate.request(action="restart_container", target="waspid-api", reason="r",
                       risk_summary="s", expected_effect="e", next_step="n")
    gate.decide(req.approval_id, approved=True)
    # wrong target → blocked
    with pytest.raises(ApprovalRequired):
        tools["restart_container"]("waspid-db", approval_id=req.approval_id)
    # right target → allowed once
    tools["restart_container"]("waspid-api", approval_id=req.approval_id)
    # replay → blocked
    with pytest.raises(ApprovalRequired):
        tools["restart_container"]("waspid-api", approval_id=req.approval_id)


def test_rejected_token_raises_stop():
    fake, gate, _ = make_engine()
    tools = build_tools(fake, gate)
    req = gate.request(action="stop_container", target="waspid-api", reason="r",
                       risk_summary="s", expected_effect="e", next_step="n")
    gate.decide(req.approval_id, approved=False)
    with pytest.raises(ApprovalRejected):
        tools["stop_container"]("waspid-api", approval_id=req.approval_id)


def test_sandbox_failure_halts_runbook_before_destructive_step():
    fake = FakeDockerEngine()
    fake.sandbox_exit_code = 1
    fake.sandbox_output = "FAIL migration dry-run"
    fake2, gate, eng = make_engine(fake)
    assert eng.run() == "failed"
    validate = next(s for s in eng.runbook.steps if s.id == "validate")
    assert validate.status is StepStatus.FAILED
    assert not any(c[0] == "restart_container" for c in fake.calls)
    assert eng.runbook.steps[-1].status is StepStatus.SKIPPED_STOPPED


def test_unhealthy_verification_fails_step():
    fake = FakeDockerEngine()
    fake.containers["waspid-api"]["health"] = "unhealthy"
    _, gate, eng = make_engine(fake)
    assert eng.run() == "failed"
    assert eng.runbook.steps[1].status is StepStatus.FAILED


def test_destructive_always_requires_approval_even_if_runbook_omits_flag():
    data = {"name": "x", "steps": [{"id": "s", "description": "d", "action": "remove_volume",
                                    "target": "waspid-db-data", "risk": "destructive",
                                    "requires_approval": False}]}
    rb = Runbook.from_dict(data)
    assert rb.steps[0].requires_approval is True
