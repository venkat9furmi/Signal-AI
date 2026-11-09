"""Voice activity detection backends.

The entire voice/crowd separation rests on the VAD: crowd level is measured in
the gaps between speech, so every frame the VAD mislabels contaminates one of
the two estimates. On a stadium feed the dominant noise is babble, which is
exactly the case classical VADs handle worst -- hence Silero as the default and
webrtcvad only as a fallback.

Both backends are stateful and stream-oriented: state carries across chunks for
the life of a connection and is reset on reconnect.
"""

from __future__ import annotations

import os
from typing import Protocol

import numpy as np

from commentary_qc.config import VAD_CHUNK_16K, VAD_RATE


class VadBackend(Protocol):
    """A streaming 16 kHz voice activity detector."""

    #: Samples required per :meth:`speech_prob` call.
    chunk_samples: int

    def speech_prob(self, chunk: np.ndarray) -> float:
        """Return P(speech) in [0, 1] for one chunk of float32 mono @ 16 kHz."""

    def reset(self) -> None:
        """Drop stream state. Called on every reconnect."""


class SileroVad:
    """Silero VAD v5 through onnxruntime.

    ~2.3 MB, CPU-only, and far faster than real time, so it costs us nothing
    per stream while being markedly more robust to crowd babble than a
    GMM/energy VAD. Deliberately run through onnxruntime rather than the
    ``silero-vad`` pip package, which drags in torch (~2 GB) for a 2 MB model.
    """

    chunk_samples = VAD_CHUNK_16K

    #: Silero v5 is trained on 512-sample hops that carry 64 samples of the
    #: preceding chunk as left context, so each call actually sees 576 samples.
    #: Feeding a bare 512 does not error -- it silently returns ~0.002 for
    #: everything, including clean speech.
    CONTEXT_16K = 64

    def __init__(self, model_path: str, threshold: float = 0.5) -> None:
        if not os.path.exists(model_path):
            raise FileNotFoundError(
                f"Silero model not found at {model_path!r}. Fetch it with:\n"
                "  curl -L -o models/silero_vad.onnx https://github.com/snakers4/"
                "silero-vad/raw/master/src/silero_vad/data/silero_vad.onnx"
            )
        import onnxruntime as ort

        opts = ort.SessionOptions()
        # One stream per worker process; extra threads only cause contention
        # when many capture workers share a node.
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        opts.log_severity_level = 3
        self._sess = ort.InferenceSession(
            model_path, sess_options=opts, providers=["CPUExecutionProvider"]
        )
        self.threshold = threshold
        self._sr = np.array(VAD_RATE, dtype=np.int64)
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._context = np.zeros(self.CONTEXT_16K, dtype=np.float32)

    def speech_prob(self, chunk: np.ndarray) -> float:
        if chunk.shape[0] != self.chunk_samples:
            raise ValueError(
                f"Silero needs exactly {self.chunk_samples} samples, got {chunk.shape[0]}"
            )
        chunk = chunk.astype(np.float32, copy=False)
        inp = np.concatenate([self._context, chunk]).reshape(1, -1)
        out, self._state = self._sess.run(
            None, {"input": inp, "state": self._state, "sr": self._sr}
        )
        self._context = chunk[-self.CONTEXT_16K :].copy()
        return float(out[0, 0])

    def reset(self) -> None:
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._context = np.zeros(self.CONTEXT_16K, dtype=np.float32)


class WebrtcVad:
    """webrtcvad fallback.

    Kept so the engine still runs where the model file is unavailable, but it
    is materially worse at rejecting crowd noise, which biases the crowd floor
    upward and suppresses ``crowd_dominating``. Returns a hard 0/1 rather than
    a probability.
    """

    chunk_samples = VAD_CHUNK_16K

    def __init__(self, aggressiveness: int = 2) -> None:
        import webrtcvad

        self._vad = webrtcvad.Vad(aggressiveness)
        # webrtcvad accepts only 10/20/30 ms frames. A 512-sample chunk (32 ms)
        # is not one of them, so we score the 30 ms prefix (480 samples) and
        # treat that as the chunk's label.
        self._frame = int(VAD_RATE * 0.03)

    def speech_prob(self, chunk: np.ndarray) -> float:
        pcm = np.clip(chunk[: self._frame] * 32767.0, -32768, 32767).astype(np.int16)
        return 1.0 if self._vad.is_speech(pcm.tobytes(), VAD_RATE) else 0.0

    def reset(self) -> None:  # webrtcvad keeps no cross-frame state we can clear
        pass


def make_vad(backend: str, model_path: str, threshold: float, aggressiveness: int) -> VadBackend:
    """Build the configured VAD, falling back to webrtcvad if Silero is unusable.

    A missing model file or a broken onnxruntime install must degrade the
    engine, not stop it: a worker that runs with a weaker VAD still catches
    dead air, clipping and loudness faults.
    """
    if backend == "webrtc":
        return WebrtcVad(aggressiveness)
    if backend != "silero":
        raise ValueError(f"unknown vad_backend {backend!r} (expected 'silero' or 'webrtc')")
    try:
        return SileroVad(model_path, threshold)
    except Exception as exc:  # noqa: BLE001 - degradation path is deliberate
        import logging

        logging.getLogger(__name__).warning(
            "Silero VAD unavailable (%s); falling back to webrtcvad. "
            "Crowd/voice balance will be less reliable.",
            exc,
        )
        return WebrtcVad(aggressiveness)
