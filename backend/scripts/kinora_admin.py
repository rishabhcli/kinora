#!/usr/bin/env python3
"""Convenience shim for the Kinora admin / operations CLI.

Equivalent to ``python -m app.cli`` and the ``kinora-admin`` console script
(``[project.scripts]`` in ``pyproject.toml``); handy when running straight from a
venv checkout without an editable install::

    backend/.venv/bin/python backend/scripts/kinora_admin.py doctor
    backend/.venv/bin/python backend/scripts/kinora_admin.py budget report -f json

All logic lives in :mod:`app.cli`; this file only forwards argv + the exit code.
"""

from __future__ import annotations

import sys

from app.cli.main import main

if __name__ == "__main__":
    sys.exit(main())
