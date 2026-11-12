"""Security properties, as tests.

Written after a review on 2026-11-09 that found two things worth keeping
fixed. Both are here so they stay fixed:

* **A key can travel in a URL.** The Gemini live socket documents no header
  form, so the key rides in the query string -- and the WebSocket library
  echoes the URI inside several of its own error messages. A `GEMINI_LIVE_URL`
  mistyped as `http://` produced "http://host/path?key=AIza... isn't a valid
  URI", and that string would have been stored in the run's error column and
  rendered on the page.
* **A channel name reaches a URL builder and a subprocess.** `../../x`,
  `x\\r\\ny` and a 300-character name were all accepted and started a run.

    python tests/test_security.py
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

ROOT = pathlib.Path(__file__).resolve().parents[1]

from commentary_qc.config import SourceConfig  # noqa: E402
from commentary_qc.translate.adapters.gemini import protocol as gp  # noqa: E402
from commentary_qc.translate.adapters.gemini.provider import (  # noqa: E402
    _closed_note,
    _opening_failed,
    _safe,
)
from commentary_qc.translate.core.channels import (  # noqa: E402
    MAX_LENGTH,
    clean_channel,
    is_channel,
)
from commentary_qc.translate.core.models import NotConfigured  # noqa: E402
from commentary_qc.translate.infra.audio_source import (  # noqa: E402
    FFmpegAudioSource,
)
from commentary_qc.translate.settings import TranslateSettings  # noqa: E402

PASS, FAIL = 0, 0
SECRET = "AIzaSyFAKEKEYFORTESTS1234567890abcdefg"


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
def test_channel_names() -> None:
    section("a channel name is an identifier, not free text")

    for good in ("football-41", "football-100", "horses-03", "greyhounds-01",
                 "cricket-1", "a1"):
        check(f"{good} is accepted", clean_channel(good) == good)
    check("case and space are forgiven, because a human typing Football-41 "
          "means the channel", clean_channel("  Football-41 ") == "football-41")

    dangerous = {
        "../../x": "path traversal",
        "..": "path traversal",
        "foo/bar": "a slash, which would change the URL path",
        "x\r\ny": "CRLF, which is header injection wherever it lands",
        "x\ny": "a newline",
        "a b": "whitespace",
        "@evil.example": "something that looks like a host",
        "a..b": "dots",
        "x?y=1": "a query string",
        "x#y": "a fragment",
        "x%2e%2e": "an encoded traversal",
        "-leading": "a leading hyphen",
        "": "nothing at all",
        "A" * (MAX_LENGTH + 1): "longer than any real channel",
    }
    for bad, why in dangerous.items():
        try:
            clean_channel(bad)
            check(f"refused: {why}", False, f"{bad!r} was accepted")
        except NotConfigured:
            check(f"refused: {why}", True)

    check("is_channel answers without raising",
          is_channel("football-41") and not is_channel("../x"))
    try:
        clean_channel("football/41")
        check("the message says what a channel looks like", False)
    except NotConfigured as exc:
        check("the message says what a channel looks like, so a person can "
              "fix it", "football-41" in str(exc), str(exc))


def test_the_boundary_enforces_it() -> None:
    section("validation sits where a channel becomes a URL")

    audio = FFmpegAudioSource(SourceConfig(), "ffmpeg")
    check("a good channel passes through",
          audio.config_for("football-41").channel == "football-41")
    for bad in ("../../etc/passwd", "x\r\ny", "A" * 200, ""):
        try:
            audio.config_for(bad)
            check(f"{bad[:16]!r} refused at the infra boundary", False)
        except NotConfigured:
            check(f"{bad[:16]!r} refused at the infra boundary", True)
    check("so every caller is covered -- the bench, a grab, a live run, and "
          "anything added later that forgets to check", True)


# ======================================================================
def test_the_key_never_escapes() -> None:
    section("the key rides in a URL, so nothing may print it")

    url = gp.url_for(SECRET)
    check("the URL does contain the key, because the socket documents no "
          "header form", SECRET in url)
    check("redacting removes it", SECRET not in gp.redact(url))
    check("and leaves the rest readable",
          "generativelanguage.googleapis.com" in gp.redact(url))

    # the real failure that leaked it
    from websockets.sync.client import connect

    for bad in ("http://host/path", "wss://"):
        try:
            connect(gp.url_for(SECRET, bad), open_timeout=1)
            check(f"{bad} should not connect", False)
        except Exception as exc:  # noqa: BLE001
            check(f"the library DOES echo the URI for {bad!r} -- which is why "
                  f"this test exists", SECRET in str(exc))
            check(f"but our message for {bad!r} does not",
                  SECRET not in str(_opening_failed(exc)),
                  str(_opening_failed(exc))[:90])

    check("the close note is redacted too",
          SECRET not in _closed_note(RuntimeError(f"died at ?key={SECRET}")))
    check("_safe redacts anything handed to it",
          SECRET not in _safe(RuntimeError(f"x?key={SECRET}")))

    check("a key in the middle of a sentence is caught",
          SECRET not in gp.redact(f"oops wss://h/p?key={SECRET}&alt=json failed"))
    check("a quoted URL is caught",
          SECRET not in gp.redact(f'said "wss://h?key={SECRET}" ok'))
    for other in ("access_token", "api_key", "token"):
        check(f"{other}= is redacted as well, since other vendors use it",
              SECRET not in gp.redact(f"x?{other}={SECRET}"))
    check("ordinary text is untouched",
          gp.redact("nothing secret here") == "nothing secret here")


def test_no_endpoint_returns_a_key() -> None:
    section("nothing the API returns carries a credential")

    from commentary_qc.translate import adapters

    settings = TranslateSettings(
        provider="mock", camb_api_key=SECRET, gemini_api_key=SECRET,
    )
    rows = adapters.describe(settings)
    blob = repr(rows)
    check("the provider listing says a key is set, never what it is",
          SECRET not in blob, blob[:120])

    for name in ("camb", "gemini"):
        provider = adapters.build(name, settings)
        for label, payload in (
            ("live_capabilities", provider.live_capabilities()),
            ("capabilities", provider.capabilities()),
            ("healthcheck", provider.healthcheck()),
        ):
            check(f"{name}.{label}() carries no key",
                  SECRET not in repr(payload), repr(payload)[:110])
        provider.close()

    check("and the endpoint it advertises is the bare URL, not the one with "
          "the key in it",
          "key=" not in adapters.build("gemini", settings).live_capabilities()["endpoint"])


# ======================================================================
def test_secrets_are_not_shippable() -> None:
    section("the files holding credentials cannot be committed or copied in")

    for name in (".translate.env", ".slack.env"):
        for listing in (".gitignore", ".dockerignore"):
            text = (ROOT / listing).read_text(encoding="utf-8")
            check(f"{name} is in {listing}", name in text)

    example = ROOT / ".translate.env.example"
    check("the committed example exists", example.exists())
    if example.exists():
        body = example.read_text(encoding="utf-8")
        for line in body.splitlines():
            if "=" in line and not line.strip().startswith("#"):
                key, _, value = line.partition("=")
                check(f"{key.strip()} in the example has no real value",
                      len(value.strip()) < 24, value.strip()[:30])


def test_no_live_credential_in_the_tree() -> None:
    section("no credential pattern anywhere in the tracked source")

    import re

    patterns = {
        "slack bot token": re.compile(r"xoxb-[0-9]{8,}"),
        "slack app token": re.compile(r"xapp-[0-9]-[A-Z0-9]{8,}"),
        "google api key": re.compile(r"AIza[0-9A-Za-z_-]{35}"),
        "openai key": re.compile(r"sk-[A-Za-z0-9]{40,}"),
    }
    hits: list[str] = []
    for path in list((ROOT / "src").rglob("*.py")) + \
            list((ROOT / "tests").rglob("*.py")) + \
            [ROOT / "README.md", ROOT / "ENGINEERING-LOG.md"]:
        if "__pycache__" in path.parts or not path.exists():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for label, rx in patterns.items():
            for found in rx.findall(text):
                # this file's own fake key, and obvious placeholders, are fine
                if "FAKEKEY" in found or "test" in found.lower():
                    continue
                hits.append(f"{path.name}: {label}")
    check("nothing that looks like a live credential is in the source",
          not hits, "; ".join(sorted(set(hits))[:5]))


def test_header_filenames_are_sanitised() -> None:
    section("an id from the URL never lands raw in a response header")

    from commentary_qc.translate.web.api import _safe_name

    check("a normal id survives", _safe_name("a1b2c3d4") == "a1b2c3d4")
    for bad, why in (
        ('a"b', "a quote, which would end the filename early"),
        ("a\r\nX-Injected: yes", "CRLF, which is header injection"),
        ("../../etc/passwd", "traversal"),
        ("a;b", "a separator"),
    ):
        out = _safe_name(bad)
        check(f"stripped: {why}",
              all(c.isalnum() or c in "-_" for c in out), f"{bad!r} -> {out!r}")
    check("an over-long id is cut", len(_safe_name("a" * 500)) <= 64)
    check("and an id reduced to nothing still yields a filename",
          _safe_name('"\r\n/') == "audio")


def main() -> int:
    print("=" * 70)
    print("Commentary QC - security properties")
    print("=" * 70)
    test_channel_names()
    test_the_boundary_enforces_it()
    test_the_key_never_escapes()
    test_no_endpoint_returns_a_key()
    test_secrets_are_not_shippable()
    test_no_live_credential_in_the_tree()
    test_header_filenames_are_sanitised()
    print("\n" + "=" * 70)
    print(f"{PASS} passed, {FAIL} failed")
    print("=" * 70)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
