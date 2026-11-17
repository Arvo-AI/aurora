"""Rule tests for Startup Check — fixtures mirror aurora-lab fault shapes.

Every HIGH/MEDIUM fixture reproduces the manifest precondition of a lab
scenario (github.com/Arvo-AI/aurora-lab); every "clean" fixture guards the
false-alarm discipline — the light must stay off on healthy repos.
"""

import pytest  # noqa: F401  (fixtures land here as the rule set grows)

from services.startup_check.context import UserContext, parse_user_context
from services.startup_check.scanner import build_index, run_scan


def _findings(result, rule):
    return [f for f in result["findings"] if f["rule"] == rule]


def _one(result, rule):
    found = _findings(result, rule)
    assert len(found) == 1, f"expected 1 {rule} finding, got {len(found)}"
    return found[0]


EMPTY = UserContext()


# ---------------------------------------------------------------------------
# Lab 07 / 15 — Service selector matches no pods
# ---------------------------------------------------------------------------

def test_svc_selector_no_pods_fires():
    files = {
        "k8s/service.yaml": """
apiVersion: v1
kind: Service
metadata:
  name: api
spec:
  selector:
    app: api
  ports:
    - port: 80
      targetPort: 8080
""",
        "k8s/deploy.yaml": """
apiVersion: apps/v1
kind: Deployment
metadata:
  name: api
spec:
  replicas: 2
  template:
    metadata:
      labels:
        app: frontend   # mismatch -> no endpoints
    spec:
      containers:
        - name: api
          image: api:1.0
          ports:
            - containerPort: 8080
""",
    }
    result = run_scan(files, EMPTY)
    f = _one(result, "svc-selector-no-pods")
    assert f["severity"] == "HIGH"
    assert f["file_path"] == "k8s/service.yaml"
    assert result["verdict"] == "RISKY"


def test_svc_selector_with_matching_pods_stays_clean():
    files = {
        "k8s/service.yaml": """
apiVersion: v1
kind: Service
metadata:
  name: api
spec:
  selector:
    app: api
  ports:
    - port: 80
      targetPort: 8080
""",
        "k8s/deploy.yaml": """
apiVersion: apps/v1
kind: Deployment
metadata:
  name: api
spec:
  replicas: 2
  template:
    metadata:
      labels:
        app: api
    spec:
      containers:
        - name: api
          image: api:1.0
          ports:
            - containerPort: 8080
""",
    }
    result = run_scan(files, EMPTY)
    assert _findings(result, "svc-selector-no-pods") == []
    assert _findings(result, "svc-targetport-mismatch") == []


def test_svc_selector_suppressed_by_known_workloads():
    files = {
        "k8s/service.yaml": """
apiVersion: v1
kind: Service
metadata:
  name: api
spec:
  selector:
    app: api
  ports:
    - port: 80
""",
    }
    ctx = parse_user_context({"known_workloads": ["api"]})
    result = run_scan(files, ctx)
    assert _findings(result, "svc-selector-no-pods") == []


# ---------------------------------------------------------------------------
# Lab 11 — targetPort mismatch
# ---------------------------------------------------------------------------

def test_targetport_mismatch_fires():
    files = {
        "k8s/svc.yaml": """
apiVersion: v1
kind: Service
metadata:
  name: api
spec:
  selector:
    app: api
  ports:
    - port: 80
      targetPort: 9090   # nobody listens here
""",
        "k8s/deploy.yaml": """
apiVersion: apps/v1
kind: Deployment
metadata:
  name: api
spec:
  template:
    metadata:
      labels:
        app: api
    spec:
      containers:
        - name: api
          image: api:1.0
          ports:
            - containerPort: 8080
""",
    }
    result = run_scan(files, EMPTY)
    f = _one(result, "svc-targetport-mismatch")
    assert f["severity"] == "HIGH"


