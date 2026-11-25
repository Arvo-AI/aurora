"""
Built-in Check Review Action

Default instructions for the system action that looks at detection checks
which stopped working: demoted for firing on nothing, or erroring on every run.

It has no schedule of its own. services/detection/runner.py starts it for an
org only when that org has checks waiting, so an org with healthy checks pays
for no run at all.
"""

SYSTEM_KEY = "detection_check_review"

DEFAULT_CHECK_REVIEW_INSTRUCTIONS = """Some of the checks Aurora runs by itself have stopped being useful: \
they fired on nothing more than once, or they error on every run. Nobody reads a silent check's \
failures, so you are the one who looks.

Call list_detection_checks with needs_review true. You get at most a handful, each with why it \
is here, its recent runs (status, the value seen, the error) and its recent fires. If the list \
is empty, respond "DONE: nothing to review" and stop.

For each check, find out what actually happened before changing anything. Run its command \
yourself with your read-only tools and look at the output.

- It errors because the command no longer works (a resource was renamed, the output changed \
  shape, a flag is wrong): fix the command and save it under the same name with \
  save_detection_check. Keep the same asset, pattern and origin.
- It fires on nothing because the threshold is wrong for how this system normally behaves: \
  save it with a threshold the recent values support. Say in the reason which values you used.
- It errors because Aurora no longer has access to that system: leave it. That is reported \
  separately and fixing the check will not help.
- The thing it watched is gone, or you cannot tell what healthy looks like: retire it with \
  retire_detection_check and say why.
- You are not sure: leave it and say why. A check left alone comes back for review only if it \
  goes wrong again.

Do not make a check quieter just to stop it firing. A check that fired because something was \
actually wrong is doing its job, even if no incident was declared.

Finish with one line per check: what you found and what you did."""
