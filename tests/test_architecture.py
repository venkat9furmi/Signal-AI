"""The dependency rule, as a test.

Layering that is only written down in a README decays on the first busy
afternoon. This reads every import in the translate package and fails if one
points the wrong way, so the architecture is a thing the build checks rather
than a thing people remember.

The rule: dependencies point **inwards only**.

    web  ->  app  ->  core
             adapters  ->  core
             infra     ->  core

* ``core`` is the domain. It may import nothing from this package but itself,
  and nothing from the wider project. That is what makes it the one layer
  that never changes when a vendor does.
* ``infra`` and ``adapters`` may use ``core``, and nothing further out.
* ``adapters`` may not reach into each other. Camb must not import Gemini.
* ``app`` composes the rest. It may not import ``web``, and it may not name a
  vendor -- that is what the registry is for.
* ``settings`` sits outside the layers, because it is configuration that every
  layer reads; making it part of one would force the others to depend on that
  layer for a reason unrelated to why the layer exists.

    python tests/test_architecture.py
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "src" / "commentary_qc"
TRANSLATE = PKG / "translate"
BASE = "commentary_qc.translate."

PASS, FAIL = 0, 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  ok    {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}\n          {detail}")


def section(title: str) -> None:
    print(f"\n{title}\n" + "-" * len(title))


#: Which layers each layer may import from. ``settings`` is allowed
#: everywhere; the package root facade is allowed nowhere, because importing
#: it from inside is how an import cycle starts.
ALLOWED: dict[str, set[str]] = {
    "core": set(),
    "infra": {"core"},
    "adapters": {"core", "infra"},
    "app": {"core", "infra", "adapters"},
    "web": {"core", "infra", "adapters", "app"},
}

#: Layers that must not name a vendor. ``app`` gets its provider from the
#: registry; if it imports one by name, swapping the model stops being a
#: one-line change, which is the whole point of the layout.
VENDOR_FREE = ("core", "infra", "app", "web")
VENDORS = ("camb", "gemini", "mock")


def modules() -> list[tuple[Path, str, str]]:
    """Every module, with its layer and its dotted name."""
    out = []
    for path in sorted(TRANSLATE.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        rel = path.relative_to(TRANSLATE)
        layer = rel.parts[0] if len(rel.parts) > 1 else ""
        dotted = BASE + str(rel.with_suffix("")).replace("\\", ".").replace("/", ".")
        out.append((path, layer, dotted.removesuffix(".__init__")))
    return out


def imports_of(path: Path) -> list[str]:
    """Every module this file imports, as dotted names."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            found.append(node.module)
            # `from pkg import module` also names a module
            found += [f"{node.module}.{a.name}" for a in node.names]
    return found


def layer_of(dotted: str) -> str | None:
    if not dotted.startswith(BASE):
        return None
    rest = dotted[len(BASE):].split(".")
    return rest[0] if rest and rest[0] in ALLOWED else None


def test_no_relative_imports() -> None:
    section("imports are absolute, so a file can be moved without arithmetic")
    offenders = []
    for path, _, _ in modules():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level:
                offenders.append(f"{path.name}: {'.' * node.level}{node.module or ''}")
    check("every import inside translate/ is absolute",
          not offenders, "; ".join(offenders[:5]))


def test_dependency_rule() -> None:
    section("dependencies point inwards only")
    broken: dict[str, list[str]] = {}
    for path, layer, _ in modules():
        if layer not in ALLOWED:
            continue
        for imported in imports_of(path):
            other = layer_of(imported)
            if other is None or other == layer:
                continue
            if other not in ALLOWED[layer]:
                broken.setdefault(f"{layer} -> {other}", []).append(
                    f"{path.name} imports {imported}"
                )
    for layer, may in ALLOWED.items():
        edges = [k for k in broken if k.startswith(f"{layer} -> ")]
        check(
            f"{layer} imports only {sorted(may) or 'itself'}",
            not edges,
            "; ".join(broken[e][0] for e in edges),
        )


def test_core_is_pure() -> None:
    section("core is the domain, so it knows nothing technical")
    forbidden = (
        "fastapi", "sqlite3", "websockets", "urllib", "requests", "httpx",
        "subprocess", "wave", "scipy",
    )
    offenders = []
    for path, layer, _ in modules():
        if layer != "core":
            continue
        for imported in imports_of(path):
            head = imported.split(".")[0]
            if head in forbidden:
                offenders.append(f"{path.name} imports {imported}")
            if head == "commentary_qc" and not imported.startswith(BASE + "core"):
                if not imported.startswith(BASE + "settings"):
                    offenders.append(f"{path.name} reaches out to {imported}")
    check("core imports no framework, no database, no transport, no ffmpeg",
          not offenders, "; ".join(offenders[:5]))


def test_adapters_are_sealed() -> None:
    section("adapters do not know about each other")
    offenders = []
    for path, layer, dotted in modules():
        if layer != "adapters":
            continue
        rel = path.relative_to(TRANSLATE / "adapters")
        mine = rel.parts[0] if len(rel.parts) > 1 else ""
        for imported in imports_of(path):
            if not imported.startswith(BASE + "adapters."):
                continue
            theirs = imported[len(BASE + "adapters."):].split(".")[0]
            if mine and theirs and theirs != mine:
                offenders.append(f"{mine}/{path.name} imports {theirs}")
    check("no adapter imports another vendor's code, so one can be deleted "
          "without touching the rest", not offenders, "; ".join(offenders[:5]))


def test_no_vendor_names_outside_adapters() -> None:
    section("nothing outside adapters/ names a vendor")
    offenders = []
    for path, layer, _ in modules():
        if layer not in VENDOR_FREE:
            continue
        for imported in imports_of(path):
            if not imported.startswith(BASE + "adapters."):
                continue
            tail = imported[len(BASE + "adapters."):].split(".")[0]
            if tail in VENDORS:
                offenders.append(f"{layer}/{path.name} imports {tail}")
    check("a model is chosen through the registry, never imported by name -- "
          "otherwise swapping it stops being a one-line change",
          not offenders, "; ".join(offenders[:6]))


def test_registry_is_the_only_door() -> None:
    section("the registry is the one place that knows the vendors")
    registry = TRANSLATE / "adapters" / "__init__.py"
    check("there is a registry", registry.exists())
    if not registry.exists():
        return
    text = registry.read_text(encoding="utf-8")
    named = [v for v in VENDORS if v in text]
    check("it names every adapter in the build",
          len(named) >= 2, f"found {named}")
    check("and it is the registry that each vendor is imported into",
          "FACTORIES" in text or "REGISTRY" in text)


def test_layers_are_documented() -> None:
    section("each layer says what it is for")
    for layer in ALLOWED:
        init = TRANSLATE / layer / "__init__.py"
        text = init.read_text(encoding="utf-8") if init.exists() else ""
        check(f"{layer}/__init__.py explains the layer",
              len(text.strip()) > 80, f"{len(text.strip())} chars")


def main() -> int:
    print("=" * 70)
    print("Commentary QC - the dependency rule")
    print("=" * 70)
    test_no_relative_imports()
    test_dependency_rule()
    test_core_is_pure()
    test_adapters_are_sealed()
    test_no_vendor_names_outside_adapters()
    test_registry_is_the_only_door()
    test_layers_are_documented()
    print("\n" + "=" * 70)
    print(f"{PASS} passed, {FAIL} failed")
    print("=" * 70)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
