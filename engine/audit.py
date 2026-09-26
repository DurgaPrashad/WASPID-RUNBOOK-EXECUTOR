"""Structured, append-only audit log (JSONL on disk + in-memory)."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional


class AuditLog:
    def __init__(self, run_id: str, path: Optional[Path] = None) -> None:
        self.run_id = run_id
        self.path = path
        self.events: List[Dict[str, Any]] = []

    def record(self, *, step: str, action: str, tool: str, risk: str, result: str,
               target: str = "", approval_required: bool = False,
               approval_status: Optional[str] = None, detail: Any = None) -> Dict[str, Any]:
        event = {
            "run_id": self.run_id,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "step": step, "action": action, "tool": tool, "target": target,
            "risk": risk, "approval_required": approval_required,
            "approval_status": approval_status, "result": result, "detail": detail,
        }
        self.events.append(event)
        if self.path:
            with open(self.path, "a") as f:
                f.write(json.dumps(event, default=str) + "\n")
        return event
