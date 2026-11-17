# Aurora Watch on Aurora Lab

`run_lab.py` measures what Aurora Watch reports when the faults of
[Aurora Lab](https://github.com/Arvo-AI/aurora-lab) happen on a live cluster.

For each scenario it resets the healthy `shop` app, lets Aurora Watch learn the
cluster (5 polls), injects the fault with the scenario's own `break.sh`, and
records every finding Aurora Watch confirms: how long after the fault, how
severe, and which change it blames. The detector, change tracker and collector
are imported from `server/services/watch`, the code the Celery task runs; only
the kubectl runner differs (a local `kubectl` here, the internal kubectl API in
Aurora). Polls are every 10 s here and every 60 s in Aurora, so production
detection times scale accordingly.

## Run

Needs Python 3.11+, kubectl, bash, and a **disposable** cluster (kind or
minikube; use a CNI that enforces NetworkPolicy, e.g. Calico, or scenario 13
cannot reproduce).

```bash
git clone https://github.com/Arvo-AI/aurora-lab ../aurora-lab
export KUBECONFIG=/path/to/disposable/kubeconfig
python scripts/watch-lab/run_lab.py --lab ../aurora-lab --out /tmp/watch-lab --context <context>        # all scenarios
python scripts/watch-lab/run_lab.py --lab ../aurora-lab --out /tmp/watch-lab --context <context> 12 07  # some
python scripts/watch-lab/run_lab.py --lab ../aurora-lab --out /tmp/watch-lab --context <context> --healthy 600
python scripts/watch-lab/run_lab.py --report /tmp/watch-lab
```

The script deletes and recreates everything in the `shop` namespace and runs
fault-injection scripts. It refuses to start unless kubectl's current context is
the one passed with `--context` and its API server is on localhost, and it checks
again from inside the shell that runs `break.sh` (on Windows, `bash` from
System32 is WSL's, which carries its own kubeconfig).

## What it measures, and what it does not

* **Detected**: Aurora Watch confirmed an incident-level (high or critical)
  finding after the fault.
* **First incident**: seconds from running `break.sh` to that finding.
* **Blamed the injected change**: the primary suspect of an incident is the
  object the scenario changed (`EXPECTED_CHANGE`, taken from `scenario.yaml`).
* **Healthy control**: findings over a fault-free window, to count false alarms.

It does not run the RCA: each incident's investigation is the existing Aurora
pipeline (and needs an LLM). The lab app is small and synthetic; a production
cluster has more noise than a 7-pod namespace.
