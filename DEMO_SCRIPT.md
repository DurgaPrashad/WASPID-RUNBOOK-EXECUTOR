# WASPID Demo Script (3–5 minutes)

## Pre-demo checklist

- [ ] `./scripts/live.sh` (or `./scripts/live.sh --public` for a shareable URL)
      → `waspid-api`, `waspid-worker`, `waspid-db` running and healthy; console at http://localhost:8787
- [ ] Open the printed operator link once so the browser is signed in as operator
- [ ] Click **Reset** so no run / pending approvals are active
- [ ] Terminal ready with large font; `docker ps` visible in a second terminal

## Take 1 — Approve path (7 scenes)

**Scene 1 — Show the runbook.**
Open `waspid/runbooks/production_deployment.yaml`.
> "This is a production deployment runbook. Some steps are read-only, some run generated code, and two are destructive: restarting the production container and removing an old image."

**Scene 2 — Start the agent.**
Click **Start run** on the console (or `.venv/bin/python demo.py`).
> "I'm handing this runbook to the agent. It executes the steps in order — it doesn't get to choose."

**Scene 3 — It inspects real infrastructure.**
Steps `inspect`, `health`, `api`, `db`, `version` turn green; point at the **Integrations** panel
(Docker, WASPID API, WASPID DB, AWS, LLM) and show `docker ps` alongside.
> "These are real calls — the Docker Engine, an HTTP health check on the WASPID API, a live probe of the Postgres database. Read-only tools need no approval. If the database were down, the runbook would stop right here."

**Scene 4 — Generated code runs in a sandbox.**
Steps `validate` and `build` execute via `run_sandbox_command`.
> "The agent generated validation code. It runs it in a disposable container with no network, 256 megabytes of RAM, non-root. Generated code never touches the host."

**Scene 5 — Halt at the destructive step.**
The run halts at `restart` with an **APPROVAL REQUIRED** card (action, target `waspid-api`, reason, risk, expected effect).
> "And here it stops. restart_container is destructive. The agent can't do this — not because we asked it nicely, but because the tool layer physically blocks it without a human-granted token."

**Scene 6 — Approve.**
Click **APPROVE**. The restart executes; show `docker ps` — `waspid-api` restarted.
> "I approve. The token is single-use and bound to exactly this action and this target. The agent spends it on the restart — it can't reuse it for anything else."

**Scene 7 — Verify + complete.**
Step `verify` re-checks `container_health` → healthy; approve `cleanup` (remove_image); runbook shows **completed**; show the JSONL audit log.
> "The engine independently re-verifies health — it doesn't take the model's word for it. Every step, every approval, every result is in an append-only audit log."

## Take 2 — Reject path

Click **Reset**, then **Start run**, let it halt at `restart` again.
Click **REJECT**.
> "This time I say no. The tool returns the directive STOP RUNBOOK. The step is marked rejected, every remaining step is marked stopped, and the agent doesn't retry or look for a workaround. Rejection isn't a suggestion — it's enforced in code."

Show the timeline: `restart` = rejected, `verify` and `cleanup` = stopped.
> "This exact behavior — approve, reject, forged tokens, replayed tokens — is covered by 31 passing tests, including an LLM agent that tries to approve itself."

Optional — Take 3: an LLM drives it (OpenAI or TrueFoundry AI Gateway).
Run `.venv/bin/python run_agent.py` (or `WASPID_FAKE_AWS=1 … --runbook aws_ecs_redeploy.yaml`).
> "Same tools, same gate — now a model plans the calls. It still stops at the destructive step and waits for me."

Close:
> "WASPID: give AI the runbook, let it execute the safe steps, and make it ask before anything destructive."
