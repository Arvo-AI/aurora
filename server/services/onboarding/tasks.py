"""Celery side of the setup journey: a periodic sweep, and a quick look right
after something connects so the "X is connected" note arrives in minutes."""

from __future__ import annotations

import logging

from celery_config import celery_app
from utils.log_sanitizer import hash_for_log

logger = logging.getLogger(__name__)

# A connect usually comes in a burst (credentials, then a test call, then a
# second tool). Wait for it to settle, once per org.
KICK_DELAY_S = 120
_KICK_KEY = "onboarding_kick:{org_id}"


@celery_app.task(name="services.onboarding.tasks.evaluate_journey_task")
def evaluate_journey_task(user_id: str) -> str | None:
    from services.onboarding.journey import evaluate
    try:
        return evaluate(user_id)
    except Exception:
        logger.exception("[ONBOARDING] evaluate failed for user_hash=%s", hash_for_log(user_id))
        return None


@celery_app.task(name="services.onboarding.tasks.sweep_journeys")
def sweep_journeys() -> int:
    from services.onboarding.journey import active_journeys, evaluate
    from services.onboarding.sender import emails_enabled
    if not emails_enabled():
        return 0
    sent = 0
    for user_id in active_journeys():
        try:
            if evaluate(user_id):
                sent += 1
        except Exception:
            logger.exception("[ONBOARDING] sweep failed for user_hash=%s", hash_for_log(user_id))
    return sent


def kick_org(org_id: str | None) -> None:
    """Something just connected in this org; look at its journeys shortly. Never raises."""
    if not org_id:
        return
    try:
        from services.onboarding.sender import emails_enabled
        if not emails_enabled():
            return
        from utils.cache.redis_client import get_redis_client
        redis = get_redis_client()
        if redis and not redis.set(_KICK_KEY.format(org_id=org_id), "1", nx=True, ex=KICK_DELAY_S):
            return
        from services.onboarding.journey import journeys_in_org
        for user_id in journeys_in_org(org_id):
            evaluate_journey_task.apply_async(args=[user_id], countdown=KICK_DELAY_S)
    except Exception:
        logger.debug("[ONBOARDING] kick failed", exc_info=True)
