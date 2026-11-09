"""Import boundary linter: stdlib-only, no network, one-way dependency direction.

Every module under ``src/life_os`` may import only:

- the Python standard library (minus network-capable modules), and
- ``life_os`` itself, respecting the layering ``core -> life_mentor -> weekly_tracker``
  (``core`` imports neither product; ``life_mentor`` never imports ``weekly_tracker``).

If this test fails, a plain ``pip install life-os`` with zero dependencies would
break, or a module could reach the network.
"""

from __future__ import annotations

import ast
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src" / "life_os"
PYPROJECT = SRC.parent.parent / "pyproject.toml"

NETWORK_FORBIDDEN = frozenset(
    {
        "socket",
        "socketserver",
        "ssl",
        "http",
        "urllib",
        "ftplib",
        "telnetlib",
        "smtplib",
        "poplib",
        "imaplib",
        "nntplib",
        "xmlrpc",
        "asyncio",
        "webbrowser",
        "requests",
        "urllib3",
        "aiohttp",
        "httpx",
    }
)

# Allowed ``urllib`` submodules: pure string parsing, no I/O.
NETWORK_ALLOWED_DOTTED = frozenset({"urllib.parse"})

# layer -> life_os subpackages it must NOT import
LAYER_FORBIDS = {
    "core": ("life_os.life_mentor", "life_os.weekly_tracker"),
    "life_mentor": ("life_os.weekly_tracker",),
    "weekly_tracker": (),
}


def _modules() -> Iterator[Path]:
    yield from sorted(SRC.rglob("*.py"))


def _layer(path: Path) -> str:
    rel = path.relative_to(SRC).parts
    return rel[0] if len(rel) > 1 else "root"


def _absolute_imports(path: Path) -> set[str]:
    """Fully-qualified names of every absolute import in *path*."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
            names.add(node.module)
    return names


def _relative_imports(path: Path) -> list[ast.ImportFrom]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return [n for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.level]


def _dynamic_import_calls(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            name = fn.id if isinstance(fn, ast.Name) else fn.attr if isinstance(fn, ast.Attribute) else ""
            if name in {"__import__", "import_module"}:
                hits.append(f"line {node.lineno}: {name}()")
    return hits


MODULES = list(_modules())


@pytest.mark.parametrize("path", MODULES, ids=lambda p: p.relative_to(SRC).as_posix())
def test_stdlib_only_and_no_network(path: Path) -> None:
    stdlib = set(sys.stdlib_module_names) | {"__future__"}
    for name in sorted(_absolute_imports(path)):
        top = name.split(".")[0]
        if top == "life_os":
            continue
        if name not in NETWORK_ALLOWED_DOTTED:
            assert top not in NETWORK_FORBIDDEN, f"{path} imports network module {name!r}"
        assert top in stdlib, f"{path} imports non-stdlib module {name!r} (runtime must be stdlib-only)"


@pytest.mark.parametrize("path", MODULES, ids=lambda p: p.relative_to(SRC).as_posix())
def test_dependency_direction(path: Path) -> None:
    forbidden = LAYER_FORBIDS.get(_layer(path), ())
    for name in _absolute_imports(path):
        for bad in forbidden:
            assert not (name == bad or name.startswith(bad + ".")), (
                f"{path} ({_layer(path)}) imports {name!r}; layering is core -> life_mentor -> weekly_tracker"
            )


@pytest.mark.parametrize("path", MODULES, ids=lambda p: p.relative_to(SRC).as_posix())
def test_relative_imports_stay_inside_their_layer(path: Path) -> None:
    # Package depth below life_os: life_os/core/gate.py -> 1, life_os/life_mentor/finance/tax.py -> 2.
    # A relative import with level > depth would resolve to life_os itself and cross layers.
    depth = len(path.relative_to(SRC).parts) - 1
    for node in _relative_imports(path):
        assert node.level <= depth, f"{path}:{node.lineno} relative import climbs out of its layer"


@pytest.mark.parametrize("path", MODULES, ids=lambda p: p.relative_to(SRC).as_posix())
def test_no_dynamic_imports(path: Path) -> None:
    assert not _dynamic_import_calls(path), f"{path} uses dynamic imports: {_dynamic_import_calls(path)}"


def test_boundary_covers_every_layer() -> None:
    layers = {_layer(p) for p in MODULES}
    assert {"core", "life_mentor", "weekly_tracker"} <= layers
    rels = {p.relative_to(SRC).as_posix() for p in MODULES}
    assert {
        "core/gate.py",
        "life_mentor/cli.py",
        "life_mentor/security/store.py",
        "weekly_tracker/cli.py",
        "weekly_tracker/sources/git_guard.py",
    } <= rels


def test_pyproject_declares_no_runtime_dependencies() -> None:
    text = PYPROJECT.read_text(encoding="utf-8")
    assert "\ndependencies = []\n" in text


def test_no_restricted_food_tables_bundled() -> None:
    # Only CC0 USDA data may be bundled; never IFCT/INDB/GI tables (licence-restricted).
    data = [p.name.casefold() for p in SRC.rglob("*") if p.is_file()]
    assert not [n for n in data if any(t in n for t in ("ifct", "indb", "glycemic_index"))]


def test_importing_everything_pulls_no_network_module() -> None:
    import subprocess

    code = (
        "import sys, pkgutil, importlib\n"
        "before=set(sys.modules)\n"
        "import life_os\n"
        "for m in pkgutil.walk_packages(life_os.__path__, 'life_os.'):\n"
        "    importlib.import_module(m.name)\n"
        "new={m for m in set(sys.modules)-before}\n"
        "bad={'socket','ssl','http.client','urllib.request','urllib.error','asyncio','requests','httpx'}\n"
        "print(sorted(new & bad))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False, timeout=60
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "[]"
