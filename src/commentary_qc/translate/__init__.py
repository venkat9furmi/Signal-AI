"""Live commentary, transcribed and translated.

A passenger on the QC engine: the audio is already being pulled, decoded and
split into speech and gaps for the quality rules, so turning it into text
costs one more read of a buffer that already exists.

Laid out as ports and adapters, because the model will change and the vendor
might:

``models``      the vocabulary -- segments, transcripts, captions, failures
``settings``    every knob, validated at startup
``ports``       the two capabilities a provider must offer
``mappers/``    vendor JSON <-> our types, pure and testable
``providers/``  one adapter per model, chosen from a registry
``segmenter``   cuts the feed at pauses, reusing the QC engine's own VAD
``budget``      the guard on the free credits
``control``     the runtime switch, written by the page, read by the recorder
``pipeline``    the worker: bounded queue, never blocks a capture thread
``store``       captions, successes and failures alike
``api``         the HTTP surface the page talks to

Three modes, and the gap between the middle two is the point: ``shadow`` runs
the whole thing on real matches and stores every result without publishing
any of it, so what goes to production is exactly what was measured.
"""

from .models import AudioSegment, Caption, Transcript, Translation, TranslateError
from .pipeline import ChannelTap, TranslationPipeline
from .settings import Mode, TranslateSettings

__all__ = [
    "AudioSegment",
    "Caption",
    "ChannelTap",
    "Mode",
    "Transcript",
    "Translation",
    "TranslateError",
    "TranslateSettings",
    "TranslationPipeline",
]
