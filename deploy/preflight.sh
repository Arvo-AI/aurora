#!/usr/bin/env bash
set -euo pipefail

# ─────────────────────────────────────────────────────────────────────────────
# Aurora Cluster Preflight Check
# ─────────────────────────────────────────────────────────────────────────────
# Run this BEFORE deploying Aurora to verify your cluster is ready.
#
# Usage:
#   ./deploy/preflight.sh
#   ./deploy/preflight.sh aurora-oss
# ─────────────────────────────────────────────────────────────────────────────

NAMESPACE="${1:-aurora-oss}"
PASS=0
FAIL=0
WARN=0

ok()   { printf '\033[1;32m  ✓ PASS\033[0m  %s\n' "$1"; PASS=$((PASS + 1)); }
fail() { printf '\033[1;31m  ✗ FAIL\033[0m  %s\n' "$1"; FAIL=$((FAIL + 1)); }
warn() { printf '\033[1;33m  ! WARN\033[0m  %s\n' "$1"; WARN=$((WARN + 1)); }
info() { printf '\033[1;34m  →\033[0m %s\n' "$1"; }

echo ""
echo "═══════════════════════════════════════════════"
echo "  Aurora Cluster Preflight Check"
echo "═══════════════════════════════════════════════"
echo ""

# ─── 1. kubectl connected ────────────────────────────────────────────────────

echo "1. Cluster Connection"
echo ""

if ! command -v kubectl &>/dev/null; then
  fail "kubectl not installed"
  info "Install: https://kubernetes.io/docs/tasks/tools/"
  echo ""
  echo "Cannot continue without kubectl."
  exit 1
fi

if kubectl cluster-info &>/dev/null 2>&1; then
  CONTEXT=$(kubectl config current-context 2>/dev/null || echo "unknown")
  ok "kubectl connected — context: $CONTEXT"
else
  fail "kubectl not connected to any cluster"
  info "Connect first:"
  info "  EKS:  aws eks update-kubeconfig --name <cluster> --region <region>"
  info "  GKE:  gcloud container clusters get-credentials <cluster> --region <region>"
  info "  AKS:  az aks get-credentials --resource-group <rg> --name <cluster>"
  echo ""
  echo "Cannot continue without a cluster connection."
  exit 1
fi

# Detect cloud provider
CLOUD="generic"
if [[ "$CONTEXT" == gke_* ]]; then
  CLOUD="gke"
elif [[ "$CONTEXT" == *arn:aws* ]] || [[ "$CONTEXT" == *eks* ]]; then
  CLOUD="eks"
elif [[ "$CONTEXT" == *aks* ]]; then
  CLOUD="aks"
fi
info "Detected cloud: $CLOUD"
echo ""

# ─── 2. Required tools ───────────────────────────────────────────────────────

echo "2. Required Tools"
echo ""

for cmd in helm yq openssl; do
  if command -v "$cmd" &>/dev/null; then
    ok "$cmd installed"
  else
    fail "$cmd not installed"
    case "$cmd" in
      helm)    info "Install: https://helm.sh/docs/intro/install/" ;;
      yq)      info "Install: https://github.com/mikefarah/yq#install" ;;
      openssl) info "Install: apt install openssl / brew install openssl" ;;
    esac
  fi
done
echo ""

# ─── 3. Node resources ───────────────────────────────────────────────────────

echo "3. Node Resources (Aurora needs ~4 CPU, 12GB RAM)"
echo ""

NODE_COUNT=$(kubectl get nodes --no-headers 2>/dev/null | wc -l | tr -d ' ')
if [[ "$NODE_COUNT" -eq 0 ]]; then
  fail "No nodes found in cluster"
else
  ok "$NODE_COUNT node(s) found"

  TOTAL_CPU_MILLI=0
  TOTAL_MEM_MI=0
  while IFS= read -r line; do
    cpu=$(echo "$line" | awk '{print $1}')
    mem=$(echo "$line" | awk '{print $2}')
    if [[ "$cpu" == *m ]]; then
      TOTAL_CPU_MILLI=$((TOTAL_CPU_MILLI + ${cpu%m}))
    else
      TOTAL_CPU_MILLI=$((TOTAL_CPU_MILLI + cpu * 1000))
    fi
    if [[ "$mem" == *Ki ]]; then
      TOTAL_MEM_MI=$((TOTAL_MEM_MI + ${mem%Ki} / 1024))
    elif [[ "$mem" == *Mi ]]; then
      TOTAL_MEM_MI=$((TOTAL_MEM_MI + ${mem%Mi}))
    elif [[ "$mem" == *Gi ]]; then
      TOTAL_MEM_MI=$((TOTAL_MEM_MI + ${mem%Gi} * 1024))
    fi
  done < <(kubectl get nodes -o jsonpath='{range .items[*]}{.status.allocatable.cpu} {.status.allocatable.memory}{"\n"}{end}' 2>/dev/null)

  TOTAL_CPU=$((TOTAL_CPU_MILLI / 1000))
  TOTAL_MEM_GB=$((TOTAL_MEM_MI / 1024))

  if [[ $TOTAL_CPU_MILLI -lt 3500 ]]; then
    fail "CPU: ${TOTAL_CPU} cores allocatable (need at least 4)"
    info "Add more nodes or use larger instance types"
  else
    ok "CPU: ${TOTAL_CPU} cores allocatable"
  fi

  if [[ $TOTAL_MEM_MI -lt 12288 ]]; then
    fail "RAM: ${TOTAL_MEM_GB}GB allocatable (need at least 12GB)"
    info "Add more nodes or use larger instance types"
  else
    ok "RAM: ${TOTAL_MEM_GB}GB allocatable"
  fi
