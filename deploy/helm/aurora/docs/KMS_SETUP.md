# Vault Auto-Unseal with KMS

Auto-unseal eliminates manual unsealing after pod restarts by delegating key decryption to a cloud KMS.

## Should You Use Auto-Unseal?

| Scenario | Recommendation |
|----------|----------------|
| Production Kubernetes (any cloud) | Yes - pods restart frequently |
| On-premises with internet access | Yes - use cloud KMS with service account key |
| Development/testing | Optional - manual is fine |
| Air-gapped / no internet | No - use Shamir seals (manual) |

## Quick Decision

```
Where does your Vault run?
│
├─ GCP (GKE/Compute Engine)
│   └─ Use GCP Cloud KMS → see KMS_GCP.md
│
├─ On-premises / Other cloud (with internet)
│   └─ Use GCP Cloud KMS with service account key → see KMS_GCP.md "Option C"
│
└─ Air-gapped / No internet
    └─ Use Shamir seals (manual unsealing required)
```

## GCP Cloud KMS (Example)

| | GCP Cloud KMS |
|---|---------------|
| **Monthly Cost** | ~$0.06 |
| **Setup Time** | 25-35 min |
| **Best For** | GKE, Compute Engine, or on-prem with internet |
| **On-Prem Support** | Yes (service account key) |
| **Auth Method** | Workload Identity / Service Account Key |

## How It Works

```
Pod Restart → Vault Sealed → KMS Decrypt Call → Auto-Unseal → Ready
                                  │
                                  └─ Uses cloud IAM or stored credentials
```

1. Vault starts sealed, reads encrypted unseal key from storage
2. Calls KMS decrypt API using pod's cloud identity (or stored credentials for on-prem)
3. KMS returns decrypted key, Vault unseals automatically
4. Total time: 10-30 seconds (vs 5-30 minutes manual)

## Critical Warnings

**KMS Unavailable = Vault Outage**
- If KMS is down or unreachable, Vault cannot unseal
- Recovery keys cannot bypass KMS
- Monitor KMS availability separately

**KMS Key Deleted = Permanent Data Loss**
- No recovery possible, even from backups
- Enable key deletion protection
- Use IAM policies to prevent accidental deletion

## Helm Configuration

Add to `values.generated.yaml` (GCP example):

```yaml
vault:
  seal:
    type: "gcpckms"
    gcpckms:
      project: "your-project-id"
      region: "us-central1"
      key_ring: "vault-keyring"
      crypto_key: "vault-unseal-key"
      # For on-prem: add credentials: "/vault/gcp/credentials.json"
```

## Next Steps

1. Follow the [GCP Cloud KMS Setup](KMS_GCP.md) guide (includes GKE, Compute Engine, and on-prem with service account key)

2. Complete the step-by-step setup

3. Test with a pod restart:
   ```bash
   kubectl rollout restart statefulset/aurora-oss-vault -n aurora
   kubectl logs -n aurora statefulset/aurora-oss-vault -f
   # Should see: "vault is unsealed"
   ```

## Troubleshooting

| Error | Cause | Fix |
|-------|-------|-----|
| `permission denied` | IAM/RBAC misconfigured | Check cloud IAM permissions |
| `key not found` | Wrong key ID/ARN | Verify key exists and ID is correct |
| `network timeout` | Can't reach KMS | Check network/firewall rules |
| `sealed after restart` | Auto-unseal not configured | Verify seal config in Vault |

## Migrating from Manual to Auto-Unseal

1. Backup Vault data
2. Update Vault config with seal block
3. Restart Vault with `-migrate` flag
4. Provide existing unseal keys when prompted
5. Verify auto-unseal works

See [Seal Migration](https://developer.hashicorp.com/vault/docs/concepts/seal#seal-migration) for details.
