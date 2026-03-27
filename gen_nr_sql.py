#!/usr/bin/env python3
"""Generate demo-incident-newrelic.sql using only base table columns."""
import subprocess
import json
import sys

SRC_INCIDENT_ID = '133de658-05b3-488f-b0b3-dd3452de3c8f'
INCIDENT_ID = 'b1c2d3e4-f5a6-4b7c-8d9e-0f1a2b3c4d5e'
CHAT_SESSION_ID = 'c2d3e4f5-a6b7-4c8d-9e0f-1a2b3c4d5e6f'
DEMO_USER_ID = 'ab209180-626b-4601-8042-9f6328d03ae9'
DEMO_ORG_ID = '26db606d-1453-41be-b994-256ea1c7ee5b'
NR_EVENT_ID = 99901

# Only columns that exist in the base CREATE TABLE (not migrations)
INCIDENT_BASE_COLS = [
    'id', 'user_id', 'org_id', 'source_type', 'source_alert_id',
    'status', 'severity', 'alert_title', 'alert_service', 'alert_environment',
    'aurora_status', 'aurora_summary', 'aurora_chat_session_id',
    'started_at', 'analyzed_at', 'slack_message_ts', 'active_tab',
    'created_at', 'updated_at', 'merged_into_incident_id'
]
# Migration columns - set via DO block that ignores missing columns
INCIDENT_MIGRATION_COLS = [
    'alert_metadata', 'correlated_alert_count', 'affected_services',
    'visualization_code', 'visualization_updated_at', 'rca_celery_task_id'
]

INCIDENT_ALERT_BASE_COLS = [
    'id', 'incident_id', 'user_id', 'org_id', 'source_type',
    'source_alert_id', 'alert_title', 'alert_service', 'alert_metadata'
]

THOUGHT_BASE_COLS = ['incident_id', 'timestamp', 'content', 'thought_type', 'created_at']
CITATION_BASE_COLS = ['incident_id', 'citation_key', 'tool_name', 'command', 'output', 'executed_at', 'created_at']
SUGGESTION_BASE_COLS = ['incident_id', 'title', 'description', 'type', 'risk', 'command', 'file_path', 'original_content', 'suggested_content', 'user_edited_content', 'repository', 'pr_url', 'pr_number', 'created_branch', 'applied_at', 'created_at']
CHAT_BASE_COLS = ['id', 'user_id', 'org_id', 'title', 'messages', 'ui_state', 'created_at', 'updated_at', 'is_active', 'status', 'incident_id']

def psql(sql):
    r = subprocess.run(
        ['docker', 'exec', 'aurora-postgres', 'psql', '-U', 'aurora', '-d', 'aurora_db', '-tA', '-c', sql],
        capture_output=True, text=True
    )
    if r.returncode != 0:
        print(f"SQL ERROR: {r.stderr}", file=sys.stderr)
    return r.stdout

def psql_json(sql):
    r = psql(f"SELECT row_to_json(t)::text FROM ({sql}) t;")
    rows = []
    for line in r.strip().split('\n'):
        if line.strip():
            rows.append(json.loads(line))
    return rows

def escape_val(v):
    if v is None:
        return 'NULL'
    if isinstance(v, bool):
        return 'TRUE' if v else 'FALSE'
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, list):
        if all(isinstance(x, str) for x in v):
            escaped = [x.replace("'", "''") for x in v]
            return "ARRAY[" + ", ".join(f"'{x}'" for x in escaped) + "]::text[]"
        j = json.dumps(v, ensure_ascii=False).replace("'", "''")
        return f"'{j}'::jsonb"
    if isinstance(v, dict):
        j = json.dumps(v, ensure_ascii=False).replace("'", "''")
        return f"'{j}'::jsonb"
    s = str(v).replace("\\", "\\\\").replace("'", "''")
    s = s.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    return f"E'{s}'"

def gen_insert(table, row, cols=None):
    if cols:
        use_cols = [c for c in cols if c in row]
    else:
        use_cols = list(row.keys())
    vals = [escape_val(row.get(c)) for c in use_cols]
    return f"INSERT INTO {table} ({', '.join(use_cols)}) VALUES ({', '.join(vals)});"