def test_named_targetport_resolves_cleanly():
    files = {
        "k8s/svc.yaml": """
apiVersion: v1
kind: Service
metadata:
  name: api
spec:
  selector:
    app: api
  ports:
    - port: 80
      targetPort: http
""",
        "k8s/deploy.yaml": """
apiVersion: apps/v1
kind: Deployment
metadata:
  name: api
spec:
  template:
    metadata:
      labels:
        app: api
    spec:
      containers:
        - name: api
          image: api:1.0
          ports:
            - containerPort: 8080
              name: http
""",
    }
    result = run_scan(files, EMPTY)
    assert _findings(result, "svc-targetport-mismatch") == []


# ---------------------------------------------------------------------------
# Lab 06 — probe port undeclared
# ---------------------------------------------------------------------------

def test_probe_named_port_missing_fires_high():
    files = {
        "k8s/deploy.yaml": """
apiVersion: apps/v1
kind: Deployment
metadata:
  name: api
spec:
  template:
    metadata:
      labels:
        app: api
    spec:
      containers:
        - name: api
          image: api:1.0
          ports:
            - containerPort: 8080
          livenessProbe:
            httpGet:
              path: /health
              port: healthz   # no such named port
""",
    }
    result = run_scan(files, EMPTY)
    f = _one(result, "probe-port-undeclared")
    assert f["severity"] == "HIGH"


def test_probe_numeric_port_not_declared_fires_medium():
    files = {
        "k8s/deploy.yaml": """
apiVersion: apps/v1
kind: Deployment
metadata:
  name: api
spec:
  template:
    metadata:
      labels:
        app: api
    spec:
      containers:
        - name: api
          image: api:1.0
          readinessProbe:
            httpGet:
              path: /health
              port: 9999
""",
    }
    result = run_scan(files, EMPTY)
    f = _one(result, "probe-port-undeclared")
    assert f["severity"] == "MEDIUM"


def test_probe_on_declared_port_stays_clean():
    files = {
        "k8s/deploy.yaml": """
apiVersion: apps/v1
kind: Deployment
metadata:
  name: api
spec:
  template:
    metadata:
      labels:
        app: api
    spec:
      containers:
        - name: api
          image: api:1.0
          ports:
            - containerPort: 8080
              name: http
          livenessProbe:
            httpGet:
              path: /health
              port: http
""",
    }
    result = run_scan(files, EMPTY)
    assert _findings(result, "probe-port-undeclared") == []


# ---------------------------------------------------------------------------
# Lab 04 — missing ConfigMap / Secret refs
# ---------------------------------------------------------------------------

def test_configmap_ref_missing_fires():
    files = {
        "k8s/deploy.yaml": """
apiVersion: apps/v1
kind: Deployment
metadata:
  name: worker
spec:
  template:
    metadata:
      labels:
        app: worker
    spec:
      containers:
        - name: worker
          image: worker:1.0
          envFrom:
            - configMapRef:
                name: worker-config   # not defined anywhere
""",
    }
    result = run_scan(files, EMPTY)
    f = _one(result, "configmap-ref-missing")
    assert f["severity"] == "HIGH"


def test_configmap_ref_suppressed_when_defined():
    files = {
        "k8s/deploy.yaml": """
apiVersion: apps/v1
kind: Deployment
metadata:
  name: worker
spec:
  template:
    metadata:
      labels:
        app: worker
    spec:
      containers:
        - name: worker
          image: worker:1.0
          env:
            - name: CONF
              valueFrom:
                configMapKeyRef:
                  name: worker-config
                  key: conf
""",
        "k8s/cm.yaml": """
apiVersion: v1
kind: ConfigMap
metadata:
  name: worker-config
data:
  conf: yes
""",
    }
    result = run_scan(files, EMPTY)
    assert _findings(result, "configmap-ref-missing") == []


