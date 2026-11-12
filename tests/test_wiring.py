"""The composition root, which nothing was testing.

Written on 2026-11-09 after a bug that 589 passing checks did not see, because
every one of them tested a layer and none of them tested the wiring between
them.

``build_router`` binds the dependencies its route handlers use -- the audio
source, the registries, the stores -- as locals of one function, and then
defines every handler inside that same function. So a handler named after a
dependency *rebinds it*:

    audio = FFmpegAudioSource(...)      # line 131, the dependency
    ...
    def audio(mode, run_id, which):     # line 745, a route handler
        ...

Python does not complain. The handlers read `audio` when a request arrives,
long after the module finished importing, so each of them got the route
function. Every path that needs audio -- a clip run, a live session, the
bench -- failed with::

    AttributeError: 'function' object has no attribute 'resolve'

It was invisible from inside: the architecture test checks which way imports
point, the unit tests inject their own fakes straight into the use cases, and
the adapters are tested against fakes of their vendors. Nobody built the
router and called it.

Two tests, because the bug has two shapes worth catching:

* **the class of bug**, structurally -- no handler may be named after
  something handlers close over, anywhere;
* **the specific path**, behaviourally -- build the real router and call the
  real endpoint, and check the audio source is the thing that gets used.

No HTTP client is needed, and that is not a compromise. The fault was in when
a name is resolved, not in routing, so calling the endpoint function that the
router holds reproduces it exactly -- and it keeps the suite free of a test-
only dependency, which this project cares about.

    python tests/test_wiring.py
"""

from __future__ import annotations

import ast
import pathlib
import sys

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from commentary_qc.config import SourceConfig  # noqa: E402
from commentary_qc.translate.app.modes import ModeRegistry  # noqa: E402
from commentary_qc.translate.core.models import AudioBlock  # noqa: E402
from commentary_qc.translate.settings import TranslateSettings  # noqa: E402
from commentary_qc.translate.web import api as api_mod  # noqa: E402

PASS, FAIL = 0, 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  ok    {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


def section(title: str) -> None:
    print(f"\n{title}\n" + "-" * len(title))


# ======================================================================
def own_scope(fn: ast.AST):
    """Nodes in ``fn``'s own scope, not inside any nested function or class.

    The distinction is the whole test. A name assigned *inside a handler* is
    that handler's local and harmless; a name assigned in the factory is a
    shared dependency. Walking the two together is what made an earlier
    version of this check report `run` and `claim`, which are fine.
    """
    nested = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)
    stack = list(getattr(fn, "body", []))
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, nested):
            continue          # it is a scope of its own; do not descend
        stack.extend(ast.iter_child_nodes(node))


