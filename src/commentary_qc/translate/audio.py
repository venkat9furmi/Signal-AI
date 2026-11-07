"""Turning analysis audio into something a provider will accept.

The capture path gives us 48 kHz mono float32, which is what the loudness
meter and the VAD want. No speech API wants that: they want a container, 16-bit
samples, and as few bytes as possible. Camb caps an upload at 20 MB.

Downsampling to 16 kHz costs nothing in accuracy -- speech recognition models
are trained at 16 kHz, and telephone bandwidth carries intelligible speech at
8 -- and saves two thirds of the bytes. 48000/16000 is exactly 3, so this is a
clean decimation rather than a resample.
"""

from __future__ import annotations

import io
import wave

import numpy as np

from ..config import ANALYSIS_RATE


def decimate(samples: np.ndarray, rate_in: int, rate_out: int) -> np.ndarray:
    """Reduce the sample rate, filtering first so nothing aliases.

    Dropping every third sample without a filter folds everything above
    8 kHz back down into the speech band as a hiss that a transcriber hears
    as consonants. ``scipy.signal.decimate`` applies the anti-alias filter;
    without scipy we fall back to a short moving average, which is a poor
    filter but far better than none.
    """
    if rate_in == rate_out or samples.size == 0:
        return samples
    if rate_in % rate_out:
        raise ValueError(f"{rate_in} Hz does not divide evenly into {rate_out} Hz")
    factor = rate_in // rate_out
    try:
        from scipy.signal import decimate as _scipy_decimate

        # zero_phase keeps the audio aligned with the timestamps we report.
        return np.asarray(
            _scipy_decimate(samples, factor, ftype="fir", zero_phase=True),
            dtype=np.float32,
        )
    except Exception:  # noqa: BLE001 - a usable upload beats a perfect one
        usable = samples.size - (samples.size % factor)
        if usable <= 0:
            return samples[:0]
        return samples[:usable].reshape(-1, factor).mean(axis=1).astype(np.float32)


def to_wav(
    samples: np.ndarray,
    rate_in: int = ANALYSIS_RATE,
    rate_out: int = 16_000,
) -> bytes:
    """A 16-bit mono WAV, in memory.

    WAV rather than MP3 or AAC because it needs no encoder: ffmpeg is in the
    image but shelling out per segment would add a process spawn and a
    temporary file to something that happens every few seconds on every
    channel. At 16 kHz a 20 s segment is 640 KB, comfortably inside the
    provider's 20 MB limit.

    Samples are clipped rather than normalised. Normalising would hide exactly
    the clipping and level faults the rest of this system exists to report,
    and the transcript should be made from what went out on air.
    """
    audio = np.asarray(samples, dtype=np.float32).ravel()
    audio = decimate(audio, rate_in, rate_out)
    pcm = np.clip(audio, -1.0, 1.0)
    # 32767 rather than 32768: scaling by 32768 makes a sample at exactly
    # +1.0 wrap to the most negative value, which is an audible click.
    ints = np.round(pcm * 32767.0).astype("<i2")

    buf = io.BytesIO()
    with wave.open(buf, "wb") as fh:
        fh.setnchannels(1)
        fh.setsampwidth(2)
        fh.setframerate(rate_out)
        fh.writeframes(ints.tobytes())
    return buf.getvalue()


def wav_seconds(blob: bytes) -> float:
    """How long a WAV blob is. Used to check what we are about to be charged for."""
    with wave.open(io.BytesIO(blob), "rb") as fh:
        return fh.getnframes() / float(fh.getframerate() or 1)
