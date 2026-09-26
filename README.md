# WASPID Runbook Executor
images:

<img width="2684" height="1582" alt="image" src="https://github.com/user-attachments/assets/adbc4289-ed46-43fe-a306-3d3b59d4e00d" />






> **Give AI the runbook. Let it execute the safe steps. Make it ask before anything destructive.**

Built for the TrueFoundry × Polaris **"Agents That Act"** hackathon.

## Problem

Ops runbooks are executed by tired humans at 3 AM, step by step, copy-pasting commands.
LLM agents can read a runbook and act on real infrastructure — but nobody sane gives an
LLM unrestricted access to Docker in production. Prompt-level "please be careful"
guardrails are not guardrails: a confused or jailbroken model can still delete a volume.

## Solution

WASPID lets an agent execute runbooks against real Docker infrastructure with safety
**enforced in code, not in the prompt**:

- Read-only steps run freely; generated code runs only in disposable, network-less sandboxes.
- Every destructive tool call is physically blocked at the tool layer until a human grants
  a **single-use approval token bound to that exact action and target**.
- Rejection means **STOP RUNBOOK** — the engine halts and marks remaining steps stopped.
- Everything is written to an append-only JSONL audit log.


<img width="2940" height="1598" alt="image" src="https://github.com/user-attachments/assets/39032dd4-ea3f-492e-b27a-db3508925fd3" />
## Architecture

```
┌────────────────────────┐
│  TrueForge (agent      │   system prompt + model gateway
│  harness, approval UI) │   human clicks Approve / Reject
└───────────┬────────────┘
            │ MCP (stdio)
            ▼
┌─────────────────────────────────────────────┐
│  WASPID Infrastructure MCP server           │
│  (waspid.mcp_server.server)                 │
│                                             │
│   read-only tools ──────────────┐           │
│   run_sandbox_command ──┐       │           │
│   destructive tools     │       │           │
│        │                │       │           │
│   ┌────▼─────────┐      │       │           │
│   │ ApprovalGate │      │       │           │
│   │ (safety.py)  │      │       │           │
│   └────┬─────────┘      │       │           │
└────────┼────────────────┼───────┼───────────┘
         │ token OK       │       │
         ▼                ▼       ▼
   ┌──────────────────────────────────┐
   │           Docker Engine          │
   │  waspid-api / waspid-worker /    │
   │  waspid-db  +  disposable        │
   │  sandbox container (no network,  │
   │  256MB RAM, 0.5 CPU, non-root)   │
   └──────────────────────────────────┘
```

## TrueForge

WASPID runs as a TrueForge agent: the MCP server is registered as a stdio MCP connector
(`python -m waspid.mcp_server.server`), the system prompt is attached verbatim, the model
is configured via TrueForge's model gateway (no API keys in code), and the
`approve_request` / `reject_request` tools are bound to TrueForge's human-in-the-loop
approval UI. Full setup: [truforge/README.md](truforge/README.md).

## MCP

Server name: `waspid-infrastructure` (FastMCP, stdio).

| Tool | Risk | Requires approval |
|---|---|---|
| `list_containers` | read_only | no |
| `inspect_container` | read_only | no |
| `container_logs` | read_only | no |
| `container_health` | read_only | no |
| `list_images` | read_only | no |
| `inspect_network` | read_only | no |
| `run_sandbox_command` | safe (sandboxed) | no |
| `restart_container` | destructive | **yes** |
| `stop_container` | destructive | **yes** |
| `remove_container` | destructive | **yes** |
| `remove_image` | destructive | **yes** |
| `remove_volume` | destructive | **yes** |
| `list_pending_approvals` | read_only | no |
| `approve_request` | human-only | — (is the approval) |
| `reject_request` | human-only | — (is the rejection) |

## Sandbox

Any code the agent generates (validation scripts, build steps, …) runs **only** through
`run_sandbox_command`, which executes it in a **disposable container** with
`network_mode="none"`, a 256 MB memory limit, a 0.5-CPU quota, and a non-root user
(`1000:1000`). Files are written into `/work` inside the container; the container is
removed afterwards. Generated code never runs on the host.

## Approval Model

- Destructive tools called without a token return
  `{"ok": false, "status": "waiting_for_approval", "approval_card": {...}}`.
- Tokens (`approval_id`) are minted only by the `ApprovalGate` and decided only by
  `approve_request` / `reject_request` — wired to the human, not the model.
- Tokens are **single-use** and **bound to one action + target**: replay, forgery, or
  using a token for a different target all fail with `ApprovalRequired`.
- Rejection raises `ApprovalRejected` and the response carries `"directive": "STOP RUNBOOK"`.
- The gate is enforced in code (`mcp_server/safety.py`), and the runbook engine
  additionally forces `requires_approval: true` on every destructive step even if the
  runbook author omits the flag.

```
Agent                MCP server / ApprovalGate           Human (TrueForge UI)
  │                          │                                  │
  │ restart_container(x) ───►│ no token → mint request          │
  │◄── waiting_for_approval, │ approval_card ──────────────────►│  card shown
  │    approval_card         │                                  │
  │        (agent waits)     │◄──────────── approve_request ────│  click APPROVE
  │ restart_container(x,     │ consume(token, action, target)   │
  │   approval_id) ─────────►│ ── docker restart ──► ok         │
  │◄── ok ───────────────────│ token now consumed (single-use)  │
  │ container_health(x) ────►│ independent re-verification      │
  │                          │                                  │
  │        — reject path —   │◄──────────── reject_request ─────│  click REJECT
  │◄── rejected,             │                                  │
  │    "STOP RUNBOOK" ───────│  engine halts, remaining         │
  │                          │  steps marked "stopped"          │
```

