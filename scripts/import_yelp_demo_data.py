#!/usr/bin/env python3
"""Download Yelp demo JSON and import it into Couchbase with cbimport.

Reads COUCHBASE_USERNAME, COUCHBASE_PASSWORD, and ZEUS_BUCKET from .env.
YELP_DEMO_DATA_URL defaults to the static CDN directory. The cluster
credentials and bucket are checked before any file is downloaded.

    python scripts/import_yelp_demo_data.py
"""

from __future__ import annotations

import base64
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urljoin, urlparse

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CDN_URL = (
    "https://koten-static-cdn.nyc3.cdn.digitaloceanspaces.com/yelp-demo/data"
)
TOOLS_DOC_URL = "https://docs.couchbase.com/cloud/reference/command-line-tools.html"
TOOLS_DIR_NAME = "couchbase-dev-tools"
USER_AGENT = "demo-yelp-import/1"
# Match the keys this app reads. Business hydrate looks up biz:yelp:<id>
# (n1ql_hydrate.business_doc_keys). Users are user:<id>
# (detail.yelp_user_doc_key). Review USE KEYS values are rev:<id>.
# Check-in and tip each have their own #MONO_INCR# sequence. A shared
# counter would restart at 1 in the second cbimport process and overwrite
# the first file's documents.
IMPORT_JOBS = (
    ("yelp_academic_dataset_business_with_type.json", "biz:yelp:%business_id%"),
    ("yelp_academic_dataset_checkin_with_type.json", "checkin::#MONO_INCR#"),
    ("yelp_academic_dataset_review_with_type.json", "rev:%review_id%"),
    ("yelp_academic_dataset_tip_with_type.json", "tip::#MONO_INCR#"),
    ("yelp_academic_dataset_user_with_type.json", "user:%user_id%"),
)
REQUIRED_FILES = tuple(name for name, _key in IMPORT_JOBS)
STOP_MESSAGE = (
    "Stopped before download. cbimport will do the import into the Couchbase "
    "bucket, and this script cannot continue without it."
)
INSTALL_PROMPT = (
    "cbimport was not found. Download and install the latest Couchbase "
    "command-line tools? [y/N] "
)
_TOOL_URL_RE = re.compile(
    r"https://packages\.couchbase\.com/releases/[0-9.]+/"
    r"couchbase-server-dev-tools[_-][0-9.]+-linux_(?:x86_64|aarch64)\.tar\.gz"
)
_IMPORTED_RE = re.compile(r"(\d+)\s+documents imported", re.IGNORECASE)
_PERCENT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%")
_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")


class SettingsError(Exception):
    """Required Couchbase settings are missing from the environment."""


class CouchbaseError(Exception):
    """The cluster rejected the configured user or does not have the bucket."""


