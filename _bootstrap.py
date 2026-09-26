"""Register this project directory as the `waspid` package.

All modules import each other as `waspid.*`, which only resolves when the
checkout folder is literally named `waspid`. Importing this module makes those
imports work whatever the folder is called (e.g. `waspid-runbook-executor`).
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

if "waspid" not in sys.modules:
    _spec = importlib.util.spec_from_file_location(
        "waspid", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    _module = importlib.util.module_from_spec(_spec)
    sys.modules["waspid"] = _module
    _spec.loader.exec_module(_module)
