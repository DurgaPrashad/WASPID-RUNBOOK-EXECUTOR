"""Connectors — WASPID API + DB and AWS — sit behind the same approval gate as Docker."""
from pathlib import Path

import pytest

from waspid.connectors import AWSConnector, FakeAWSConnector, FakeWaspidPlatform
from waspid.engine.audit import AuditLog
from waspid.engine.runbook import RunbookEngine, StepStatus, load_runbook
from waspid.mcp_server.docker_engine import FakeDockerEngine
from waspid.mcp_server.safety import ApprovalGate, ApprovalRequired
from waspid.mcp_server.tools import build_tools, invoke

RUNBOOKS = Path(__file__).resolve().parent.parent / "runbooks"


def make(runbook, docker=None, aws=None):
    docker = docker or FakeDockerEngine()
    aws = aws or FakeAWSConnector()
    gate = ApprovalGate()
    tools = build_tools(docker, gate, [FakeWaspidPlatform(docker), aws])
    engine = RunbookEngine(load_runbook(RUNBOOKS / runbook), tools, gate, AuditLog("run_test"))
    return docker, aws, gate, tools, engine


def statuses(engine):
    return {s.id: s.status for s in engine.runbook.steps}


# ---- WASPID API + DB -------------------------------------------------------
def test_platform_health_follows_real_container_state():
    docker = FakeDockerEngine()
    platform = FakeWaspidPlatform(docker)
    assert platform.waspid_api_health()["status"] == "ok"
    assert platform.waspid_db_health()["status"] == "ok"
    docker.containers["waspid-api"]["health"] = "unhealthy"
    api = platform.waspid_api_health()
    assert (api["status"], api["http_status"]) == ("down", 503)
    docker.stop_container("waspid-db")
    assert platform.waspid_db_health()["status"] == "down"


def test_production_runbook_checks_api_and_db_then_completes_with_approvals():
    docker, _, _, _, engine = make("production_deployment.yaml")
    assert engine.run() == "waiting_for_approval"
    assert engine.pending_approval_step.id == "restart"
    st = statuses(engine)
    assert st["api"] is StepStatus.SUCCESSFUL and st["db"] is StepStatus.SUCCESSFUL
    assert engine.approve() == "waiting_for_approval"          # cleanup: remove_image
    assert engine.approve() == "completed"
    assert set(statuses(engine).values()) == {StepStatus.SUCCESSFUL}


def test_production_runbook_halts_before_anything_destructive_when_db_is_down():
    docker = FakeDockerEngine()
    docker.stop_container("waspid-db")
    docker.calls.clear()
    _, _, _, _, engine = make("production_deployment.yaml", docker=docker)
    assert engine.run() == "failed"
    st = statuses(engine)
    assert st["db"] is StepStatus.FAILED and st["restart"] is StepStatus.SKIPPED_STOPPED
    assert not any(c[0] in ("restart_container", "remove_image") for c in docker.calls)


# ---- AWS -------------------------------------------------------------------
def test_aws_destructive_tools_are_gated_single_use_and_target_bound():
    _, aws, gate, tools, _ = make("aws_ecs_redeploy.yaml")
    with pytest.raises(ApprovalRequired):
        tools["aws_ecs_redeploy_service"]("waspid-prod/waspid-api")
    req = gate.request(action="aws_ecs_redeploy_service", target="waspid-prod/waspid-api", reason="r",
                       risk_summary="s", expected_effect="e", next_step="n")
    gate.decide(req.approval_id, approved=True)
    with pytest.raises(ApprovalRequired):   # same token, different destructive action
        tools["aws_reboot_ec2_instance"]("i-0a1b2c3d4e5f60001", approval_id=req.approval_id)
    tools["aws_ecs_redeploy_service"]("waspid-prod/waspid-api", approval_id=req.approval_id)
    with pytest.raises(ApprovalRequired):   # replay
        tools["aws_ecs_redeploy_service"]("waspid-prod/waspid-api", approval_id=req.approval_id)
    assert aws.calls == [("aws_ecs_redeploy_service", "waspid-prod/waspid-api")]


def test_aws_runbook_approve_path_waits_for_steady_state():
    _, aws, _, _, engine = make("aws_ecs_redeploy.yaml")
    assert engine.run() == "waiting_for_approval"
    assert engine.pending_approval_step.id == "redeploy" and aws.calls == []
    assert engine.approve(decided_by="oncall") == "completed"
    assert aws.calls == [("aws_ecs_redeploy_service", "waspid-prod/waspid-api")]
    assert engine.runbook.steps[4].result["status"] == "steady"


def test_aws_runbook_reject_path_never_touches_ecs():
    _, aws, _, _, engine = make("aws_ecs_redeploy.yaml")
    engine.run()
    assert engine.reject() == "rejected"
    st = statuses(engine)
    assert st["redeploy"] is StepStatus.REJECTED
    assert st["stable"] is StepStatus.SKIPPED_STOPPED and st["alarms_after"] is StepStatus.SKIPPED_STOPPED
    assert aws.calls == []


def test_firing_alarm_blocks_the_aws_runbook():
    aws = FakeAWSConnector()
    aws.alarms.append({"name": "waspid-api-5xx", "reason": "Threshold crossed"})
    _, _, _, _, engine = make("aws_ecs_redeploy.yaml", aws=aws)
    assert engine.run() == "failed"
    assert engine.runbook.steps[0].status is StepStatus.FAILED
    assert aws.calls == []