class DownloadError(Exception):
    """A demo file could not be downloaded."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Settings:
    username: str
    password: str
    bucket: str
    cdn_url: str
    host: str


def load_settings(
    root: Path,
    environ: Mapping[str, str] | None = None,
) -> Settings:
    """Load Couchbase settings. Process environment wins over `.env`."""
    process = os.environ if environ is None else environ
    merged: dict[str, str] = {}
    env_path = root / ".env"
    if env_path.is_file():
        loaded = dotenv_values(env_path, interpolate=False)
        for key, value in loaded.items():
            if key and value is not None:
                merged[key] = value
    for key, value in process.items():
        if value is not None:
            merged[key] = value

    username = (merged.get("COUCHBASE_USERNAME") or "").strip()
    password = merged.get("COUCHBASE_PASSWORD")
    if password is None:
        password = ""
    bucket = (merged.get("ZEUS_BUCKET") or "").strip()
    missing = []
    if not username:
        missing.append("COUCHBASE_USERNAME")
    if password == "":
        missing.append("COUCHBASE_PASSWORD")
    if not bucket:
        missing.append("ZEUS_BUCKET")
    if missing:
        names = ", ".join(missing)
        raise SettingsError(
            f"Set {names} in .env before downloading the Yelp demo data."
        )
    cdn = (merged.get("YELP_DEMO_DATA_URL") or "").strip() or DEFAULT_CDN_URL
    return Settings(
        username=username,
        password=password,
        bucket=bucket,
        cdn_url=cdn,
        host=resolve_host(merged),
    )


def resolve_host(merged: Mapping[str, str]) -> str:
    host = (merged.get("COUCHBASE_HOST") or "").strip()
    if host:
        return host
    query = (merged.get("COUCHBASE_QUERY_URL") or "").strip()
    if query:
        hostname = urlparse(query).hostname
        if hostname:
            return hostname
    return "localhost"


def open_url(
    urlopen: Callable,
    url: str,
    *,
    method: str | None = None,
    headers: Mapping[str, str] | None = None,
    timeout: float = 120,
):
    request_headers = {"User-Agent": USER_AGENT}
    if headers:
        request_headers.update(headers)
    request = urllib.request.Request(url, headers=request_headers, method=method)
    try:
        response = urlopen(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        return exc.code, exc
    status = getattr(response, "status", None)
    if status is None:
        status = response.getcode()
    return status, response


def check_couchbase(
    settings: Settings,
    *,
    urlopen: Callable = urllib.request.urlopen,
) -> str | None:
    """Verify the user and bucket. Return the cluster version when present."""
    token = base64.b64encode(
        f"{settings.username}:{settings.password}".encode()
    ).decode("ascii")
    headers = {"Authorization": f"Basic {token}"}

    def get(path: str):
        url = f"http://{settings.host}:8091{path}"
        try:
            status, response = open_url(
                urlopen, url, headers=headers, timeout=15
            )
        except urllib.error.URLError as exc:
            raise CouchbaseError(
                f"Cannot reach Couchbase at {settings.host}:8091 ({exc.reason})."
            ) from exc
        try:
            body = response.read()
        finally:
            response.close()
        return status, body

    status, body = get("/pools/default")
    if status in {401, 403}:
        raise CouchbaseError(
            "Couchbase rejected COUCHBASE_USERNAME or COUCHBASE_PASSWORD."
        )
    if status != 200:
        raise CouchbaseError(f"Couchbase credential check failed (HTTP {status}).")
    version = cluster_version(body)

    bucket_path = "/pools/default/buckets/" + quote(settings.bucket, safe="")
    status, _body = get(bucket_path)
    if status == 404:
        raise CouchbaseError(
            f"Couchbase bucket {settings.bucket!r} does not exist."
        )
    if status in {401, 403}:
        raise CouchbaseError(
            "Couchbase rejected COUCHBASE_USERNAME or COUCHBASE_PASSWORD."
        )
    if status != 200:
        raise CouchbaseError(f"Couchbase bucket check failed (HTTP {status}).")
    return version


def cluster_version(body: bytes) -> str | None:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    nodes = payload.get("nodes") or []
    if not isinstance(nodes, list):
        return None
    for node in nodes:
        if not isinstance(node, dict):
            continue
        for key in ("version", "implementationVersion"):
            value = node.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def find_cbimport(
    root: Path,
    *,
    path_lookup: Callable[[str], str | None] = shutil.which,
    system_path: Path = Path("/opt/couchbase/bin/cbimport"),
) -> Path | None:
    found = path_lookup("cbimport")
    if found:
        return Path(found)
    candidates = [system_path]
    tools = root / "tools" / TOOLS_DIR_NAME
    if tools.is_dir():
        candidates.extend(
            path for path in tools.rglob("cbimport") if path.is_file()
        )
    for path in candidates:
        if path.is_file() and os.access(path, os.X_OK):
            return path
    return None


def ask_install(stdin=None) -> bool:
    stream = sys.stdin if stdin is None else stdin
    is_tty = getattr(stream, "isatty", lambda: False)()
    if not is_tty:
        print(INSTALL_PROMPT + "n", file=sys.stderr)
        return False
    print(INSTALL_PROMPT, end="", file=sys.stderr, flush=True)
    try:
        answer = stream.readline()
    except EOFError:
        answer = ""
    return answer.strip().lower() in {"y", "yes"}


def version_tuple(text: str | None) -> tuple[int, ...]:
    match = _VERSION_RE.search(text or "")
    if not match:
        return ()
    return tuple(int(part) for part in match.groups())


def linux_arch(machine: str) -> str:
    normalized = machine.lower()
    if normalized in {"x86_64", "amd64"}:
        return "linux_x86_64"
    if normalized in {"aarch64", "arm64"}:
        return "linux_aarch64"
    raise RuntimeError(
        f"Unsupported CPU architecture {machine!r} for the Couchbase tools."
    )


def tool_urls_for_version(version: str | None, arch: str) -> list[str]:
    parsed = version_tuple(version)
    if len(parsed) != 3:
        return []
    release = ".".join(str(part) for part in parsed)
    names = (
        f"couchbase-server-dev-tools_{release}-{arch}.tar.gz",
        f"couchbase-server-dev-tools-{release}-{arch}.tar.gz",
    )
    return [
        f"https://packages.couchbase.com/releases/{release}/{name}"
        for name in names
    ]


def ranked_tool_urls(
    page: str,
    arch: str,
    cluster_version_text: str | None,
) -> list[str]:
    """Newest packages first, with builds older than the cluster after them."""
    found = [
        url
        for url in _TOOL_URL_RE.findall(page)
        if url.endswith(f"-{arch}.tar.gz")
    ]
    pool: list[str] = []
    seen: set[str] = set()
    for url in tool_urls_for_version(cluster_version_text, arch) + found:
        if url in seen:
            continue
        seen.add(url)
        pool.append(url)
    cluster_key = version_tuple(cluster_version_text)

    def sort_key(url: str) -> tuple:
        parsed = version_tuple(url)
        meets_cluster = parsed >= cluster_key if cluster_key else True
        return (meets_cluster, parsed)

    pool.sort(key=sort_key, reverse=True)
    return pool


def select_tool_url(
    page: str,
    arch: str,
    cluster_version_text: str | None,
    *,
    urlopen: Callable,
) -> str:
    urls = ranked_tool_urls(page, arch, cluster_version_text)
    if not urls:
        raise RuntimeError("Could not find a Couchbase dev-tools download URL.")
    tried: list[str] = []
    for url in urls:
        status = head_status(urlopen, url)
        if status == 200:
            return url
        tried.append(f"{url} (HTTP {status})")
    detail = "; ".join(tried)
    raise RuntimeError(f"Couchbase dev-tools package was not available: {detail}")


def head_status(urlopen: Callable, url: str) -> int:
    try:
        status, response = open_url(urlopen, url, method="HEAD", timeout=30)
    except urllib.error.URLError:
        return 0
    try:
        response.read()
    finally:
        response.close()
    if status in {200, 206}:
        return 200
    return status


def install_cbimport(
    root: Path,
    cluster_version_text: str | None,
    *,
    machine: str | None = None,
    urlopen: Callable = urllib.request.urlopen,
    stderr=None,
) -> Path:
    if stderr is None:
        stderr = sys.stderr
    arch = linux_arch(machine or platform.machine())
    status, response = open_url(urlopen, TOOLS_DOC_URL, timeout=60)
    try:
        page = response.read().decode("utf-8", "replace")
    finally:
        response.close()
    if status != 200:
        raise RuntimeError(
            f"Could not read the Couchbase tools page (HTTP {status})."
        )
    url = select_tool_url(
        page, arch, cluster_version_text, urlopen=urlopen
    )
    tools_root = root / "tools" / TOOLS_DIR_NAME
    tools_root.mkdir(parents=True, exist_ok=True)
    archive_name = Path(urlparse(url).path).name
    archive = tools_root / archive_name
    print(f"Downloading {archive_name}", file=stderr)
    download_file(url, archive, urlopen=urlopen, stderr=stderr)
    package_dir = tools_root / "pkg"
    if package_dir.exists():
        shutil.rmtree(package_dir)
    package_dir.mkdir(parents=True)
    safe_extract(archive, package_dir)
    return locate_cbimport(package_dir)


def _inside(root: Path, path: Path) -> bool:
    resolved = path.resolve()
    return resolved == root or root in resolved.parents


def safe_extract(archive: Path, dest: Path) -> None:
    root = dest.resolve()
    with tarfile.open(archive) as tar:
        members = []
        for member in tar.getmembers():
            name = member.name
            if name.startswith(("/", "\\")) or not _inside(root, dest / name):
                raise RuntimeError(f"Refusing to extract {name}")
            if member.issym() or member.islnk():
                link = Path(member.linkname)
                if link.is_absolute():
                    raise RuntimeError(f"Refusing to extract link {name}")
                base = (dest / name).parent if member.issym() else dest
                if not _inside(root, base / member.linkname):
                    raise RuntimeError(f"Refusing to extract link {name}")
            elif not (member.isfile() or member.isdir()):
                raise RuntimeError(f"Refusing to extract {name}")
            members.append(member)
        tar.extractall(dest, members=members)


def locate_cbimport(dest: Path) -> Path:
    matches = [path for path in dest.rglob("cbimport") if path.is_file()]
    if not matches:
        raise RuntimeError("The Couchbase tools archive did not contain cbimport.")
    matches.sort(key=lambda path: (path.parent.name != "bin", len(path.parts)))
    chosen = matches[0]
    chosen.chmod(chosen.stat().st_mode | 0o755)
    return chosen


def header_value(response, name: str) -> str | None:
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    value = headers.get(name)
    if value is None:
        return None
    return str(value)


def content_length(response, status: int) -> int | None:
    if status == 206:
        content_range = header_value(response, "Content-Range") or ""
        match = re.search(r"/(\d+)\s*$", content_range)
        if match:
            return int(match.group(1))
        return None
    raw = header_value(response, "Content-Length")
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def scan_state(path: Path) -> tuple[int, int | None]:
    newlines = 0
    last: int | None = None
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            newlines += chunk.count(b"\n")
            last = chunk[-1]
    return newlines, last


def count_records(path: Path) -> int:
    newlines, last = scan_state(path)
    if last is None:
        return 0
    if last != ord("\n"):
        return newlines + 1
    return newlines


class Progress:
    def __init__(self, name: str, stderr) -> None:
        self.name = name
        self.stderr = stderr
        self._last = ""
        self._width = 0
        self._finished = False

    def update(self, done: int, total: int | None) -> None:
        if total:
            percent = min(100.0, 100.0 * done / total)
            line = (
                f"{self.name}  {percent:5.1f}%  "
                f"{format_bytes(done)} / {format_bytes(total)}"
            )
        else:
            line = f"{self.name}  {format_bytes(done)}"
        if line == self._last:
            return
        self._last = line
        self._width = max(self._width, len(line))
        print("\r" + line.ljust(self._width), end="", file=self.stderr, flush=True)

    def finish(self) -> None:
        if self._finished:
            return
        self._finished = True
        if self._last:
            print(file=self.stderr)


def format_bytes(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def download_file(
    url: str,
    dest: Path,
    *,
    urlopen: Callable = urllib.request.urlopen,
    stderr=None,
) -> int:
    """Stream `url` to `dest`. Return the JSON Lines record count."""
    if stderr is None:
        stderr = sys.stderr
    name = dest.name
    progress = Progress(name, stderr)
    response = None
    try:
        head_status_code, head = open_url(urlopen, url, method="HEAD", timeout=60)
        try:
            head.read()
            advertised = content_length(head, head_status_code)
        finally:
            head.close()
        if head_status_code >= 400 and head_status_code not in {405, 501}:
            raise download_http_error(name, head_status_code)
        if (
            head_status_code == 200
            and advertised is not None
            and dest.is_file()
            and dest.stat().st_size == advertised
        ):
            progress.update(advertised, advertised)
            return count_records(dest)

        dest.parent.mkdir(parents=True, exist_ok=True)
        partial = dest.with_name(dest.name + ".partial")
        request_headers: dict[str, str] = {}
        if partial.is_file() and partial.stat().st_size > 0:
            request_headers["Range"] = f"bytes={partial.stat().st_size}-"
        status, response = open_url(
            urlopen, url, headers=request_headers, timeout=120
        )
        if status == 416 and advertised is not None and partial.is_file():
            if partial.stat().st_size == advertised:
                os.replace(partial, dest)
                progress.update(advertised, advertised)
                return count_records(dest)
        if status >= 400:
            raise download_http_error(name, status)

        total = content_length(response, status) or advertised
        append = status == 206 and partial.is_file()
        if not append and partial.exists():
            partial.unlink()
        newlines = 0
        last: int | None = None
        done = 0
        if append:
            newlines, last = scan_state(partial)
            done = partial.stat().st_size
        with partial.open("ab" if append else "wb") as handle:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                handle.write(chunk)
                newlines += chunk.count(b"\n")
                last = chunk[-1]
                done += len(chunk)
                progress.update(done, total)
        if total is not None and done != total:
            raise DownloadError(
                f"{name}: download stopped at {done} of {total} bytes."
            )
        os.replace(partial, dest)
        progress.update(done, total if total else done)
        if last is None:
            return 0
        if last != ord("\n"):
            newlines += 1
        return newlines
    finally:
        progress.finish()
        if response is not None:
            response.close()


def download_http_error(name: str, status: int) -> DownloadError:
    if status == 403:
        message = (
            f"{name}: HTTP 403 from the CDN. "
            "Not importing until this file can be downloaded."
        )
    else:
        message = f"{name}: HTTP {status}"
    return DownloadError(message, status=status)


def join_cdn(cdn_url: str, name: str) -> str:
    return urljoin(cdn_url.rstrip("/") + "/", name)


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_listing(body: bytes) -> list[str] | None:
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return None
    if local_name(root.tag) != "ListBucketResult":
        return None
    names: list[str] = []
    for element in root.iter():
        if local_name(element.tag) != "Key" or not element.text:
            continue
        base = element.text.strip().rstrip("/").rsplit("/", 1)[-1]
        if base.endswith(".json"):
            names.append(base)
    return names


def merge_names(listed: list[str] | None) -> list[str]:
    names = list(REQUIRED_FILES)
    if not listed:
        return names
    seen = set(names)
    for name in listed:
        if name.endswith(".json") and name not in seen:
            seen.add(name)
            names.append(name)
    return names


def listing_urls(cdn_url: str) -> list[str]:
    base = cdn_url.rstrip("/") + "/"
    parsed = urlparse(base)
    prefix = parsed.path.lstrip("/")
    root = f"{parsed.scheme}://{parsed.netloc}/"
    query = f"{root}?list-type=2&prefix={quote(prefix, safe='/')}"
    return [base, query]


def discover_json_names(
    cdn_url: str,
    *,
    urlopen: Callable = urllib.request.urlopen,
) -> list[str]:
    listed: list[str] | None = None
    for url in listing_urls(cdn_url):
        try:
            status, response = open_url(urlopen, url, timeout=30)
        except urllib.error.URLError:
            continue
        try:
            body = response.read()
        finally:
            response.close()
        if status != 200:
            continue
        parsed = parse_listing(body)
        if parsed:
            listed = parsed
            break
    return merge_names(listed)


def download_datasets(
    settings: Settings,
    dest_dir: Path,
    names: list[str],
    *,
    urlopen: Callable = urllib.request.urlopen,
    stderr=None,
) -> tuple[dict[str, int], list[str]]:
    if stderr is None:
        stderr = sys.stderr
    dest_dir.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}
    errors: list[str] = []
    for name in names:
        try:
            counts[name] = download_file(
                join_cdn(settings.cdn_url, name),
                dest_dir / name,
                urlopen=urlopen,
                stderr=stderr,
            )
        except DownloadError as exc:
            errors.append(str(exc))
        except urllib.error.URLError as exc:
            errors.append(f"{name}: {exc.reason}")
    return counts, errors


def import_command(
    cbimport: Path,
    settings: Settings,
    dataset: Path,
    key_expr: str,
) -> list[str]:
    return [
        str(cbimport),
        "json",
        "--format",
        "lines",
        "-c",
        f"couchbase://{settings.host}",
        "-u",
        settings.username,
        "-b",
        settings.bucket,
        "-d",
        dataset.resolve().as_uri(),
        "--generate-key",
        key_expr,
        "--verbose",
    ]


def format_import_line(
    raw: str,
    dataset_name: str,
    line_count: int,
    password: str,
) -> tuple[str, bool]:
    """Return the display line and whether it is an in-place percent update."""
    if password and password in raw:
        raw = raw.replace(password, "***")
    if _PERCENT_RE.search(raw):
        return raw, True
    match = _IMPORTED_RE.search(raw)
    if match and line_count > 0:
        imported = int(match.group(1))
        percent = min(100.0, 100.0 * imported / line_count)
        return f"{dataset_name}  {percent:5.1f}%  {raw}", True
    return raw, False


def run_import(
    cbimport: Path,
    settings: Settings,
    dataset: Path,
    key_expr: str,
    line_count: int,
    *,
    stderr=None,
) -> int:
    if stderr is None:
        stderr = sys.stderr
    command = import_command(cbimport, settings, dataset, key_expr)
    stdbuf = shutil.which("stdbuf")
    if stdbuf:
        command = [stdbuf, "-oL", "-eL", *command]
    print(f"importing {dataset.name} (key {key_expr})", file=stderr)
    # CB_PASSWORD is read by cbimport when -p is omitted, so the password
    # is not visible in /proc/<pid>/cmdline. The child environ is mode 0400.
    child_env = os.environ.copy()
    child_env["CB_PASSWORD"] = settings.password
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=child_env,
        text=True,
        bufsize=1,
    )
    assert process.stdout is not None
    carriage = False
    width = 0
    for line in process.stdout:
        shown, in_place = format_import_line(
            line.rstrip("\n"),
            dataset.name,
            line_count,
            settings.password,
        )
        if in_place:
            width = max(width, len(shown))
            print("\r" + shown.ljust(width), end="", file=stderr, flush=True)
            carriage = True
            continue
        if carriage:
            print(file=stderr)
            carriage = False
            width = 0
        print(shown, file=stderr)
    if carriage:
        print(file=stderr)
    return process.wait()


def main(
    argv: list[str] | None = None,
    *,
    root: Path | None = None,
    environ: Mapping[str, str] | None = None,
    stdin=None,
) -> int:
    del argv
    project = ROOT if root is None else root
    try:
        settings = load_settings(project, environ)
    except SettingsError as exc:
        print(exc, file=sys.stderr)
        return 1
    try:
        cluster = check_couchbase(settings)
    except CouchbaseError as exc:
        print(exc, file=sys.stderr)
        return 1

    cbimport = find_cbimport(project)
    if cbimport is None:
        if not ask_install(stdin):
            print(STOP_MESSAGE, file=sys.stderr)
            return 1
        try:
            cbimport = install_cbimport(project, cluster)
        except (DownloadError, RuntimeError, urllib.error.URLError, OSError) as exc:
            print(f"Could not install cbimport: {exc}", file=sys.stderr)
            return 1

    demo_dir = project / "data" / "demo"
    try:
        names = discover_json_names(settings.cdn_url)
        counts, errors = download_datasets(settings, demo_dir, names)
    except OSError as exc:
        print(f"Download failed: {exc}", file=sys.stderr)
        return 1
    for message in errors:
        print(message, file=sys.stderr)
    missing = [name for name in REQUIRED_FILES if name not in counts]
    if missing:
        print(
            "Not importing. These files are required and were not downloaded: "
            + ", ".join(missing),
            file=sys.stderr,
        )
        return 1

    for name, key_expr in IMPORT_JOBS:
        try:
            code = run_import(
                cbimport,
                settings,
                demo_dir / name,
                key_expr,
                counts[name],
            )
        except OSError as exc:
            print(f"Could not run cbimport for {name}: {exc}", file=sys.stderr)
            return 1
        if code != 0:
            print(
                f"cbimport failed for {name} (exit {code}). "
                "Remaining files were not imported.",
                file=sys.stderr,
            )
            return code if code > 0 else 1
    print(
        f"Imported {len(IMPORT_JOBS)} files into bucket {settings.bucket}.",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
