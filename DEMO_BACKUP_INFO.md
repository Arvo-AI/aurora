# Demo Data Backup Information

## Backup Locations

Your original demo data has been backed up in two places:

1. **Directory Backup**: `demo-snapshots-backup/incident-demo-ready/`
   - Contains all original snapshot files
   - Location: `/Users/dloch/aurora/aurora/demo-snapshots-backup/`

2. **Compressed Archive**: `demo-snapshots-backup-20260214_162424.tar.gz`
   - 15MB compressed archive of entire `demo-snapshots/` directory
   - Can be extracted with: `tar xzf demo-snapshots-backup-20260214_162424.tar.gz`

3. **Original Snapshot**: `demo-snapshots/incident-demo-ready/`
   - Still intact and unchanged
   - This is the source for the demo-data/ files used in the demo branch

## Demo Data Files in Demo Branch

The demo branch uses copies from `demo-snapshots/incident-demo-ready/`:
- `demo-data/aurora_db.sql` - PostgreSQL database dump with incident data
- `demo-data/weaviate_data.tar.gz` - Weaviate vector database for incident knowledge
- `demo-data/restore-demo-data.sh` - Automated restoration script

## Restoring from Backup

If you need to restore the original demo snapshots:

```bash
# From the compressed archive
tar xzf demo-snapshots-backup-20260214_162424.tar.gz

# Or copy from the directory backup
cp -r demo-snapshots-backup/incident-demo-ready demo-snapshots/
```

## Creating New Snapshots

To create a new snapshot of your running Aurora instance:

```bash
./scripts/demo-snapshot.sh my-new-snapshot-name
```

This will create a new snapshot in `demo-snapshots/my-new-snapshot-name/`.
