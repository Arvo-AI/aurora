import psycopg2
import os
import uuid
import bcrypt

pw_hash = bcrypt.hashpw(b'password123', bcrypt.gensalt()).decode('utf-8')
conn = psycopg2.connect(
    host=os.environ.get("POSTGRES_HOST", "postgres"),
    port=5432,
    dbname="aurora_db",
    user="aurora",
    password=os.environ.get("POSTGRES_PASSWORD", ""),
)
conn.autocommit = False
cur = conn.cursor()

default_org_id = "d0000000-0000-0000-0000-000000000001"
users = [
    ("a1000000-0000-0000-0000-000000000001", "alice@acme.com", "Alice"),
    ("b2000000-0000-0000-0000-000000000002", "bob@acme.com", "Bob"),
    ("c3000000-0000-0000-0000-000000000003", "carol@acme.com", "Carol"),
    ("d4000000-0000-0000-0000-000000000004", "dave@acme.com", "Dave"),
    ("e5000000-0000-0000-0000-000000000005", "eve@acme.com", "Eve"),
]

cur.execute(
    "INSERT INTO users (id,email,password_hash,name,org_id,must_change_password,role) VALUES (%s,%s,%s,%s,NULL,FALSE,%s)",
    (users[0][0], users[0][1], pw_hash, users[0][2], "admin"),
)
cur.execute(
    "INSERT INTO organizations (id,name,slug,created_by) VALUES (%s,%s,%s,%s)",
    (default_org_id, "Default Organization", "default-organization", users[0][0]),
)
cur.execute("UPDATE users SET org_id=%s WHERE id=%s", (default_org_id, users[0][0]))
for uid, email, name in users[1:]:
    cur.execute(
        "INSERT INTO users (id,email,password_hash,name,org_id,must_change_password,role) VALUES (%s,%s,%s,%s,%s,FALSE,%s)",
        (uid, email, pw_hash, name, default_org_id, "admin"),
    )
for uid, _, _ in users:
    cur.execute("INSERT INTO casbin_rule (ptype,v0,v1,v2) VALUES ('g',%s,'admin',%s)", (uid, default_org_id))

alert_counter = 1000

def add_incidents(user_idx, items):
    global alert_counter
    for src, status, sev, title, astatus in items:
        alert_counter += 1
        cur.execute(
            "INSERT INTO incidents (user_id,org_id,source_type,source_alert_id,status,severity,alert_title,aurora_status,started_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,NOW())",
            (users[user_idx][0], default_org_id, src, alert_counter, status, sev, title, astatus),
        )

def add_conn(user_idx, provider, account_id):
    cur.execute(
        "INSERT INTO user_connections (user_id,org_id,provider,account_id,status) VALUES (%s,%s,%s,%s,%s)",
        (users[user_idx][0], default_org_id, provider, account_id, "active"),
    )

def add_token(user_idx, provider):
    cur.execute(
        "INSERT INTO user_tokens (user_id,org_id,provider,is_active) VALUES (%s,%s,%s,TRUE)",
        (users[user_idx][0], default_org_id, provider),
    )

def add_chat(user_idx, title):
    cur.execute(
        "INSERT INTO chat_sessions (id,user_id,org_id,title,is_active) VALUES (%s,%s,%s,%s,TRUE)",
        (str(uuid.uuid4()), users[user_idx][0], default_org_id, title),
    )

# ALICE: 3 incidents, GCP conn+token, 2 chats
add_incidents(0, [
    ("grafana", "investigating", "critical", "Alice: CPU spike", "pending"),
    ("grafana", "analyzed", "high", "Alice: Memory leak", "analyzed"),
    ("pagerduty", "resolved", "medium", "Alice: Disk full", "resolved"),
])
add_conn(0, "gcp", "alice-gcp-proj")
add_token(0, "gcp")
add_chat(0, "Alice: Debug 1")
add_chat(0, "Alice: Debug 2")

# BOB: 2 incidents, AWS conn+token, 1 chat
add_incidents(1, [
    ("datadog", "investigating", "high", "Bob: Lambda timeout", "pending"),
    ("datadog", "resolved", "low", "Bob: API latency", "resolved"),
])
add_conn(1, "aws", "bob-aws-acct")
add_token(1, "aws")
add_chat(1, "Bob: Lambda debug")

# CAROL: 2 incidents, Azure+Datadog conns+tokens, 1 chat
add_incidents(2, [
    ("splunk", "investigating", "critical", "Carol: K8s OOM", "pending"),
    ("netdata", "analyzed", "medium", "Carol: Network congestion", "analyzed"),
])
add_conn(2, "azure", "carol-azure-sub")
add_conn(2, "datadog", "carol-dd-acct")
add_token(2, "azure")
add_token(2, "datadog")
add_chat(2, "Carol: K8s debug")

# DAVE: 1 incident, GCP conn+token, 1 chat
add_incidents(3, [("grafana", "investigating", "high", "Dave: DB slow queries", "pending")])
add_conn(3, "gcp", "dave-gcp-proj")
add_token(3, "gcp")
add_chat(3, "Dave: DB optimization")

# EVE: 2 incidents, Splunk conn, Grafana token, 2 chats
add_incidents(4, [
    ("dynatrace", "investigating", "critical", "Eve: Payment svc down", "pending"),
    ("grafana", "resolved", "low", "Eve: Cache miss rate", "resolved"),
])
add_conn(4, "splunk", "eve-splunk-acct")
add_token(4, "grafana")
add_chat(4, "Eve: Payment debug")
add_chat(4, "Eve: Cache tuning")

conn.commit()
cur.close()
conn.close()
print("Done!")