def test_secret_ref_missing_medium_and_suppressible():
    files = {
        "k8s/deploy.yaml": """
apiVersion: apps/v1
kind: Deployment
metadata:
  name: worker
spec:
  template:
    metadata:
      labels:
        app: worker
    spec:
      containers:
        - name: worker
          image: worker:1.0
          env:
            - name: TOKEN
              valueFrom:
                secretKeyRef:
                  name: api-tokens
                  key: token
""",
    }
    result = run_scan(files, EMPTY)
    f = _one(result, "secret-ref-missing")
    assert f["severity"] == "MEDIUM"
    ctx = parse_user_context({"known_secrets": ["api-tokens"]})
    result2 = run_scan(files, ctx)
    assert _findings(result2, "secret-ref-missing") == []


# ---------------------------------------------------------------------------
# Lab 10 — env var pointing at unknown in-cluster service
# ---------------------------------------------------------------------------

def test_env_host_unresolved_fires():
    files = {
        "k8s/deploy.yaml": """
apiVersion: apps/v1
kind: Deployment
metadata:
  name: worker
spec:
  template:
    metadata:
      labels:
        app: worker
    spec:
      containers:
        - name: worker
          image: worker:1.0
          env:
            - name: REDIS_HOST
              value: redis   # no such Service in repo
""",
    }
    result = run_scan(files, EMPTY)
    f = _one(result, "env-host-unresolved")
    assert f["severity"] == "MEDIUM"


def test_env_host_matching_service_stays_clean():
    files = {
        "k8s/deploy.yaml": """
apiVersion: apps/v1
kind: Deployment
metadata:
  name: worker
spec:
  template:
    metadata:
      labels:
        app: worker
    spec:
      containers:
        - name: worker
          image: worker:1.0
          env:
            - name: REDIS_HOST
              value: redis.default.svc.cluster.local
""",
        "k8s/redis.yaml": """
apiVersion: v1
kind: Service
metadata:
  name: redis
spec:
  selector:
    app: redis
  ports:
    - port: 6379
""",
    }
    result = run_scan(files, EMPTY)
    assert _findings(result, "env-host-unresolved") == []


def test_env_host_external_url_not_flagged():
    files = {
        "k8s/deploy.yaml": """
apiVersion: apps/v1
kind: Deployment
metadata:
  name: worker
spec:
  template:
    metadata:
      labels:
        app: worker
    spec:
      containers:
        - name: worker
          image: worker:1.0
          env:
            - name: REDIS_HOST
              value: redis.external.example.com
""",
    }
    result = run_scan(files, EMPTY)
    assert _findings(result, "env-host-unresolved") == []


# ---------------------------------------------------------------------------
# Lab 09 family — StatefulSet volumeClaimTemplate mismatch
# ---------------------------------------------------------------------------

def test_sts_vct_mismatch_fires():
    files = {
        "k8s/sts.yaml": """
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: redis
spec:
  template:
    metadata:
      labels:
        app: redis
    spec:
      containers:
        - name: redis
          image: redis:7
          volumeMounts:
            - name: redis-data   # VCT is named 'data' -> never binds
              mountPath: /data
  volumeClaimTemplates:
    - metadata:
        name: data
      spec:
        accessModes: ["ReadWriteOnce"]
""",
    }
    result = run_scan(files, EMPTY)
    f = _one(result, "sts-volumeclaim-mismatch")
    assert f["severity"] == "HIGH"


def test_sts_vct_matching_stays_clean():
    files = {
        "k8s/sts.yaml": """
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: redis
spec:
  template:
    metadata:
      labels:
        app: redis
    spec:
      containers:
        - name: redis
          image: redis:7
          volumeMounts:
            - name: data
              mountPath: /data
  volumeClaimTemplates:
    - metadata:
        name: data
      spec:
        accessModes: ["ReadWriteOnce"]
""",
    }
    result = run_scan(files, EMPTY)
    assert _findings(result, "sts-volumeclaim-mismatch") == []


# ---------------------------------------------------------------------------
# Lab 14 — ResourceQuota blocks surge pod
# ---------------------------------------------------------------------------

