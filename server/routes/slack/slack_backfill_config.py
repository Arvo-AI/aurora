"""Shared so celery_config's beat schedule and slack_channel_metadata's org
rotation can't drift apart — a mismatch makes the rotation skip orgs. Its own
module because slack_channel_metadata already imports celery_config, and
slack_channels needs the stale window without importing the metadata module.
"""

BACKFILL_INTERVAL_SECONDS = 900.0

# How long a 'pending' row must sit before the sweep treats it as stuck rather
# than still in flight. NOT the same knob as BACKFILL_INTERVAL_SECONDS: a started
# task is 'generating', so a still-'pending' row is one waiting in the broker
# queue, and this measures worst-case queue wait, not sweep cadence. Hours, not
# minutes: a backlog of description jobs on the default queue has been observed
# ~85 min deep, and anything shorter re-claims rows that are merely queued.
# (generate_channel_metadata also claims the row, so a duplicate costs no LLM
# call — this window only decides how fast a genuinely lost task is retried.)
BACKFILL_STALE_MINUTES = 180
