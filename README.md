# WASPID Runbook Executor





> **Give AI the runbook. Let it execute the safe steps. Make it ask before anything destructive.**

![Python](https://img.shields.io/badge/python-3.12%2B-3776AB)
![MCP](https://img.shields.io/badge/MCP-server-6E56CF)
![TrueFoundry](https://img.shields.io/badge/TrueFoundry-AI%20Gateway%20%2B%20TrueForge-0B5FFF)
![OpenAI](https://img.shields.io/badge/OpenAI-function%20calling-10A37F)
![AWS](https://img.shields.io/badge/AWS-EC2%20·%20ECS%20·%20RDS%20·%20CloudWatch-FF9900)
![Tests](https://img.shields.io/badge/tests-31%20passing-2EA44F)

<img width="2684" height="1582" alt="WASPID operator console" src="https://github.com/user-attachments/assets/adbc4289-ed46-43fe-a306-3d3b59d4e00d" />

Built for the **TrueFoundry × Polaris "Agents That Act"** hackathon.

WASPID lets an AI agent run operational runbooks against **real infrastructure**: Docker,
the WASPID API and database, and AWS. Safety is **enforced in code, not in the prompt**. Read-only
steps run freely, generated code runs only in a disposable sandbox, and every destructive
action is physically blocked until a human approves that exact action on that exact target.

---

**Contents:** [Problem](#problem) · [What's connected](#whats-connected) · [Architecture](#architecture) ·
[Quick start](#quick-start) · [LLM agent (OpenAI / TrueFoundry)](#llm-agent-openai-or-truefoundry-ai-gateway) ·
[AWS](#aws) · [MCP tools](#mcp-tools) · [Approval model](#approval-model) · [Security](#security) ·
[Roadmap](#roadmap-connect-anything) · [Project layout](#project-layout)

## Problem

Tired humans execute ops runbooks at 3 AM, step by step, copy-pasting commands.
LLM agents can read a runbook and act on real infrastructure, but nobody sane gives an
LLM unrestricted access to production. Prompt-level "please be careful" guardrails are not
guardrails: a confused or jailbroken model can still delete a volume or reboot a database.

## Solution

- **Read-only steps run freely.** The agent can inspect containers, check API and DB
  health, and read AWS state.
- **Generated code runs only in disposable, network-less sandboxes** and never on the host.
- **Every destructive call is blocked at the tool layer** until a human grants a
  **single-use approval token bound to that exact action and target**. This applies to
  Docker and AWS alike.
- **Rejection means STOP RUNBOOK.** The engine halts and marks the remaining steps stopped.
- **Everything is audited** in an append-only JSONL log.


## What's connected
<img width="2552" height="1442" alt="image" src="https://github.com/user-attachments/assets/8bc04b4b-2884-4216-97ce-3344a1779c4d" />


| Platform | How WASPID connects | What the agent can do | Status |
|---|---|---|---|
| **Docker Engine** | docker SDK (Docker Desktop, Colima, OrbStack, Rancher) | inspect, logs, health, sandbox · restart / stop / remove (gated) | ✅ live |
| **WASPID API** | HTTP `GET /health` (`WASPID_API_URL`) | status, version, latency | ✅ live |
| **WASPID DB** | Postgres probe inside `waspid-db` (`pg_isready` + `psql`) | accepting connections, version, size, connections | ✅ live |
| **AWS** | boto3, standard credential chain (profile / SSO / IAM role) | EC2, ECS, RDS, CloudWatch · reboot / stop / redeploy (gated) | ✅ opt-in |
| **OpenAI** | OpenAI SDK, function calling | drives runbooks as an LLM agent (`run_agent.py`) | ✅ |
| **TrueFoundry** | AI Gateway (OpenAI-compatible) + TrueForge agent harness over MCP | any gateway model drives WASPID; human approvals in TrueForge | ✅ |
| **Monitoring** | CloudWatch alarms today; connectors for the rest | pre- and post-flight checks that gate a runbook | ✅ CloudWatch · 🛣️ [more](#roadmap-connect-anything) |
| **GCP · Azure · Kubernetes** | connector interface, same approval gate | — | 🛣️ [roadmap](#roadmap-connect-anything) |

Every platform is a **connector**: a class with risk-classified tools. When it plugs in,
its destructive tools automatically go behind the same approval gate, audit log and
STOP RUNBOOK semantics. The safety layer doesn't change.

## Operator console

- **Live.** State streams over Server-Sent Events. Container health, CPU and memory come
  from the real Docker Engine.
- **Integrations panel.** It shows live health of Docker, the WASPID API, the WASPID DB, AWS and the
  configured LLM provider. It never displays keys or account IDs.
- **Read-only for everyone else.** Anyone with the URL can watch. Only a signed-in operator
  (`WASPID_OPERATOR_TOKEN`) can start runs or approve / reject. An approval must name the
  exact `approval_id` on screen, so a stale card can never approve a different action.
- It also shows the pipeline with per-step timing and results, the approval card, the activity feed, live
  container logs, run history with downloadable JSONL audit logs, and the runbook source.
  Light and dark themes.
- Only `waspid-*` containers are shown. The page is served with a strict CSP and
  `X-Frame-Options: DENY`, so the approve button can't be clickjacked.

## Architecture

There are three ways to drive WASPID. All of them share one tool layer, one approval gate and one audit log:

```
 Operator console           TrueForge on TrueFoundry        run_agent.py
 runbook engine,            MCP stdio, approval UI,         OpenAI or TrueFoundry
 browser approvals          AI Gateway model                AI Gateway, CLI approvals
        │                           │                               │
        └───────────────────────────┼───────────────────────────────┘
                                    ▼
┌──────────────────────────────────────────────────────────────────────┐
│ WASPID tool layer: every tool carries risk metadata                  │
│                                                                      │
│   read_only    ──► runs freely                                       │
│   safe         ──► disposable sandbox: no network, 256 MB, non-root  │
│   destructive  ──► ApprovalGate: single-use token bound to           │
│                    action + target, otherwise blocked; reject = STOP │
│                                                                      │
│   append-only JSONL audit log of every call and every human decision │
└────────┬────────────────┬────────────────┬────────────────┬──────────┘
         ▼                ▼                ▼                ▼
   Docker Engine     WASPID API       WASPID DB        AWS
   containers,       HTTP /health     Postgres         EC2 · ECS · RDS
   images, sandbox                                     CloudWatch

   roadmap connectors: GCP · Azure · Kubernetes · Prometheus · Datadog · …
```

## Quick start

Requires Python 3.12+ and a Docker runtime (Docker Desktop, OrbStack, or
[Colima](https://github.com/abiosoft/colima): `brew install colima docker docker-compose && colima start`).

```bash
./scripts/live.sh            # demo infra on real Docker + operator console on http://localhost:8787
./scripts/live.sh --public   # same, plus a public HTTPS URL via ngrok
```

The script creates `.venv`, generates `.env` (DB password and operator token, gitignored),
starts `waspid-api` / `waspid-worker` / `waspid-db`, pulls the sandbox image and launches
the console. It prints an **operator link** (`…/#operator=<token>`); open it once to sign in.

Manual setup, plus every other entry point:

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt   # pins mcp<2 (2.x renamed FastMCP)
cp .env.example .env                                                 # fill in what you use

export WASPID_DB_PASSWORD=<choose-a-password>
docker compose -f demo_infra/docker-compose.yml up -d --build

.venv/bin/python -m pytest tests          # 31 tests
.venv/bin/python demo.py                  # CLI demo (deterministic runbook engine)
.venv/bin/python dashboard/server.py      # operator console (:8787)
.venv/bin/python run_agent.py             # LLM agent: OpenAI or TrueFoundry AI Gateway
.venv/bin/python serve_mcp.py             # MCP server (stdio) for TrueForge / any MCP client
```

**No Docker?** Run everything against a fake in-memory engine with `WASPID_FAKE_DOCKER=1`
(the console then shows **SIMULATED**). Add `WASPID_FAKE_AWS=1` for a simulated AWS.

## LLM agent: OpenAI or TrueFoundry AI Gateway


<img width="2940" height="1598" alt="WASPID runbook pipeline and approval flow" src="https://github.com/user-attachments/assets/39032dd4-ea3f-492e-b27a-db3508925fd3" />
`run_agent.py` gives the runbook to an LLM. **The model plans and calls tools, and WASPID
enforces the rules in code:**

- The model is offered only the risk-classified tools. `approve_request` / `reject_request`
  are never exposed to it.
- A destructive call without a human-granted token returns `waiting_for_approval`, and
  **the loop pauses for the human before the model gets another turn**.
- Any other tool calls the model batched after that boundary are **not executed**.
- A rejection **ends the run in the harness** (STOP RUNBOOK). The model gets one final,
  tool-less turn to report and no further tool calls.
- Every tool call and every human decision goes to the audit log.

The **TrueFoundry AI Gateway** speaks the OpenAI API, so one client covers both:
<img width="2626" height="1568" alt="image" src="https://github.com/user-attachments/assets/37fcbb78-dfcc-4a32-bb1d-6ade0f6a07cc" />

```bash
# TrueFoundry AI Gateway: any model your gateway exposes (OpenAI, Anthropic, Bedrock, self-hosted…)
export TFY_API_KEY=...  TFY_GATEWAY_BASE_URL=...  WASPID_MODEL=openai-main/gpt-5-mini

# or OpenAI directly
export OPENAI_API_KEY=...            # WASPID_MODEL defaults to gpt-5-mini

.venv/bin/python run_agent.py                                   # production deployment runbook
WASPID_FAKE_AWS=1 .venv/bin/python run_agent.py --runbook aws_ecs_redeploy.yaml
WASPID_AUTO_DECISION=reject .venv/bin/python run_agent.py       # non-interactive
```

When both are configured, TrueFoundry wins; set `WASPID_LLM_PROVIDER=openai|truefoundry` to choose.
To run the agent inside TrueFoundry's own harness instead, see **[truforge/README.md](truforge/README.md)**.
There, the MCP server is registered as a stdio connector, the system prompt is attached
verbatim, and `approve_request` / `reject_request` are bound to TrueForge's human approval UI.

## AWS

AWS is opt-in: `WASPID_ENABLE_AWS=1`, with credentials from the standard AWS chain
(`AWS_PROFILE`, SSO, IAM role) and `AWS_REGION`. **No keys ever go in code.** Use
`WASPID_FAKE_AWS=1` for a simulated AWS in demos and CI.

The bundled [`runbooks/aws_ecs_redeploy.yaml`](runbooks/aws_ecs_redeploy.yaml) runs these steps:
no CloudWatch alarms firing → ECS service steady → RDS available →
**force-redeploy `waspid-prod/waspid-api` (destructive, halts for approval)** →
wait for steady state → no alarms after rollout. A firing alarm stops the runbook before it
touches ECS.

```bash
WASPID_ENABLE_AWS=1 AWS_PROFILE=ops WASPID_RUNBOOK=aws_ecs_redeploy.yaml .venv/bin/python dashboard/server.py
```

The real connector is tested with botocore's `Stubber`, which validates every request and
response against the AWS API models.

## MCP tools

Server name: `waspid-infrastructure` (FastMCP, stdio). It has 17 tools by default and 26 with AWS enabled.

| Tool | Platform | Risk | Requires approval |
|---|---|---|---|
| `list_containers` · `inspect_container` · `container_logs` · `container_health` · `list_images` · `inspect_network` | Docker | read_only | no |
| `run_sandbox_command` | Docker sandbox | safe (sandboxed) | no |
| `restart_container` · `stop_container` · `remove_container` · `remove_image` · `remove_volume` | Docker | destructive | **yes** |
| `waspid_api_health` | WASPID API | read_only | no |
| `waspid_db_health` | WASPID DB | read_only | no |
| `aws_list_ec2_instances` · `aws_cloudwatch_alarms` · `aws_ecs_service_status` · `aws_ecs_wait_stable` · `aws_rds_status` | AWS | read_only | no |
| `aws_reboot_ec2_instance` · `aws_stop_ec2_instance` · `aws_ecs_redeploy_service` · `aws_reboot_rds_instance` | AWS | destructive | **yes** |
| `list_pending_approvals` | — | read_only | no |
| `approve_request` · `reject_request` | — | human-only | — (they *are* the decision) |

`inspect_container` redacts environment values. The WASPID API and DB tools take no
arguments: their endpoints come from the environment, so the model can't point them anywhere else.

## Sandbox

Any code the agent generates (validation scripts, build steps, …) runs **only** through
`run_sandbox_command`, which executes it in a **disposable container** with
`network_mode="none"`, a 256 MB memory limit, a 0.5-CPU quota and a non-root user
(`1000:1000`). Files are written into `/work` inside the container, and the container is
removed afterwards. Generated code never runs on the host.

## Approval model

- Destructive tools called without a token return
  `{"ok": false, "status": "waiting_for_approval", "approval_card": {...}}`.
- Only the `ApprovalGate` mints tokens (`approval_id`), and only `approve_request` /
  `reject_request` decide them. Those tools are wired to the human, not the model.
- Tokens are **single-use** and **bound to one action and target**. Replay, forgery, or
  using a token for a different target all fail with `ApprovalRequired`.
- Rejection raises `ApprovalRejected`, and the response carries `"directive": "STOP RUNBOOK"`.
- The gate is enforced in code (`mcp_server/safety.py`). The runbook engine also forces
  `requires_approval: true` on every destructive step, even if the runbook author omits it.

```
Agent                MCP server / ApprovalGate           Human (console / TrueForge / CLI)
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

If any step fails, the engine **halts immediately** and marks all remaining steps as
`stopped`. A step fails on a tool error, a sandbox exit code ≠ 0, an unhealthy API or DB,
a firing alarm, or a `verify` check that doesn't match. There is no improvised recovery.

## Audit log

Every transition is appended to a JSONL audit log (`engine/audit.py`):

```json
{"run_id": "run_20250101_030000", "timestamp": "2025-01-01T03:00:12Z",
 "step": "restart", "action": "restart_container", "tool": "restart_container",
 "target": "waspid-api", "risk": "destructive", "approval_required": true,
 "approval_status": "approved", "result": "approval_granted", "detail": null}
```

## Security

- **No secrets in code, logs, or this README.** The DB password, operator token, OpenAI /
  TrueFoundry keys and AWS credentials come only from the environment (see
  [`.env.example`](.env.example)). `.env` is gitignored.
- The console's Integrations panel shows provider, model and region only, never keys,
  ARNs or account IDs.
- `inspect_container` redacts environment variable values.
- Sandbox containers have **no network** and run **non-root** with memory and CPU limits.
- The model cannot mint or forge approval tokens, and a token for one target or action
  never unlocks another.
- AWS is **off unless explicitly enabled**, so credentials on a laptop are never exposed to
  an agent by accident.

## Demo

The demo executes [`runbooks/production_deployment.yaml`](runbooks/production_deployment.yaml):
inspect containers → check `waspid-api` health → **check the WASPID API over HTTP** →
**check the WASPID DB accepts connections** → inspect version → run generated validation
tests in the sandbox → sandboxed build → **restart `waspid-api` (destructive, halts for
approval)** → verify health → **remove image `waspid/api:1.4.1` (destructive, halts for approval)**.

- **Approve path.** The agent halts at `restart_container` with an approval card, and
  a human approves. The restart executes with the single-use token, and health is
  independently re-verified. The same happens at `remove_image`, and the runbook completes.
- **Reject path.** The human rejects, and the response carries `STOP RUNBOOK`. The step is
  marked `rejected` and all remaining steps are marked `stopped`. Nothing destructive runs.

The full walkthrough is in [DEMO_SCRIPT.md](DEMO_SCRIPT.md).

## Roadmap: connect anything

WASPID's connector interface means any platform with an API can become a runbook target
**under the same approval gate**. What's next:

| Area | Planned connectors | Example gated actions |
|---|---|---|
| **Google Cloud** | Compute Engine, Cloud Run, GKE, Cloud SQL, Cloud Monitoring | restart a Cloud Run revision, resize a node pool |
| **Microsoft Azure** | VMs, App Service, AKS, Azure SQL, Azure Monitor | restart an App Service slot, swap a deployment slot |
| **Kubernetes** | any cluster via kubeconfig | rollout restart, scale, cordon / drain |
| **Monitoring & observability** | Prometheus, Grafana, Datadog, New Relic, Elastic, OpenTelemetry | gate a runbook on SLOs and alerts, verify error rates after a change |
| **Incident response** | PagerDuty, Opsgenie, Slack, Microsoft Teams | runbooks triggered by an incident, approvals in chat |
| **Infrastructure as code** | Terraform / OpenTofu, Pulumi | `plan` runs freely, `apply` requires approval |
| **Governance** | policy-as-code (OPA), multi-approver rules, approval expiry | two-person rule for data-loss actions |
| **Console** | LLM agent mode in the browser, runbook editor, SSO | watch the model plan, then approve from the console |

**Adding a connector** means writing one class. Its destructive tools are gated automatically:

```python
from waspid.mcp_server.safety import Risk, ToolSpec
from waspid.mcp_server.tools import arg

class CloudRunConnector:
    TOOL_SPECS = {
        "gcp_run_service_status": ToolSpec("gcp_run_service_status", Risk.READ_ONLY,
                                           "Cloud Run service readiness", {"service": arg("region/service")}),
        "gcp_run_restart_service": ToolSpec("gcp_run_restart_service", Risk.DESTRUCTIVE,
                                            "Roll a new Cloud Run revision", {"service": arg("region/service")}),
    }
    def gcp_run_service_status(self, service): ...
    def gcp_run_restart_service(self, service): ...
    def status(self): return [{"key": "gcp", "name": "GCP", "ok": True, "detail": "…"}]
```

Register it in `connectors/default_connectors()`. It then appears in the runbook engine, the
LLM agent and the console's Integrations panel, and every destructive call is gated and audited.

## Tests

```bash
.venv/bin/python -m pytest tests     # 31 tests, no Docker or cloud account needed
```

- `test_acceptance.py`: approve and reject paths, blocked destructive calls, token
  forgery, replay and target binding, and failure halting.
- `test_integrations.py`: WASPID API and DB health, the runbook halting when the DB is down,
  gated AWS actions, the AWS runbook approve / reject / alarm paths, and the real boto3
  connector validated against the AWS API models.
- `test_agent.py`: the LLM agent's approve and reject paths, a model trying to self-approve or
  forge tokens, batched calls past an approval boundary, and OpenAI / TrueFoundry provider selection.

## Project layout

```
├── README.md · DEMO_SCRIPT.md · requirements.txt · .env.example
├── demo.py                     # CLI demo (deterministic runbook engine)
├── run_agent.py                # LLM agent CLI: OpenAI or TrueFoundry AI Gateway
├── serve_mcp.py                # MCP server launcher (works from any folder name)
├── _bootstrap.py               # registers this folder as the `waspid` package
├── scripts/live.sh             # one-command live stack (+ --public tunnel)
├── mcp_server/
│   ├── server.py               # FastMCP stdio server "waspid-infrastructure"
│   ├── tools.py                # risk-classified tool registry + invoke()
│   ├── safety.py               # ApprovalGate: tokens, enforcement
│   └── docker_engine.py        # real + fake Docker engines, sandbox, Postgres probe
├── connectors/
│   ├── waspid_platform.py      # WASPID API (HTTP) + WASPID DB (Postgres)
│   └── aws.py                  # EC2 · ECS · RDS · CloudWatch (+ in-memory fake)
├── agent/
│   ├── llm.py                  # OpenAI / TrueFoundry AI Gateway provider selection
│   └── runner.py               # function-calling loop with code-enforced approvals
├── engine/
│   ├── runbook.py              # sequential runbook engine
│   └── audit.py                # JSONL audit log
├── runbooks/
│   ├── production_deployment.yaml
│   └── aws_ecs_redeploy.yaml
├── dashboard/
│   ├── server.py               # live operator console backend (SSE, :8787)
│   └── index.html              # operator console UI
├── demo_infra/                 # docker-compose: waspid-api, waspid-worker, waspid-db
├── truforge/                   # TrueFoundry / TrueForge setup + agent system prompt
└── tests/                      # acceptance, integration and agent tests
```

The code imports itself as `waspid.*`. `_bootstrap.py` makes that work whatever the checkout
folder is called.

## AI Disclosure
<div align="center">

## ⚡ We Built Reflex — An Agent That Acts

> **From AI that talks to AI that acts.**

┌──────────────────────────────────────────────────────────────┐
│                                                              │
│                         **REFLEX**                           │
│                                                              │
│     A local-first AI agent built to **see, understand,      │
│              and act on your computer.**                    │
│                                                              │
│     👁️ See  ·  🧠 Understand  ·  🖱️ Act  ·  🧠 Learn       │
│                                                              │
│     **Offline-first · Open Source · macOS · Windows · Linux**│
│                                                              │
└──────────────────────────────────────────────────────────────┘

We built **WASPID Reflex** as an AI agent that doesn't just generate
responses — it **operates the computer**, using its own cursor, local
AI models, voice, memory, and verified actions.

**WASPID acts on infrastructure.
Reflex acts on the computer.**

🌐 **https://reflex.waspid.com/**

</div>


AI assistants used:
- TrueFoundry: code generation and documentation