def test_quota_blocks_surge_fires():
    files = {
        "k8s/quota.yaml": """
apiVersion: v1
kind: ResourceQuota
metadata:
  name: quota
  namespace: prod
spec:
  hard:
    cpu: "2"
""",
        "k8s/deploy.yaml": """
apiVersion: apps/v1
kind: Deployment
metadata:
  name: api
  namespace: prod
spec:
  replicas: 2
  template:
    metadata:
      labels:
        app: api
    spec:
      containers:
        - name: api
          image: api:1.0
          resources:
            requests:
              cpu: "1"   # peak 2 replicas + 1 surge = 3 cores > quota 2
""",
    }
    result = run_scan(files, EMPTY)
    f = _one(result, "quota-blocks-surge")
    assert f["severity"] == "MEDIUM"


def test_quota_generous_stays_clean():
    files = {
        "k8s/quota.yaml": """
apiVersion: v1
kind: ResourceQuota
metadata:
  name: quota
  namespace: prod
spec:
  hard:
    cpu: "32"
""",
        "k8s/deploy.yaml": """
apiVersion: apps/v1
kind: Deployment
metadata:
  name: api
  namespace: prod
spec:
  replicas: 2
  template:
    metadata:
      labels:
        app: api
    spec:
      containers:
        - name: api
          image: api:1.0
          resources:
            requests:
              cpu: "1"
""",
    }
    result = run_scan(files, EMPTY)
    assert _findings(result, "quota-blocks-surge") == []


# ---------------------------------------------------------------------------
# image tag / request>limit
# ---------------------------------------------------------------------------

def test_latest_image_low_finding():
    files = {
        "k8s/deploy.yaml": """
apiVersion: apps/v1
kind: Deployment
metadata:
  name: api
spec:
  template:
    metadata:
      labels:
        app: api
    spec:
      containers:
        - name: api
          image: api:latest
""",
    }
    result = run_scan(files, EMPTY)
    f = _one(result, "image-uses-latest")
    assert f["severity"] == "LOW"


def test_request_exceeds_limit_fires():
    files = {
        "k8s/deploy.yaml": """
apiVersion: apps/v1
kind: Deployment
metadata:
  name: api
spec:
  template:
    metadata:
      labels:
        app: api
    spec:
      containers:
        - name: api
          image: api:1.0
          resources:
            requests:
              memory: "512Mi"
            limits:
              memory: "128Mi"
""",
    }
    result = run_scan(files, EMPTY)
    f = _one(result, "request-exceeds-limit")
    assert f["severity"] == "MEDIUM"


# ---------------------------------------------------------------------------
# Layer 2 — user context rules
# ---------------------------------------------------------------------------

def test_request_exceeds_node_fires_with_context():
    files = {
        "k8s/deploy.yaml": """
apiVersion: apps/v1
kind: Deployment
metadata:
  name: big
spec:
  template:
    metadata:
      labels:
        app: big
    spec:
      containers:
        - name: big
          image: big:1.0
          resources:
            requests:
              cpu: "32"
""",
    }
    result = run_scan(files, EMPTY)
    assert _findings(result, "request-exceeds-node") == []  # silent without facts
    ctx = parse_user_context({"nodes": [{"name": "pool", "cpu_cores": 16, "memory_mb": 65536}]})
    result2 = run_scan(files, ctx)
    f = _one(result2, "request-exceeds-node")
    assert f["severity"] == "HIGH"
    assert f["source"] == "context"


def test_limit_below_measured_usage_fires():
    files = {
        "k8s/deploy.yaml": """
apiVersion: apps/v1
kind: Deployment
metadata:
  name: worker
spec:
  template:
    metadata:
      labels:
        app: worker
    spec:
      containers:
        - name: worker
          image: worker:1.0
          resources:
            limits:
              memory: "128Mi"
""",
    }
    ctx = parse_user_context({"service_memory_mb": {"worker": 150}})  # scenario 01
    result = run_scan(files, ctx)
    f = _one(result, "limit-below-usage")
    assert f["severity"] == "HIGH"
    assert "OOMKill" in f["explanation"] or "OOM" in f["explanation"]


