"""Aurora Watch: Aurora opens the incident itself.

Every minute, for orgs that turned it on, Aurora reads each connected
Kubernetes cluster with the read-only access its RCAs already use, learns what
normal looks like, and opens an incident (and its RCA) when something regresses
that no alert rule caught, naming the change most likely to have caused it.

    collector  read-only kubectl, gated by the read-only classifier and org policy
    snapshot   kubectl JSON -> a compact model (no Secret or ConfigMap values)
    changes    what changed between two polls, in readable diffs
    logs       log lines -> templates, HTTP status kept as a class
    detector   learned baseline -> confirmed regressions -> ranked suspect changes
    report     findings -> incident title, RCA payload, rail text
    incidents  open or extend the incident and dispatch the RCA
    store      org settings and per-cluster state (aurora_watch_state)
    tasks      Celery beat tick and per-cluster poll
"""
