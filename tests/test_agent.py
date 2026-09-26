"""LLM agent harness (OpenAI / TrueFoundry AI Gateway): the model plans, WASPID enforces.

A scripted stand-in for the OpenAI client plays the model, including a model that
tries to self-approve, forge tokens, or batch a destructive call past the boundary.
"""
import json
import re
from types import SimpleNamespace

import pytest

from waspid.agent import RunbookAgent, detect, make_client, tool_schemas
from waspid.connectors import FakeWaspidPlatform
from waspid.engine.audit import AuditLog
from waspid.mcp_server.docker_engine import FakeDockerEngine
from waspid.mcp_server.safety import ApprovalGate
from waspid.mcp_server.tools import build_tools


class ScriptedLLM:
    """Quacks like openai.OpenAI().chat.completions; replays a script of model turns."""

    def __init__(self, *turns):
        self.turns, self.requests = list(turns), []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.requests.append(kw)
        turn = self.turns.pop(0)
        message = turn(kw["messages"]) if callable(turn) else turn
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def say(text):
    return SimpleNamespace(content=text, tool_calls=None)


def calls(*pairs):
    return SimpleNamespace(content=None, tool_calls=[
        SimpleNamespace(id=f"call_{i}", type="function",
                        function=SimpleNamespace(name=name, arguments=json.dumps(args)))
        for i, (name, args) in enumerate(pairs)])


def granted(messages):
    return re.search(r"approval_id=(apr_\w+)", messages[-1]["content"]).group(1)


def make_agent(llm, decision):
    docker = FakeDockerEngine()
    gate = ApprovalGate()
    tools = build_tools(docker, gate, [FakeWaspidPlatform(docker)])
    decisions = []

    def decide(card):
        decisions.append(card)
        return decision, "oncall@waspid"

    agent = RunbookAgent(llm, "test-model", tools, gate, AuditLog("run_agent_test"), decide, "SYSTEM PROMPT")
    return docker, agent, decisions


def tool_results(llm):
    return [json.loads(m["content"]) for m in llm.requests[-1]["messages"] if m["role"] == "tool"]


def test_approve_path_executes_once_with_the_granted_token():
    llm = ScriptedLLM(
        calls(("list_containers", {}), ("waspid_api_health", {})),
        calls(("restart_container", {"container": "waspid-api"})),
        lambda msgs: calls(("restart_container", {"container": "waspid-api", "approval_id": granted(msgs)})),
        calls(("container_health", {"container": "waspid-api"})),
        say("All steps succeeded."),
    )
    docker, agent, decisions = make_agent(llm, True)
    result = agent.run("prod.yaml", "steps: …")
    assert result.status == "finished" and result.tool_calls == 5
    assert [c for c in docker.calls if c[0] == "restart_container"] == [("restart_container", "waspid-api")]
    assert len(decisions) == 1 and decisions[0]["target"] == "waspid-api"
    assert any(e["result"] == "approval_granted" and e["detail"]["decided_by"] == "oncall@waspid"
               for e in agent.audit.events)


def test_reject_stops_the_run_in_code():
    llm = ScriptedLLM(calls(("restart_container", {"container": "waspid-api"})), say("Runbook stopped."))
    docker, agent, _ = make_agent(llm, False)
    result = agent.run("prod.yaml", "steps: …")
    assert result.status == "rejected" and result.summary == "Runbook stopped."
    assert llm.requests[-1]["tool_choice"] == "none"      # final report: no tools allowed
    assert not any(c[0] == "restart_container" for c in docker.calls)
    assert any(e["result"] == "STOP RUNBOOK" for e in agent.audit.events)


