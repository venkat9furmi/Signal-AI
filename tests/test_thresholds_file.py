"""`thresholds.json` is how the next person retunes the engine.

It is documentation that runs, which is the only kind that stays true. Three
things have to hold, and each has gone wrong in some project at some point:

1. The file the containers mount actually loads.
2. Its `_example` block really does list the current defaults. A worked
   example that has drifted out of date is worse than none -- somebody copies
   a line out of it believing it is what is running.
3. A typo is refused rather than silently ignored. A dropped threshold is an
   outage nobody can see.

    python tests/test_thresholds_file.py
"""

from __future__ import annotations

import dataclasses
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from commentary_qc.config import Settings, Thresholds  # noqa: E402

PASS, FAIL = 0, 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  ok    {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}\n          {detail}")


FILE = ROOT / "thresholds.json"
print(f"\n{FILE.name}\n" + "-" * len(FILE.name))

check("the file the containers mount exists", FILE.exists(), str(FILE))
raw = json.loads(FILE.read_text(encoding="utf-8"))

check("it loads through the same path the services use",
      isinstance(Settings.load(str(FILE)).thresholds, Thresholds))

check("it ships with no overrides, so the defaults are what runs",
      raw.get("thresholds") == {},
      f"it overrides {sorted(raw.get('thresholds', {}))} -- intended?")

check("it explains itself to whoever opens it",
      bool(raw.get("_readme")) and "docker compose restart" in
      " ".join(raw["_readme"]))

# -- the worked example must not drift ------------------------------------
example = {
    k: v for k, v in raw.get("_example", {}).items()
    if not k.startswith("_") and k != "comment"
}
defaults = Thresholds()
missing = [f.name for f in dataclasses.fields(defaults)
           if f.name not in example and f.name != "disabled_rules"]
check("every threshold is listed in the example",
      not missing, f"missing: {missing}")

wrong = []
for name, value in example.items():
    if not hasattr(defaults, name):
        wrong.append(f"{name} is not a threshold at all")
    elif getattr(defaults, name) != value:
        wrong.append(f"{name}: example says {value!r}, default is "
                     f"{getattr(defaults, name)!r}")
check("and every listed value matches the real default",
      not wrong, "; ".join(wrong))

check("so the example is loadable as-is",
      isinstance(Settings.from_mapping({"thresholds": example}).thresholds,
                 Thresholds))

# -- failing loudly -------------------------------------------------------
print("\na bad edit must not run quietly\n" + "-" * 30)
try:
    Settings.from_mapping({"thresholds": {"too_quite_lufs": -38.0}})
    check("a misspelt threshold is refused", False, "it was accepted")
except ValueError as exc:
    check("a misspelt threshold is refused, naming it",
          "too_quite_lufs" in str(exc), str(exc))

try:
    Settings.from_mapping({"thresholds": {"too_quiet_clear_lufs": -45.0}})
    check("a clear level below its fire level is refused", False,
          "it was accepted, and would clear an alert the moment it fired")
except ValueError as exc:
    check("a clear level below its fire level is refused", True)

applied = Settings.from_mapping(
    {"thresholds": {"too_quiet_lufs": -38.0}}
).thresholds
check("an override applies", applied.too_quiet_lufs == -38.0)
check("and a value not listed keeps its default",
      applied.too_loud_lufs == defaults.too_loud_lufs)
check("unknown top-level keys are ignored, so _readme is safe",
      Settings.from_mapping({"_readme": "x", "thresholds": {}}) is not None)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
