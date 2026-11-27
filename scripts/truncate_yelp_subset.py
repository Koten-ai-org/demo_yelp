#!/usr/bin/env python3
"""Add a key/value pair to every object in a JSON or JSONL file.

Compatibility entry point. The implementation is add_json_field.py in this
directory; run that script directly. This file calls the same main.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _load():
    path = Path(__file__).resolve().parent / "add_json_field.py"
    existing = sys.modules.get("add_json_field")
    if existing is not None:
        existing_file = getattr(existing, "__file__", None)
        if existing_file and Path(existing_file).resolve() == path:
            return existing
    spec = importlib.util.spec_from_file_location("add_json_field", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["add_json_field"] = module
    spec.loader.exec_module(module)
    return module


def main(argv: list[str] | None = None) -> int:
    return _load().main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
