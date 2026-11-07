"""Drive a whole broadcast through the Slack notifier, against a fake Slack.

Nobody wants to discover on a live match that alerts arrive in the wrong
thread, or that a burst gets dropped the moment Slack throttles us. This
stands up a local HTTP server that speaks enough of `chat.postMessage` to be
convincing -- including a `rate_limited` reply -- and checks what the notifier
actually sends.

What it proves:
  * one root message per broadcast, carrying commentator and fixture
  * every alert posted as a reply in that broadcast's thread
  * escalations are delivered (they are a new message, not a silent update)
  * info-level rules stay out of Slack
  * throttling is waited out, not dropped

Run:  python tests/test_slack_delivery.py
"""

from __future__ import annotations

import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from commentary_qc.notify import SlackNotifier  # noqa: E402
from commentary_qc.registry import Channel  # noqa: E402

POSTS: list[dict] = []
#: Set to a number to make that many posts answer "rate_limited" first.
THROTTLE = {"remaining": 0}


class FakeSlack(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802 - name fixed by BaseHTTPRequestHandler
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if THROTTLE["remaining"] > 0:
            THROTTLE["remaining"] -= 1
            reply = {"ok": False, "error": "rate_limited", "retry_after": 1}
        else:
            POSTS.append({"auth": self.headers.get("Authorization", ""), **body})
            reply = {"ok": True, "ts": f"17000000.{len(POSTS):06d}"}
        raw = json.dumps(reply).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args) -> None:  # keep the test output clean
        pass


def alert(rule: str, event: str = "FIRE", severity: str = "warning", message: str = "") -> dict:
    return {"event": event, "rule": rule, "severity": severity,
            "message": message or f"{rule} message", "channel": "football-21",
            "t": 0.0, "wall": time.time(), "held_s": 30.0, "evidence": {}}


def main() -> int:
    server = HTTPServer(("127.0.0.1", 0), FakeSlack)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"

    n = SlackNotifier(bot_token="xoxb-test-token", channel_id="C_TEST",
                      min_severity="warning", api_base=base)
    assert n.mode == "bot", n.mode
    ch = Channel("football-21", "Football Ch.21", "Italian")
    claim = {"commentator": "Marco", "fixture": "Genoa v Sudtirol"}

    n.session_started(ch, "sess1", claim)
    n.send(ch, "sess1", alert("too_quiet", message="Too quiet: -41.3 LUFS over the last minute"))
    n.send(ch, "sess1", alert("too_quiet", event="ESCALATE",
                              message="Getting worse - Too quiet: -48.0 LUFS over the last minute"))
    n.send(ch, "sess1", alert("no_crowd", severity="info"))      # too minor for Slack
    n.send(ch, "sess1", alert("too_quiet", event="CLEAR"))
    # A second broadcast must not land in the first one's thread.
    ch2 = Channel("football-31", "Football Ch.31", "Portuguese")
    n.session_started(ch2, "sess2", {"commentator": "Ana", "fixture": "Porto v Benfica"})
    n.send(ch2, "sess2", alert("clipping", severity="critical", message="Distorting"))
    n.session_ended(ch, "sess1")

    deadline = time.time() + 15
    while time.time() < deadline and len(POSTS) < 7:
        time.sleep(0.2)
    n.close()

    ok = True

    def check(label: str, passed: bool, detail: str = "") -> None:
        nonlocal ok
        ok &= passed
        print(f"{'PASS' if passed else 'FAIL'}  {label}{(' - ' + detail) if detail and not passed else ''}")

    texts = [p["text"] for p in POSTS]
    roots = [p for p in POSTS if "thread_ts" not in p]
    replies = [p for p in POSTS if "thread_ts" in p]

    check("token is sent as a bearer header",
          all(p["auth"] == "Bearer xoxb-test-token" for p in POSTS))
    check("one root message per broadcast", len(roots) == 2, f"{len(roots)} roots")
    check("root names the commentator and the fixture",
          any("Marco" in t and "Genoa v Sudtirol" in t for t in texts))
    check("alerts are replies in a thread", len(replies) >= 4, f"{len(replies)} replies")
    check("the two broadcasts use different threads",
          len({p["thread_ts"] for p in replies}) == 2)
    check("escalation is delivered", any("Getting worse" in t for t in texts))
    check("info-level no_crowd stays out of Slack", not any("no_crowd" in t for t in texts))
    check("recovery is announced", any("resolved" in t.lower() for t in texts))

    # Throttling: Slack answers rate_limited twice, the notifier must wait and
    # land the message rather than drop it.
    before = len(POSTS)
    THROTTLE["remaining"] = 2
    n2 = SlackNotifier(bot_token="xoxb-test-token", channel_id="C_TEST", api_base=base)
    n2.session_started(ch, "sess3", claim)
    deadline = time.time() + 20
    while time.time() < deadline and len(POSTS) <= before:
        time.sleep(0.2)
    n2.close()
    check("a throttled message is retried, not dropped", len(POSTS) > before)

    server.shutdown()
    print("\n" + ("ALL PASS" if ok else "FAILURES"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
