#!/usr/bin/env bash
# Aurora Azure access setup. Designed to run in Azure Cloud Shell, which already
# provides az/jq/python3/kubectl and an authenticated session.
#
# Grants Aurora two service principals:
#   agent    - Contributor (no authorization writes) + AKS RBAC Writer
#   readonly - Log Analytics Reader + AKS Cluster User + AKS RBAC Reader
# Built-in roles only: AKS RBAC Reader's pods/read already covers `kubectl logs`,
# since Azure models Kubernetes subresources under the parent resource verb.
#
# Re-runnable: role assignments are upserted.
set -euo pipefail

SCOPE_ARG="${1:-}"   # optional: management group id, else all enabled subscriptions
STAMP="$(date +%Y%m%d-%H%M%S)"

die() { echo "ERROR: $*" >&2; exit 1; }
note() { echo "  $*"; }

# --- Preflight ------------------------------------------------------------
# Fail before mutating anything, with the exact remedy for each blocker.
for tool in az jq python3; do
  command -v "$tool" >/dev/null 2>&1 || die "$tool not found. Run this script in Azure Cloud Shell (https://shell.azure.com)."
done

az account show >/dev/null 2>&1 || die "Not logged in. Run: az login"

TENANT_ID="$(az account show --query tenantId -o tsv)"
echo "Aurora Azure setup (tenant $TENANT_ID)"

# App registration is commonly restricted by tenant policy; check before we start.
if ! az ad signed-in-user show >/dev/null 2>&1; then
  note "Warning: cannot read your directory profile; app creation may fail."
fi

# --- Resolve target scopes -----------------------------------------------
# Only subscriptions in the SP's home tenant are usable: a service principal
# exists in one tenant, so cross-tenant role assignment always fails. Operators
# with several `az login` sessions see subscriptions from multiple tenants here.
list_tenant_subscriptions() {
  az account list --query "[?state=='Enabled' && tenantId=='$TENANT_ID'].id" -o tsv
}

# Subscriptions actually under the management group, at any depth. Reporting
# tenant-wide subscriptions here would hand Aurora ids it has no rights on,
# since MG assignments only inherit to descendants. The response nests child
# management groups, so the tree is walked rather than filtered flat.
list_mg_subscriptions() {
  az account management-group show --name "$1" --expand --recurse -o json 2>/dev/null \
    | python3 -c '
import json, sys
def walk(node):
    for child in node.get("children") or []:
        if child.get("type", "").endswith("/subscriptions"):
            yield child["name"]
        else:
            yield from walk(child)
try:
    print("\n".join(walk(json.load(sys.stdin))))
except Exception:
    pass
'
}

if [[ -n "$SCOPE_ARG" ]]; then
  MG_SCOPE="/providers/Microsoft.Management/managementGroups/${SCOPE_ARG#/providers/Microsoft.Management/managementGroups/}"
  az account management-group show --name "${MG_SCOPE##*/}" >/dev/null 2>&1 \
    || die "Management group '${MG_SCOPE##*/}' not found or not readable. Grant yourself Management Group Reader, or omit the argument to use per-subscription scope."
  ASSIGN_SCOPES=("$MG_SCOPE")
  mapfile -t SUBSCRIPTIONS < <(list_mg_subscriptions "${MG_SCOPE##*/}")
  echo "Scope: management group ${MG_SCOPE##*/} (${#SUBSCRIPTIONS[@]} subscription(s) below it)"
else
  mapfile -t SUBSCRIPTIONS < <(list_tenant_subscriptions)
  ASSIGN_SCOPES=()
  for s in "${SUBSCRIPTIONS[@]}"; do ASSIGN_SCOPES+=("/subscriptions/$s"); done
  echo "Scope: ${#SUBSCRIPTIONS[@]} enabled subscription(s) in tenant $TENANT_ID"
fi

