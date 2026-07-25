"""Inject a per-turn execute-don't-propose reminder."""

NUDGE = (
    "Reminder: execute this directly using tools. "
    "Do not return a plan for approval. "
    "Stop only when the work is verifiably done or you are truly blocked."
)


def _inject(**kwargs):
    return {"context": NUDGE}


def register(ctx):
    ctx.register_hook("pre_llm_call", _inject)