output = []
output.append("-- Demo Incident: New Relic - checkout-service High Memory")
output.append("-- Source: New Relic | Cloud: AWS/ECS")
output.append("-- Uses only base table columns (safe for any schema version)")
output.append("")
output.append("CREATE TABLE IF NOT EXISTS newrelic_events (")
output.append("    id SERIAL PRIMARY KEY,")
output.append("    user_id VARCHAR(255) NOT NULL,")
output.append("    org_id VARCHAR(255),")
output.append("    issue_id VARCHAR(255),")
output.append("    issue_title TEXT,")
output.append("    priority VARCHAR(20),")
output.append("    state VARCHAR(50),")
output.append("    entity_names TEXT,")
output.append("    payload JSONB NOT NULL,")
output.append("    received_at TIMESTAMP NOT NULL,")
output.append("    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP")
output.append(");")
output.append("")
output.append("-- Clean ONLY this demo incident")
output.append(f"DELETE FROM incident_citations WHERE incident_id = '{INCIDENT_ID}';")
output.append(f"DELETE FROM incident_suggestions WHERE incident_id = '{INCIDENT_ID}';")
output.append(f"DELETE FROM incident_thoughts WHERE incident_id = '{INCIDENT_ID}';")
output.append(f"DELETE FROM incident_alerts WHERE incident_id = '{INCIDENT_ID}';")
output.append(f"DELETE FROM incident_alerts WHERE id = '000cec6c-a366-42ea-918c-834611e0fc39';")
output.append(f"DELETE FROM chat_sessions WHERE id = '{CHAT_SESSION_ID}';")
output.append(f"DELETE FROM incidents WHERE id = '{INCIDENT_ID}';")
output.append(f"DELETE FROM newrelic_events WHERE id = {NR_EVENT_ID};")
output.append(f"DELETE FROM newrelic_events WHERE issue_id = 'INC-20260326-003' AND user_id = '{DEMO_USER_ID}';")
output.append("")

# Get source data
src = psql_json(f"SELECT source_alert_id, aurora_chat_session_id FROM incidents WHERE id = '{SRC_INCIDENT_ID}'")
if not src:
    print("ERROR: Source incident not found!", file=sys.stderr)
    sys.exit(1)
src_alert_id = src[0]['source_alert_id']
src_chat_id = src[0].get('aurora_chat_session_id')
print(f"Source alert_id={src_alert_id}, chat_session={src_chat_id}", file=sys.stderr)

# 1. newrelic_events
rows = psql_json(
    f"SELECT user_id, org_id, issue_id, issue_title, priority, state, entity_names, payload, received_at, created_at "
    f"FROM newrelic_events WHERE id = {src_alert_id}"
)
output.append("-- 1. New Relic event")
for row in rows:
    row['org_id'] = DEMO_ORG_ID
    row['user_id'] = DEMO_USER_ID
    cols = ['id'] + list(row.keys())
    vals = [str(NR_EVENT_ID)] + [escape_val(row[c]) for c in row.keys()]
    output.append(f"INSERT INTO newrelic_events ({', '.join(cols)}) VALUES ({', '.join(vals)}) ON CONFLICT (id) DO NOTHING;")
output.append("")
print(f"newrelic_events: {len(rows)}", file=sys.stderr)

# 2. incidents (BASE columns only)
rows = psql_json(f"SELECT * FROM incidents WHERE id = '{SRC_INCIDENT_ID}'")
output.append("-- 2. Incident record (base columns only)")
for row in rows:
    row['id'] = INCIDENT_ID
    row['user_id'] = DEMO_USER_ID
    row['org_id'] = DEMO_ORG_ID
    row['source_alert_id'] = NR_EVENT_ID
    row['aurora_chat_session_id'] = CHAT_SESSION_ID
    row['alert_title'] = 'checkout-service High Memory / Connection Pool Exhaustion'
    output.append(gen_insert('incidents', row, INCIDENT_BASE_COLS))
output.append("")