fi
echo ""

# ─── 4. Storage ──────────────────────────────────────────────────────────────

echo "4. Storage"
echo ""

DEFAULT_SC=$(kubectl get storageclass -o jsonpath='{range .items[?(@.metadata.annotations.storageclass\.kubernetes\.io/is-default-class=="true")]}{.metadata.name}{end}' 2>/dev/null || true)

if [[ -z "$DEFAULT_SC" ]]; then
  fail "No default StorageClass found"
  if [[ "$CLOUD" == "eks" ]]; then
    info "EKS requires the EBS CSI driver. See the deployment docs for setup instructions."
  else
    info "Create a StorageClass and annotate it as default"
  fi
else
  PROVISIONER=$(kubectl get storageclass "$DEFAULT_SC" -o jsonpath='{.provisioner}' 2>/dev/null || true)
  ok "Default StorageClass: $DEFAULT_SC (provisioner: $PROVISIONER)"

  # EKS-specific: warn if using the broken in-tree gp2 provisioner
  if [[ "$CLOUD" == "eks" && "$PROVISIONER" == "kubernetes.io/aws-ebs" ]]; then
    fail "StorageClass '$DEFAULT_SC' uses deprecated in-tree provisioner (kubernetes.io/aws-ebs)"
    info "This will NOT work on EKS. Install the EBS CSI driver and create a gp3 StorageClass."
    info "See the deployment docs for step-by-step instructions."
  fi
fi

# EKS: check CSI driver health
if [[ "$CLOUD" == "eks" ]]; then
  CSI_PODS=$(kubectl get pods -n kube-system -l app=ebs-csi-controller --no-headers 2>/dev/null || true)
  if [[ -z "$CSI_PODS" ]]; then
    fail "EBS CSI driver not installed (no ebs-csi-controller pods in kube-system)"
    info "Install it: eksctl create addon --name aws-ebs-csi-driver --cluster <CLUSTER>"
  else
    NOT_RUNNING=$(echo "$CSI_PODS" | grep -v "Running" || true)
    if [[ -n "$NOT_RUNNING" ]]; then
      fail "EBS CSI controller pods are not healthy:"
      echo "$NOT_RUNNING" | while read -r line; do info "  $line"; done
      info "Check logs: kubectl logs -n kube-system -l app=ebs-csi-controller --all-containers --tail=10"
    else
      RUNNING_COUNT=$(echo "$CSI_PODS" | wc -l | tr -d ' ')
      ok "EBS CSI driver: $RUNNING_COUNT controller pod(s) running"
    fi
  fi
fi
echo ""

# ─── 5. Existing Aurora deployment ───────────────────────────────────────────

echo "5. Namespace: $NAMESPACE"
echo ""

if kubectl get namespace "$NAMESPACE" &>/dev/null 2>&1; then
  EXISTING_PODS=$(kubectl get pods -n "$NAMESPACE" --no-headers 2>/dev/null | wc -l | tr -d ' ')
  EXISTING_PVCS=$(kubectl get pvc -n "$NAMESPACE" --no-headers 2>/dev/null | wc -l | tr -d ' ')
  warn "Namespace '$NAMESPACE' already exists ($EXISTING_PODS pods, $EXISTING_PVCS PVCs)"
  info "Helm will upgrade the existing release. Data in bound PVCs will be preserved."

  PENDING_PVCS=$(kubectl get pvc -n "$NAMESPACE" --no-headers 2>/dev/null | grep -v "Bound" || true)
  if [[ -n "$PENDING_PVCS" ]]; then
    fail "Found stuck (non-Bound) PVCs that will block deployment:"
    echo "$PENDING_PVCS" | while read -r line; do info "  $line"; done
    info "Delete them: kubectl delete pvc -n $NAMESPACE \$(kubectl get pvc -n $NAMESPACE --no-headers | grep -v Bound | awk '{print \$1}')"
  fi
else
  ok "Namespace '$NAMESPACE' does not exist (clean install)"
fi
echo ""

# ─── Summary ─────────────────────────────────────────────────────────────────

echo "═══════════════════════════════════════════════"
echo ""
if [[ $FAIL -gt 0 ]]; then
  printf '\033[1;31m  ✗ %d FAILED\033[0m, %d passed' "$FAIL" "$PASS"
  [[ $WARN -gt 0 ]] && printf ', %d warnings' "$WARN"
  echo ""
  echo ""
  echo "  Fix the failures above before deploying Aurora."
  exit 1
elif [[ $WARN -gt 0 ]]; then
  printf '\033[1;32m  ✓ %d passed\033[0m, \033[1;33m%d warnings\033[0m\n' "$PASS" "$WARN"
  echo ""
  echo "  Cluster is ready. Warnings are non-blocking."
  echo "  Run: ./deploy/k8s-deploy.sh"
  exit 0
else
  printf '\033[1;32m  ✓ All %d checks passed\033[0m\n' "$PASS"
  echo ""
  echo "  Cluster is ready. Run: ./deploy/k8s-deploy.sh"
  exit 0
fi
