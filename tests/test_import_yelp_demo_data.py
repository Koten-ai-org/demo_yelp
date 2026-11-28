"""CDN download and cbimport wiring for the Yelp demo bucket."""

from __future__ import annotations

import http.client
import importlib.util
import io
import json
import sys
import tarfile
import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


def _load():
    path = SCRIPTS / "import_yelp_demo_data.py"
    name = "import_yelp_demo_data"
    existing = sys.modules.get(name)
    if existing is not None:
        existing_file = getattr(existing, "__file__", None)
        if existing_file and Path(existing_file).resolve() == path:
            return existing
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def mod():
    return _load()


def _http_error(url: str, status: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url,
        status,
        "error",
        http.client.HTTPMessage(),
        io.BytesIO(b""),
    )


class _Body:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body

    def read(self) -> bytes:
        return self._body

    def close(self) -> None:
        return None


def test_env_example_has_cdn_url() -> None:
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert (
        "YELP_DEMO_DATA_URL="
        "https://koten-static-cdn.nyc3.cdn.digitaloceanspaces.com/yelp-demo/data"
        in example
    )


def test_settings_keep_dollar_password_and_process_env(tmp_path, mod, monkeypatch) -> None:
    (tmp_path / ".env").write_text(
        "COUCHBASE_USERNAME=file-user\n"
        "COUCHBASE_PASSWORD=pa$$w0rd${DOLLAR}\n"
        "ZEUS_BUCKET=yelp-demo\n"
        "COUCHBASE_QUERY_URL=http://cb.example:8093\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("DOLLAR", "expanded")
    settings = mod.load_settings(tmp_path, environ={})
    assert settings.username == "file-user"
    assert settings.password == "pa$$w0rd${DOLLAR}"
    assert settings.bucket == "yelp-demo"
    assert settings.host == "cb.example"
    assert settings.cdn_url == mod.DEFAULT_CDN_URL

    overridden = mod.load_settings(
        tmp_path,
        environ={
            "COUCHBASE_USERNAME": "env-user",
            "COUCHBASE_PASSWORD": "env-secret",
            "ZEUS_BUCKET": "other-bucket",
            "COUCHBASE_HOST": "10.0.0.8",
            "YELP_DEMO_DATA_URL": "https://cdn.example/yelp-demo/data",
        },
    )
    assert overridden.username == "env-user"
    assert overridden.password == "env-secret"
    assert overridden.bucket == "other-bucket"
    assert overridden.host == "10.0.0.8"
    assert overridden.cdn_url == "https://cdn.example/yelp-demo/data"


def test_missing_password_stops_before_couchbase(tmp_path, mod, monkeypatch, capsys) -> None:
    (tmp_path / ".env").write_text(
        "COUCHBASE_USERNAME=Administrator\nCOUCHBASE_PASSWORD=\nZEUS_BUCKET=yelp-demo\n",
        encoding="utf-8",
    )

    def fail(*_args, **_kwargs):
        raise AssertionError("Couchbase was checked")

    monkeypatch.setattr(mod, "check_couchbase", fail)
    code = mod.main(root=tmp_path, environ={})
    assert code == 1
    assert "COUCHBASE_PASSWORD" in capsys.readouterr().err


def test_rejected_credentials_do_not_download(tmp_path, mod, monkeypatch, capsys) -> None:
    (tmp_path / ".env").write_text(
        "COUCHBASE_USERNAME=Administrator\nCOUCHBASE_PASSWORD=secret\nZEUS_BUCKET=yelp-demo\n",
        encoding="utf-8",
    )

    def reject(_settings, **_kwargs):
        raise mod.CouchbaseError(
            "Couchbase rejected COUCHBASE_USERNAME or COUCHBASE_PASSWORD."
        )

    def download(*_args, **_kwargs):
        raise AssertionError("files were downloaded")

    monkeypatch.setattr(mod, "check_couchbase", reject)
    monkeypatch.setattr(mod, "download_datasets", download)
    code = mod.main(root=tmp_path, environ={})
    assert code == 1
    assert "rejected" in capsys.readouterr().err


def test_missing_bucket_does_not_download(tmp_path, mod, monkeypatch, capsys) -> None:
    (tmp_path / ".env").write_text(
        "COUCHBASE_USERNAME=Administrator\nCOUCHBASE_PASSWORD=secret\nZEUS_BUCKET=yelp-demo\n",
        encoding="utf-8",
    )

    def missing(_settings, **_kwargs):
        raise mod.CouchbaseError("Couchbase bucket 'yelp-demo' does not exist.")

    def download(*_args, **_kwargs):
        raise AssertionError("files were downloaded")

    monkeypatch.setattr(mod, "check_couchbase", missing)
    monkeypatch.setattr(mod, "download_datasets", download)
    code = mod.main(root=tmp_path, environ={})
    assert code == 1
    assert "does not exist" in capsys.readouterr().err


def test_check_couchbase_401_and_404(mod) -> None:
    settings = mod.Settings(
        "Administrator",
        "secret",
        "yelp-demo",
        mod.DEFAULT_CDN_URL,
        "localhost",
    )
    seen: list[str] = []

    def urlopen_401(request, timeout=15):
        del timeout
        seen.append(request.full_url)
        raise _http_error(request.full_url, 401)

    with pytest.raises(mod.CouchbaseError, match="rejected"):
        mod.check_couchbase(settings, urlopen=urlopen_401)
    assert seen == ["http://localhost:8091/pools/default"]

    def urlopen_404(request, timeout=15):
        del timeout
        if request.full_url.endswith("/pools/default"):
            return _Body(200, b'{"nodes":[{"version":"8.0.4-6414"}]}')
        assert request.full_url.endswith("/pools/default/buckets/yelp-demo")
        raise _http_error(request.full_url, 404)

    with pytest.raises(mod.CouchbaseError, match="does not exist"):
        mod.check_couchbase(settings, urlopen=urlopen_404)


def test_decline_install_stops_before_download(tmp_path, mod, monkeypatch, capsys) -> None:
    (tmp_path / ".env").write_text(
        "COUCHBASE_USERNAME=Administrator\nCOUCHBASE_PASSWORD=secret\nZEUS_BUCKET=yelp-demo\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(mod, "check_couchbase", lambda _settings, **_kwargs: "8.0.4")
    monkeypatch.setattr(mod, "find_cbimport", lambda _root: None)

    def download(*_args, **_kwargs):
        raise AssertionError("files were downloaded")

    monkeypatch.setattr(mod, "download_datasets", download)
    monkeypatch.setattr(mod, "ask_install", lambda _stdin=None: False)
    code = mod.main(root=tmp_path, environ={})
    assert code == 1
    assert "cbimport will do the import" in capsys.readouterr().err


def test_install_failure_stops_before_download(tmp_path, mod, monkeypatch, capsys) -> None:
    (tmp_path / ".env").write_text(
        "COUCHBASE_USERNAME=Administrator\nCOUCHBASE_PASSWORD=secret\nZEUS_BUCKET=yelp-demo\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(mod, "check_couchbase", lambda _settings, **_kwargs: "8.0.4")
    monkeypatch.setattr(mod, "find_cbimport", lambda _root: None)
    monkeypatch.setattr(mod, "ask_install", lambda _stdin=None: True)

    def install(*_args, **_kwargs):
        raise RuntimeError("no package")

    def download(*_args, **_kwargs):
        raise AssertionError("files were downloaded")

    monkeypatch.setattr(mod, "install_cbimport", install)
    monkeypatch.setattr(mod, "download_datasets", download)
    code = mod.main(root=tmp_path, environ={})
    assert code == 1
    assert "no package" in capsys.readouterr().err


def test_ask_install_treats_non_tty_as_no(mod) -> None:
    stdin = io.StringIO("y\n")
    assert mod.ask_install(stdin) is False


def test_ask_install_accepts_yes(mod) -> None:
    stdin = io.StringIO("yes\n")
    stdin.isatty = lambda: True  # type: ignore[method-assign]
    assert mod.ask_install(stdin) is True


def test_ranked_tool_urls_prefer_cluster_or_newer(mod) -> None:
    page = (
        "https://packages.couchbase.com/releases/8.0.3/"
        "couchbase-server-dev-tools-8.0.3-linux_x86_64.tar.gz "
        "https://packages.couchbase.com/releases/8.0.3/"
        "couchbase-server-dev-tools-8.0.3-linux_aarch64.tar.gz"
    )
    urls = mod.ranked_tool_urls(page, "linux_x86_64", "8.0.4-6414")
    assert urls[0].endswith(
        "couchbase-server-dev-tools_8.0.4-linux_x86_64.tar.gz"
    )
    assert all("aarch64" not in url for url in urls)
    assert urls[-1].endswith("couchbase-server-dev-tools-8.0.3-linux_x86_64.tar.gz")


def test_listing_keeps_required_files_and_extra_json(mod) -> None:
    denied = b'<?xml version="1.0"?><Error><Code>AccessDenied</Code></Error>'
    assert mod.parse_listing(denied) is None
    body = b"""<?xml version="1.0" encoding="UTF-8"?>
<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
  <Contents><Key>yelp-demo/data/extra.json</Key></Contents>
  <Contents><Key>yelp-demo/data/yelp_academic_dataset_business_with_type.json</Key></Contents>
  <Contents><Key>yelp-demo/data/notes.txt</Key></Contents>
</ListBucketResult>
"""
    names = mod.merge_names(mod.parse_listing(body))
    assert names[: len(mod.REQUIRED_FILES)] == list(mod.REQUIRED_FILES)
    assert names[-1] == "extra.json"
    assert "notes.txt" not in names


def test_count_records_without_trailing_newline(tmp_path, mod) -> None:
    path = tmp_path / "rows.json"
    path.write_bytes(b'{"a":1}\n{"a":2}')
    assert mod.count_records(path) == 2


def test_import_commands_use_file_uris_and_key_expressions(tmp_path, mod) -> None:
    settings = mod.Settings(
        "user",
        "secret",
        "yelp-demo",
        mod.DEFAULT_CDN_URL,
        "localhost",
    )
    cbimport = tmp_path / "cbimport"
    expected = {
        "yelp_academic_dataset_business_with_type.json": "biz:yelp:%business_id%",
        "yelp_academic_dataset_checkin_with_type.json": "checkin::#MONO_INCR#",
        "yelp_academic_dataset_review_with_type.json": "rev:%review_id%",
        "yelp_academic_dataset_tip_with_type.json": "tip::#MONO_INCR#",
        "yelp_academic_dataset_user_with_type.json": "user:%user_id%",
    }
    assert [name for name, _key in mod.IMPORT_JOBS] == list(expected)
    for name, key in mod.IMPORT_JOBS:
        dataset = tmp_path / "data" / "demo" / name
        argv = mod.import_command(cbimport, settings, dataset, key)
        assert argv[:4] == [str(cbimport), "json", "--format", "lines"]
        assert argv[argv.index("-c") + 1] == "couchbase://localhost"
        assert argv[argv.index("-u") + 1] == "user"
        assert "-p" not in argv
        assert "--password" not in argv
        assert "secret" not in argv
        assert argv[argv.index("-b") + 1] == "yelp-demo"
        assert argv[argv.index("-d") + 1] == dataset.resolve().as_uri()
        assert argv[argv.index("--generate-key") + 1] == key
        assert key == expected[name]
        assert argv[-1] == "--verbose"


def test_run_import_shows_percent_and_hides_password(tmp_path, mod, capsys) -> None:
    args_out = tmp_path / "args.txt"
    secret_out = tmp_path / "secret.txt"
    tool = tmp_path / "cbimport"
    tool.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$@\" > {json.dumps(str(args_out))}\n"
        f"printf '%s' \"$CB_PASSWORD\" > {json.dumps(str(secret_out))}\n"
        "echo 'JSON import - 1 documents imported, 0 errors'\n"
        "echo 'finished 100%'\n",
        encoding="utf-8",
    )
    tool.chmod(0o755)
    dataset = tmp_path / "yelp_academic_dataset_business_with_type.json"
    dataset.write_text('{"business_id":"a"}\n{"business_id":"b"}\n', encoding="utf-8")
    settings = mod.Settings(
        "user",
        "s3cret-value",
        "yelp-demo",
        mod.DEFAULT_CDN_URL,
        "localhost",
    )
    code = mod.run_import(tool, settings, dataset, "biz:yelp:%business_id%", 2)
    captured = capsys.readouterr()
    assert code == 0
    assert "50.0%" in captured.err
    assert "100%" in captured.err
    assert "s3cret-value" not in captured.err
    recorded = args_out.read_text(encoding="utf-8")
    assert "biz:yelp:%business_id%" in recorded
    assert dataset.resolve().as_uri() in recorded
    assert "-p" not in recorded.splitlines()
    assert "s3cret-value" not in recorded
    assert secret_out.read_text(encoding="utf-8") == "s3cret-value"


def _serve(body: bytes, ranges: bool = False):
    counts = {"get": 0, "head": 0}

    class Handler(BaseHTTPRequestHandler):
        def do_HEAD(self) -> None:  # noqa: N802
            counts["head"] += 1
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()

        def do_GET(self) -> None:  # noqa: N802
            counts["get"] += 1
            data = body
            status = 200
            extra = []
            if ranges and self.headers.get("Range"):
                start = int(self.headers["Range"].split("=", 1)[1].split("-", 1)[0])
                data = body[start:]
                status = 206
                extra.append(
                    (
                        "Content-Range",
                        f"bytes {start}-{len(body) - 1}/{len(body)}",
                    )
                )
            self.send_response(status)
            self.send_header("Content-Length", str(len(data)))
            for key, value in extra:
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, fmt: str, *args) -> None:
            del fmt, args

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, counts


def test_download_shows_percent_and_skips_complete_file(tmp_path, mod) -> None:
    body = b'{"id":1}\n{"id":2}\n'
    server, counts = _serve(body)
    port = server.server_address[1]
    dest = tmp_path / "rows.json"
    err = io.StringIO()
    try:
        first = mod.download_file(
            f"http://127.0.0.1:{port}/rows.json",
            dest,
            stderr=err,
        )
        second = mod.download_file(
            f"http://127.0.0.1:{port}/rows.json",
            dest,
            stderr=err,
        )
    finally:
        server.shutdown()
    assert first == 2
    assert second == 2
    assert dest.read_bytes() == body
    assert not dest.with_name(dest.name + ".partial").exists()
    assert "100.0%" in err.getvalue()
    assert counts["get"] == 1


def test_download_resumes_partial_file(tmp_path, mod) -> None:
    body = b'{"id":1}\n{"id":2}\n'
    server, _counts = _serve(body, ranges=True)
    port = server.server_address[1]
    dest = tmp_path / "rows.json"
    partial = dest.with_name(dest.name + ".partial")
    partial.write_bytes(body[:8])
    err = io.StringIO()
    try:
        count = mod.download_file(
            f"http://127.0.0.1:{port}/rows.json",
            dest,
            stderr=err,
        )
    finally:
        server.shutdown()
    assert count == 2
    assert dest.read_bytes() == body
    assert not partial.exists()
    assert "100.0%" in err.getvalue()


def test_safe_extract_rejects_parent_path(tmp_path, mod) -> None:
    archive = tmp_path / "tools.tar"
    payload = b"nope"
    with tarfile.open(archive, "w") as tar:
        info = tarfile.TarInfo("../outside.txt")
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
    dest = tmp_path / "pkg"
    dest.mkdir()
    with pytest.raises(RuntimeError, match="Refusing"):
        mod.safe_extract(archive, dest)
    assert not (tmp_path / "outside.txt").exists()


def test_safe_extract_finds_cbimport(tmp_path, mod) -> None:
    archive = tmp_path / "tools.tar"
    payload = b"#!/bin/sh\n"
    with tarfile.open(archive, "w") as tar:
        info = tarfile.TarInfo("bin/cbimport")
        info.size = len(payload)
        info.mode = 0o755
        tar.addfile(info, io.BytesIO(payload))
    dest = tmp_path / "pkg"
    dest.mkdir()
    mod.safe_extract(archive, dest)
    assert mod.locate_cbimport(dest).name == "cbimport"
