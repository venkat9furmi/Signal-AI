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
    """Which execution wiring the translation endpoints run.

    A mode names a wiring, not a volume knob: it decides which provider is
    built, what it may spend, where the result is kept and who is allowed to
    see it. The endpoints are namespaced by it -- ``/api/translate/shadow/run``
    -- so calling one is choosing the wiring rather than passing a flag that
    something downstream might ignore. :mod:`commentary_qc.translate.app.modes`
    holds the implementations.

    ``off``
        Nothing runs. No tap is installed on the monitors, so no audio is
        buffered and no credit can be spent. This is the default.
    ``shadow``
        The testing wiring. Runs against real commentary, keeps the audio so
        the output can be listened to, spends from a small deliberate budget,
        and shows the result to nobody but whoever asked for it.
    ``standard``
        The production wiring. Declared, not built: see ``modes/standard.py``
        for exactly what building it means.
    """

    OFF = "off"
    SHADOW = "shadow"
    STANDARD = "standard"
    ALL = (OFF, SHADOW, STANDARD)

    #: ``prod`` was the earlier name. Accepted so a control row written before
    #: the rename still loads instead of failing validation at startup.
    ALIASES = {"prod": STANDARD, "production": STANDARD, "test": SHADOW}

    @classmethod
    def normalise(cls, name: str | None) -> str:
        key = (name or "").strip().lower()
        return cls.ALIASES.get(key, key)


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
    #: Shadow mode's own, much smaller allowance. Separate from the figures
    #: above on purpose: testing is a handful of short clips pressed by hand,
    #: and the cap exists so an afternoon of trying things cannot quietly
    #: consume a month of free credit.
    #:
    #: 5 minutes because Camb's free plan grants **6 minutes of API speech to
    #: text in total, once** - not per month and not per day. So this cap is
    #: the wrong shape to protect it: one day at 10 minutes would have spent
    #: the whole allowance. It is set to survive a single day of testing, and
    #: anyone on a paid plan should raise it deliberately.
    shadow_daily_minutes: float = 5.0
    shadow_per_channel_minutes: float = 2.0

    # -- the listen-back path -------------------------------------------
    #: How many seconds of a live channel one shadow run grabs. Long enough
    #: to contain a whole sentence of commentary, short enough that a
    #: mistuned run costs almost nothing.
    grab_seconds: float = 15.0
    #: Seconds to wait for ffmpeg to deliver that audio before giving up. A
    #: live HLS pull starts within a second or two; a channel that has gone
    #: off air never starts at all, and the page needs to be told so.
    grab_timeout_s: float = 25.0

    # -- the live path ---------------------------------------------------
    #: ``fast`` or ``slow``. Fast takes audio almost at once and carries the
    #: fourteen languages Camb publishes for realtime. Slow carries more but
    #: documents a cold boot of thirty seconds or more, which is a long time
    #: to hold a page on "starting" - so it is opt-in, and the only option
    #: for a language fast refuses.
    live_mode: str = "fast"
    #: Hard ceiling on one live session, in seconds. Not a guess at what is
    #: useful: it is what stops a session somebody opened and walked away
    #: from running for the four hours the protocol allows. A realtime
    #: session is billed by the clock rather than per call, so wall time is
    #: the thing that has to be bounded.
    live_seconds: float = 60.0
    #: How many live sessions at once. One, because Camb's free plan
    #: documents an API concurrency of 1 and a second session is refused by
    #: the far end only after audio has started flowing.
    live_concurrent: int = 1

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
    #: Voice the translation is spoken in. 147320 is "Gary", the documented
    #: default; ``GET /list-voices`` is what the page offers instead of this.
    camb_voice_id: int = 147320
    #: Camb's synthesis models. ``mars-8.1-flash-beta`` is their current
    #: default and the fastest, which is what matters for commentary.
    camb_speech_model: str = "mars-8.1-flash-beta"
    #: Container for the spoken audio. WAV because every browser plays it
    #: without a codec question; Camb also offers flac, mp3, adts and raw PCM.
    camb_audio_format: str = "wav"

    raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)

    #: Camb's documented containers for streamed speech.
    AUDIO_FORMATS = ("wav", "flac", "mp3", "adts", "pcm_s16le", "pcm_s32le")

    def __post_init__(self) -> None:
        self.mode = Mode.normalise(self.mode)
        if self.mode not in Mode.ALL:
            raise ValueError(f"mode must be one of {Mode.ALL}, not {self.mode!r}")
        if self.camb_audio_format not in self.AUDIO_FORMATS:
            raise ValueError(
                f"camb_audio_format must be one of {self.AUDIO_FORMATS}, "
                f"not {self.camb_audio_format!r}"
            )
        if self.grab_seconds <= 0:
            raise ValueError("grab_seconds must be positive")
        if self.shadow_daily_minutes < 0 or self.shadow_per_channel_minutes < 0:
            raise ValueError("shadow budgets cannot be negative")
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
        if self.live_mode not in ("fast", "slow"):
            raise ValueError("live_mode must be 'fast' or 'slow'")
        if self.live_seconds <= 0:
            raise ValueError("live_seconds must be positive")
        if self.live_concurrent < 1:
            raise ValueError("live_concurrent must be at least 1")
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
        return self.mode == Mode.STANDARD

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
        if env.get("QC_SHADOW_DAILY_MINUTES"):
            data["shadow_daily_minutes"] = float(env["QC_SHADOW_DAILY_MINUTES"])
        if env.get("QC_SHADOW_SECONDS"):
            data["grab_seconds"] = float(env["QC_SHADOW_SECONDS"])
        if env.get("QC_LIVE_MODE"):
            data["live_mode"] = env["QC_LIVE_MODE"].strip().lower()
        if env.get("QC_LIVE_SECONDS"):
            data["live_seconds"] = float(env["QC_LIVE_SECONDS"])
        if env.get("QC_LIVE_CONCURRENT"):
            data["live_concurrent"] = int(env["QC_LIVE_CONCURRENT"])
        # The voice and the synthesis model are environment settings so they
        # can be changed on a deployment without editing code -- which is the
        # whole point of the adapter being swappable.
        if env.get("CAMB_VOICE_ID"):
            data["camb_voice_id"] = int(env["CAMB_VOICE_ID"])
        if env.get("CAMB_SPEECH_MODEL"):
            data["camb_speech_model"] = env["CAMB_SPEECH_MODEL"].strip()
        if env.get("CAMB_AUDIO_FORMAT"):
            data["camb_audio_format"] = env["CAMB_AUDIO_FORMAT"].strip().lower()
        if env.get("CAMB_API_KEY"):
            data["camb_api_key"] = env["CAMB_API_KEY"].strip()
        if env.get("CAMB_API_BASE"):
            data["camb_base_url"] = env["CAMB_API_BASE"].strip().rstrip("/")
        return cls.from_mapping(data)
