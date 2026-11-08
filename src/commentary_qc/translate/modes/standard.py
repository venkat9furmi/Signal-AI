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

The reason it is not built now is that there is nothing to promote yet: what
the model produces on real commentary has not been listened to. Shadow mode
exists to answer that, and until it has, a production wiring would be a guess
dressed up as a deployment.
"""

from __future__ import annotations

from .base import ModeInfo, NotBuiltMode

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
