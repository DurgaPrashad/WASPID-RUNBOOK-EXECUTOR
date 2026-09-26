"""WASPID LLM agent: an OpenAI-compatible model executes a runbook through WASPID's gated tools.

The model reads the runbook, plans, and calls tools. WASPID enforces the rules in code:
  * the model is offered only risk-classified tools, never approve/reject,
  * a destructive call without a human-granted token returns waiting_for_approval,
    and the loop pauses for the human before the model gets another turn,
  * a rejection ends the run in the harness (STOP RUNBOOK): no further tool calls run,
  * every tool call and every human decision is written to the audit log.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Tuple

from waspid.engine.audit import AuditLog
from waspid.mcp_server.safety import ApprovalGate
from waspid.mcp_server.tools import arg, invoke

# Tool names as the MCP server exposes them, so the same system prompt fits both harnesses.
EXPOSED = {"run_sandbox": "run_sandbox_command"}

APPROVED = ("The human operator ({who}) APPROVED approval_id={approval_id} for {action} on {target}. "
            "Call {action} on {target} again with approval_id=\"{approval_id}\" exactly once, then verify "
            "the result with read-only tools before continuing.")
REJECTED = ("The human operator ({who}) REJECTED {action} on {target}. STOP RUNBOOK. No more tools will run. "
            "Report the final status of every runbook step.")

Decide = Callable[[Dict[str, Any]], Tuple[bool, str]]  # approval card -> (approved, decided_by)


@dataclass
class AgentResult:
    status: str        # finished | rejected | max_turns
    summary: str
    tool_calls: int
    turns: int


def tool_schemas(tools: Dict[str, Callable[..., Any]]) -> List[Dict[str, Any]]:
    """OpenAI function-calling schemas, generated from the same ToolSpecs the gate enforces."""
    out = []
    for name, fn in tools.items():
        spec = fn.tool_spec
        props = dict(spec.params)
        if spec.requires_approval:
            props["approval_id"] = arg("Human-granted approval token. Omit it on the first call to request approval.")
        out.append({"type": "function", "function": {
            "name": EXPOSED.get(name, name),
            "description": f"{spec.description}. RISK: {spec.risk.value}, "
                           f"requires_approval: {str(spec.requires_approval).lower()}",
            "parameters": {"type": "object", "properties": props,
                           "required": [p for p in spec.params if p not in spec.optional]},
        }})
    return out


class RunbookAgent:
    def __init__(self, client: Any, model: str, tools: Dict[str, Callable[..., Any]], gate: ApprovalGate,
                 audit: AuditLog, decide: Decide, system_prompt: str, max_turns: int = 40,
                 on_event: Callable[[str, Any], None] = lambda kind, data: None) -> None:
        self.client, self.model = client, model
        self.tools, self.gate, self.audit = tools, gate, audit
        self.decide, self.system_prompt, self.max_turns = decide, system_prompt, max_turns
        self.on_event = on_event
        self.schemas = tool_schemas(tools)
        self.internal = {EXPOSED.get(n, n): n for n in tools}
        self.calls = 0

    def run(self, runbook_name: str, runbook_source: str) -> AgentResult:
        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": f"Execute this runbook ({runbook_name}):\n\n{runbook_source}"},
        ]
        for turn in range(1, self.max_turns + 1):
            msg = self._complete(messages)
            calls = msg.tool_calls or []
            messages.append(self._assistant(msg, calls))
            if msg.content:
                self.on_event("say", msg.content)
            if not calls:
                self._record_end("finished")
                return AgentResult("finished", msg.content or "", self.calls, turn)

            card = None
            for call in calls:
                if card is None:
                    result = self._invoke(call)
                    if result.get("status") == "waiting_for_approval":
                        card = result["approval_card"]
                else:
                    result = {"ok": False, "status": "not_executed",
                              "message": "Not executed: an earlier call is waiting for human approval."}
                messages.append({"role": "tool", "tool_call_id": call.id, "content": json.dumps(result, default=str)})

            if card is not None:
                approved, who = self.decide(card)
                self.gate.decide(card["approval_id"], approved=approved, decided_by=who)
                self.audit.record(step="agent", action=card["action"], tool=card["action"], target=card["target"],
                                  risk="destructive", approval_required=True,
                                  approval_status="approved" if approved else "rejected",
                                  result="approval_granted" if approved else "STOP RUNBOOK",
                                  detail={"decided_by": who, "approval_id": card["approval_id"]})
                if not approved:
                    messages.append({"role": "user", "content": REJECTED.format(who=who, **card)})
                    return AgentResult("rejected", self._final_report(messages), self.calls, turn)
                messages.append({"role": "user", "content": APPROVED.format(who=who, **card)})

        self._record_end("max_turns")
        return AgentResult("max_turns", f"Stopped after {self.max_turns} model turns.", self.calls, self.max_turns)

    # ---- internals ------------------------------------------------------
    def _complete(self, messages: List[Dict[str, Any]], **extra: Any) -> Any:
        resp = self.client.chat.completions.create(model=self.model, messages=messages, tools=self.schemas, **extra)
        return resp.choices[0].message

    @staticmethod
    def _assistant(msg: Any, calls: List[Any]) -> Dict[str, Any]:
        out: Dict[str, Any] = {"role": "assistant", "content": msg.content}
        if calls:
            out["tool_calls"] = [{"id": c.id, "type": "function",
                                  "function": {"name": c.function.name, "arguments": c.function.arguments}}
                                 for c in calls]
        return out

    def _invoke(self, call: Any) -> Dict[str, Any]:
        self.calls += 1
        exposed = call.function.name
        name = self.internal.get(exposed)
        try:
            args = json.loads(call.function.arguments or "{}")
        except json.JSONDecodeError:
            args = None
        if name is None or not isinstance(args, dict):
            result = {"ok": False, "status": "error",
                      "message": f"unknown tool: {exposed}" if name is None else "arguments must be a JSON object"}
            self.on_event("call", (exposed, args, result))
            return result

        spec = self.tools[name].tool_spec
        approval_id = args.pop("approval_id", None)
        first = next(iter(spec.params), None)
        positional = [args.pop(first)] if first in args else []  # the target: what tokens are bound to
        result = invoke(self.tools, name, *positional, approval_id=approval_id, **args)

        ok, status = result["ok"], result.get("status")
        approval = None
        if spec.requires_approval:
            approval = "approved" if ok else status if status in ("waiting_for_approval", "rejected") else None
        self.audit.record(step=f"agent#{self.calls}", action=name, tool=exposed,
                          target=str(positional[0]) if positional else "", risk=spec.risk.value,
                          approval_required=spec.requires_approval, approval_status=approval,
                          result="success" if ok else {"waiting_for_approval": "halted",
                                                       "rejected": "STOP RUNBOOK"}.get(status, "failed"),
                          detail=result.get("result") if ok else result.get("message") or result.get("approval_card"))
        self.on_event("call", (exposed, {**args, **({first: positional[0]} if positional else {})}, result))
        return result

    def _final_report(self, messages: List[Dict[str, Any]]) -> str:
        try:
            return self._complete(messages, tool_choice="none").content or ""
        except Exception as e:  # noqa: BLE001 — the run already stopped; the report is best-effort
            return f"(final report unavailable: {e})"

    def _record_end(self, status: str) -> None:
        self.audit.record(step="agent", action="report", tool="-", risk="read_only", result=status)
