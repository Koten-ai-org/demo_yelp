"""Failure cases for scripts/add_json_field.py and its compatibility wrapper."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


def _load(filename: str, module_name: str):
    path = SCRIPTS / filename
    existing = sys.modules.get(module_name)
    if existing is not None:
        existing_file = getattr(existing, "__file__", None)
        if existing_file and Path(existing_file).resolve() == path:
            return existing
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def field_mod():
    return _load("add_json_field.py", "add_json_field")


@pytest.fixture
def wrapper_mod():
    return _load("truncate_yelp_subset.py", "truncate_yelp_subset_script")


def _run(mod, tmp_path: Path, *args: str) -> int:
    return mod.main([*args, "--progress-every", "0"])


def _temps(tmp_path: Path) -> list[Path]:
    return list(tmp_path.glob("*.tmp"))


def test_in_place_invalid_jsonl_keeps_input_and_removes_temp(tmp_path, field_mod):
    src = tmp_path / "in.jsonl"
    original = '{"a": 1}\nNOT JSON\n'
    src.write_text(original)
    src.chmod(0o644)

    code = _run(field_mod, tmp_path, str(src), "-k", "type", "-v", "User", "--in-place")

    assert code == 1
    assert src.read_text() == original
    assert src.stat().st_mode & 0o777 == 0o644
    assert _temps(tmp_path) == []


def test_failed_output_leaves_existing_file(tmp_path, field_mod):
    src = tmp_path / "in.jsonl"
    src.write_text('{"a": 1}\nNOT JSON\n')
    out = tmp_path / "out.jsonl"
    out.write_text('{"keep": true}\n')

    code = _run(field_mod, tmp_path, str(src), "-k", "type", "-v", "User", "-o", str(out))

    assert code == 1
    assert out.read_text() == '{"keep": true}\n'
    assert _temps(tmp_path) == []


def test_failed_array_leaves_existing_output(tmp_path, field_mod):
    src = tmp_path / "in.json"
    src.write_text('[1, {"a": 1}]')
    out = tmp_path / "out.json"
    out.write_text('{"keep": true}')

    code = _run(field_mod, tmp_path, str(src), "-k", "type", "-v", "User", "-o", str(out))

    assert code == 1
    assert out.read_text() == '{"keep": true}'
    assert _temps(tmp_path) == []


def test_hard_link_output_keeps_the_other_name(tmp_path, field_mod):
    real = tmp_path / "real.jsonl"
    original = '{"a": 1}\n'
    real.write_text(original)
    hard = tmp_path / "hard.jsonl"
    os.link(real, hard)

    code = _run(
        field_mod,
        tmp_path,
        str(hard),
        "-k",
        "type",
        "-v",
        "User",
        "-o",
        str(real),
    )

    assert code == 0
    assert hard.read_text() == original
    assert real.read_text() == '{"a":1,"type":"User"}\n'
    assert hard.stat().st_ino != real.stat().st_ino
    assert _temps(tmp_path) == []


def test_bom_jsonl_is_rewritten(tmp_path, field_mod):
    src = tmp_path / "bom.jsonl"
    src.write_bytes('{"a": 1}\n{"b": 2}\n'.encode("utf-8-sig"))
    out = tmp_path / "out.jsonl"

    code = _run(field_mod, tmp_path, str(src), "-k", "type", "-v", "User", "-o", str(out))

    assert code == 0
    assert out.read_bytes() == b'{"a":1,"type":"User"}\n{"b":2,"type":"User"}\n'


def test_one_record_jsonl_stays_compact(tmp_path, field_mod):
    src = tmp_path / "one.jsonl"
    src.write_text('{"a":1,"name":"José"}\n', encoding="utf-8")
    out = tmp_path / "out.jsonl"

    code = _run(field_mod, tmp_path, str(src), "-k", "type", "-v", "User", "-o", str(out))

    assert code == 0
    assert out.read_text(encoding="utf-8") == '{"a":1,"name":"José","type":"User"}\n'


def test_one_line_array_updates_each_element(tmp_path, field_mod):
    src = tmp_path / "arr.json"
    src.write_text('[{"a": 1}, {"b": 2}]')
    out = tmp_path / "out.json"

    code = _run(field_mod, tmp_path, str(src), "-k", "type", "-v", "User", "-o", str(out))

    assert code == 0
    assert json.loads(out.read_text()) == [
        {"a": 1, "type": "User"},
        {"b": 2, "type": "User"},
    ]


def test_pretty_object_with_nested_brace_is_one_object(tmp_path, field_mod):
    src = tmp_path / "obj.json"
    src.write_text('{"user":\n{"name": "a"}}\n')
    out = tmp_path / "out.json"

    code = _run(field_mod, tmp_path, str(src), "-k", "type", "-v", "User", "-o", str(out))

    assert code == 0
    assert json.loads(out.read_text()) == {"user": {"name": "a"}, "type": "User"}


def test_symlink_in_place_updates_target(tmp_path, field_mod, capsys):
    real = tmp_path / "real.jsonl"
    real.write_text('{"a": 1}\n')
    real.chmod(0o640)
    link = tmp_path / "link.jsonl"
    link.symlink_to(real)

    code = _run(field_mod, tmp_path, str(link), "-k", "type", "-v", "User", "--in-place")

    assert code == 0
    assert link.is_symlink()
    assert real.read_text() == '{"a":1,"type":"User"}\n'
    assert real.stat().st_mode & 0o777 == 0o640
    assert str(real.resolve()) in capsys.readouterr().err
    assert _temps(tmp_path) == []


def test_symlink_output_updates_target_not_the_link(tmp_path, field_mod, capsys):
    real = tmp_path / "real.jsonl"
    real.write_text('{"a": 1}\n')
    link = tmp_path / "link.jsonl"
    link.symlink_to(real.name)

    code = _run(
        field_mod,
        tmp_path,
        str(link),
        "-k",
        "type",
        "-v",
        "User",
        "-o",
        str(real),
    )

    assert code == 0
    assert link.is_symlink()
    assert os.readlink(link) == real.name
    assert real.read_text() == '{"a":1,"type":"User"}\n'
    assert str(real.resolve()) in capsys.readouterr().err


def test_output_symlink_is_preserved(tmp_path, field_mod):
    real = tmp_path / "real.jsonl"
    real.write_text('{"a": 1}\n')
    link = tmp_path / "link.jsonl"
    link.symlink_to(real.name)
    src = tmp_path / "src.jsonl"
    src.write_text('{"b": 2}\n')

    code = _run(
        field_mod,
        tmp_path,
        str(src),
        "-k",
        "type",
        "-v",
        "User",
        "-o",
        str(link),
    )

    assert code == 0
    assert link.is_symlink()
    assert real.read_text() == '{"b":2,"type":"User"}\n'
    assert src.read_text() == '{"b": 2}\n'


def test_in_place_preserves_mode(tmp_path, field_mod):
    src = tmp_path / "in.jsonl"
    src.write_text('{"a": 1}\n')
    src.chmod(0o640)

    code = _run(field_mod, tmp_path, str(src), "-k", "type", "-v", "User", "--in-place")

    assert code == 0
    assert src.read_text() == '{"a":1,"type":"User"}\n'
    assert src.stat().st_mode & 0o777 == 0o640


def test_new_output_uses_umask_not_private_temp_mode(tmp_path, field_mod):
    src = tmp_path / "in.jsonl"
    src.write_text('{"a": 1}\n')
    out = tmp_path / "out.jsonl"
    ref = tmp_path / "ref.txt"
    ref.write_text("x")

    code = _run(field_mod, tmp_path, str(src), "-k", "type", "-v", "User", "-o", str(out))

    assert code == 0
    assert out.stat().st_mode & 0o777 == ref.stat().st_mode & 0o777


def test_keyboard_interrupt_removes_temp(tmp_path, field_mod, monkeypatch):
    src = tmp_path / "in.jsonl"
    original = '{"a": 1}\n'
    src.write_text(original)

    def boom(*_args, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(field_mod, "process_jsonl", boom)

    with pytest.raises(KeyboardInterrupt):
        _run(field_mod, tmp_path, str(src), "-k", "type", "-v", "User", "--in-place")

    assert src.read_text() == original
    assert _temps(tmp_path) == []


def test_wrapper_calls_add_json_field(tmp_path, field_mod, wrapper_mod):
    src = tmp_path / "one.jsonl"
    src.write_text('{"a": 1}\n')
    out = tmp_path / "out.jsonl"

    code = _run(wrapper_mod, tmp_path, str(src), "-k", "type", "-v", "User", "-o", str(out))

    assert code == 0
    assert out.read_text() == '{"a":1,"type":"User"}\n'
    assert wrapper_mod._load() is field_mod
