"""Rule engine: metrics windows in, FIRE/CLEAR alert events out.

Rules are deliberately plain predicates over one :class:`MetricsWindow`. All
temporal behaviour lives in the hysteresis layer, so a rule stays a single
readable condition and the noise control is uniform across every detector.

Hysteresis is the first of the two noise layers named in the architecture: a
condition must hold continuously for ``attack_s`` before it FIREs, and must be
false continuously for ``release_s`` before it CLEARs. Without it, a metric
sitting on a threshold produces an alert per second. The second layer
(cross-stream dedup and rate limiting) belongs to alert delivery, not here.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Iterable

from commentary_qc.config import WINDOW_SECONDS, Thresholds
from commentary_qc.metrics import MetricsWindow

#: A rule returns True (fault present), False (fault absent), or None when the
#: window carries no evidence either way -- e.g. the crowd balance cannot be
#: judged in a window where nobody is speaking. None freezes the hysteresis
#: timers instead of resetting them, which matters more than it sounds: with a
#: plain boolean, every pause for breath resets the attack timer, so a rule
#: needing 8 s of continuous evidence can never fire on real speech.
Predicate = Callable[[MetricsWindow, Thresholds], "bool | None"]


@dataclass(slots=True)
class Rule:
    id: str
    severity: str
    describe: Callable[[MetricsWindow, Thresholds], str]
    predicate: Predicate
    #: Fields copied into the alert payload so a responder sees the evidence
    #: without joining against the metrics table.
    evidence: tuple[str, ...]
    #: Optional thermostat: while the alert is up, this decides whether the
    #: fault still holds, instead of ``predicate``. Lets an alert fire at one
    #: level and clear only past another, so a level sitting on the line
    #: produces one alert rather than a stream of fire/clear pairs.
    hold: Predicate | None = None


@dataclass(slots=True)
class Alert:
    """A state transition on one rule for one stream."""

    event: str  # "FIRE" | "CLEAR"
    rule: str
    severity: str
    channel: str
    message: str
    #: Media time of the transition, seconds since connect.
    t: float
    wall: float
    #: How long the condition had held when it fired.
    held_s: float
    evidence: dict

    def to_dict(self) -> dict:
        return {
            "event": self.event,
            "rule": self.rule,
            "severity": self.severity,
            "channel": self.channel,
            "message": self.message,
            "t": round(self.t, 2),
            "wall": round(self.wall, 3),
            "held_s": round(self.held_s, 2),
            "evidence": self.evidence,
        }


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------

def _speech_present(m: MetricsWindow, th: Thresholds) -> bool:
    return m.speech_ratio >= th.speech_ratio_present


def clip_playing(m: MetricsWindow, th: Thresholds) -> bool:
    """Is this audio a playout clip rather than a person?

    Two independent tests, either of which is enough:
      - the repeat test: the loudness pattern of the last ~3 minutes lines up
        with itself a fixed interval later, which a playout loop does and a
        person never does. Catches loops with crowd in them.
      - the dry-announcement test: continuous speech, no crowd, flat level, all
        three together, so a thin crowd at a small ground or a studio feed
        cannot trip it on the crowd measure alone.

    With one veto: audio below the quiet line carrying essentially no speech is
    a dead-ish input, not a clip. Near-silence correlates with itself, so the
    repeat test drifts over its threshold there (German Ch.44, 2026-09-18:
    -41.4 LUFS, no speech, score 0.67 against 0.79-1.00 for real clips), and
    calling it a clip would mask the "too quiet" alert somebody needs.

    The same answer drives the rule and the alert masking, so the two can never
    disagree about what is playing.
    """
    if (m.lufs_minute < th.too_quiet_lufs
            and m.speech_ratio_med < th.filler_quiet_speech_floor):
        return False
    return bool(
        m.loop_score >= th.filler_loop_score
        or (
            m.speech_ratio_med > th.filler_speech_ratio
            and m.crowd_lufs_med < th.filler_crowd_lufs
            and m.level_sd < th.filler_level_sd
        )
    )


RULES: tuple[Rule, ...] = (
    Rule(
        id="silence",
        severity="critical",
        predicate=lambda m, th: m.lufs_short < th.dead_air_lufs,
        describe=lambda m, th: f"Dead air: {m.lufs_short:.1f} LUFS, effectively silent",
        evidence=("lufs", "lufs_short", "sample_peak_dbfs"),
    ),
    Rule(
        id="clipping",
        severity="critical",
        # A flat-topped waveform already in the source: audible distortion.
        # Kept apart from true-peak overs, which used to share this rule and
        # made a -0.8 dBTP peak read "DISTORTING" on the commentator's page.
        predicate=lambda m, th: m.clip_run >= th.clip_run_samples,
        describe=lambda m, th: (
            f"Distorting: {m.clip_run} samples at full scale "
            f"(true peak {m.true_peak_dbtp:+.1f} dBTP)"
        ),
        evidence=("clip_run", "true_peak_dbtp", "sample_peak_dbfs", "lufs"),
    ),
    Rule(
        id="peak_over",
        severity="warning",
        # Peaks above the EBU R128 -1 dBTP ceiling without a flat top yet:
        # headroom is gone and a downstream encoder may clip, but nothing is
        # audibly broken. A window that is actually clipping carries no
        # evidence here -- the clipping rule owns it -- so one fault never
        # raises two alerts.
        predicate=lambda m, th: (
            None if m.clip_run >= th.clip_run_samples
            else m.true_peak_dbtp > th.true_peak_dbtp
        ),
        describe=lambda m, th: (
            f"Peaks too hot: true peak {m.true_peak_dbtp:+.1f} dBTP "
            f"(limit {th.true_peak_dbtp:+.0f})"
        ),
        evidence=("true_peak_dbtp", "sample_peak_dbfs", "lufs"),
    ),
    Rule(
        id="too_loud",
        severity="warning",
        predicate=lambda m, th: m.lufs_minute > th.too_loud_lufs,
        hold=lambda m, th: m.lufs_minute > th.too_loud_clear_lufs,
        describe=lambda m, th: f"Too loud: {m.lufs_minute:.1f} LUFS over the last minute",
        evidence=("lufs_minute", "lufs_short", "true_peak_dbtp"),
    ),
    Rule(
        id="too_quiet",
        severity="warning",
        # Above the dead-air floor, otherwise silence owns the window and we
        # would raise two alerts for one fault.
        predicate=lambda m, th: th.dead_air_lufs <= m.lufs_minute < th.too_quiet_lufs,
        # Fires below -40 but stays up until the level is back above -37.
        hold=lambda m, th: th.dead_air_lufs <= m.lufs_minute < th.too_quiet_clear_lufs,
        describe=lambda m, th: f"Too quiet: {m.lufs_minute:.1f} LUFS over the last minute",
        evidence=("lufs_minute", "lufs_short", "speech_ratio"),
    ),
    Rule(
        id="crowd_dominating",
        severity="warning",
        # Only judged while the commentator is actually talking and the crowd
        # floor was genuinely measured in this window's gaps; otherwise the
        # window carries no evidence and the timers hold.
        predicate=lambda m, th: (
            None
            if (m.speech_ratio < th.min_speech_ratio_for_balance or not m.crowd_measured)
            else m.voice_to_crowd_med < th.crowd_dominating_db
        ),
        describe=lambda m, th: (
            f"Commentator buried in crowd: voice only {m.voice_to_crowd_med:.1f} dB "
            f"over crowd ({m.crowd_lufs:.1f} LUFS)"
        ),
        evidence=("voice_to_crowd_med", "voice_to_crowd_db", "voice_lufs", "crowd_lufs",
                  "speech_ratio"),
    ),
    Rule(
        id="voice_dominates",
        severity="warning",
        # The other end of MMC's 4-14 dB band: the commentator is mixed too far
        # forward and the stadium atmosphere is too thin behind them. Judged
        # under the same conditions as crowd_dominating -- only while someone is
        # talking and the crowd floor was actually measured.
        predicate=lambda m, th: (
            None
            if (m.speech_ratio < th.min_speech_ratio_for_balance or not m.crowd_measured)
            else m.voice_to_crowd_med > th.voice_dominates_db
        ),
        describe=lambda m, th: (
            f"Atmosphere too thin: voice {m.voice_to_crowd_med:.1f} dB over crowd "
            f"(crowd at {m.crowd_lufs:.1f} LUFS)"
        ),
        evidence=("voice_to_crowd_med", "voice_to_crowd_db", "voice_lufs", "crowd_lufs",
                  "speech_ratio"),
    ),
    Rule(
        id="filler_playing",
        severity="critical",
        # The channel is technically healthy but carrying a holding loop rather
        # than commentary. Observed live on 2026-09-07: Ch.22 played filler
        # through a scheduled Serie A match while every quality metric looked
        # fine. To a bookmaker that is a total failure; to every other rule
        # here it is a perfectly good feed.
        #
        predicate=clip_playing,
        describe=lambda m, th: (
            f"Filler playing: the audio repeats itself every {m.loop_lag_s:.0f} s "
            f"(match {m.loop_score:.2f})"
            if m.loop_score >= th.filler_loop_score
            else f"Filler playing: continuous speech ({m.speech_ratio_med:.0%}), no crowd "
                 f"({m.crowd_lufs_med:.0f} LUFS), flat level (sd {m.level_sd:.2f})"
        ),
        evidence=("loop_score", "loop_lag_s", "speech_ratio_med", "crowd_lufs_med",
                  "level_sd", "lufs_short"),
    ),
    Rule(
        id="no_crowd",
        severity="info",
        # Atmosphere missing: the gaps are near-silent while the feed itself is
        # alive, i.e. a dry commentary-only mix rather than a dead line. A
        # window with no measured gaps, or a dead feed, says nothing about the
        # crowd bed either way.
        predicate=lambda m, th: (
            None
            if (not m.crowd_measured or m.lufs_short <= th.dead_air_lufs)
            else m.crowd_lufs < th.no_crowd_lufs
        ),
        describe=lambda m, th: f"No crowd atmosphere: bed at {m.crowd_lufs:.1f} LUFS",
        evidence=("crowd_lufs", "lufs_short", "speech_ratio"),
    ),
)


#: Rules that say nothing useful while a holding clip is playing. The
#: commentator is not on air, so level, peak and balance faults belong to a
#: recorded file nobody can fix mid-match -- and with a dozen channels sitting
#: on pre-match clips they were the loudest thing in the alert feed. Dead air
#: and a stream that stops are NOT masked: those matter whatever is playing.
_CLIP_MASKED = frozenset({
    "too_loud", "too_quiet", "peak_over", "clipping",
    "crowd_dominating", "voice_dominates", "no_crowd",
})


#: How many identical fault messages prove a recording. Two could be a
#: steady signal sitting on a threshold; three with the same sample count and
#: the same true peak to one decimal place cannot be a person -- live speech
#: does not peak identically twice.
REPEAT_EVIDENCE = 3

#: And within how long. A playout clip comes round on a cycle of tens of
#: seconds, so the repeats are close together. The same message an hour apart
#: is a channel with a persistent fault, which is a real alert.
REPEAT_WINDOW_S = 420.0

#: How long the verdict stands once latched. Long enough to outlast a clip
#: between halves, short enough that a channel which genuinely came back and
#: then developed the same fault is heard again.
REPEAT_HOLD_S = 600.0


#: How bad a fault is, on a dB-ish scale where bigger is worse. A rule with an
#: entry here can escalate: while it is up, another `escalate_step_db` of
#: deterioration raises a fresh alert instead of sitting silent. Rules with no
#: sensible scale (a clip is playing, there is no crowd) are absent.
#:
#: **`silence` is deliberately absent.** It escalated 73 times on 293 fires in
#: the two months to 2026-11-12 -- a quarter of them -- because a dead channel
#: drifting from -50 to -56 LUFS trips a 6 dB step. It is already dead; there
#: is no worse, and nobody can act on the difference.
_ESCALATION_SCALE = {
    "too_quiet": lambda m: -m.lufs_minute,
    "too_loud": lambda m: m.lufs_minute,
    "clipping": lambda m: m.true_peak_dbtp,
    "peak_over": lambda m: m.true_peak_dbtp,
    "crowd_dominating": lambda m: -m.voice_to_crowd_med,
    "voice_dominates": lambda m: m.voice_to_crowd_med,
}


@dataclass(slots=True)
class _RuleState:
    active: bool = False
    held_s: float = 0.0
    clear_s: float = 0.0
    fired_at: float = 0.0
    #: How bad it was when we last said something, for escalation.
    mark: float = 0.0


class AlertEngine:
    """Applies :data:`RULES` with per-rule hysteresis."""

    def __init__(self, thresholds: Thresholds, channel: str, rules: Iterable[Rule] = RULES) -> None:
        self.th = thresholds
        self.channel = channel
        disabled = set(thresholds.disabled_rules)
        self.rules = tuple(r for r in rules if r.id not in disabled)
        self._state: dict[str, _RuleState] = {r.id: _RuleState() for r in self.rules}
        #: rule id -> (message, [media times it was said]). Only the rules
        #: that a clip can fake are watched; a repeated "dead air" is a
        #: genuinely repeated fault and must still be reported.
        self._said: dict[str, tuple[str, list[float]]] = {}
        #: Media time until which a repeat has convinced us this is playout.
        self._repeat_until = 0.0
        self._repeat_reason = ""
        #: When the current capture gap began, and whether it has lasted
        #: long enough to have been reported. See on_gap().
        self._gap_since = 0.0
        self._gap_fired = False
        self._gap_reason = ""

    def active_alerts(self) -> list[str]:
        return sorted(rid for rid, st in self._state.items() if st.active)

    def _step(self, rule_id: str) -> float:
        """How much worse a firing fault must get before it is said again.

        Per rule, falling back to the global step. The balance rules live on
        a far wider scale than the loudness ones, so one number for both made
        "getting worse" mean something different depending on which rule said
        it. See ``Thresholds.escalate_step_by_rule``.
        """
        return (getattr(self.th, "escalate_step_by_rule", None) or {}).get(
            rule_id, self.th.escalate_step_db
        )

    def on_gap(self, wall: float, reason: str) -> list[Alert]:
        """Handle a capture gap. Says nothing until the gap has lasted.

        A stream that is not delivering is not silent -- it is absent. Firing
        silence here would misdirect whoever picks up the alert, so instead we
        clear the audio-quality rules (their inputs are stale) and report the
        transport fault itself.

        **But not immediately.** This used to fire on the first gap block,
        which is a stall of ten seconds or a single failed reconnect. Measured
        over 2,010 of them: the median outage lasted **9 seconds** and 53%
        were over inside 15 -- fixed before anyone could read the message --
        while 556 were our own container failing to resolve the vendor's
        hostname, which is not a broadcast fault at all. So it waits
        ``attack_s["stream_down"]`` before saying anything, like every other
        rule.

        The other rules are cleared at the same moment rather than on the
        first gap block, for the same reason: a nine-second blip should not
        clear a "too quiet" that is genuinely firing, only to re-fire it a
        moment later.

        Called once per gap block, and the source keeps producing those for
        as long as the outage lasts (one per reconnect attempt, backing off
        to every 30 s), so the deadline is checked against the wall clock
        rather than counted in blocks.
        """
        if not self._gap_since:
            self._gap_since = wall
            self._gap_reason = reason
            return []
        # Keep the latest reason: "stalled" often becomes a named error on
        # the next attempt, and the named one is the useful one.
        self._gap_reason = reason or self._gap_reason
        hold = self.th.attack_s.get("stream_down", 45.0)
        if self._gap_fired or wall - self._gap_since < hold:
            return []

        self._gap_fired = True
        out: list[Alert] = []
        for rule in self.rules:
            st = self._state[rule.id]
            if st.active:
                st.active = False
                st.held_s = st.clear_s = 0.0
                out.append(
                    Alert("CLEAR", rule.id, rule.severity, self.channel,
                          "cleared: stream gap", 0.0, wall, 0.0, {})
                )
        down_s = wall - self._gap_since
        out.append(
            Alert("FIRE", "stream_down", "critical", self.channel,
                  f"Stream not delivering audio ({self._gap_reason})",
                  0.0, wall, down_s, {"reason": self._gap_reason,
                                      "down_s": round(down_s, 1)})
        )
        return out

    def on_reconnect(self, wall: float) -> Alert | None:
        """Audio is back. ``None`` when the gap never lasted long enough to
        be reported -- clearing an alert nobody was sent is noise of its own.
        """
        fired, began = self._gap_fired, self._gap_since
        self._gap_since = 0.0
        self._gap_fired = False
        self._gap_reason = ""
        if not fired:
            return None
        return Alert("CLEAR", "stream_down", "critical", self.channel,
                     "Stream delivering audio again", 0.0, wall,
                     wall - began if began else 0.0, {})

    def push(self, m: MetricsWindow) -> list[Alert]:
        """Advance every rule by one window; return any state transitions."""
        dt = WINDOW_SECONDS
        out: list[Alert] = []
        # A clip is "playing" for masking purposes as soon as the repeat test
        # says so, not only once the alert has served its 3-minute hold --
        # otherwise every restart spends six minutes alerting about clips.
        filler = (
            ("filler_playing" in self._state and self._state["filler_playing"].active)
            or clip_playing(m, self.th)
        )
        # For the first ~3 minutes of a connection the repeat test has no
        # verdict yet, so we cannot tell a commentator from a playout clip.
        # Judging levels then means every restart opens with "too loud" and
        # "distorting" on channels that are only playing the vendor's clip.
        undecided = m.loop_lag_s == 0.0 and m.loop_score == 0.0
        # A third signal, and the only one that works on a channel which was
        # live and then switched to a clip: the same fault message, word for
        # word, several times over. See REPEAT_EVIDENCE.
        repeating = m.t < self._repeat_until
        if repeating:
            filler = True
        for rule in self.rules:
            st = self._state[rule.id]
            if undecided and not filler and rule.id in _CLIP_MASKED:
                continue
            if filler and rule.id in _CLIP_MASKED:
                # Clear anything already up, then leave the rule frozen until
                # the clip ends: its inputs describe the clip, not a person.
                if st.active:
                    st.active = False
                    st.held_s = st.clear_s = 0.0
                    out.append(self._alert("CLEAR", rule, m, m.t - st.fired_at))
                continue
            if st.active and rule.hold is not None:
                hit = rule.hold(m, self.th)
            else:
                hit = rule.predicate(m, self.th)

            if hit is None:
                # No evidence this window: hold both timers and the current
                # state rather than treating silence as absence of the fault.
                continue

            if hit:
                st.held_s += dt
                st.clear_s = 0.0
            else:
                st.clear_s += dt
                st.held_s = 0.0

            scale = _ESCALATION_SCALE.get(rule.id)
            if not st.active and hit and st.held_s >= self.th.attack_s.get(rule.id, 5.0):
                alert = self._alert("FIRE", rule, m, st.held_s)
                if rule.id in _CLIP_MASKED and self._is_a_recording(rule, alert, m):
                    # The proof arrived with this very alert, so it is not
                    # sent: saying "distorting" a third time with the same
                    # numbers would be the third time we blamed a person for
                    # a recorded file. The explanation goes out instead.
                    st.held_s = st.clear_s = 0.0
                    out.append(self._repeat_notice(m))
                    continue
                st.active = True
                st.fired_at = m.t
                st.mark = scale(m) if scale else 0.0
                out.append(alert)
            elif st.active and hit and scale and scale(m) >= st.mark + self._step(rule.id):
                # Same fault, materially worse: say so again rather than leave
                # the first message standing while it deteriorates.
                st.mark = scale(m)
                out.append(self._alert("ESCALATE", rule, m, m.t - st.fired_at))
            elif st.active and not hit and st.clear_s >= self.th.release_s.get(rule.id, 10.0):
                st.active = False
                out.append(self._alert("CLEAR", rule, m, m.t - st.fired_at))
        return out

    def _is_a_recording(self, rule: Rule, alert: Alert, m: MetricsWindow) -> bool:
        """Has this exact fault message been said enough times to be playout?

        Called with the alert that is about to go out, so the comparison is
        on the words a human would read -- which is where the sample count and
        the true peak live, and those are what a recording repeats exactly.
        """
        previous, times = self._said.get(rule.id, ("", []))
        if alert.message != previous:
            # A different message. Not a repeat, and it resets the count:
            # a changing fault is a live one.
            self._said[rule.id] = (alert.message, [m.t])
            return False
        times = [t for t in times if m.t - t <= REPEAT_WINDOW_S]
        times.append(m.t)
        self._said[rule.id] = (alert.message, times)
        if len(times) < REPEAT_EVIDENCE:
            return False
        self._repeat_until = m.t + REPEAT_HOLD_S
        self._repeat_reason = (
            f"{rule.id} reported exactly the same thing {len(times)} times "
            f"in {m.t - times[0]:.0f}s"
        )
        return True

    def _repeat_notice(self, m: MetricsWindow) -> Alert:
        """Say what we concluded, once, instead of the fault we suppressed."""
        return Alert(
            "FIRE", "clip_repeat", "info", self.channel,
            "This looks like a recording, not someone on air: "
            f"{self._repeat_reason}. Level, peak and balance alerts are "
            "paused until it changes -- nobody can fix a recorded file "
            "mid-match. Dead air and stream loss still report.",
            m.t, m.wall, 0.0,
            {"reason": self._repeat_reason,
             "until_t": round(self._repeat_until, 2),
             "loop_score": m.loop_score,
             "loop_lag_s": m.loop_lag_s},
        )

    def _alert(self, event: str, rule: Rule, m: MetricsWindow, held: float) -> Alert:
        payload = m.to_dict()
        if event == "CLEAR":
            message = f"{rule.id} cleared"
        elif event == "ESCALATE":
            message = "Getting worse - " + rule.describe(m, self.th)
        else:
            message = rule.describe(m, self.th)
        return Alert(
            event=event,
            rule=rule.id,
            severity=rule.severity,
            channel=self.channel,
            message=message,
            t=m.t,
            wall=m.wall or time.time(),
            held_s=held,
            evidence={k: payload[k] for k in rule.evidence},
        )
