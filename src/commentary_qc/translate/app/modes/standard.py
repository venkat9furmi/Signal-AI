"""Standard mode: the production wiring. Declared, not built.

This file is deliberately short and deliberately present. The mode is listed
by the registry, shown on the page as not built, and its endpoints answer with
a plain "not built yet" rather than a 404 that reads as a wrong URL.

What building it means, so it is a known quantity rather than an open
question. Everything else -- the provider adapters, the mappers, the audio
conversion, the budget, the run store -- is shared with shadow and does not
change:

* a real audience. ``publishes=True``, which means deciding where a caption
  goes: the commentator's live view, the dashboard, or Slack.
* a budget worth the name. Shadow's cap is a handful of minutes meant to stop
  an afternoon of testing running away. Production needs a figure agreed with
  whoever pays for the credits.
* a decision about who triggers a run. Shadow is one button pressed by one
  person. Production is either continuous per claimed channel -- which is what
  ``pipeline.py`` already does -- or triggered by the commentator, and that is
  a product question, not a technical one.
* retention. Shadow keeps every byte of audio so it can be listened to.
  Keeping both sides of every translation on every channel all match day is a
  different storage problem and almost certainly not wanted.

**And the open one: broadcasting the translated audio back into a channel.**

Shadow mode answers "is this any good" by letting a person listen. A product
means the translated voice reaching an audience, and today nothing here can
put audio anywhere -- we only ever pull. What it would take, written down so
the size of it is known:

* **A publishing point.** Commentary reaches the platform by RTMP, to
  ``rtmp://origin.mediaondemand.net/commentaries/<channel>/live``, and that
  path is publish-only -- we proved it is not pullable in September. Pushing
  a translated channel means a publishing point of our own from the vendor,
  one per output language, which is a conversation with Matt and not a code
  change. Writing into an existing commentator's channel is not an option:
  it is their channel and their voice.
* **Continuous audio, not segments.** A listener needs an unbroken stream.
  The models return speech in bursts with silence between, and a live
  translation runs behind and sometimes ahead of the source, so something has
  to fill the gaps and absorb the drift. That is a jitter buffer and a timing
  policy, and it is the hard part.
* **A decision about what happens when it falls behind.** Another language
  takes a different length of time to say. Either the voice speeds up, or it
  drops audio, or it drifts further behind the match. There is no fourth
  option, and which one is right is a product decision about what a listener
  would rather have.
* **Whose voice it is.** A synthetic voice reading translated commentary
  replaces the commentator rather than helping them. Whether MMC and the
  bookmaker want that is the first question, and it is not ours to answer.
* **Terms.** Both model vendors restrict what their output may be used for,
  and the audience here is a bookmaker's customers. That needs a written
  answer before anything is published, not after.

The reason it is not built now is that there is nothing to promote yet: what
the model produces on real commentary has not been listened to. Shadow mode
exists to answer that, and until it has, a production wiring would be a guess
dressed up as a deployment.
"""

from __future__ import annotations

from commentary_qc.translate.app.modes.base import ModeInfo, NotBuiltMode

INFO = ModeInfo(
    name="standard",
    title="Standard",
    purpose="The same pipeline with the output published to commentators.",
    audience="commentators, and anyone watching the dashboard",
    built=False,
    publishes=True,
    retains_audio=False,
    pending="shadow mode has to prove the output is worth publishing first - "
            "and a production budget has to be agreed, because this one spends "
            "credit on every channel for the length of every match",
)


class StandardMode(NotBuiltMode):
    """Production. Inherits the refusal, so there is no half-wired path."""

    def __init__(self) -> None:
        super().__init__(INFO)
