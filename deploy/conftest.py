"""Pytest config for the deploy/ orchestration tests.

Ensures the repo root is importable (so ``import deploy.orchestrator`` resolves
when pytest is invoked from anywhere) and enables asyncio auto mode so the
async orchestrator tests need no per-test decorator.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
