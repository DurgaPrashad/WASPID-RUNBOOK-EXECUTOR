"""Docker engine adapters.

RealDockerEngine wraps the docker SDK (import is lazy — only needed at runtime).
FakeDockerEngine simulates the same interface for tests / no-daemon environments.
No tool ever fabricates success: every result reflects actual engine state.
"""
from __future__ import annotations

import os
import secrets
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol

SANDBOX_LIMITS = {"network": "none", "memory": "256m", "cpus": 0.5, "user": "1000:1000"}

# docker-py ignores `docker context`, so find the socket of common local runtimes.
_SOCKET_CANDIDATES = (
    "/var/run/docker.sock",
    "~/.docker/run/docker.sock",        # Docker Desktop
    "~/.colima/default/docker.sock",    # Colima
    "~/.colima/docker.sock",
    "~/.orbstack/run/docker.sock",      # OrbStack
    "~/.rd/docker.sock",                # Rancher Desktop
)


def _connect():
    import docker  # runtime dependency

    if not os.environ.get("DOCKER_HOST"):
        for candidate in _SOCKET_CANDIDATES:
            sock = Path(candidate).expanduser()
            if sock.exists():
                return docker.DockerClient(base_url=f"unix://{sock}")
    return docker.from_env()


class DockerEngine(Protocol):
    def list_containers(self) -> List[Dict[str, Any]]: ...
    def inspect_container(self, name: str) -> Dict[str, Any]: ...
    def container_logs(self, name: str, tail: int = 100) -> str: ...
    def container_health(self, name: str) -> Dict[str, Any]: ...
    def list_images(self) -> List[Dict[str, Any]]: ...
    def inspect_network(self, name: str = "bridge") -> Dict[str, Any]: ...
    def restart_container(self, name: str) -> Dict[str, Any]: ...
    def stop_container(self, name: str) -> Dict[str, Any]: ...
    def remove_container(self, name: str) -> Dict[str, Any]: ...
    def remove_image(self, image: str) -> Dict[str, Any]: ...
    def remove_volume(self, volume: str) -> Dict[str, Any]: ...
    def run_sandbox(self, image: str, command: List[str], files: Dict[str, str],
                    timeout: int = 120) -> Dict[str, Any]: ...


