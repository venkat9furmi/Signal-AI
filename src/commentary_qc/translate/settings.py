"""Every knob the translation layer has, in one place.

Deliberately the same shape as :mod:`commentary_qc.config`: a frozen-ish
dataclass validated in ``__post_init__``, so a typo in a setting name fails at
startup instead of silently keeping a default. A silently-ignored setting is an
outage nobody sees.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field, fields
from typing import Any, Mapping


class Mode:
    """How much of the translation layer is switched on.

    Three states rather than a boolean, because "working" and "trusted" are
    different questions and the gap between them is where this feature will
    live for a while:

    ``off``
        Nothing runs. No tap is installed on the monitors, so no audio is
        buffered and no credit can be spent. This is the default.
    ``shadow``
        The whole pipeline runs on real commentary and every result is stored
        and measurable -- but nothing is published. No Slack message, no
        caption on the commentator's live view. This is how the output gets
        judged against real matches before anyone relies on it, and with the
        ``mock`` provider it costs nothing at all.
    ``prod``
        The same pipeline, with the results published.

    The only difference between shadow and prod is who gets to see the output.
    That is the point: whatever is measured in shadow is exactly what goes
    live, so promoting it is a switch rather than a rewrite.
    """

    OFF = "off"
    SHADOW = "shadow"
    PROD = "prod"
    ALL = (OFF, SHADOW, PROD)


@dataclass(slots=True)
class TranslateSettings:
    # -- the switches ---------------------------------------------------
    #: One of :class:`Mode`. Changed at runtime from the page.
    mode: str = Mode.OFF
    #: Which adapter does the work. ``mock`` is offline and free.
    provider: str = "mock"
    #: Adapters that may be selected at all. A provider switched off here
    #: cannot be turned on from the page, which is what keeps a paid model
    #: from being enabled by accident.
    enabled_providers: tuple[str, ...] = ("mock", "camb")
    #: What everything is translated into.
    target_language: str = "en-us"

    # -- scope ----------------------------------------------------------
    #: Only channels a commentator has claimed, by default. The platform
    #: carries other providers' feeds and roughly 25 channels of holding clip
    #: at any moment; transcribing those would spend credit on nothing.
    claimed_only: bool = True
    #: Restrict further to these channels. Empty means "whatever `claimed_only`
    #: allows".
    channels: tuple[str, ...] = ()
    #: Skip audio that is mostly not speech. A holding clip, a crowd bed and a
    #: dead channel all look the same to a transcriber: expensive and empty.
    min_speech_ratio: float = 0.35

    # -- segmentation ---------------------------------------------------
    #: Never send a clip shorter than this, even at a long pause: a two-second
    #: fragment costs a whole request and transcribes badly.
    segment_min_s: float = 8.0
    #: Always close by here, pause or not. Commentary during a goal can run
    #: for a minute without a gap, and latency is a feature.
    segment_max_s: float = 20.0
    #: How many consecutive quiet windows count as "the sentence ended".
    segment_gap_windows: int = 2
    #: Speech ratio below which a window is considered quiet.
    segment_gap_speech: float = 0.25
    #: Sample rate for the upload. Speech recognition gains nothing above
    #: 16 kHz and the provider caps uploads at 20 MB, so sending 48 kHz would
    #: be three times the bytes for no accuracy.
    upload_rate: int = 16_000

    # -- spending -------------------------------------------------------
    #: Hard cap on audio sent per UTC day. This is the free-credit guard: the
    #: pipeline stops and says so rather than discovering the limit by having
    #: the account refused.
    daily_minutes: float = 60.0
    #: And per channel, so one stuck channel cannot eat the whole day.
    per_channel_minutes: float = 20.0

    # -- plumbing -------------------------------------------------------
    #: Segments waiting to be sent. Small on purpose: if the provider is
    #: slower than the match, the right answer is to drop old audio, not to
    #: build an ever-growing backlog of captions nobody can still use.
    queue_size: int = 8
    #: Concurrent provider calls.
    workers: int = 2
    #: How often the recorder re-reads the control row the page writes.
    control_poll_s: float = 3.0

    # -- provider HTTP --------------------------------------------------
    timeout_s: float = 30.0
    poll_interval_s: float = 1.0
    poll_backoff: float = 1.5
    poll_max_interval_s: float = 8.0
    #: Give up on an asynchronous job after this. A transcript that arrives
    #: two minutes late is worthless live, and holding the worker blocks the
    #: segments behind it.
    job_timeout_s: float = 90.0
    #: Overridable so tests can point the adapter at a local fake, which is
    #: how the whole path gets exercised without spending a credit.
    camb_base_url: str = "https://client.camb.ai/apis"
    camb_api_key: str = ""
    #: Camb's transcription modes: ``fast`` or ``slow``.
    camb_transcription_mode: str = "fast"

    raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)

    def __post_init__(self) -> None:
        if self.mode not in Mode.ALL:
            raise ValueError(f"mode must be one of {Mode.ALL}, not {self.mode!r}")
        if self.provider not in self.enabled_providers:
            raise ValueError(
                f"provider {self.provider!r} is not in enabled_providers "
                f"{list(self.enabled_providers)}"
            )
        if self.segment_min_s <= 0 or self.segment_max_s <= 0:
            raise ValueError("segment lengths must be positive")
        if self.segment_min_s > self.segment_max_s:
            raise ValueError(
                f"segment_min_s ({self.segment_min_s}) is above segment_max_s "
                f"({self.segment_max_s}) - no segment could ever close"
            )
        if self.upload_rate <= 0:
            raise ValueError("upload_rate must be positive")
        if self.workers < 1:
            raise ValueError("workers must be at least 1")
        if self.queue_size < 1:
            raise ValueError("queue_size must be at least 1")
        if self.daily_minutes < 0 or self.per_channel_minutes < 0:
            raise ValueError("budgets cannot be negative")
        if self.camb_transcription_mode not in ("fast", "slow"):
            raise ValueError("camb_transcription_mode must be 'fast' or 'slow'")
        # Tuples, so a settings object shared between threads cannot be
        # mutated underneath one of them.
        self.enabled_providers = tuple(self.enabled_providers)
        self.channels = tuple(self.channels)

    # -- derived --------------------------------------------------------
    @property
    def running(self) -> bool:
        """Is any work being done at all?"""
        return self.mode != Mode.OFF

    @property
    def publishes(self) -> bool:
        """May the output be shown to a commentator or sent to Slack?"""
        return self.mode == Mode.PROD

    def covers(self, channel: str, claimed: bool) -> bool:
        """Should this channel be translated right now?"""
        if not self.running:
            return False
        if self.channels and channel not in self.channels:
            return False
        if self.claimed_only and not claimed:
            return False
        return True

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out.pop("raw", None)
        # Never hand the key to the page.
        out["camb_api_key"] = bool(self.camb_api_key)
        out["enabled_providers"] = list(self.enabled_providers)
        out["channels"] = list(self.channels)
        return out

    # -- construction ---------------------------------------------------
    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "TranslateSettings":
        known = {f.name for f in fields(cls)} - {"raw"}
        unknown = set(data) - known
        if unknown:
            raise ValueError(f"unknown TranslateSettings keys: {sorted(unknown)}")
        kwargs = dict(data)
        for key in ("enabled_providers", "channels"):
            if key in kwargs and kwargs[key] is not None:
                kwargs[key] = tuple(kwargs[key])
        return cls(**kwargs, raw=dict(data))

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "TranslateSettings":
        """Settings from the environment, which is how the container gets them.

        The API key comes from the environment and nowhere else -- never from
        the config file, which is committed, and never from the control row,
        which the page can write.
        """
        env = os.environ if env is None else env
        data: dict[str, Any] = {}
        if env.get("QC_TRANSLATE_MODE"):
            data["mode"] = env["QC_TRANSLATE_MODE"].strip().lower()
        if env.get("QC_TRANSLATE_PROVIDER"):
            data["provider"] = env["QC_TRANSLATE_PROVIDER"].strip().lower()
        if env.get("QC_TRANSLATE_TARGET"):
            data["target_language"] = env["QC_TRANSLATE_TARGET"].strip().lower()
        if env.get("QC_TRANSLATE_DAILY_MINUTES"):
            data["daily_minutes"] = float(env["QC_TRANSLATE_DAILY_MINUTES"])
        if env.get("CAMB_API_KEY"):
            data["camb_api_key"] = env["CAMB_API_KEY"].strip()
        if env.get("CAMB_API_BASE"):
            data["camb_base_url"] = env["CAMB_API_BASE"].strip().rstrip("/")
        return cls.from_mapping(data)
