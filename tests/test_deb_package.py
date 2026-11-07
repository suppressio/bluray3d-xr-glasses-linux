"""The .deb's dependencies (scripts/build-deb.sh) against what src/ imports:
the package installs src/*.py as they are, so every import must be the
standard library, another src/ module or a package the control file names.
The built package itself is installed and checked by scripts/deb-install-test.sh."""
import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"


def _control_field(name: str) -> list[str]:
    script = (ROOT / "scripts" / "build-deb.sh").read_text(encoding="utf-8")
    match = re.search(rf"^{name}: (.*)$", script, re.MULTILINE)
    assert match, name
    return [p.strip() for p in match.group(1).split(",")]


def _imported_modules() -> set[str]:
    modules: set[str] = set()
    for path in SRC.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                modules.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                modules.add(node.module.split(".")[0])
    return modules


def test_former_fuse_packages_are_gone() -> None:
    fields = _control_field("Depends") + _control_field("Recommends")
    assert not [p for p in fields if "pyfuse3" in p or "trio" in p]


def test_fuse_and_samba_stay_optional() -> None:
    # --share dlna needs neither: a --no-install-recommends install stays usable
    assert _control_field("Recommends") == ["fuse3", "samba"]
    assert not [p for p in _control_field("Depends") if "fuse" in p or "samba" in p]


def test_src_imports_only_stdlib_and_own_modules() -> None:
    own = {path.stem for path in SRC.glob("*.py")}
    # _winreg: mfusepy's Python 2 name for winreg, tried first on Windows only
    allowed = own | set(sys.stdlib_module_names) | {"__future__", "_winreg"}
    third_party = _imported_modules() - allowed
    assert third_party == set()


def test_vendored_mfusepy_keeps_its_license() -> None:
    # ISC: the notice must travel with every copy, and the package installs the file as is
    head = (SRC / "mfusepy.py").read_text(encoding="utf-8")[:1000]
    assert "Copyright (c) 2008 Giorgos Verigakis" in head
    assert "Permission to use, copy, modify, and distribute this software" in head