class RealDockerEngine:
    """Talks to a real local Docker daemon via the docker SDK."""

    SANDBOX_LABEL = {"waspid.sandbox": "true"}

    def __init__(self) -> None:
        self._client = _connect()
        self._client.ping()

    def describe(self) -> Dict[str, Any]:
        v = self._client.version()
        return {"kind": "docker", "version": v.get("Version"), "os": v.get("Os"),
                "arch": v.get("Arch"), "api": v.get("ApiVersion")}

    def list_containers(self) -> List[Dict[str, Any]]:
        out = []
        for c in self._client.containers.list(all=True):
            image = c.attrs.get("Config", {}).get("Image") or c.attrs.get("Image", "")
            out.append({"name": c.name, "image": image, "status": c.status,
                        "id": c.short_id, "labels": c.labels})
        return out

    def inspect_container(self, name: str) -> Dict[str, Any]:
        c = self._client.containers.get(name)
        a = c.attrs
        return {
            "name": c.name,
            "id": c.short_id,
            "image": a["Config"]["Image"],
            "status": a["State"]["Status"],
            "started_at": a["State"]["StartedAt"],
            "restart_count": a.get("RestartCount", 0),
            "env_keys": [e.split("=", 1)[0] for e in a["Config"].get("Env", [])],  # never leak values
            "ports": a["NetworkSettings"].get("Ports", {}),
            "labels": a["Config"].get("Labels", {}),
        }

    def container_logs(self, name: str, tail: int = 100) -> str:
        return self._client.containers.get(name).logs(tail=tail).decode(errors="replace")

    def container_health(self, name: str) -> Dict[str, Any]:
        c = self._client.containers.get(name)
        state = c.attrs["State"]
        health = state.get("Health", {})
        return {
            "name": name,
            "status": state["Status"],
            "health": health.get("Status", "none"),
            "failing_streak": health.get("FailingStreak", 0),
        }

    def list_images(self) -> List[Dict[str, Any]]:
        return [{"tags": i.tags, "id": i.short_id, "size": i.attrs.get("Size")} for i in self._client.images.list()]

    def inspect_network(self, name: str = "bridge") -> Dict[str, Any]:
        n = self._client.networks.get(name)
        return {"name": n.name, "id": n.short_id,
                "containers": [c.name for c in n.containers]}

    # --- destructive (only reachable through the approval gate) ---
    def restart_container(self, name: str) -> Dict[str, Any]:
        c = self._client.containers.get(name)
        c.restart(timeout=10)
        c.reload()
        return {"name": name, "status": c.status, "action": "restarted"}

    def stop_container(self, name: str) -> Dict[str, Any]:
        c = self._client.containers.get(name)
        c.stop(timeout=10)
        c.reload()
        return {"name": name, "status": c.status, "action": "stopped"}

    def remove_container(self, name: str) -> Dict[str, Any]:
        self._client.containers.get(name).remove(force=False)
        return {"name": name, "action": "removed"}

    def remove_image(self, image: str) -> Dict[str, Any]:
        self._client.images.remove(image)
        return {"image": image, "action": "removed"}

    def remove_volume(self, volume: str) -> Dict[str, Any]:
        self._client.volumes.get(volume).remove()
        return {"volume": volume, "action": "removed"}

    # --- dashboard-only helpers (not exposed as MCP tools) ---
    def container_stats(self, name: str) -> Dict[str, Any]:
        s = self._client.containers.get(name).stats(stream=False)
        cpu, pre = s.get("cpu_stats", {}), s.get("precpu_stats", {})
        cpu_delta = cpu.get("cpu_usage", {}).get("total_usage", 0) - pre.get("cpu_usage", {}).get("total_usage", 0)
        sys_delta = cpu.get("system_cpu_usage", 0) - pre.get("system_cpu_usage", 0)
        ncpu = cpu.get("online_cpus") or len(cpu.get("cpu_usage", {}).get("percpu_usage") or []) or 1
        mem = s.get("memory_stats", {})
        used = mem.get("usage", 0) - mem.get("stats", {}).get("inactive_file", 0)
        return {"cpu_percent": round(cpu_delta / sys_delta * ncpu * 100, 2) if sys_delta > 0 else 0.0,
                "mem_bytes": max(used, 0), "mem_limit": mem.get("limit", 0)}

    def image_exists(self, image: str) -> bool:
        import docker

        try:
            self._client.images.get(image)
            return True
        except docker.errors.ImageNotFound:
            return False

    def build_image(self, path: str, tag: str, buildargs: Optional[Dict[str, str]] = None) -> None:
        self._client.images.build(path=path, tag=tag, buildargs=buildargs or {}, rm=True)

    def pull_image(self, image: str) -> None:
        self._client.images.pull(image)

    # --- sandbox: generated code runs ONLY here (isolated container) ---
    def run_sandbox(self, image: str, command: List[str], files: Dict[str, str],
                    timeout: int = 120) -> Dict[str, Any]:
        import tarfile, io, docker

        if not self.image_exists(image):
            self._client.images.pull(image)
        name = f"waspid-sandbox-{secrets.token_hex(3)}"
        started = time.time()
        container = self._client.containers.create(
            image, command=command, working_dir="/work", name=name,
            network_mode=SANDBOX_LIMITS["network"],   # no network for generated code
            mem_limit=SANDBOX_LIMITS["memory"], nano_cpus=int(SANDBOX_LIMITS["cpus"] * 1e9),
            labels=dict(self.SANDBOX_LABEL),
            user=SANDBOX_LIMITS["user"], read_only=False,
        )
        try:
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w") as tar:
                for path, content in files.items():
                    data = content.encode()
                    info = tarfile.TarInfo(name=path)
                    info.size = len(data)
                    tar.addfile(info, io.BytesIO(data))
            buf.seek(0)
            container.put_archive("/work", buf)
            container.start()
            result = container.wait(timeout=timeout)
            logs = container.logs(stdout=True, stderr=True).decode(errors="replace")
            return {"exit_code": result.get("StatusCode", -1), "output": logs[-20000:],
                    "sandbox": {"container": name, "image": image, **SANDBOX_LIMITS,
                                "duration_s": round(time.time() - started, 2)}}
        finally:
            container.remove(force=True)


