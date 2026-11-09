"""Alert delivery to Slack.

One message per broadcast, with every alert for that broadcast posted as a
reply underneath it. With ~11 channels running, a flat feed becomes unreadable
within minutes; threading keeps the channel showing one tidy line per match and
puts the detail where someone can find it afterwards.

Delivery is best-effort and never blocks monitoring: sending happens on its own
thread, and a Slack outage degrades to log lines rather than stalling capture.
The store already holds everything, so nothing is lost if a post fails.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
import urllib.error
import urllib.request

from commentary_qc.registry import Channel

log = logging.getLogger(__name__)

#: Rules quiet enough that a message would cost more attention than it saves.
#: They are still recorded and still show in the app.
_QUIET_RULES = frozenset({"no_crowd"})

_EMOJI = {
    "critical": ":red_circle:",
    "warning": ":large_orange_circle:",
    "info": ":large_blue_circle:",
}


def _who(claim: dict | None) -> str:
    """' · Marco · Porto v Man City' from a claim, or nothing."""
    if not claim:
        return ""
    parts = [p for p in (claim.get("commentator"), claim.get("fixture")) if p]
    return "".join(f" · {p}" for p in parts)


class SlackNotifier:
    """Posts alerts to Slack, one thread per broadcast.

    ``webhook_url`` posts via an incoming webhook (simplest to set up, but
    cannot thread). ``bot_token`` + ``channel_id`` uses chat.postMessage, which
    can thread and is what you want in practice. With neither, it runs in dry
    mode and logs what it would have sent -- useful for testing before anyone
    has created the Slack app.
    """

    def __init__(
        self,
        webhook_url: str | None = None,
        bot_token: str | None = None,
        channel_id: str | None = None,
        min_severity: str = "warning",
        api_base: str | None = None,
    ) -> None:
        #: Where the Slack API lives. Configurable so the whole delivery path
        #: -- threading, retries, throttling -- can be exercised against a
        #: local stand-in before anyone trusts it with a real workspace.
        self.api_base = (api_base or os.environ.get("SLACK_API_BASE")
                         or "https://slack.com/api").rstrip("/")
        self.webhook_url = webhook_url
        self.bot_token = bot_token
        self.channel_id = channel_id
        self.dry_run = not (webhook_url or (bot_token and channel_id))
        order = {"info": 0, "warning": 1, "critical": 2}
        self._min = order.get(min_severity, 1)
        self._threads: dict[str, str] = {}  # session -> slack thread ts
        self._q: queue.Queue = queue.Queue(maxsize=1000)
        self._stop = threading.Event()
        self._worker = threading.Thread(target=self._run, name="slack", daemon=True)
        self._worker.start()
        if self.dry_run:
            log.warning("Slack not configured - running in dry mode, alerts will be logged only")

    @property
    def mode(self) -> str:
        """How alerts leave this process, for the status page."""
        if self.dry_run:
            return "dry-run"
        return "bot" if self.bot_token and self.channel_id else "webhook"

    def close(self) -> None:
        self._stop.set()
        self._q.put(None)
        self._worker.join(timeout=10)

    # -- public API -------------------------------------------------------
    def session_started(self, channel: Channel, session: str, claim: dict | None = None) -> None:
        lang = f" · {channel.language}" if channel.language else ""
        self._enqueue(
            session, f":studio_microphone: *{channel.label}* on air{lang}{_who(claim)}", root=True
        )

    def claimed(self, channel: Channel, session: str, claim: dict) -> None:
        """A commentator picked a channel that was already being watched.

        This opens the thread (root=True), it does not reply into one. Every
        broadcasting channel is already monitored before anyone claims it, so
        no root message exists yet for that session; posting this as a reply
        left the alerts that followed as loose top-level messages.
        """
        lang = f" · {channel.language}" if channel.language else ""
        self._enqueue(
            session,
            f":studio_microphone: *{channel.label}* claimed{lang}{_who(claim)}",
            root=True,
        )

    def session_ended(self, channel: Channel, session: str) -> None:
        self._enqueue(session, f":black_square_for_stop: *{channel.label}* off air")
        self._threads.pop(session, None)

    def send(self, channel: Channel, session: str, alert: dict) -> None:
        order = {"info": 0, "warning": 1, "critical": 2}
        if order.get(alert["severity"], 1) < self._min or alert["rule"] in _QUIET_RULES:
            return
        if alert["event"] in ("FIRE", "ESCALATE"):
            icon = _EMOJI.get(alert["severity"], ":warning:")
            text = f"{icon} {alert['message']}"
        else:
            # Clears matter -- they tell whoever is watching the problem is
            # over, so nobody chases a fault that already fixed itself.
            text = f":white_check_mark: {alert['rule'].replace('_', ' ')} resolved"
        self._enqueue(session, text)

    # -- internals --------------------------------------------------------
    def _enqueue(self, session: str, text: str, root: bool = False) -> None:
        try:
            self._q.put_nowait((session, text, root))
        except queue.Full:
            log.warning("slack queue full, dropping: %s", text)

    def _run(self) -> None:
        while not self._stop.is_set():
            item = self._q.get()
            if item is None:
                break
            session, text, root = item
            try:
                self._post(session, text, root)
            except Exception:  # noqa: BLE001 - delivery is best effort
                log.exception("slack post failed: %s", text)

    def _post(self, session: str, text: str, root: bool) -> None:
        if self.dry_run:
            # DEBUG, not INFO: the engine already logs every alert. Logging it
            # again here put each one in the file twice.
            log.debug("[slack dry-run] %s", text)
            return

        if self.bot_token and self.channel_id:
            payload = {"channel": self.channel_id, "text": text}
            if not root and session in self._threads:
                payload["thread_ts"] = self._threads[session]
            body = self._call(
                f"{self.api_base}/chat.postMessage",
                payload,
                {"Authorization": f"Bearer {self.bot_token}"},
            )
            if root and body and body.get("ok"):
                self._threads[session] = body["ts"]
            elif body and not body.get("ok"):
                log.warning("slack error: %s", body.get("error"))
        else:
            # Webhooks cannot thread, so the channel label is inlined instead.
            self._call(self.webhook_url, {"text": text}, {})

    def _call(self, url: str, payload: dict, headers: dict) -> dict | None:
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            url, data=data,
            headers={"Content-Type": "application/json; charset=utf-8", **headers},
        )
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    raw = resp.read().decode("utf-8", "replace")
                try:
                    body = json.loads(raw)
                except json.JSONDecodeError:
                    return {"ok": raw.strip() == "ok"}
                # Slack throttles at roughly one message per second per
                # channel and answers `rate_limited` rather than failing. The
                # old code logged that and dropped the alert, which is the one
                # moment you least want to lose one: a burst means several
                # channels went wrong at once.
                if not body.get("ok") and body.get("error") == "rate_limited":
                    wait = float(body.get("retry_after") or 1)
                    log.info("slack throttled, waiting %.0fs", wait)
                    time.sleep(min(wait, 30))
                    continue
                return body
            except urllib.error.HTTPError as exc:
                if exc.code == 429:
                    wait = float(exc.headers.get("Retry-After") or 1)
                    log.info("slack throttled (429), waiting %.0fs", wait)
                    time.sleep(min(wait, 30))
                    continue
                log.warning("slack HTTP %s", exc.code)
                return None
            except (urllib.error.URLError, OSError) as exc:
                if attempt == 3:
                    log.warning("slack unreachable: %s", exc)
                    return None
                time.sleep(2 ** attempt)
        log.warning("slack gave up after throttling")
        return None
