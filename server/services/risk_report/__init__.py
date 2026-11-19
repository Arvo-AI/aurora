"""Incident backtest ("Risk Report").

Replays Aurora's existing pre-merge PR reviewer (``services.change_gating``)
on a public repository's recently merged PRs, using only what existed before
each merge, and scores it against what actually happened afterwards (reverts,
hotfixes). Gives a new user a real result from a repo URL in minutes, with no
GitHub App install and no production credentials.
"""
