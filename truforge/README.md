# Running WASPID on TrueForge

WASPID is built to run as an agent on **TrueForge** (TrueFoundry's agent harness):
TrueForge hosts the agent (model + system prompt + human-in-the-loop UI) and talks to
the **WASPID Infrastructure MCP server** over MCP stdio.

```
TrueForge agent ──MCP (stdio)──> waspid-infrastructure server ──> Docker Engine
```

> **Note:** exact TrueForge screens/field names vary by deployment. Everything below is
> configuration guidance — **adapt to your TrueForge deployment**.

## 1. Register the MCP connector

Register the WASPID Infrastructure MCP server as an MCP connector using a stdio command:

```
python -m waspid.mcp_server.server
```

Example connector config (JSON — adapt to your TrueForge deployment):

```json
{
  "name": "waspid-infrastructure",
  "transport": "stdio",
  "command": "python",
  "args": ["-m", "waspid.mcp_server.server"],
  "env": {
    "WASPID_FAKE_DOCKER": "0"
  }
}
```

- The working directory must be the repository root (the directory containing `waspid/`).
- Set `WASPID_FAKE_DOCKER=1` to run against a fake in-memory Docker engine (no Docker
  daemon needed — useful for dry runs and CI).
- The server registers itself as `waspid-infrastructure` and exposes all tools with risk
  metadata embedded in their descriptions.

## 2. Attach the system prompt

Set the agent's system instruction to the contents of
[`agent_system_prompt.txt`](./agent_system_prompt.txt) (verbatim). It instructs the model
to execute runbook steps in order, run generated code only through
`run_sandbox_command`, and stop at destructive steps until a human decides.

## 3. Model configuration

Configure the model through **TrueForge's model gateway**. There are **no API keys in
this codebase** — model credentials live entirely in TrueForge connector configuration /
environment variables managed by the platform. WASPID itself is model-agnostic.

## 4. Human-in-the-loop approvals

Two tools exist solely for the human side of the loop:

- `approve_request(approval_id, decided_by)`
- `reject_request(approval_id, decided_by)`

Bind these to TrueForge's **human approval UI** (i.e. mark them as human-gated /
operator-only tools so they are invoked from the approval widget, not by the model).
The flow:

1. The agent calls a destructive tool (e.g. `restart_container`) without a token.
2. The tool returns `{"ok": false, "status": "waiting_for_approval", "approval_card": {...}}`
   — the card contains `approval_id`, action, target, reason, risk, expected effect.
3. TrueForge renders the card; the human clicks Approve or Reject, which calls
   `approve_request` / `reject_request`.
4. On approve, the agent retries the same tool with the granted `approval_id`.
5. On reject, the tool response carries `"directive": "STOP RUNBOOK"` and the agent must halt.

The model **cannot self-approve**: the system prompt forbids calling the decision tools,
and even if it did, the resulting token is still bound to one action + target and
single-use.

## 5. Defense in depth

The approval gate is enforced **in code at the tool layer**
(`waspid/mcp_server/safety.py`), not by prompting. Even a misbehaving or jailbroken
model gets `ApprovalRequired` when calling a destructive tool without a valid,
human-granted, unconsumed token bound to that exact action and target. Rejected tokens
raise `ApprovalRejected` → `STOP RUNBOOK`. Tokens cannot be forged, replayed, or
repointed at a different target — see `waspid/tests/test_acceptance.py`.
