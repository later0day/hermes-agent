"""Real detached default-plugin installation against a loopback Git catalog.

Only the catalog, plugin repository, TLS endpoint, and pre-provisioned dependency
artifacts are fixtures. The parent, detached module child, Git transport, PM
publication, enablement, discovery, and tool dispatch all run production code.
"""
from __future__ import annotations

import ipaddress
import json
import os
import shutil
import signal
import ssl
import subprocess
import sys
import threading
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import psutil
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from hermes_yaml import safe_load
from tests.waiting import wait_until

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_NAME = "default-install-e2e"
TOOL_NAME = "default_install_e2e_ping"


def _git(cwd: Path, *args: str) -> str:
    env = os.environ.copy()
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        env.pop(key, None)
    env.update(
        GIT_AUTHOR_NAME="Hermes test",
        GIT_AUTHOR_EMAIL="hermes-test@example.invalid",
        GIT_COMMITTER_NAME="Hermes test",
        GIT_COMMITTER_EMAIL="hermes-test@example.invalid",
    )
    result = subprocess.run(
        ["git", *args], cwd=cwd, env=env, capture_output=True, text=True,
        timeout=30, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout.strip()


def _plugin_repository(root: Path) -> tuple[Path, str]:
    work = root / "plugin-work"
    work.mkdir()
    (work / "plugin.yaml").write_text(
        f"name: {PLUGIN_NAME}\n"
        "manifest_version: 1\n"
        "version: 1.0.0\n"
        "description: Real detached default-plugin installation fixture\n"
        f"provides_tools: [{TOOL_NAME}]\n",
        encoding="utf-8",
    )
    (work / "__init__.py").write_text(
        "import json\n\n"
        "def ping(args, **kwargs):\n"
        f"    return json.dumps({{'ok': True, 'plugin': {PLUGIN_NAME!r}}})\n\n"
        "def register(ctx):\n"
        f"    ctx.register_tool(name={TOOL_NAME!r}, toolset='default_plugins_e2e',\n"
        "        schema={'name': " + repr(TOOL_NAME) + ", 'description': 'loopback install probe',\n"
        "        'parameters': {'type': 'object', 'properties': {}, 'additionalProperties': False}},\n"
        "        handler=ping)\n",
        encoding="utf-8",
    )
    _git(work, "init", "-qb", "main")
    _git(work, "add", "plugin.yaml", "__init__.py")
    _git(work, "commit", "-qm", "Default plugin E2E fixture")
    revision = _git(work, "rev-parse", "HEAD")
    bare = root / f"{PLUGIN_NAME}.git"
    _git(root, "clone", "-q", "--bare", str(work), str(bare))
    return bare, revision


def _certificate(root: Path) -> tuple[Path, Path]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Hermes loopback Git")])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(key, hashes.SHA256())
    )
    certificate_path = root / "loopback-ca.pem"
    key_path = root / "loopback-key.pem"
    certificate_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ))
    return certificate_path, key_path


@contextmanager
def _serve_git(repository: Path, certificate: Path, key: Path):
    requests: list[str] = []
    request_lock = threading.Lock()

    class GitHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            self._git_backend()

        def do_POST(self):
            self._git_backend()

        def _git_backend(self):
            with request_lock:
                requests.append(self.path)
            parsed = urlsplit(self.path)
            length = int(self.headers.get("Content-Length", "0"))
            env = os.environ.copy()
            env.update({
                "GIT_PROJECT_ROOT": str(repository.parent),
                "GIT_HTTP_EXPORT_ALL": "1",
                "PATH_INFO": parsed.path,
                "QUERY_STRING": parsed.query,
                "REQUEST_METHOD": self.command,
                "CONTENT_TYPE": self.headers.get("Content-Type", ""),
                "CONTENT_LENGTH": str(length),
                "REMOTE_ADDR": self.client_address[0],
                "SERVER_PROTOCOL": self.request_version,
                "SERVER_NAME": "127.0.0.1",
                "SERVER_PORT": str(self.server.server_port),
                "HTTPS": "on",
            })
            if self.headers.get("Git-Protocol"):
                env["HTTP_GIT_PROTOCOL"] = self.headers["Git-Protocol"]
            result = subprocess.run(
                ["git", "http-backend"], input=self.rfile.read(length),
                capture_output=True, env=env, timeout=30, check=False,
            )
            assert result.returncode == 0, result.stderr.decode(errors="replace")
            head, separator, body = result.stdout.partition(b"\r\n\r\n")
            if not separator:
                head, separator, body = result.stdout.partition(b"\n\n")
            assert separator, result.stdout[:500]
            headers = []
            status = 200
            for line in head.splitlines():
                name, value = line.decode("iso-8859-1").split(":", 1)
                if name.lower() == "status":
                    status = int(value.strip().split()[0])
                else:
                    headers.append((name, value.strip()))
            self.send_response_only(status)
            for name, value in headers:
                self.send_header(name, value)
            if not any(name.lower() == "content-length" for name, _ in headers):
                self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.close_connection = True
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def log_message(self, _format, *args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), GitHandler) as server:
        server.daemon_threads = True
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certificate, key)
        server.socket = context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"https://127.0.0.1:{server.server_port}/{repository.name}", requests
        finally:
            server.shutdown()
            thread.join(timeout=10)


