"""
Shared sampling-parameter guard for chat-model providers.

Some Claude models (e.g. Anthropic Opus 4.7+) removed ``temperature`` / ``top_p`` /
``top_k`` and return a 400 when those params are sent — on the direct Anthropic API
(``"temperature is deprecated for this model"``) and on Bedrock Converse (a
``ValidationException`` naming the field). Aurora hardcodes ``temperature=0.4``, so any
call to such a model fails unless the param is dropped.

Rather than hardcode a per-model list (which would need updating for every future
model), we react to the model's own error: on a rejection whose message names a
sampling field, drop the offending params and let the caller retry once. Used by the
Anthropic (``ChatAnthropic``) and Bedrock (``ChatBedrockConverse``) providers via thin
adaptive subclasses that wrap ``_generate`` / ``_stream`` (+ async variants).
"""

# Sampling params some models accept and newer ones reject outright.
SAMPLING_FIELDS = ("temperature", "top_p", "top_k")


def _model_label(model) -> str:
    """Best-effort model identifier across client classes. Order matches
    ``llm.py``'s ``_model_label``: prefer ``model_name``, then ``model_id`` (the
    canonical attr on ChatBedrockConverse — ``.model`` there is only an input
    alias), then ``model``."""
    return (
        getattr(model, "model_name", None)
        or getattr(model, "model_id", None)
        or getattr(model, "model", None)
        or type(model).__name__
    )


def strip_rejected_sampling(model, err, logger) -> bool:
    """If ``err`` names a sampling param, clear the ones currently set on ``model``.

    Returns True only when something was actually stripped — so a retry differs from
    the failed call, and a second already-stripped call returns False (no retry loop).
    Errors that don't name a sampling field return False and should be re-raised.
    """
    msg = str(err).lower()
    if not any(field in msg for field in SAMPLING_FIELDS):
        return False
    stripped = False
    for field in SAMPLING_FIELDS:
        if getattr(model, field, None) is not None:
            setattr(model, field, None)
            stripped = True
    if stripped:
        logger.warning(
            "Model %s rejected a sampling parameter; retrying without it.",
            _model_label(model),
        )
    return stripped
