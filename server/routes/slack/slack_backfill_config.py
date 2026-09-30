"""Shared tuning for the Slack channel-description backfill sweep.

Deliberately dependency-free. It exists so the sweep's interval has exactly one
definition: :mod:`celery_config` imports it to build the beat schedule, and
:mod:`routes.slack.slack_channel_metadata` imports it to rotate which org leads
each run. The constant cannot simply live in either of those modules --
``slack_channel_metadata`` imports ``celery_config``, so putting it in the task
module would make ``celery_config`` import it back circularly.
"""

# How often the backfill sweep runs, in seconds.
#
# Single source of truth for two things that MUST agree: the Celery beat
# schedule, and the wall-clock org rotation in ``_rotate_orgs``. The rotation
# derives its offset as ``now // BACKFILL_INTERVAL_SECONDS``, so when this equals
# the real schedule the leading org advances by exactly one per run and every org
# takes a turn.
#
# If the two drifted such that the real schedule were a multiple of this value,
# the offset would step by more than one per run and could permanently skip orgs
# -- stepping by 2 across an even number of orgs only ever visits half of them --
# reintroducing the starvation the rotation exists to prevent. Hence one constant
# rather than a literal in each file.
BACKFILL_INTERVAL_SECONDS = 900.0