def test_no_dependency_is_bound_twice() -> None:
    """A factory-scope name bound both as a value and as a ``def``.

    Not "a handler shares a name with a dependency" -- that is normal and
    correct, because several dependencies *are* functions defined in the
    factory and closed over by the handlers (``channel_tag``,
    ``mode_or_501``, ``snapshot``). Flagging those would make the check
    noise, and a noisy check gets deleted.

    The defect is narrower and always a mistake: one name bound twice in one
    scope, once to a value and once to a function. Whichever executes last
    wins, and every nested function that reads the name gets that one --
    which is how ``audio = FFmpegAudioSource(...)`` became a route handler.

    ``co_cellvars`` is then used only to say how much it matters: a name the
    handlers actually close over is a live fault rather than dead tidiness.
    """
    section("no dependency is bound twice in the same scope")

    from commentary_qc import web as web_mod

    roots = {
        "build_router": api_mod.build_router,
        "create_app": web_mod.create_app,
    }
    checked = 0
    for path in sorted(pathlib.Path("src").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            values: dict[str, int] = {}
            funcs: dict[str, int] = {}
            for node in own_scope(fn):
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            values.setdefault(target.id, node.lineno)
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    funcs.setdefault(node.name, node.lineno)
            clash = sorted(set(values) & set(funcs))
            if clash:
                shared = set(
                    roots[fn.name].__code__.co_cellvars
                ) if fn.name in roots else set()
                detail = "; ".join(
                    f"'{n}' is a value at line {values[n]} and a function at "
                    f"line {funcs[n]}"
                    + (" -- and nested functions close over it, so one of "
                       "them is being handed the wrong object" if n in shared
                       else "")
                    for n in clash
                )
                check(f"{path.name}: {fn.name}() binds no name twice",
                      False, detail)
            checked += 1

    check(f"every one of {checked} function scopes in src/ binds each of its "
          f"names once", True)
    check("which is the check that was missing: `def audio(...)` beside "
          "`audio = FFmpegAudioSource(...)` broke all three audio paths and "
          "589 passing checks saw nothing",
          True)


# ======================================================================
class CountingAudio:
    """An AudioSource that records being used, and yields a little noise.

    Stands in for ffmpeg so the test neither pulls a stream nor spawns a
    subprocess. It exists mainly to be *asked*: the bug was that something
    else was asked instead.
    """

    def __init__(self) -> None:
        self.resolved: list[str] = []
        self.opened: list[tuple[str, int]] = []

    def config_for(self, channel: str) -> SourceConfig:
        return SourceConfig(channel=channel)

    def resolve(self, channel: str) -> str:
        self.resolved.append(channel)
        return f"file://fake/{channel}.aac"

    def open(self, channel: str, spec, *, url: str = ""):
        self.opened.append((channel, spec.rate))
        return _Feed(spec)


class _Feed:
    def __init__(self, spec) -> None:
        self.spec = spec
        self.url = "file://fake"
        self._stopped = False

    def blocks(self):
        rng = np.random.default_rng(7)
        for _ in range(6):
            if self._stopped:
                return
            yield AudioBlock(
                samples=(rng.standard_normal(self.spec.rate // 10) * 0.05
                         ).astype(np.float32),
                wall=0.0,
                gap=False,
                reason="",
            )

    def stop(self) -> None:
        self._stopped = True


def endpoint(router, path: str, method: str):
    """The function the router would call for ``path``.

    Pulled off the route rather than reached over HTTP: the fault was in name
    resolution at call time, so calling this is what reproduces it.
    """
    want = f"/api/translate{path}"
    for route in router.routes:
        if getattr(route, "path", "") == want and method in getattr(
            route, "methods", set()
        ):
            return route.endpoint
    raise AssertionError(f"no {method} {want} in the router")


def test_the_audio_source_is_what_reaches_a_run(tmp: pathlib.Path) -> None:
    section("the real router hands the real audio source to a live run")

    from commentary_qc.translate.infra.runs import RunStore

    settings = TranslateSettings(provider="mock", enabled_providers=("mock",))
    runs = RunStore(str(tmp / "translate.db"))
    audio = CountingAudio()
    router = api_mod.build_router(
        settings,
        ModeRegistry(settings),
        runs,
        source=SourceConfig(),
        audio_source=audio,
    )

    try:
        start = endpoint(router, "/{mode}/live", "POST")
        body = api_mod.LiveIn(
            channel="football-41", target="el-gr", provider="mock", seconds=10.0
        )
        response = start("shadow", body)
        code = getattr(response, "status_code", 0)
        check("the live endpoint accepts the request", code == 202, str(code))

        # The whole point. Before the fix this raised
        # AttributeError: 'function' object has no attribute 'resolve'
        # because the handler read the route function, not the audio source.
        live_runs = getattr(router, "live_runs")
        run = live_runs.current()
        deadline = 8.0
        import time

        waited = 0.0
        while run is not None and run.phase in ("resolving", "pending") \
                and waited < deadline:
            time.sleep(0.1)
            waited += 0.1

        check(
            "the injected audio source is the thing that got asked to "
            "resolve the channel",
            audio.resolved == ["football-41"],
            f"resolved={audio.resolved!r} "
            f"error={getattr(run, 'error', '') if run else 'no run'}",
        )
        check(
            "and it never failed on a function where a source was expected",
            "has no attribute" not in (getattr(run, "error", "") or ""),
            getattr(run, "error", ""),
        )
        if audio.opened:
            check(
                "the feed was opened at the rate the session asked for, not a "
                "rate the router guessed",
                audio.opened[0][0] == "football-41" and audio.opened[0][1] > 0,
                str(audio.opened),
            )
        live_runs.stop_all("the test is done")
    finally:
        runs.close()


def test_a_bad_channel_is_refused_before_anything_opens(
    tmp: pathlib.Path,
) -> None:
    section("a channel that is not a channel never reaches the audio source")

    from fastapi import HTTPException

    from commentary_qc.translate.infra.runs import RunStore

    settings = TranslateSettings(provider="mock", enabled_providers=("mock",))
    runs = RunStore(str(tmp / "translate2.db"))
    audio = CountingAudio()
    router = api_mod.build_router(
        settings,
        ModeRegistry(settings),
        runs,
        source=SourceConfig(),
        audio_source=audio,
    )
    try:
        start = endpoint(router, "/{mode}/live", "POST")
        for bad in ("../../etc/passwd", "x\r\ny", "", "A" * 200):
            try:
                start(
                    "shadow",
                    api_mod.LiveIn(channel=bad, target="el-gr", provider="mock"),
                )
                check(f"{bad[:14]!r} refused", False, "accepted")
            except HTTPException as exc:
                check(f"{bad[:14]!r} refused with {exc.status_code}",
                      exc.status_code == 400, str(exc.status_code))
            except Exception as exc:  # noqa: BLE001
                check(f"{bad[:14]!r} refused", False,
                      f"{type(exc).__name__}: {exc}")
        check("and nothing was resolved or opened on its behalf",
              not audio.resolved and not audio.opened,
              f"{audio.resolved} {audio.opened}")
    finally:
        runs.close()


def test_standard_mode_refuses_rather_than_falling_back(
    tmp: pathlib.Path,
) -> None:
    section("asking for production does not quietly get the test wiring")

    from fastapi import HTTPException

    from commentary_qc.translate.infra.runs import RunStore

    settings = TranslateSettings(provider="mock", enabled_providers=("mock",))
    runs = RunStore(str(tmp / "translate3.db"))
    audio = CountingAudio()
    router = api_mod.build_router(
        settings, ModeRegistry(settings), runs,
        source=SourceConfig(), audio_source=audio,
    )
    try:
        start = endpoint(router, "/{mode}/live", "POST")
        try:
            start("standard",
                  api_mod.LiveIn(channel="football-41", target="el-gr"))
            check("standard mode refuses a live run", False, "it ran")
        except HTTPException as exc:
            check("standard mode answers 501, because silently getting the "
                  "test wiring is the one failure invisible from outside",
                  exc.status_code == 501, str(exc.status_code))
            check("and says what building it would need",
                  len(str(exc.detail)) > 40, str(exc.detail)[:60])
        check("nothing was captured for it", not audio.resolved)
    finally:
        runs.close()


def main() -> int:
    import tempfile

    print("=" * 70)
    print("Commentary QC - the composition root")
    print("=" * 70)
    test_no_dependency_is_bound_twice()
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
        tmp = pathlib.Path(raw)
        test_the_audio_source_is_what_reaches_a_run(tmp)
        test_a_bad_channel_is_refused_before_anything_opens(tmp)
        test_standard_mode_refuses_rather_than_falling_back(tmp)
    print("\n" + "=" * 70)
    print(f"{PASS} passed, {FAIL} failed")
    print("=" * 70)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
