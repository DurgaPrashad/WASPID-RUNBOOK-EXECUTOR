"""Run a runbook with an LLM agent: OpenAI, or any model behind the TrueFoundry AI Gateway.

  python run_agent.py                                  # runbooks/production_deployment.yaml
  python run_agent.py --runbook aws_ecs_redeploy.yaml  # needs WASPID_ENABLE_AWS=1 or WASPID_FAKE_AWS=1
  WASPID_FAKE_DOCKER=1 python run_agent.py             # no Docker needed

The model plans and calls WASPID's tools; approvals are enforced in code. When the
model reaches a destructive tool you are asked to approve or reject it
(WASPID_AUTO_DECISION=approve|reject for non-interactive runs). LLM setup: see
.env.example (OPENAI_API_KEY, or TFY_API_KEY + TFY_GATEWAY_BASE_URL + WASPID_MODEL).
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import secrets
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bootstrap  # noqa: E402,F401 — makes `waspid.*` importable

from demo import decide, render_approval_box  # noqa: E402
from waspid.agent import LLMConfigError, RunbookAgent, detect, make_client  # noqa: E402
from waspid.connectors import default_connectors  # noqa: E402
from waspid.engine.audit import AuditLog  # noqa: E402
from waspid.mcp_server.docker_engine import FakeDockerEngine, RealDockerEngine  # noqa: E402
from waspid.mcp_server.safety import ApprovalGate  # noqa: E402
from waspid.mcp_server.tools import build_tools  # noqa: E402

ROOT = Path(__file__).resolve().parent
PROMPT = ROOT / "truforge" / "agent_system_prompt.txt"


def show(kind: str, data) -> None:
    if kind == "say":
        print(f"\n\033[1m agent>\033[0m {data.strip()}")
        return
    name, args, result = data
    mark = "✓" if result["ok"] else "⚠" if result.get("status") == "waiting_for_approval" else "✗"
    shown = {k: v for k, v in (args or {}).items() if k != "files"}
    print(f"  {mark} {name}({json.dumps(shown)[1:-1]}) -> {result.get('status', 'ok')}")


def ask(card: dict) -> tuple[bool, str]:
    print()
    print(render_approval_box(card))
    return decide() == "approve", f"{getpass.getuser()} (cli)"


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--runbook", default="production_deployment.yaml", help="file under runbooks/")
    p.add_argument("--model", help="override WASPID_MODEL")
    p.add_argument("--max-turns", type=int, default=40)
    opts = p.parse_args()
    if opts.model:
        os.environ["WASPID_MODEL"] = opts.model

    cfg = detect()
    try:
        client = make_client(cfg)
    except LLMConfigError as e:
        print(e, file=sys.stderr)
        return 2

    docker = FakeDockerEngine() if os.environ.get("WASPID_FAKE_DOCKER") == "1" else RealDockerEngine()
    connectors = default_connectors(docker)
    gate = ApprovalGate()
    tools = build_tools(docker, gate, connectors)
    run_id = time.strftime("run_%Y%m%d_%H%M%S", time.gmtime()) + f"_{secrets.token_hex(2)}_agent"
    (ROOT / "audit").mkdir(exist_ok=True)
    audit = AuditLog(run_id, path=ROOT / "audit" / f"{run_id}.jsonl")

    runbook = ROOT / "runbooks" / opts.runbook
    print(f"WASPID agent — {cfg.describe()}")
    print(f"Engine: {type(docker).__name__} · connectors: {', '.join(type(c).__name__ for c in connectors)}")
    print(f"Runbook: {runbook.name} · {len(tools)} tools offered (approve/reject are human-only)\n")

    agent = RunbookAgent(client, cfg.model, tools, gate, audit, decide=ask,
                         system_prompt=PROMPT.read_text(), max_turns=opts.max_turns, on_event=show)
    result = agent.run(runbook.name, runbook.read_text())

    print(f"\n=== {result.status.upper()} === {result.tool_calls} tool calls in {result.turns} turns")
    if result.status == "rejected":
        print("STOP RUNBOOK — a human rejected a destructive step; no further tools ran.")
        if result.summary:
            print(f"\n{result.summary.strip()}")
    print(f"\nAudit log: {audit.path} ({len(audit.events)} events)")
    return 0 if result.status == "finished" else 1


if __name__ == "__main__":
    sys.exit(main())