# Migration columns via safe DO block
output.append("-- Set migration columns (safe if columns don't exist yet)")
for row in rows:
    md = row.get('alert_metadata', {})
    if isinstance(md, str):
        md = json.loads(md)
    md['is_demo'] = True
    
    output.append("DO $$ BEGIN")
    output.append(f"  UPDATE incidents SET")
    output.append(f"    alert_metadata = {escape_val(md)},")
    output.append(f"    correlated_alert_count = {escape_val(row.get('correlated_alert_count', 0))},")
    output.append(f"    affected_services = {escape_val(row.get('affected_services'))},")
    output.append(f"    visualization_code = {escape_val(row.get('visualization_code'))},")
    output.append(f"    visualization_updated_at = {escape_val(row.get('visualization_updated_at'))}")
    output.append(f"  WHERE id = '{INCIDENT_ID}';")
    output.append("EXCEPTION WHEN undefined_column THEN NULL;")
    output.append("END $$;")
output.append("")
print(f"incidents: {len(rows)}", file=sys.stderr)

# 3. incident_alerts
rows = psql_json(f"SELECT * FROM incident_alerts WHERE incident_id = '{SRC_INCIDENT_ID}'")
output.append("-- 3. Incident alerts")
for row in rows:
    row['incident_id'] = INCIDENT_ID
    row['user_id'] = DEMO_USER_ID
    row['org_id'] = DEMO_ORG_ID
    row['source_alert_id'] = NR_EVENT_ID
    row['id'] = '000cec6c-a366-42ea-918c-834611e0fc39'
    output.append(gen_insert('incident_alerts', row, INCIDENT_ALERT_BASE_COLS))
output.append("")
print(f"incident_alerts: {len(rows)}", file=sys.stderr)

# 4. incident_thoughts
rows = psql_json(f"SELECT * FROM incident_thoughts WHERE incident_id = '{SRC_INCIDENT_ID}' ORDER BY created_at")
output.append("-- 4. Incident thoughts")
for row in rows:
    row['incident_id'] = INCIDENT_ID
    for k in ['id', 'org_id']:
        row.pop(k, None)
    output.append(gen_insert('incident_thoughts', row, THOUGHT_BASE_COLS))
output.append("")
print(f"incident_thoughts: {len(rows)}", file=sys.stderr)

# 5. incident_citations
rows = psql_json(f"SELECT * FROM incident_citations WHERE incident_id = '{SRC_INCIDENT_ID}' ORDER BY created_at")
output.append("-- 5. Incident citations")
for row in rows:
    row['incident_id'] = INCIDENT_ID
    for k in ['id', 'org_id']:
        row.pop(k, None)
    output.append(gen_insert('incident_citations', row, CITATION_BASE_COLS))
output.append("")
print(f"incident_citations: {len(rows)}", file=sys.stderr)

# 6. incident_suggestions
rows = psql_json(f"SELECT * FROM incident_suggestions WHERE incident_id = '{SRC_INCIDENT_ID}' ORDER BY created_at")
output.append("-- 6. Incident suggestions")
for row in rows:
    row['incident_id'] = INCIDENT_ID
    for k in ['id', 'org_id']:
        row.pop(k, None)
    output.append(gen_insert('incident_suggestions', row, SUGGESTION_BASE_COLS))
output.append("")
print(f"incident_suggestions: {len(rows)}", file=sys.stderr)

# 7. chat_sessions
if src_chat_id:
    rows = psql_json(f"SELECT * FROM chat_sessions WHERE id = '{src_chat_id}'")
else:
    rows = []
output.append("-- 7. Chat session")
for row in rows:
    row['id'] = CHAT_SESSION_ID
    row['user_id'] = DEMO_USER_ID
    row['org_id'] = DEMO_ORG_ID
    row['incident_id'] = INCIDENT_ID
    output.append(gen_insert('chat_sessions', row, CHAT_BASE_COLS))
output.append("")
print(f"chat_sessions: {len(rows)}", file=sys.stderr)

sql = '\n'.join(output)
with open('demo-data/demo-incident-newrelic.sql', 'w') as f:
    f.write(sql)
print(f"\nSQL size: {len(sql)} bytes", file=sys.stderr)
