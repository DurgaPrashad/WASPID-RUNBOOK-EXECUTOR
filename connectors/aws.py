"""AWS connector: EC2, ECS, RDS and CloudWatch through boto3.

Opt-in with WASPID_ENABLE_AWS=1 (or WASPID_FAKE_AWS=1 for the in-memory fake).
Credentials come from the standard AWS chain (env vars, AWS_PROFILE, SSO, IAM
role) and the region from AWS_REGION — never from code.

Destructive tools (reboot/stop EC2, force-redeploy ECS, reboot RDS) sit behind
the same ApprovalGate as Docker's: the approval token is single-use and bound
to the exact instance / service / database id.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from waspid.mcp_server.safety import Risk, ToolSpec
from waspid.mcp_server.tools import arg

EC2 = {"instance_id": arg("EC2 instance id, e.g. i-0abc123")}
ECS = {"service": arg("ECS service as cluster/service, e.g. waspid-prod/waspid-api")}
RDS = {"db_instance": arg("RDS DB instance identifier, e.g. waspid-db-prod")}
MAX_INSTANCES = 200

AWS_TOOL_SPECS = {
    # read-only
    "aws_list_ec2_instances": ToolSpec("aws_list_ec2_instances", Risk.READ_ONLY,
                                       "List EC2 instances in the configured region (id, name, state, type)"),
    "aws_cloudwatch_alarms": ToolSpec("aws_cloudwatch_alarms", Risk.READ_ONLY,
                                      "CloudWatch alarms currently firing; status is 'ok' when none are",
                                      {"prefix": arg("Only alarms whose name starts with this")}, ("prefix",)),
    "aws_ecs_service_status": ToolSpec("aws_ecs_service_status", Risk.READ_ONLY,
                                       "ECS service rollout state (steady | deploying | failed | inactive) "
                                       "with running / desired task counts", ECS),
    "aws_ecs_wait_stable": ToolSpec("aws_ecs_wait_stable", Risk.READ_ONLY,
                                    "Wait for an ECS service to reach a steady state, then report it",
                                    {**ECS, "timeout": arg("Max seconds to wait (default 600)", "integer")},
                                    ("timeout",)),
    "aws_rds_status": ToolSpec("aws_rds_status", Risk.READ_ONLY,
                               "RDS instance status, engine, class and Multi-AZ", RDS),
    # destructive — approval enforced by the gate
    "aws_reboot_ec2_instance": ToolSpec("aws_reboot_ec2_instance", Risk.DESTRUCTIVE,
                                        "Reboot an EC2 instance (service interruption)", EC2),
    "aws_stop_ec2_instance": ToolSpec("aws_stop_ec2_instance", Risk.DESTRUCTIVE,
                                      "Stop an EC2 instance (takes it offline)", EC2),
    "aws_ecs_redeploy_service": ToolSpec("aws_ecs_redeploy_service", Risk.DESTRUCTIVE,
                                         "Force a new deployment of an ECS service (replaces every running task)", ECS),
    "aws_reboot_rds_instance": ToolSpec("aws_reboot_rds_instance", Risk.DESTRUCTIVE,
                                        "Reboot an RDS instance (connections drop during the reboot)", RDS),
}


def _split_service(service: str) -> Tuple[str, str]:
    cluster, _, name = service.partition("/")
    if not cluster or not name:
        raise ValueError(f"expected cluster/service, got {service!r}")
    return cluster, name


def _ecs_summary(cluster: str, svc: Dict[str, Any]) -> Dict[str, Any]:
    deployments = svc.get("deployments", [])
    primary = next((d for d in deployments if d.get("status") == "PRIMARY"), {})
    rollout = primary.get("rolloutState")
    if svc.get("status") != "ACTIVE":
        state = "inactive"
    elif rollout == "FAILED":
        state = "failed"
    elif len(deployments) <= 1 and svc.get("runningCount") == svc.get("desiredCount") and rollout in (None, "COMPLETED"):
        state = "steady"
    else:
        state = "deploying"
    return {
        "service": f"{cluster}/{svc['serviceName']}", "status": state,
        "desired": svc.get("desiredCount"), "running": svc.get("runningCount"), "pending": svc.get("pendingCount"),
        "task_definition": (svc.get("taskDefinition") or "").rsplit("/", 1)[-1],
        "deployments": [{"id": d.get("id"), "status": d.get("status"), "rollout": d.get("rolloutState"),
                         "running": d.get("runningCount"), "desired": d.get("desiredCount")} for d in deployments],
        "recent_events": [e.get("message") for e in svc.get("events", [])[:3]],
    }


class AWSConnector:
    TOOL_SPECS = AWS_TOOL_SPECS

    def __init__(self, session: Any = None) -> None:
        if session is None:
            import boto3  # runtime dependency, only when AWS is enabled
            session = boto3.session.Session()
        self._session = session
        self._clients: Dict[str, Any] = {}

    @property
    def region(self) -> Optional[str]:
        return self._session.region_name

    def _client(self, service: str) -> Any:
        if service not in self._clients:
            self._clients[service] = self._session.client(service)
        return self._clients[service]

    # ---- read-only ---------------------------------------------------------
    def aws_list_ec2_instances(self) -> Dict[str, Any]:
        rows: List[Dict[str, Any]] = []
        for page in self._client("ec2").get_paginator("describe_instances").paginate():
            for reservation in page["Reservations"]:
                for i in reservation["Instances"]:
                    tags = {t["Key"]: t["Value"] for t in i.get("Tags", [])}
                    rows.append({"id": i["InstanceId"], "name": tags.get("Name"), "state": i["State"]["Name"],
                                 "type": i.get("InstanceType"),
                                 "az": i.get("Placement", {}).get("AvailabilityZone")})
        return {"region": self.region, "count": len(rows), "instances": rows[:MAX_INSTANCES]}

    def aws_cloudwatch_alarms(self, prefix: str = "") -> Dict[str, Any]:
        kwargs: Dict[str, Any] = {"StateValue": "ALARM", "MaxRecords": 100}
        if prefix:
            kwargs["AlarmNamePrefix"] = prefix
        alarms = self._client("cloudwatch").describe_alarms(**kwargs).get("MetricAlarms", [])
        rows = [{"name": a["AlarmName"], "reason": a.get("StateReason"),
                 "metric": f"{a.get('Namespace')}/{a.get('MetricName')}",
                 "since": a.get("StateUpdatedTimestamp")} for a in alarms]
        return {"region": self.region, "status": "ok" if not rows else "alarm", "firing": len(rows), "alarms": rows}

    def aws_ecs_service_status(self, service: str) -> Dict[str, Any]:
        cluster, name = _split_service(service)
        resp = self._client("ecs").describe_services(cluster=cluster, services=[name])
        if not resp.get("services"):
            reason = resp["failures"][0].get("reason") if resp.get("failures") else "not found"
            raise LookupError(f"ECS service {service}: {reason}")
        return _ecs_summary(cluster, resp["services"][0])

    def aws_ecs_wait_stable(self, service: str, timeout: int = 600) -> Dict[str, Any]:
        from botocore.exceptions import WaiterError

        cluster, name = _split_service(service)
        try:
            self._client("ecs").get_waiter("services_stable").wait(
                cluster=cluster, services=[name],
                WaiterConfig={"Delay": 15, "MaxAttempts": max(1, int(timeout) // 15)})
        except WaiterError:
            pass  # not stable in time — the fresh status below is the evidence
        return self.aws_ecs_service_status(service)

    def aws_rds_status(self, db_instance: str) -> Dict[str, Any]:
        d = self._client("rds").describe_db_instances(DBInstanceIdentifier=db_instance)["DBInstances"][0]
        return {"db_instance": db_instance, "status": d["DBInstanceStatus"],
                "engine": f"{d.get('Engine')} {d.get('EngineVersion')}", "class": d.get("DBInstanceClass"),
                "multi_az": d.get("MultiAZ")}

    # ---- destructive (only reachable through the approval gate) ------------
    def aws_reboot_ec2_instance(self, instance_id: str) -> Dict[str, Any]:
        self._client("ec2").reboot_instances(InstanceIds=[instance_id])
        return {"instance_id": instance_id, "action": "reboot requested"}

    def aws_stop_ec2_instance(self, instance_id: str) -> Dict[str, Any]:
        r = self._client("ec2").stop_instances(InstanceIds=[instance_id])["StoppingInstances"][0]
        return {"instance_id": instance_id, "action": "stopped", "status": r["CurrentState"]["Name"]}

    def aws_ecs_redeploy_service(self, service: str) -> Dict[str, Any]:
        cluster, name = _split_service(service)
        svc = self._client("ecs").update_service(cluster=cluster, service=name, forceNewDeployment=True)["service"]
        return {**_ecs_summary(cluster, svc), "action": "new deployment started"}

    def aws_reboot_rds_instance(self, db_instance: str) -> Dict[str, Any]:
        d = self._client("rds").reboot_db_instance(DBInstanceIdentifier=db_instance)["DBInstance"]
        return {"db_instance": db_instance, "action": "reboot requested", "status": d["DBInstanceStatus"]}

    # ---- dashboard -----------------------------------------------------------
    def status(self) -> List[Dict[str, Any]]:
        try:
            self._client("sts").get_caller_identity()  # proves the credentials work
            return [{"key": "aws", "name": "AWS", "ok": True, "detail": f"{self.region or 'no region set'} · authenticated"}]
        except Exception as e:  # noqa: BLE001 — surfaced in the UI
            return [{"key": "aws", "name": "AWS", "ok": False, "detail": str(e).split("\n")[0][:160]}]


class FakeAWSConnector:
    """Deterministic in-memory AWS for tests, demos and CI (WASPID_FAKE_AWS=1)."""

    TOOL_SPECS = AWS_TOOL_SPECS
    region = "us-east-1"

    def __init__(self) -> None:
        self.instances = {
            "i-0a1b2c3d4e5f60001": {"name": "waspid-api-1", "state": "running", "type": "t3.small", "az": "us-east-1a"},
            "i-0a1b2c3d4e5f60002": {"name": "waspid-worker-1", "state": "running", "type": "t3.small", "az": "us-east-1b"},
        }
        self.services = {"waspid-prod/waspid-api": {"desired": 2, "running": 2, "active": True, "rolling": False,
                                                    "task_definition": "waspid-api:42"}}
        self.databases = {"waspid-db-prod": {"status": "available", "engine": "postgres 16.4",
                                             "class": "db.t4g.medium", "multi_az": True}}
        self.alarms: List[Dict[str, Any]] = []
        self.calls: List[tuple] = []

    def _get(self, table: Dict[str, Any], key: str, kind: str) -> Dict[str, Any]:
        if key not in table:
            raise LookupError(f"{kind} not found: {key}")
        return table[key]

    def _ecs(self, service: str) -> Dict[str, Any]:
        s = self._get(self.services, service, "ECS service")
        return {"service": service, "status": "deploying" if s["rolling"] else "steady" if s["active"] else "inactive",
                "desired": s["desired"], "running": s["running"], "pending": 0,
                "task_definition": s["task_definition"], "deployments": [], "recent_events": []}

    def aws_list_ec2_instances(self) -> Dict[str, Any]:
        rows = [{"id": k, **v} for k, v in self.instances.items()]
        return {"region": self.region, "count": len(rows), "instances": rows}

    def aws_cloudwatch_alarms(self, prefix: str = "") -> Dict[str, Any]:
        rows = [a for a in self.alarms if a["name"].startswith(prefix)]
        return {"region": self.region, "status": "ok" if not rows else "alarm", "firing": len(rows), "alarms": rows}

    def aws_ecs_service_status(self, service: str) -> Dict[str, Any]:
        return self._ecs(service)

    def aws_ecs_wait_stable(self, service: str, timeout: int = 600) -> Dict[str, Any]:
        self._get(self.services, service, "ECS service")["rolling"] = False
        return self._ecs(service)

    def aws_rds_status(self, db_instance: str) -> Dict[str, Any]:
        return {"db_instance": db_instance, **self._get(self.databases, db_instance, "RDS instance")}

    def aws_reboot_ec2_instance(self, instance_id: str) -> Dict[str, Any]:
        self._get(self.instances, instance_id, "EC2 instance")
        self.calls.append(("aws_reboot_ec2_instance", instance_id))
        return {"instance_id": instance_id, "action": "reboot requested"}

    def aws_stop_ec2_instance(self, instance_id: str) -> Dict[str, Any]:
        self._get(self.instances, instance_id, "EC2 instance")["state"] = "stopping"
        self.calls.append(("aws_stop_ec2_instance", instance_id))
        return {"instance_id": instance_id, "action": "stopped", "status": "stopping"}

    def aws_ecs_redeploy_service(self, service: str) -> Dict[str, Any]:
        self._get(self.services, service, "ECS service")["rolling"] = True
        self.calls.append(("aws_ecs_redeploy_service", service))
        return {**self._ecs(service), "action": "new deployment started"}

    def aws_reboot_rds_instance(self, db_instance: str) -> Dict[str, Any]:
        self._get(self.databases, db_instance, "RDS instance")
        self.calls.append(("aws_reboot_rds_instance", db_instance))
        return {"db_instance": db_instance, "action": "reboot requested", "status": "rebooting"}

    def status(self) -> List[Dict[str, Any]]:
        return [{"key": "aws", "name": "AWS", "ok": True, "detail": f"simulated · {self.region}"}]
