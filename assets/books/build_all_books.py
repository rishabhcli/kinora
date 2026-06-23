#!/usr/bin/env python3
"""Build every bundled public-domain Kinora demo book PDF."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
BUILDERS = (
    "build_demo_pdf.py",
    "build_little_red_riding_hood_pdf.py",
)


def _load_builder(name: str):
    path = HERE / name
    spec = importlib.util.spec_from_file_location(path.stem, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"failed to load {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    for name in BUILDERS:
        mod = _load_builder(name)
        path = mod.build()
        info = mod.verify(path)
        print(f"\nBuilt + verified {name}:")
        for key, value in info.items():
            print(f"  {key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
