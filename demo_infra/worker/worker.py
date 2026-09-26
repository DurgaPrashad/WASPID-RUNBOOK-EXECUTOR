"""Tiny stdlib-only demo worker: heartbeats every 5 seconds."""
from __future__ import annotations

import os
import time
from pathlib import Path

HEARTBEAT_FILE = Path(os.environ.get("HEARTBEAT_FILE", "/tmp/heartbeat"))
VERSION = os.environ.get("WASPID_VERSION", "1.4.2")

if __name__ == "__main__":
    print(f"[waspid-worker] v{VERSION} started", flush=True)
    n = 0
    while True:
        n += 1
        HEARTBEAT_FILE.touch()
        print(f"[waspid-worker] heartbeat #{n} ts={time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}", flush=True)
        time.sleep(5)