[[ ${#SUBSCRIPTIONS[@]} -gt 0 ]] || die "No enabled subscriptions found in tenant $TENANT_ID."

# --- Create service principals -------------------------------------------
# create-for-rbac without --role creates the app with no assignment; roles are
# added explicitly below so each scope is auditable.
echo "Creating service principals..."
AGENT_SP="$(az ad sp create-for-rbac --name "Aurora-Agent-$STAMP" \
  --query '{clientId:appId,clientSecret:password,tenantId:tenant}' -o json)"
RO_SP="$(az ad sp create-for-rbac --name "Aurora-ReadOnly-$STAMP" \
  --query '{clientId:appId,clientSecret:password,tenantId:tenant}' -o json)"

AGENT_ID="$(jq -r .clientId <<<"$AGENT_SP")"
AGENT_SECRET="$(jq -r .clientSecret <<<"$AGENT_SP")"
RO_ID="$(jq -r .clientId <<<"$RO_SP")"
RO_SECRET="$(jq -r .clientSecret <<<"$RO_SP")"

# Assignments need the service principal object id, not the app id.
AGENT_OID="$(az ad sp show --id "$AGENT_ID" --query id -o tsv)"
RO_OID="$(az ad sp show --id "$RO_ID" --query id -o tsv)"

assign() {  # assign <object-id> <role> <scope>
  az role assignment create --assignee-object-id "$1" --assignee-principal-type ServicePrincipal \
    --role "$2" --scope "$3" >/dev/null 2>&1 \
    && note "$2 -> ${3##*/}" \
    || note "$2 -> ${3##*/} (already present or denied)"
}

echo "Assigning roles..."
for scope in "${ASSIGN_SCOPES[@]}"; do
  # Contributor excludes all Microsoft.Authorization writes, so Aurora can
  # manage resources but never escalate its own permissions.
  assign "$AGENT_OID" "Contributor" "$scope"
  assign "$AGENT_OID" "Azure Kubernetes Service RBAC Writer" "$scope"
  assign "$AGENT_OID" "Cost Management Reader" "$scope"
  # Log Analytics Reader subsumes the */read of Reader and Monitoring Reader.
  # AKS RBAC Reader's pods/read covers `kubectl logs`: Azure models Kubernetes
  # subresources under the parent verb, so no pods/log action exists.
  assign "$RO_OID" "Log Analytics Reader" "$scope"
  assign "$RO_OID" "Cost Management Reader" "$scope"
  assign "$RO_OID" "Azure Kubernetes Service Cluster User Role" "$scope"
  assign "$RO_OID" "Azure Kubernetes Service RBAC Reader" "$scope"
done

# --- Private cluster advisory --------------------------------------------
# Private AKS API servers are unreachable from Cloud Shell and from Aurora's
# network, so RBAC alone is not enough; they need the in-cluster agent.
PRIVATE=()
for sub in "${SUBSCRIPTIONS[@]}"; do
  while read -r name; do
    [[ -n "$name" ]] && PRIVATE+=("$name ($sub)")
  done < <(az aks list --subscription "$sub" \
    --query "[?apiServerAccessProfile.enablePrivateCluster].name" -o tsv 2>/dev/null || true)
done

# --- Output ---------------------------------------------------------------
echo
echo "Paste this JSON into Aurora:"
jq -n \
  --arg tenant "$TENANT_ID" \
  --arg aid "$AGENT_ID" --arg asec "$AGENT_SECRET" \
  --arg rid "$RO_ID" --arg rsec "$RO_SECRET" \
  --arg sub "${SUBSCRIPTIONS[0]}" \
  --argjson subs "$(printf '%s\n' "${SUBSCRIPTIONS[@]}" | jq -R . | jq -s .)" \
  '{agent:  {tenantId:$tenant, clientId:$aid, clientSecret:$asec, subscriptionId:$sub},
    readonly:{tenantId:$tenant, clientId:$rid, clientSecret:$rsec, subscriptionId:$sub},
    subscriptions:$subs}'

if [[ ${#PRIVATE[@]} -gt 0 ]]; then
  echo
  echo "Private AKS clusters detected. Their API servers are not reachable from Aurora;"
  echo "connect each one with the Aurora Kubernetes agent (Integrations > Kubernetes):"
  printf '  - %s\n' "${PRIVATE[@]}"
fi

echo
echo "Permissions granted: Contributor (agent mode), read-only roles (ask mode)."
echo "Aurora cannot modify role assignments in either mode."
echo "Revoke: az ad sp delete --id $AGENT_ID && az ad sp delete --id $RO_ID"