## Failure handling

If any step fails (tool error, sandbox exit code ≠ 0, or a `verify` health check that
doesn't match), the engine **halts immediately** and marks all remaining steps as
`stopped`. There is no improvised recovery.

## Audit log

Every transition is appended to a JSONL audit log (`engine/audit.py`):

```json
{"run_id": "run_20250101_030000", "timestamp": "2025-01-01T03:00:12Z",
 "step": "restart", "action": "restart_container", "tool": "restart_container",
 "target": "waspid-api", "risk": "destructive", "approval_required": true,
 "approval_status": "approved", "result": "approval_granted", "detail": null}
```

## Security

- **No secrets in code, logs, or this README.** The demo DB password comes only from the
  `WASPID_DB_PASSWORD` environment variable.
- `inspect_container` redacts environment variable values.
- Sandbox containers have **no network** and run **non-root** with memory/CPU limits.
- Approval tokens cannot be minted or forged by the model.

## AI Disclosure

AI assistants used:
- Claude (Anthropic) — code generation and documentation

## Running it live

Requires Python 3.12+ and a Docker runtime (Docker Desktop, OrbStack, or
[Colima](https://github.com/abiosoft/colima): `brew install colima docker docker-compose && colima start`).

```bash
./scripts/live.sh            # demo infra on real Docker + operator console on http://localhost:8787
./scripts/live.sh --public   # same, plus a public HTTPS URL via ngrok
```

The script creates `.venv`, generates `.env` (DB password + operator token — gitignored),
starts `waspid-api` / `waspid-worker` / `waspid-db`, pulls the sandbox image and launches
the dashboard. It prints an **operator link** (`…/#operator=<token>`); open it once to sign in.

### Operator console

- **Live** — state is streamed over Server-Sent Events; container health, CPU and memory
  come from the real Docker Engine.
- **Read-only for everyone else** — anyone with the URL can watch; only a signed-in operator
  (`WASPID_OPERATOR_TOKEN`) can start runs or approve / reject. An approval must name the
  exact `approval_id` on screen, so a stale card can never approve a different action.
- Pipeline with per-step timing and results (sandbox output, tool JSON), approval card,
  activity feed, live container logs, run history with downloadable JSONL audit logs,
  and the runbook source. Light and dark themes.
- Only `waspid-*` containers are ever shown; the page is served with a strict CSP and
  `X-Frame-Options: DENY` so the approve button can't be clickjacked.

### Manual commands

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt   # pins mcp<2 (2.x renamed FastMCP)

export WASPID_DB_PASSWORD=<choose-a-password>
docker compose -f demo_infra/docker-compose.yml up -d --build

.venv/bin/python -m pytest tests          # 8 acceptance tests
.venv/bin/python demo.py                  # CLI demo
.venv/bin/python dashboard/server.py      # operator console (:8787)
```

No Docker? Run everything against a fake in-memory engine with `WASPID_FAKE_DOCKER=1`
(the console then shows **SIMULATED**).

The code imports itself as `waspid.*`; `_bootstrap.py` makes that work whatever the
checkout folder is called.

## Demo

The demo executes `waspid/runbooks/production_deployment.yaml`:
inspect containers → check `waspid-api` health → inspect version → run generated
validation tests in the sandbox → sandboxed build → **restart `waspid-api`
(destructive, halts for approval)** → verify health → **remove image
`waspid/api:1.4.1` (destructive, halts for approval)**.

- **Approve path:** the agent halts at `restart_container` with an approval card;
  a human approves; the restart executes with the single-use token; health is
  independently re-verified; the same happens at `remove_image`; the runbook completes.
- **Reject path:** the human rejects; the response carries `STOP RUNBOOK`; the step is
  marked `rejected` and all remaining steps are marked `stopped`. Nothing destructive runs.

Both paths (plus token forgery/replay/target-binding and failure-halting) are covered by
`waspid/tests/test_acceptance.py`.

## Project layout

```
waspid/
├── README.md
├── DEMO_SCRIPT.md
├── requirements.txt
├── demo.py                     # CLI demo
├── _bootstrap.py               # registers this folder as the `waspid` package
├── scripts/
│   └── live.sh                 # one-command live stack (+ --public tunnel)
├── mcp_server/
│   ├── server.py               # FastMCP stdio server "waspid-infrastructure"
│   ├── tools.py                # risk-classified tool registry
│   ├── safety.py               # ApprovalGate: tokens, enforcement
│   └── docker_engine.py        # real + fake Docker engines, sandbox runner
├── engine/
│   ├── runbook.py              # sequential runbook engine
│   └── audit.py                # JSONL audit log
├── runbooks/
│   └── production_deployment.yaml
├── dashboard/
│   ├── server.py               # live operator console backend (SSE, :8787)
│   └── index.html              # operator console UI
├── demo_infra/
│   ├── docker-compose.yml      # waspid-api, waspid-worker, waspid-db
│   ├── api/                    # demo API image
│   └── worker/                 # demo worker image
├── truforge/
│   ├── README.md               # TrueForge setup
│   └── agent_system_prompt.txt
└── tests/
    └── test_acceptance.py      # 8 acceptance tests
```
