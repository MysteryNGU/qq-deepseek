"""Isolated test environment for the companion suite.

Why this module exists
----------------------
The production runtime lives in ``/opt/qq-deepseek`` (no git) and keeps the real
state in ``/var/lib/companion/xiaoyu``.  Running the test suite in place would let
a test create memories, rewrite ``config.toml`` or reach a real QQ session.  This
helper builds a disposable sandbox instead:

* the suite runs against a **copy** of the source tree (``test-run``), never the
  live tree; the live tree is only read,
* ``HOME``/``USERPROFILE``/``APPDATA``/``XDG_*`` and ``TMPDIR`` point into the
  sandbox, so ``Path.home()`` and ``tempfile`` never touch the operator account,
* the life database is either a brand-new temporary database (default ``fresh``)
  or an immutable byte copy of a production SQLite database made through the
  SQLite backup API (``copy``), which never writes to the source file,
* outbound platform / external execution is disabled with a deny-list of
  environment variables plus a socket guard and a subprocess guard for the
  in-process runner,
* ``config.toml`` in the sandbox is neutralised and its secrets are blanked,
* ``cleanup()`` removes the sandbox and prints ``cleaned=`` / ``home_deleted=``.

The module has no third-party imports so it works with the bare interpreter
(``python -B -X utf8 tests/companion_test_isolation.py``) and with the cloud
companion venv.

Ownership guard
---------------
``_guard_production_paths`` refuses to create a sandbox *inside* a protected
production root.  Callers may set ``COMPANION_PROTECTED_ROOTS`` (``os.pathsep``
separated) to add roots; the helper always protects ``/var/lib/companion`` and
the ``.venv-companion`` tree, plus whatever ``COMPANION_TEST_PROTECTED`` names.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import traceback
from contextlib import contextmanager
from pathlib import Path

MARKER = ".companion-test-sandbox"
EXIT_GUARD_FAILED = 86
EXIT_SELF_CHECK_FAILED = 87
BACKUP_ATTEMPTS = 5
# Directory names never staged into a test tree: VCS, virtualenvs, caches,
# bundled runtimes and the data that holds production state.  Copying them is
# slow (vendor alone is >2 GB) and, for data/, exactly what isolation forbids.
COPY_IGNORE_NAMES = {
    ".git", ".venv", ".venv-companion", "venv", ".test-deps", "__pycache__",
    "data", "local-backups", "vendor", "napcat", "napcat-onekey",
    "xiaoyu-private-migration-20261027-094202", ".agent-teams",
}
COPY_IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", *sorted(COPY_IGNORE_NAMES))

# A minimal projection of the vendored DSH bridge: tests read these paths
# (e.g. tests/test_context_efficiency.py reads the generated contract fixture
# under vendor/dsh-bridge-work/packages/sdk/server/tests/fixtures/), but the
# full tree is >2 GB and its nested node_modules are hard to delete on Windows.
# The staging step pulls in just the files below, so the baselined set of tests
# keeps the same inputs while the sandbox stays small.  Anything outside these
# patterns is not available to the suite, and TREE_CLEAN/last-line evidence says so.
VENDOR_INCLUDE = [
    "vendor/dsh-bridge-work/packages/sdk/server/tests/fixtures/*.json",
    "vendor/dsh-bridge-work/packages/sdk/server/tests/fixtures/*.yaml",
    "vendor/dsh-bridge-work/python/**",
]

# Credentials are *deleted*, not blanked: ``os.environ.get(name, '')`` treats an
# empty string as "the variable exists", and ``load_config`` honours a present
# DEEPSEEK_API_KEY, so blanking made the suite read back an empty secret
# (tests/test_settings.py asserts the file value survives).  Removing the name
# means the caller really has no credential.
SECRET_ENV = (
    "DEEPSEEK_API_KEY",
    "COMPANION_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "HF_TOKEN",
    "QQ_WS_URL",
    "QQ_WORKER_TOKEN",
    "QQ_WORKER_HEALTH",
    "ADB_PATH",
    "ANDROID_SERIAL",
    "OPENAI_BASE_URL",
)

# Protection switches must stay *present*: "1" means offline, and dropping them
# would stop disabling the platform instead of disabling it harder.
DENY_ENV = {
    "HF_HUB_OFFLINE": "1",
    "HF_HUB_DISABLE_TELEMETRY": "1",
    "TRANSFORMERS_OFFLINE": "1",
    "CUDA_VISIBLE_DEVICES": "",  # an empty value is the "no CUDA" semantics
}

# Dataclass field names that carry a secret in ``life.role_config``.
SECRET_FIELDS = ("ws_token", "api_key")
REDACTED = ""


def _protected_roots():
    roots = [Path("/var/lib/companion"), Path("/opt/qq-deepseek/.venv-companion"),
             Path("/opt/qq-deepseek/life"), Path("/opt/qq-deepseek/tests")]
    for name in ("COMPANION_PROTECTED_ROOTS", "COMPANION_TEST_PROTECTED"):
        for entry in (os.environ.get(name) or "").split(os.pathsep):
            if entry.strip():
                roots.append(Path(entry.strip()))
    return roots


def _guard_production_paths(target):
    resolved = Path(target).resolve()
    for root in _protected_roots():
        try:
            protected = root.resolve()
        except OSError:
            continue
        if resolved == protected or protected in resolved.parents:
            raise SystemExit(
                f"refusing sandbox inside protected path {protected}: {resolved}")


def _write_marker(root):
    (Path(root) / MARKER).write_text(str(os.getpid()), encoding="utf-8")


def _merge_env(base, extra):
    merged = dict(base)
    if extra:
        merged.update({str(key): str(value) for key, value in extra.items()})
    return merged


def redact_config(text):
    """Blank ``<field> = "value"`` lines for credential field names."""
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if any(stripped.startswith(f'"{field}"') for field in SECRET_FIELDS):
            key = stripped.split("=", 1)[0].strip()
            lines.append(f"{key} = {REDACTED!r}")
        else:
            lines.append(line)
    return "\n".join(lines) + ("\n" if text.endswith("\n") else "")


def snapshot_sqlite(source, destination):
    """Immutable copy of a live SQLite database; the source is only opened ro."""
    source, destination = Path(source), Path(destination)
    if destination.exists():
        _chmod(destination, 0o600)
        destination.unlink()
    last = None
    for _ in range(BACKUP_ATTEMPTS):
        try:
            origin = sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=10)
            try:
                copy = sqlite3.connect(str(destination))
                try:
                    origin.backup(copy)
                finally:
                    copy.close()
            finally:
                origin.close()
            break
        except sqlite3.OperationalError as error:  # 'database is locked' on a busy life.db
            last = error
            time.sleep(0.5)
    else:
        raise RuntimeError(f"sqlite snapshot failed for {source}: {last}")
    digest = hashlib.sha256(destination.read_bytes()).hexdigest()
    _chmod(destination, 0o444)
    return digest


def _chmod(path, mode):
    """chmod without failing on platforms that ignore it (Windows)."""
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _make_writable(root):
    """The copied life.db is chmod 0444 on POSIX; clear that before deleting."""
    for current, directories, files in os.walk(root):
        for name in files:
            _chmod(os.path.join(current, name), 0o600)
        for name in directories:
            _chmod(os.path.join(current, name), 0o700)


def _long(path):
    """Windows extended-length form so >260-char trees (vendor/) stay deletable."""
    text = str(path)
    if os.name == "nt" and not text.startswith("\\\\?\\"):
        return "\\\\?\\" + os.path.abspath(text)
    return text


def force_delete(root, attempts=12):
    """Remove a tree, tolerating read-only files and long paths; returns bool."""
    root = Path(root)
    if not root.exists():
        return True
    if os.name == "nt":
        import stat
        for _ in range(attempts):
            for current, directories, files in os.walk(_long(root)):
                for name in files:
                    path = os.path.join(current, name)
                    try:
                        os.chmod(path, stat.S_IWRITE)
                    except OSError:
                        pass
                    try:
                        os.remove(path)
                    except OSError:
                        pass
                for name in directories:
                    try:
                        os.rmdir(os.path.join(current, name))
                    except OSError:
                        pass
            try:
                os.rmdir(_long(root))
            except OSError:
                pass
            if not root.exists():
                return True
        return not root.exists()
    _make_writable(root)
    shutil.rmtree(root, ignore_errors=True)
    return not root.exists()


def sha256_manifest(root, include, exclude=None):
    """Sorted ``sha256  relpath`` lines for every file below root/include."""
    root = Path(root)
    excluded = tuple(exclude or ())
    rows = []
    for relative in include:
        base = root / relative
        if base.is_file():
            candidates = [base]
        elif base.is_dir():
            candidates = [path for path in base.rglob("*") if path.is_file()]
        else:
            continue
        for path in candidates:
            name = path.relative_to(root).as_posix()
            if any(part in name for part in excluded):
                continue
            rows.append(f"{sha256_file(path)}  {name}")
    return "\n".join(sorted(rows)) + ("\n" if rows else "")


def snapshot_manifest(root, destination, include=None, exclude=None):
    """Write a baseline manifest of the live tree; never writes inside ``root``."""
    destination = Path(destination)
    if destination.exists():
        _make_writable(destination.parent)
    text = sha256_manifest(root, include or ["."], exclude)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(text, encoding="utf-8")
    return len(text.splitlines()), sha256_file(destination)


class Sandbox:
    """Disposable test home. ``cleanup()`` is idempotent and prints a receipt."""

    def __init__(self, *, base_dir=None, db="fresh", db_source=None, copy_source=None,
                 source_root=None, keep=False, label="companion"):
        if db not in ("fresh", "copy"):
            raise ValueError("db must be 'fresh' or 'copy'")
        if db == "copy" and not db_source:
            raise ValueError("db='copy' requires db_source")
        self.base_dir = Path(base_dir) if base_dir else Path(tempfile.gettempdir())
        self.base_dir.mkdir(parents=True, exist_ok=True)
        _guard_production_paths(self.base_dir)
        self.label = label
        self.db_mode = db
        self.db_source = Path(db_source) if db_source else None
        self.copy_source = Path(copy_source) if copy_source else None
        self.keep = keep
        self.created_at = time.strftime("%Y-%m-%dT%H:%M:%S%z")

        self.root = self.base_dir / f"{label}-test-{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}"
        self.home = self.root / "home"
        self.tmp = self.root / "tmp"
        self.tree = self.root / "tree"
        self.logs = self.root / "logs"
        for directory in (self.root, self.home, self.tmp, self.logs):
            directory.mkdir(parents=True, exist_ok=True)
        _write_marker(self.root)

        self._cleaned = False
        self.report = {
            "root": str(self.root),
            "home": str(self.home),
            "db_mode": db,
            "db_path": "",
            "db_sha256": "",
            "db_source_sha256_before": "",
            "db_source_sha256_after": "",
            "tree": "",
            "config_present": False,
            "config_secrets_redacted": False,
            "keep": bool(keep),
            "created_at": self.created_at,
        }

        if self.copy_source:
            self._stage_tree()
        self.life_db = self._prepare_db()
        self.report["db_path"] = str(self.life_db)

    # -- setup -----------------------------------------------------------
    def _stage_tree(self):
        if not self.copy_source.is_dir():
            raise ValueError(f"copy_source is not a directory: {self.copy_source}")
        if os.name == "nt" and shutil.which("robocopy"):
            # robocopy is 100x faster than copytree on Windows; exit codes 0-7 are success.
            parts = ["robocopy", str(self.copy_source), str(self.tree), "/E", "/XF", "*.pyc",
                     "/NFL", "/NDL", "/NJH", "/NJS", "/NP", "/R:1", "/W:1",
                     "/XD", *sorted(COPY_IGNORE_NAMES)]
            result = subprocess.run(parts, capture_output=True, text=True)
            if result.returncode > 7:
                raise RuntimeError(f"robocopy failed ({result.returncode}): {result.stderr[-800:]}")
            self._stage_vendor()
        else:
            shutil.copytree(self.copy_source, self.tree, ignore=COPY_IGNORE, symlinks=False)
            self._stage_vendor()
        self.report["tree"] = str(self.tree)
        source_db = self.tree / "data" / "life.db"
        if self.db_source is None and source_db.is_file():
            self.db_source = source_db
            self.db_mode = "copy"

    def _stage_vendor(self):
        """Copy only the vendored bridge files VENDOR_INCLUDE names."""
        source = Path(self.copy_source)
        candidates = []
        for pattern in VENDOR_INCLUDE:
            candidates.extend(source.glob(pattern))
        copied = 0
        for path in candidates:
            if not path.is_file():
                continue
            name = path.relative_to(source).as_posix()
            destination = self.tree / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)
            copied += 1
        self.report["vendor_files_staged"] = copied

    def _prepare_db(self):
        data = (self.tree if self.copy_source else self.root) / "data"
        data.mkdir(parents=True, exist_ok=True)
        target = data / "life.db"
        if self.db_mode == "copy":
            before = sha256_file(self.db_source) if self.db_source.is_file() else ""
            self.report["db_source_sha256_before"] = before
            digest = snapshot_sqlite(self.db_source, target)
            after = sha256_file(self.db_source) if self.db_source.is_file() else ""
            self.report["db_source_sha256_after"] = after
            if before and after and before != after:
                raise RuntimeError("production database changed during snapshot")
            self.report["db_sha256"] = digest
        else:
            sqlite3.connect(str(target)).close()
            self.report["db_sha256"] = sha256_file(target)
        return target

    def neutralize_config(self):
        """Point the sandbox ``config.toml`` at the sandbox DB and blank secrets."""
        config = self.tree / "config.toml" if self.copy_source else self.root / "config.toml"
        self.report["config_present"] = config.is_file()
        if not config.is_file():
            return None
        text = config.read_text(encoding="utf-8")
        redacted = redact_config(text)
        if redacted != text:
            self.report["config_secrets_redacted"] = True
        config.write_text(redacted, encoding="utf-8")
        return config

    # -- execution -------------------------------------------------------
    def env(self, extra=None, base=None):
        environment = dict(os.environ if base is None else base)
        home, tmp = str(self.home), str(self.tmp)
        environment.update({
            "HOME": home,
            "USERPROFILE": home,
            "APPDATA": str(self.home / "AppData" / "Roaming"),
            "LOCALAPPDATA": str(self.home / "AppData" / "Local"),
            "XDG_CONFIG_HOME": str(self.home / ".config"),
            "XDG_CACHE_HOME": str(self.home / ".cache"),
            "XDG_DATA_HOME": str(self.home / ".local" / "share"),
            "XDG_STATE_HOME": str(self.home / ".local" / "state"),
            "TMPDIR": tmp,
            "TEMP": tmp,
            "TMP": tmp,
            "PYTHONPATH": str(self.tree) if self.copy_source else "",
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONDONTWRITEBYTECODE": "1",
            "QQ_OFFLINE": "1",
            "COMPANION_TEST_HOME": str(self.root),
            "COMPANION_LIFE_DB": str(self.life_db),
            "NO_PROXY": "127.0.0.1,localhost",
        })
        for name in SECRET_ENV:
            environment.pop(name, None)
        environment.update(DENY_ENV)
        return _merge_env(environment, extra)

    def cwd(self):
        return str(self.tree if self.copy_source else self.root)

    def manifest_path(self):
        return self.root / "manifest.json"

    def write_manifest(self):
        self.manifest_path().write_text(
            json.dumps(self.report, ensure_ascii=False, indent=2), encoding="utf-8")
        return self.manifest_path()

    # -- teardown --------------------------------------------------------
    def cleanup(self):
        if self._cleaned:
            print(f"cleaned={self.root} (already cleaned)")
            print("home_deleted=true")
            return True
        self._cleaned = True
        if self.keep:
            self.write_manifest()
            print(f"cleaned=false (keep requested) root={self.root}")
            print("home_deleted=false")
            return False
        root = self.root
        deleted = force_delete(root)
        home_deleted = not Path(self.home).exists()
        print(f"cleaned={root} ok={str(deleted).lower()}")
        print(f"home_deleted={str(home_deleted).lower()}")
        if not deleted:
            print(f"cleaned_residual={root}", file=sys.stderr)
        return deleted and home_deleted


def default_sandbox(**kwargs):
    """Convenience factory used by scripts: fresh temporary DB, no source copy."""
    sandbox = Sandbox(**kwargs)
    sandbox.neutralize_config()
    return sandbox


# -- in-process guards ---------------------------------------------------
class ExternalCallBlocked(RuntimeError):
    pass


def _allowed_host(host):
    return host in ("127.0.0.1", "::1", "localhost", "0.0.0.0")


@contextmanager
def network_guard():
    """Refuse sockets to anything except loopback while the block is active."""
    original = socket.socket.connect

    def connect(self, address):
        try:
            host = address[0]
        except (TypeError, IndexError):
            host = ""
        if not _allowed_host(host):
            raise ExternalCallBlocked(f"network_guard blocked connect to {address}")
        return original(self, address)

    socket.socket.connect = connect
    try:
        yield
    finally:
        socket.socket.connect = original


@contextmanager
def external_exec_guard():
    """Refuse spawning processes from the test process; the suite uses none."""
    import subprocess
    original = subprocess.Popen.__init__

    def blocked(self, *args, **kwargs):
        raise ExternalCallBlocked(f"external_exec_guard blocked Popen: {args[:1]}")

    subprocess.Popen.__init__ = blocked
    try:
        yield
    finally:
        subprocess.Popen.__init__ = original


# -- self-check ----------------------------------------------------------
def self_check(sandbox):
    """Prove the sandbox is isolated; raise SystemExit(EXIT_SELF_CHECK_FAILED)."""
    checks = []
    home = Path(sandbox.home)
    operator_home = Path(os.path.expanduser("~")).resolve()
    checks.append(("home_is_sandbox", operator_home != home.resolve() and str(sandbox.root) in str(home)))
    checks.append(("life_db_sha_matches", sha256_file(sandbox.life_db) == sandbox.report["db_sha256"]))
    if sandbox.report["db_source_sha256_before"]:
        checks.append(("db_source_unchanged",
                       sandbox.report["db_source_sha256_before"] == sandbox.report["db_source_sha256_after"]))
    try:
        with network_guard():
            probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            probe.settimeout(2)
            try:
                probe.connect(("192.0.2.1", 9))  # TEST-NET-1, never routable
            finally:
                probe.close()
        checks.append(("network_guard_blocks", False))
    except ExternalCallBlocked:
        checks.append(("network_guard_blocks", True))
    except OSError as error:  # sandboxed host refused before our guard ran
        checks.append(("network_guard_blocks", True))
        print(f"# network_guard probe note: {error}")
    try:
        with external_exec_guard():
            import subprocess
            subprocess.run([sys.executable, "-c", "pass"], check=False)
        checks.append(("external_exec_blocked", False))
    except ExternalCallBlocked:
        checks.append(("external_exec_blocked", True))
    environment = sandbox.env()
    absent = [name for name in SECRET_ENV if name not in environment]
    checks.append(("secret_env_absent", len(absent) == len(SECRET_ENV)))
    checks.append(("switch_env_present",
                   environment.get("QQ_OFFLINE") == "1"
                   and all(environment.get(name) == value for name, value in DENY_ENV.items())))
    failures = [name for name, ok in checks if not ok]
    for name, ok in checks:
        print(f"self_check {name}={'pass' if ok else 'fail'}")
    if failures:
        print(f"self_check_failed={','.join(failures)}", file=sys.stderr)
        raise SystemExit(EXIT_SELF_CHECK_FAILED)
    return True


# -- command line --------------------------------------------------------
def _parser():
    parser = argparse.ArgumentParser(description="Create/tear down an isolated companion test home.")
    parser.add_argument("--base-dir", default=None, help="parent directory for the sandbox (default: system temp)")
    parser.add_argument("--db", choices=("fresh", "copy"), default="fresh")
    parser.add_argument("--db-source", default=None, help="production life.db to copy read-only")
    parser.add_argument("--copy-source", default=None, help="source tree to stage into the sandbox")
    parser.add_argument("--keep", action="store_true")
    parser.add_argument("--self-check", action="store_true")
    parser.add_argument("--manifest", default=None, help="write the sandbox manifest to this path")
    parser.add_argument("--snapshot-manifest", default=None,
                        help="only write a baseline sha256 manifest of --copy-source to this path")
    parser.add_argument("--snapshot-include", nargs="*", default=None,
                        help="relative paths to include in the snapshot (default: whole tree)")
    parser.add_argument("--snapshot-exclude", nargs="*", default=None,
                        help="substrings whose paths are skipped, e.g. __pycache__ .git")
    return parser


def main(argv=None):
    arguments = _parser().parse_args(argv)
    if arguments.snapshot_manifest:
        source = arguments.copy_source or "."
        count, digest = snapshot_manifest(source, arguments.snapshot_manifest,
                                          arguments.snapshot_include, arguments.snapshot_exclude)
        print(f"snapshot={arguments.snapshot_manifest}")
        print(f"files={count}")
        print(f"manifest_sha256={digest}")
        return 0
    sandbox = Sandbox(base_dir=arguments.base_dir, db=arguments.db, db_source=arguments.db_source,
                      copy_source=arguments.copy_source, keep=arguments.keep)
    try:
        sandbox.neutralize_config()
        if arguments.self_check:
            self_check(sandbox)
        if arguments.manifest:
            Path(arguments.manifest).write_text(
                json.dumps(sandbox.report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"sandbox={sandbox.root}")
        print(f"home={sandbox.home}")
        print(f"life_db={sandbox.life_db} sha256={sandbox.report['db_sha256']}")
        print(f"cwd={sandbox.cwd()}")
    finally:
        sandbox.cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
