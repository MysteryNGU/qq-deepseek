"""Run one test module with only the modules it imports, inside the isolation sandbox.

Why not the whole suite: ``tests/run_isolated_suite.py`` stages a full copy of the source
tree and runs all 800+ tests (~10 min on this workstation, and staging alone takes > 10 min
when the tree has grown). For a focused change that is slow and noisy.

Why a whitelist does not work either: ``life/__init__.py`` re-exports from the whole package,
so importing ``life.companion_mobile`` drags in every module the package names — unless the
sandbox's ``life/`` is made a plain *namespace* package (no ``__init__.py``), in which case
``import life.companion_mobile`` loads only that file and its relative imports. The runner
therefore stages ``life/__init__.py`` as an empty namespace marker plus the transitive local
imports of the test module, and refuses to run if the import graph cannot be closed.

Isolation is unchanged: temporary HOME, fresh database, no network, no production path is
written (``tests/companion_test_isolation.Sandbox`` owns all of that).

Usage (from the repository root)::

    python -B -X utf8 tests/run_isolated_module.py tests.test_phone_presence
    python -B -X utf8 tests/run_isolated_module.py tests.test_phone_presence --verbose
"""
from __future__ import annotations

import argparse
import ast
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import companion_test_isolation as isolation  # noqa: E402

# Import names differ from file names on Windows (life.appraisal -> life/Appraisal.py), so
# every life/*.py file is indexed case-insensitively rather than resolved by name.
_LIFE_INDEX: dict[str, Path] | None = None


def _life_module(name: str) -> Path | None:
    global _LIFE_INDEX
    if _LIFE_INDEX is None:
        _LIFE_INDEX = {path.stem.lower(): path for path in (ROOT / "life").glob("*.py")}
    return _LIFE_INDEX.get(name.lower())


def _import_edges(module: Path) -> list[Path]:
    """Local modules one file imports; stdlib and third-party names are ignored."""
    try:
        tree = ast.parse(module.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return []
    edges: list[Path] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names = []
            if node.level == 0 and node.module and node.module.startswith("life."):
                names.append(node.module.split(".")[-1])
            elif node.level > 0 and module.parent.name == "life":
                if node.module:
                    names.append(node.module.split(".")[0])
                names.extend(alias.name for alias in node.names)
            for name in names:
                found = _life_module(name)
                if found:
                    edges.append(found)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("life."):
                    found = _life_module(alias.name.split(".")[-1])
                    if found:
                        edges.append(found)
    return edges


def closure(entry: Path) -> set[Path]:
    """Every local file the entry needs, transitively (life/*.py by name, no package init)."""
    seen: set[Path] = set()
    pending = [entry]
    while pending:
        module = pending.pop()
        if module in seen:
            continue
        seen.add(module)
        pending.extend(_import_edges(module))
    return seen


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("module", help="dotted test module, e.g. tests.test_phone_presence")
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--keep", action="store_true")
    arguments = parser.parse_args(argv)

    entry = ROOT / (arguments.module.replace(".", os.sep) + ".py")
    if not entry.is_file():
        print(f"no such test module: {entry}", file=sys.stderr)
        return 2
    needed = closure(entry)
    sandbox = isolation.Sandbox(base_dir=None, db="fresh", copy_source=None, keep=arguments.keep)
    overall = 1
    try:
        sandbox.neutralize_config()
        target = Path(sandbox.cwd())
        for module in sorted(needed):
            relative = module.relative_to(ROOT)
            destination = target / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(module.read_bytes())
        # Namespace marker: without the real __init__ the package imports stay local.
        (target / "life" / "__init__.py").write_text("", encoding="utf-8")
        (target / "tests" / "__init__.py").write_text("", encoding="utf-8")
        print(f"# sandbox={sandbox.root}")
        print(f"# staged_modules={len(needed) + 2}")
        for module in sorted(needed):
            print(f"  + {module.relative_to(ROOT).as_posix()}")
        isolation.self_check(sandbox)
        deps = [str(ROOT / ".test-deps")]
        environment = sandbox.env({"PYTHONPATH": os.pathsep.join([str(target), *deps])})
        command = [arguments.python, "-B", "-X", "utf8", "-m", "unittest", arguments.module]
        if arguments.verbose:
            command.append("-v")
        result = subprocess.run(command, cwd=str(target), env=environment, capture_output=True,
                                timeout=600)
        output = (result.stdout + result.stderr).decode("utf-8", "replace")
        print(output)
        overall = result.returncode
        print(f"MODULE_RESULT={arguments.module} exit={overall} staged={len(needed) + 2}")
    finally:
        cleaned = sandbox.cleanup()
        print(f"cleaned={str(cleaned).lower()} home_deleted={not Path(sandbox.home).exists()}")
    return 0 if overall == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