def test_pvc_unknown_storageclass_fires():
    files = {
        "k8s/pvc.yaml": """
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: redis-data
spec:
  storageClassName: missing-class
  accessModes: ["ReadWriteOnce"]
""",
    }
    result = run_scan(files, parse_user_context({"storage_classes": ["standard"]}))
    f = _one(result, "pvc-unknown-storageclass")
    assert f["severity"] == "HIGH"

    # no storageClassName + no default
    files2 = {
        "k8s/pvc.yaml": """
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: redis-data
spec:
  accessModes: ["ReadWriteOnce"]
""",
    }
    result2 = run_scan(
        files2, parse_user_context({"storage_classes": ["standard"], "default_storage_class": False})
    )
    _one(result2, "pvc-unknown-storageclass")


def test_dns_wrong_search_domain_fires():
    files = {
        "k8s/deploy.yaml": """
apiVersion: apps/v1
kind: Deployment
metadata:
  name: worker
spec:
  template:
    metadata:
      labels:
        app: worker
    spec:
      dnsConfig:
        searches:
          - worker.svc.wrong-cluster.local
      containers:
        - name: worker
          image: worker:1.0
""",
    }
    result = run_scan(files, parse_user_context({"cluster_domain": "cluster.local"}))
    f = _one(result, "dns-wrong-search-domain")
    assert f["severity"] == "MEDIUM"


# ---------------------------------------------------------------------------
# Assembly + parsing behaviour
# ---------------------------------------------------------------------------

def test_verdict_safe_on_healthy_repo():
    files = {
        "k8s/svc.yaml": """
apiVersion: v1
kind: Service
metadata:
  name: api
spec:
  selector:
    app: api
  ports:
    - port: 80
      targetPort: 8080
""",
        "k8s/deploy.yaml": """
apiVersion: apps/v1
kind: Deployment
metadata:
  name: api
spec:
  replicas: 2
  template:
    metadata:
      labels:
        app: api
    spec:
      containers:
        - name: api
          image: api:1.4.2
          ports:
            - containerPort: 8080
          livenessProbe:
            httpGet:
              path: /health
              port: 8080
          resources:
            requests:
              cpu: "250m"
              memory: "64Mi"
            limits:
              memory: "256Mi"
""",
    }
    result = run_scan(files, EMPTY)
    assert result["verdict"] == "SAFE"
    assert result["findings"] == []
    assert "no known incident preconditions" in result["summary"]
    assert result["scanned_manifests"] == 2


def test_helm_templates_and_bad_yaml_are_skipped_not_fatal():
    files = {
        "chart/values.yaml": "replicas: {{ .Values.replicas }}",
        "broken.yaml": ":\n  - [unclosed",
        "k8s/notes.txt": "not yaml but not fetched anyway",
    }
    result = run_scan(files, EMPTY)
    # Nothing parsed -> honest "nothing to check", not a green SAFE.
    assert result["verdict"] == "NO_MANIFESTS"
    assert result["skipped_files"] >= 1


def test_multi_doc_yaml_all_parsed():
    files = {
        "k8s/all.yaml": """
apiVersion: v1
kind: ConfigMap
metadata:
  name: cm-a
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: cm-b
""",
    }
    result = run_scan(files, EMPTY)
    assert result["scanned_manifests"] == 2


def test_rule_crash_does_not_kill_scan(monkeypatch):
    from services.startup_check import rules as rules_mod

    def boom(index, ctx):
        raise RuntimeError("rule exploded")

    monkeypatch.setattr(rules_mod, "BASELINE_RULES", [boom])
    files = {"k8s/deploy.yaml": "apiVersion: v1\nkind: Pod\nmetadata:\n  name: p\n"}
    index = build_index(files)
    assert rules_mod.run_all_rules(index, EMPTY) == []
