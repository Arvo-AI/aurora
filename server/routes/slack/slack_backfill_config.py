"""Shared so celery_config's beat schedule and slack_channel_metadata's org
rotation can't drift apart — a mismatch makes the rotation skip orgs. Its own
module because slack_channel_metadata already imports celery_config.
"""

BACKFILL_INTERVAL_SECONDS = 900.0