class FakeDockerEngine:
    """Deterministic in-memory Docker for tests and daemon-less dev."""

    def __init__(self) -> None:
        now = time.time()
        self.containers: Dict[str, Dict[str, Any]] = {
            n: {"name": n, "image": f"waspid/{n.split('-')[1]}:1.4.2", "status": "running",
                "health": "healthy", "restart_count": 0, "started_at": now}
            for n in ("waspid-api", "waspid-worker", "waspid-db")
        }
        self.images = [{"tags": ["waspid/api:1.4.2"], "id": "sha:fake", "size": 12345}]
        self.volumes = {"waspid-db-data": {}}
        self.calls: List[tuple] = []
        self.sandbox_exit_code = 0
        self.sandbox_output = "3 passed in 0.12s"

    def describe(self):
        return {"kind": "fake", "version": None}

    def _c(self, name):
        if name not in self.containers:
            raise KeyError(f"No such container: {name}")
        return self.containers[name]

    def list_containers(self):
        self.calls.append(("list_containers",))
        return [dict(c) for c in self.containers.values()]

    def inspect_container(self, name):
        self.calls.append(("inspect_container", name))
        return dict(self._c(name))

    def container_logs(self, name, tail=100):
        self._c(name)
        return f"[{name}] INFO ready — version 1.4.2\n" * min(tail, 3)

    def container_health(self, name):
        c = self._c(name)
        return {"name": name, "status": c["status"], "health": c["health"], "failing_streak": 0}

    def list_images(self):
        return [dict(i) for i in self.images]

    def inspect_network(self, name="bridge"):
        return {"name": name, "id": "netfake", "containers": list(self.containers)}

    def restart_container(self, name):
        c = self._c(name)
        self.calls.append(("restart_container", name))
        c["restart_count"] += 1
        c["status"], c["health"] = "running", "healthy"
        return {"name": name, "status": "running", "action": "restarted"}

    def stop_container(self, name):
        c = self._c(name)
        self.calls.append(("stop_container", name))
        c["status"], c["health"] = "exited", "none"
        return {"name": name, "status": "exited", "action": "stopped"}

    def remove_container(self, name):
        self._c(name)
        self.calls.append(("remove_container", name))
        del self.containers[name]
        return {"name": name, "action": "removed"}

    def remove_image(self, image):
        self.calls.append(("remove_image", image))
        self.images = [i for i in self.images if image not in i["tags"]]
        return {"image": image, "action": "removed"}

    def remove_volume(self, volume):
        self.calls.append(("remove_volume", volume))
        self.volumes.pop(volume, None)
        return {"volume": volume, "action": "removed"}

    def container_stats(self, name):
        self._c(name)
        return {"cpu_percent": 0.0, "mem_bytes": 0, "mem_limit": 0}

    def run_sandbox(self, image, command, files, timeout=120):
        self.calls.append(("run_sandbox", image, tuple(command), tuple(files)))
        return {"exit_code": self.sandbox_exit_code, "output": self.sandbox_output,
                "sandbox": {"container": "fake-sandbox", "image": image, **SANDBOX_LIMITS,
                            "duration_s": 0.0}}