def test_model_cannot_self_approve_or_forge_a_token():
    llm = ScriptedLLM(
        calls(("approve_request", {"approval_id": "apr_x", "decided_by": "me"})),
        calls(("restart_container", {"container": "waspid-api", "approval_id": "apr_forged"})),
        say("Stopped."),
    )
    docker, agent, decisions = make_agent(llm, False)
    result = agent.run("prod.yaml", "steps: …")
    offered = {t["function"]["name"] for t in llm.requests[0]["tools"]}
    assert "approve_request" not in offered and "reject_request" not in offered
    assert result.status == "rejected"
    assert len(decisions) == 1                              # the forged token only produced a request
    assert not any(c[0] == "restart_container" for c in docker.calls)


def test_calls_after_an_approval_boundary_in_the_same_turn_are_not_executed():
    llm = ScriptedLLM(calls(("restart_container", {"container": "waspid-api"}),
                            ("stop_container", {"container": "waspid-worker"})),
                      say("Stopped."))
    docker, agent, _ = make_agent(llm, False)
    agent.run("prod.yaml", "steps: …")
    results = tool_results(llm)
    assert [r["status"] for r in results] == ["waiting_for_approval", "not_executed"]
    assert not any(c[0] in ("restart_container", "stop_container") for c in docker.calls)


def test_schemas_mirror_the_gate_and_the_mcp_names():
    tools = build_tools(FakeDockerEngine(), ApprovalGate())
    schemas = {t["function"]["name"]: t["function"] for t in tool_schemas(tools)}
    assert "run_sandbox_command" in schemas and "run_sandbox" not in schemas
    assert "approval_id" in schemas["restart_container"]["parameters"]["properties"]
    assert "approval_id" not in schemas["container_health"]["parameters"]["properties"]
    assert "requires_approval: true" in schemas["remove_volume"]["description"]


def test_sandbox_call_runs_in_the_sandbox():
    llm = ScriptedLLM(calls(("run_sandbox_command", {"image": "python:3.12-slim", "command": ["python", "t.py"],
                                                     "files": {"t.py": "print('ok')"}})), say("done"))
    docker, agent, _ = make_agent(llm, True)
    agent.run("prod.yaml", "steps: …")
    assert docker.calls[-1][:2] == ("run_sandbox", "python:3.12-slim")
    assert tool_results(llm)[0]["result"]["sandbox"]["network"] == "none"


# ---- provider selection ----------------------------------------------------
@pytest.mark.parametrize("env, provider, model", [
    ({"OPENAI_API_KEY": "sk-x"}, "openai", "gpt-5-mini"),
    ({"OPENAI_API_KEY": "sk-x", "WASPID_MODEL": "gpt-5"}, "openai", "gpt-5"),
    ({"TFY_API_KEY": "t", "TFY_GATEWAY_BASE_URL": "https://gw.example/api/llm", "WASPID_MODEL": "openai-main/gpt-5"},
     "truefoundry", "openai-main/gpt-5"),
    ({"OPENAI_API_KEY": "sk-x", "TFY_API_KEY": "t", "TFY_GATEWAY_BASE_URL": "https://gw", "WASPID_MODEL": "m",
      "WASPID_LLM_PROVIDER": "openai"}, "openai", "m"),
    ({}, None, None),
])
def test_llm_provider_detection(env, provider, model):
    cfg = detect(env)
    assert (cfg.provider, cfg.model) == (provider, model)


def test_llm_misconfiguration_is_explained_not_guessed():
    cfg = detect({"TFY_API_KEY": "t", "TFY_GATEWAY_BASE_URL": "https://gw"})
    assert not cfg.configured and "WASPID_MODEL" in cfg.error
    assert "OPENAI_API_KEY" in detect({"WASPID_LLM_PROVIDER": "openai"}).error


def test_truefoundry_client_points_at_the_gateway():
    pytest.importorskip("openai")
    env = {"TFY_API_KEY": "tfy-key", "TFY_GATEWAY_BASE_URL": "https://gw.example/api/llm", "WASPID_MODEL": "m"}
    client = make_client(detect(env), env)
    assert str(client.base_url).startswith("https://gw.example/api/llm")
    assert client.api_key == "tfy-key"
