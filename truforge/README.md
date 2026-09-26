# Running WASPID on TrueFoundry

WASPID is built to run as an agent on **TrueFoundry**: TrueForge (TrueFoundry's agent
harness) hosts the agent (model + system prompt + human-in-the-loop UI), the
**TrueFoundry AI Gateway** serves the model, and the agent talks to the
**WASPID Infrastructure MCP server** over MCP stdio.

```
TrueForge agent ──MCP (stdio)──> waspid-infrastructure ──> Docker · WASPID API/DB · AWS
       │
       └── model via TrueFoundry AI Gateway (OpenAI, Anthropic, Bedrock, self-hosted, …)
```

> **Note:** exact TrueFoundry screens and field names vary by deployment. Everything below
> is configuration guidance; **adapt it to your TrueFoundry deployment**.

## 1. Register the MCP connector

Register the WASPID Infrastructure MCP server as a stdio MCP connector:

```
python serve_mcp.py
```

`serve_mcp.py` works whatever the checkout folder is called. If the folder is literally
named `waspid`, `python -m waspid.mcp_server.server` from its parent works too.

Example connector config (JSON; adapt it to your TrueFoundry deployment):

```json
{
  "name": "waspid-infrastructure",
  "transport": "stdio",
  "command": "python",
  "args": ["serve_mcp.py"],
  "env": {
    "WASPID_FAKE_DOCKER": "0",
    "WASPID_ENABLE_AWS": "0"
  }
}
```

- The working directory must be the repository root.
- `WASPID_FAKE_DOCKER=1` runs against an in-memory Docker engine (dry runs and CI).
- `WASPID_ENABLE_AWS=1` registers the AWS tools (credentials come from the standard AWS
  chain: profile, SSO or IAM role). `WASPID_FAKE_AWS=1` registers them against a simulated AWS.
- The server registers as `waspid-infrastructure`, and every tool description carries its
  risk metadata. That is 17 tools by default and 26 with AWS enabled.

## 2. Attach the system prompt

Set the agent's system instruction to the contents of
[`agent_system_prompt.txt`](./agent_system_prompt.txt), verbatim. It tells the model to
execute runbook steps in order, run generated code only through `run_sandbox_command`,
and stop at destructive steps until a human decides.

## 3. Model: TrueFoundry AI Gateway

Configure the model through the **TrueFoundry AI Gateway**. **This codebase contains no API
keys.** Model credentials live in TrueFoundry, and WASPID is model-agnostic.

The same gateway also powers WASPID's own agent runner, because the gateway is
OpenAI-compatible:

```bash
export TFY_API_KEY=...                 # TrueFoundry API key
export TFY_GATEWAY_BASE_URL=...        # copy from the AI Gateway "code snippet" in the UI
export WASPID_MODEL=openai-main/gpt-5-mini   # any model id your gateway exposes
python run_agent.py
```

To call OpenAI directly instead, set `OPENAI_API_KEY` (and optionally `WASPID_MODEL`).
When both are configured, TrueFoundry wins; `WASPID_LLM_PROVIDER=openai|truefoundry` overrides that.

## 4. Human-in-the-loop approvals

Two tools exist only for the human side of the loop:

- `approve_request(approval_id, decided_by)`
- `reject_request(approval_id, decided_by)`

Bind them to TrueForge's **human approval UI**: mark them as human-gated / operator-only
tools, so the approval widget invokes them and the model never does. The flow:

1. The agent calls a destructive tool (e.g. `restart_container`, `aws_ecs_redeploy_service`)
   without a token.
2. The tool returns `{"ok": false, "status": "waiting_for_approval", "approval_card": {...}}`.
   The card contains `approval_id`, action, target, reason, risk and expected effect.
3. TrueForge renders the card, and the human clicks Approve or Reject, which calls
   `approve_request` / `reject_request`.
4. On approve, the agent retries the same tool with the granted `approval_id`.
5. On reject, the tool response carries `"directive": "STOP RUNBOOK"` and the agent must halt.

The model **cannot approve its own actions**. The system prompt forbids it from calling
the decision tools, and even if it did, the resulting token is single-use and bound to one
action and target.

## 5. Defense in depth

The approval gate is enforced **in code at the tool layer** (`mcp_server/safety.py`), not
by prompting. Even a misbehaving or jailbroken model gets `ApprovalRequired` when it calls
a destructive tool, whether Docker or AWS, without a valid, human-granted, unconsumed
token bound to that exact action and target. Rejected tokens raise `ApprovalRejected`, which
means `STOP RUNBOOK`. Tokens cannot be forged, replayed, or pointed at a different target.
See `tests/test_acceptance.py`, `tests/test_integrations.py` and `tests/test_agent.py`.