def test_real_aws_connector_speaks_the_aws_api():
    """botocore's Stubber validates every request and response against the AWS API models."""
    boto3 = pytest.importorskip("boto3")
    from botocore.stub import Stubber

    session = boto3.session.Session(aws_access_key_id="testing", aws_secret_access_key="testing",
                                    region_name="us-east-1")
    aws = AWSConnector(session)
    gate = ApprovalGate()
    tools = build_tools(FakeDockerEngine(), gate, [aws])
    svc = {"serviceName": "waspid-api", "status": "ACTIVE", "desiredCount": 2, "runningCount": 2, "pendingCount": 0,
           "taskDefinition": "arn:aws:ecs:us-east-1:123456789012:task-definition/waspid-api:42",
           "deployments": [{"id": "ecs-svc/1", "status": "PRIMARY", "rolloutState": "COMPLETED",
                            "runningCount": 2, "desiredCount": 2}],
           "events": [{"message": "(service waspid-api) has reached a steady state."}]}
    rolling = {**svc, "deployments": [{"id": "ecs-svc/2", "status": "PRIMARY", "rolloutState": "IN_PROGRESS",
                                       "runningCount": 0, "desiredCount": 2}, svc["deployments"][0]]}

    with Stubber(aws._client("ecs")) as ecs, Stubber(aws._client("rds")) as rds, \
            Stubber(aws._client("cloudwatch")) as cw, Stubber(aws._client("ec2")) as ec2:
        ecs.add_response("describe_services", {"services": [svc], "failures": []},
                         {"cluster": "waspid-prod", "services": ["waspid-api"]})
        ecs.add_response("update_service", {"service": rolling},
                         {"cluster": "waspid-prod", "service": "waspid-api", "forceNewDeployment": True})
        rds.add_response("describe_db_instances", {"DBInstances": [{
            "DBInstanceIdentifier": "waspid-db-prod", "DBInstanceStatus": "available", "Engine": "postgres",
            "EngineVersion": "16.4", "DBInstanceClass": "db.t4g.medium", "MultiAZ": True}]},
            {"DBInstanceIdentifier": "waspid-db-prod"})
        cw.add_response("describe_alarms", {"MetricAlarms": [{
            "AlarmName": "waspid-api-5xx", "StateValue": "ALARM", "StateReason": "Threshold Crossed",
            "Namespace": "AWS/ApplicationELB", "MetricName": "HTTPCode_Target_5XX_Count"}]},
            {"StateValue": "ALARM", "MaxRecords": 100, "AlarmNamePrefix": "waspid"})
        ec2.add_response("describe_instances", {"Reservations": [{"Instances": [{
            "InstanceId": "i-0abc", "InstanceType": "t3.small", "State": {"Code": 16, "Name": "running"},
            "Placement": {"AvailabilityZone": "us-east-1a"}, "Tags": [{"Key": "Name", "Value": "waspid-api-1"}]}]}]}, {})
        ec2.add_response("stop_instances", {"StoppingInstances": [{
            "InstanceId": "i-0abc", "CurrentState": {"Code": 64, "Name": "stopping"},
            "PreviousState": {"Code": 16, "Name": "running"}}]}, {"InstanceIds": ["i-0abc"]})

        assert tools["aws_ecs_service_status"]("waspid-prod/waspid-api")["status"] == "steady"
        blocked = invoke(tools, "aws_ecs_redeploy_service", "waspid-prod/waspid-api")
        assert blocked["status"] == "waiting_for_approval"          # no AWS call was made
        token = blocked["approval_card"]["approval_id"]
        gate.decide(token, approved=True)
        redeploy = invoke(tools, "aws_ecs_redeploy_service", "waspid-prod/waspid-api", approval_id=token)
        assert redeploy["ok"] and redeploy["result"]["status"] == "deploying"
        assert tools["aws_rds_status"]("waspid-db-prod")["status"] == "available"
        alarms = tools["aws_cloudwatch_alarms"]("waspid")
        assert alarms["status"] == "alarm" and alarms["alarms"][0]["name"] == "waspid-api-5xx"
        ec2_list = tools["aws_list_ec2_instances"]()
        assert ec2_list["instances"] == [{"id": "i-0abc", "name": "waspid-api-1", "state": "running",
                                          "type": "t3.small", "az": "us-east-1a"}]
        stop = invoke(tools, "aws_stop_ec2_instance", "i-0abc")
        gate.decide(stop["approval_card"]["approval_id"], approved=True)
        stopped = invoke(tools, "aws_stop_ec2_instance", "i-0abc", approval_id=stop["approval_card"]["approval_id"])
        assert stopped["result"]["status"] == "stopping"
        for stub in (ecs, rds, cw, ec2):
            stub.assert_no_pending_responses()


# ---- registry --------------------------------------------------------------
def test_connectors_cannot_shadow_existing_tools():
    with pytest.raises(ValueError, match="duplicate tool name"):
        build_tools(FakeDockerEngine(), ApprovalGate(), [FakeAWSConnector(), FakeAWSConnector()])


def test_invoke_reports_unknown_tools_instead_of_raising():
    tools = build_tools(FakeDockerEngine(), ApprovalGate())
    out = invoke(tools, "aws_list_ec2_instances")
    assert out["ok"] is False and "unknown or disabled tool" in out["message"]
