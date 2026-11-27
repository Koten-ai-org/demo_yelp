#!/usr/bin/env python3
"""
Add a key/value pair to every object in a JSON file.

Supports:
  - JSON Lines (JSONL / NDJSON): one JSON object per line (Yelp dataset style)
  - JSON array: [ {...}, {...}, ... ]
  - Single JSON object: { ... }

Examples:
  # Add type=User to every line, write to a new file
  python add_json_field.py yelp_academic_dataset_user.json -k type -v User -o users_with_type.json

  # Replace the input. -o uses the same temp file and atomic rename.
  python add_json_field.py data.json -k type -v User --in-place

  # Numeric / JSON values
  python add_json_field.py data.json -k active -v true --value-type bool
  python add_json_field.py data.json -k score -v 10 --value-type int
  python add_json_field.py data.json -k meta -v '{"source":"yelp"}' --value-type json
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, TextIO


class InputError(Exception):
    """Bad input. main() prints the message and returns 1."""


def parse_value(raw: str, value_type: str) -> Any:
    """Convert CLI string to the requested Python/JSON type."""
    if value_type == "str":
        return raw
    if value_type == "int":
        return int(raw)
    if value_type == "float":
        return float(raw)
    if value_type == "bool":
        lowered = raw.strip().lower()
        if lowered in {"true", "1", "yes", "y"}:
            return True
        if lowered in {"false", "0", "no", "n"}:
            return False
        raise ValueError(f"Cannot parse bool from: {raw!r}")
    if value_type == "null":
        return None
    if value_type == "json":
        return json.loads(raw)
    raise ValueError(f"Unknown value type: {value_type}")


_NOT_JSON = object()


def _next_nonempty(handle: TextIO) -> str | None:
    while True:
        line = handle.readline()
        if not line:
            return None
        stripped = line.strip()
        if stripped:
            return stripped


def _parse_line(line: str) -> Any:
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        return _NOT_JSON


def detect_format(path: Path) -> str:
    """
    Return 'jsonl', 'array', or 'object'.

    A line that parses as one JSON object is JSONL, including a one-record
    file such as `head -n 1`. A file whose only JSON value is an array stays
    an array, so each element receives the field. A line that does not parse
    is pretty-printed: '[' is an array and '{' is one object. Counting '{'
    prefixes misreads a nested pretty-printed object as JSONL.

    Yelp records can be hundreds of KB, so this reads full lines.
    """
    with path.open("r", encoding="utf-8-sig") as handle:
        first = _next_nonempty(handle)
        if first is None:
            return "jsonl"
        parsed = _parse_line(first)
        if parsed is not _NOT_JSON:
            if isinstance(parsed, list) and _next_nonempty(handle) is None:
                return "array"
            return "jsonl"
        leader = first.lstrip()
        if leader.startswith("["):
            return "array"
        if leader.startswith("{"):
            return "object"
        return "jsonl"


def add_field(obj: dict, key: str, value: Any, overwrite: bool) -> dict:
    if not isinstance(obj, dict):
        raise InputError(f"Expected object/dict, got {type(obj).__name__}")
    if key in obj and not overwrite:
        return obj
    obj[key] = value
    return obj


def process_jsonl(
    infile: TextIO,
    outfile: TextIO,
    key: str,
    value: Any,
    overwrite: bool,
    progress_every: int,
) -> int:
    count = 0
    for line_no, line in enumerate(infile, start=1):
        stripped = line.strip()
        if not stripped:
            outfile.write(line)
            continue
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise InputError(f"Invalid JSON on line {line_no}: {exc}") from exc
        try:
            add_field(obj, key, value, overwrite)
        except InputError as exc:
            raise InputError(f"Line {line_no}: {exc}") from exc
        outfile.write(json.dumps(obj, ensure_ascii=False, separators=(",", ":")))
        outfile.write("\n")
        count += 1
        if progress_every and count % progress_every == 0:
            print(f"  processed {count:,} records...", file=sys.stderr)
    return count


def process_array(
    infile: TextIO,
    outfile: TextIO,
    key: str,
    value: Any,
    overwrite: bool,
    progress_every: int,
    indent: int | None,
) -> int:
    try:
        data = json.load(infile)
    except json.JSONDecodeError as exc:
        raise InputError(f"Invalid JSON: {exc}") from exc
    if not isinstance(data, list):
        raise InputError("Expected a JSON array at top level")
    for i, item in enumerate(data, start=1):
        try:
            add_field(item, key, value, overwrite)
        except InputError as exc:
            raise InputError(f"Item {i}: {exc}") from exc
        if progress_every and i % progress_every == 0:
            print(f"  processed {i:,} records...", file=sys.stderr)
    json.dump(data, outfile, ensure_ascii=False, indent=indent)
    if indent is not None:
        outfile.write("\n")
    return len(data)


def process_object(
    infile: TextIO,
    outfile: TextIO,
    key: str,
    value: Any,
    overwrite: bool,
    indent: int | None,
) -> int:
    try:
        data = json.load(infile)
    except json.JSONDecodeError as exc:
        raise InputError(f"Invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise InputError("Expected a JSON object at top level")
    add_field(data, key, value, overwrite)
    json.dump(data, outfile, ensure_ascii=False, indent=indent)
    if indent is not None:
        outfile.write("\n")
    return 1


def _replace_target(dest: Path) -> Path:
    """Path whose directory entry should be replaced.

    Follow a symlink so the link stays a link and the target file is updated.
    The destination itself is never opened with mode "w": that would truncate
    an inode shared with the input through a hard link before the input is read.
    """
    return dest.resolve()


def _open_temp(replace_path: Path) -> tuple[TextIO, Path]:
    # Same directory as the final name so os.replace stays on one filesystem.
    replace_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=replace_path.name + ".",
        suffix=".tmp",
        dir=replace_path.parent,
    )
    return os.fdopen(fd, "w", encoding="utf-8"), Path(tmp_name)


def _publish(tmp_path: Path, replace_path: Path) -> None:
    """Move the temp onto replace_path without leaving mkstemp's 0o600 mode."""
    if replace_path.exists():
        shutil.copymode(replace_path, tmp_path)
    else:
        current = os.umask(0)
        os.umask(current)
        os.chmod(tmp_path, 0o666 & ~current)
    os.replace(tmp_path, replace_path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Add a key/value pair to every item in a JSON/JSONL file.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("input", type=Path, help="Input JSON or JSONL file")
    parser.add_argument(
        "-k",
        "--key",
        required=True,
        help="Key to add (e.g. type)",
    )
    parser.add_argument(
        "-v",
        "--value",
        required=True,
        help="Value to set (string by default; see --value-type)",
    )
    parser.add_argument(
        "--value-type",
        choices=("str", "int", "float", "bool", "null", "json"),
        default="str",
        help="How to interpret --value (default: str)",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="Output file path (temp file, then atomic rename)",
    )
    parser.add_argument(
        "--in-place",
        action="store_true",
        help="Replace the input via a temp file and an atomic rename. "
        "A symlink is kept; its target is updated.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite the key if it already exists (default: leave existing)",
    )
    parser.add_argument(
        "--format",
        choices=("auto", "jsonl", "array", "object"),
        default="auto",
        help="Input format (default: auto-detect)",
    )
    parser.add_argument(
        "--indent",
        type=int,
        default=None,
        help="Pretty-print indent for array/object output (JSONL stays one line per record)",
    )
    parser.add_argument(
        "--progress-every",
        type=int,
        default=100_000,
        help="Print progress every N records (0 disables; default: 100000)",
    )
    args = parser.parse_args(argv)

    in_path: Path = args.input
    if not in_path.is_file():
        print(f"Error: input file not found: {in_path}", file=sys.stderr)
        return 1

    if not args.in_place and args.output is None:
        print("Error: specify -o/--output or --in-place", file=sys.stderr)
        return 1

    try:
        value = parse_value(args.value, args.value_type)
    except (ValueError, json.JSONDecodeError) as exc:
        print(f"Error parsing value: {exc}", file=sys.stderr)
        return 1

    fmt = args.format
    if fmt == "auto":
        fmt = detect_format(in_path)
        print(f"Detected format: {fmt}", file=sys.stderr)

    dest = in_path if args.in_place else args.output
    assert dest is not None
    replace_path = _replace_target(dest)
    tmp_path: Path | None = None
    try:
        outfile, tmp_path = _open_temp(replace_path)
        with in_path.open("r", encoding="utf-8-sig") as infile, outfile:
            if fmt == "jsonl":
                n = process_jsonl(
                    infile,
                    outfile,
                    args.key,
                    value,
                    args.overwrite,
                    args.progress_every,
                )
            elif fmt == "array":
                n = process_array(
                    infile,
                    outfile,
                    args.key,
                    value,
                    args.overwrite,
                    args.progress_every,
                    args.indent,
                )
            else:
                n = process_object(
                    infile,
                    outfile,
                    args.key,
                    value,
                    args.overwrite,
                    args.indent,
                )
            outfile.flush()
            os.fsync(outfile.fileno())
        _publish(tmp_path, replace_path)
        tmp_path = None
        print(
            f"Done. Added {args.key!r}={value!r} to {n:,} record(s) → {replace_path}",
            file=sys.stderr,
        )
        return 0
    except InputError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