def _copy_tree(source: Path, target: Path) -> None:
    import errno

    target.mkdir(parents=True)
    with os.scandir(source) as entries:
        for entry in entries:
            src, dst = Path(entry.path), target / entry.name
            if entry.is_symlink():
                dst.symlink_to(os.readlink(src))
            elif entry.is_dir(follow_symlinks=False):
                _copy_tree(src, dst)
            else:
                try:
                    os.link(src, dst)
                except OSError as exc:
                    if exc.errno not in {errno.EXDEV, errno.EPERM, errno.EACCES}:
                        raise
                    shutil.copy2(src, dst)


def _seed_dependency_runtime(home: Path) -> Path:
    """Clone the test interpreter into a current, offline in-process PM runtime."""
    from pm.environments import install_key
    from pm.packages import Venv
    from pm.runtime import _inputs

    source_generation = Path(sys.prefix).parent
    assert source_generation.parent.name == "test-environment", source_generation
    target_state = home / "installs" / install_key(PROJECT_ROOT)
    target_generation = target_state / "environments" / "e2e"
    _copy_tree(source_generation, target_generation)
    python = target_generation / "venv" / "bin" / "python"
    (python.parent.parent / "pm-runtime.json").write_text(json.dumps({
        "inputs": _inputs(PROJECT_ROOT, python),
    }), encoding="utf-8")
    (target_state / "facts.json").write_text(json.dumps({
        "schema": 1,
        "packages": {"venv": {
            "environment": str(python.parent.parent),
            "extras": [],
            "resolved_lock": str(PROJECT_ROOT / "uv.lock"),
            "stamp": Venv(PROJECT_ROOT).expected_stamp([], plugin_dirs=[]),
        }},
    }), encoding="utf-8")
    return python


def _child_env(home: Path, os_home: Path, certificate: Path) -> dict[str, str]:
    env = os.environ.copy()
    for key in (
        "HERMES_RUNTIME_DIR", "HERMES_INSTALL_ROOT", "__HERMES_ACTIVATED",
        "HERMES_DISABLE_LAZY_INSTALLS", "VIRTUAL_ENV", "PYTHONHOME", "PYTHONPATH",
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy",
    ):
        env.pop(key, None)
    env.update({
        "HOME": str(os_home),
        "USERPROFILE": str(os_home),
        "HERMES_HOME": str(home),
        "HERMES_TEST_ISOLATION": str(home),
        "GIT_SSL_CAINFO": str(certificate),
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
        "UV_OFFLINE": "1",
        "TMPDIR": str(os_home / "tmp"),
        "TMP": str(os_home / "tmp"),
        "TEMP": str(os_home / "tmp"),
    })
    (os_home / "tmp").mkdir(parents=True, exist_ok=True)
    return env


def _process_finished(pid: int) -> bool:
    try:
        return psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return True


def _parent_script() -> str:
    return """
import json, os, sys
from pathlib import Path
root = Path(sys.argv.pop(1))
sys.path.insert(0, str(root))
import hermes_bootstrap
import psutil
from hermes_cli.default_plugins import _identity, spawn_install
home, receipt = map(Path, sys.argv[1:3])
before = {process.pid for process in psutil.Process().children()}
spawned = spawn_install(home)
children = [process for process in psutil.Process().children() if process.pid not in before]
data = {"spawned": spawned, "children": [process.pid for process in children], "identity": _identity()}
receipt.write_text(json.dumps(data), encoding="utf-8")
assert spawned
assert len(children) == 1, data
child = children[0]
data["argv"] = child.cmdline()
data["detached"] = os.getsid(child.pid) == child.pid and os.getsid(0) != child.pid
receipt.write_text(json.dumps(data), encoding="utf-8")
assert any("hermes_cli.default_plugins" in argument for argument in data["argv"]), data
assert data["detached"], data
"""


def _discovery_script() -> str:
    return f"""
import json, sys
from pathlib import Path
root = Path(sys.argv.pop(1))
sys.path.insert(0, str(root))
import hermes_bootstrap
from hermes_cli.default_plugins import step_install_default_plugins
from hermes_cli.plugins import discover_plugins, get_plugin_manager
from tools.registry import registry
receipt = Path(sys.argv[1])
step = step_install_default_plugins()
assert step == {{"ok": True}}, step
discover_plugins(force=True)
rows = [row for row in get_plugin_manager().list_plugins() if row["name"] == {PLUGIN_NAME!r}]
assert len(rows) == 1, get_plugin_manager().list_plugins()
row = rows[0]
assert row["source"] == "user" and row["enabled"] is True, row
assert row["error"] is None and row["tools"] == 1, row
entry = registry.get_entry({TOOL_NAME!r})
assert entry is not None and entry.toolset == "default_plugins_e2e", entry
result = json.loads(registry.dispatch({TOOL_NAME!r}, {{}}))
assert result == {{"ok": True, "plugin": {PLUGIN_NAME!r}}}, result
receipt.write_text(json.dumps({{"step": step, "plugin": row, "tool_result": result}}), encoding="utf-8")
"""


def _run_real_process(python: Path, script: str, env: dict[str, str], *args: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(python), "-I", "-B", "-c", script, str(PROJECT_ROOT), *map(str, args)],
        cwd=PROJECT_ROOT, env=env, capture_output=True, text=True, timeout=60, check=False,
    )


@pytest.mark.platforms("posix")
def test_default_plugin_detached_install_is_real_end_to_end(tmp_path: Path):
    home = tmp_path / "hermes-home"
    os_home = tmp_path / "os-home"
    home.mkdir()
    os_home.mkdir()
    runtime_python = _seed_dependency_runtime(home)
    repository, revision = _plugin_repository(tmp_path)
    certificate, key = _certificate(tmp_path)
    child_pid = None

    with _serve_git(repository, certificate, key) as (repo_url, requests):
        catalog = home / "cache" / "plugin-catalog.json"
        catalog.parent.mkdir(parents=True, exist_ok=True)
        catalog.write_text(json.dumps({
            "generated_at": datetime.now(UTC).isoformat(),
            "entries": [{
                "name": PLUGIN_NAME,
                "repo": repo_url,
                "sha": revision,
                "description": "Detached installer E2E fixture",
                "maintainer": "Hermes tests",
                "tier": "official",
                "category": "tools",
                "default": True,
                "capabilities": {"provides_tools": [TOOL_NAME]},
            }],
            "removed": [],
        }), encoding="utf-8")
        env = _child_env(home, os_home, certificate)
        parent_receipt = tmp_path / "parent.json"
        try:
            parent = _run_real_process(runtime_python, _parent_script(), env, home, parent_receipt)
            receipt = json.loads(parent_receipt.read_text(encoding="utf-8")) if parent_receipt.is_file() else {}
            if receipt.get("children"):
                child_pid = receipt["children"][0]
            assert parent.returncode == 0, parent.stdout + parent.stderr + _child_log(home)
            assert receipt["spawned"] is True and receipt["detached"] is True
            assert len(receipt["children"]) == 1
            assert receipt["identity"] == _git(PROJECT_ROOT, "rev-parse", "HEAD")

            marker = home / "cache" / "default-plugins.settled"
            try:
                wait_until(marker.is_file, timeout=180, interval=0.1, what="detached default-plugin install")
            except AssertionError as exc:
                raise AssertionError(f"{exc}\n{_child_log(home)}") from exc
            assert marker.read_text(encoding="utf-8").strip() == receipt["identity"]
            wait_until(lambda: _process_finished(child_pid), timeout=30, what="detached child exit")
            child_pid = None

            installed = home / "plugins" / PLUGIN_NAME
            installed_state = sorted(
                str(path.relative_to(installed)) for path in installed.rglob("*")
            ) if installed.is_dir() else []
            assert (installed / ".git").is_dir(), (
                f"installed tree: {installed_state}\n{_child_log(home)}"
            )
            assert _git(installed, "rev-parse", "HEAD") == revision
            metadata = json.loads((home / "plugins" / ".install-metadata.json").read_text(encoding="utf-8"))
            record = metadata[PLUGIN_NAME]
            assert record["source"] == repo_url and record["revision"] == revision
            assert record["catalog"]["name"] == PLUGIN_NAME
            assert record["catalog"]["repo"] == repo_url
            assert record["catalog"]["pin"] == revision and record["catalog"]["sha"] == revision
            config = safe_load((home / "config.yaml").read_text(encoding="utf-8"))
            assert PLUGIN_NAME in config["plugins"]["enabled"]
            assert PLUGIN_NAME in config["_default_plugins_installed"]
            assert any(path.startswith(f"/{repository.name}/info/refs?service=git-upload-pack") for path in requests), requests
            assert f"/{repository.name}/git-upload-pack" in requests, requests
            assert f"Installed default plugin '{PLUGIN_NAME}'" in _child_log(home)

            discovery_receipt = tmp_path / "discovery.json"
            discovery = _run_real_process(runtime_python, _discovery_script(), env, discovery_receipt)
            assert discovery.returncode == 0, discovery.stdout + discovery.stderr + _child_log(home)
            proof = json.loads(discovery_receipt.read_text(encoding="utf-8"))
            assert proof["step"] == {"ok": True}
            assert proof["tool_result"] == {"ok": True, "plugin": PLUGIN_NAME}
        finally:
            if child_pid is not None and not _process_finished(child_pid):
                try:
                    os.killpg(child_pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass


def _child_log(home: Path) -> str:
    path = home / "logs" / "default-plugins.log"
    return path.read_text(encoding="utf-8", errors="replace") if path.is_file() else "<no detached child log>"
