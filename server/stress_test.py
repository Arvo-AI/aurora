"""
Stress test: 5 users in Default Org -> org creation + invitations + joins.
Runs entirely via HTTP API calls against the Flask backend.

Scenario:
  1. Alice creates "Acme Corp" -> her data + connectors fully migrate
  2. Alice invites Bob -> Bob accepts -> incidents migrate, connectors DELETED
  3. Alice invites Carol -> Carol accepts -> same
  4. Dave creates "Dave Corp" -> his data fully migrates
  5. Eve (last in Default Org) creates "Eve Corp" -> Default Org cleaned up
  6. Final DB audit
"""
import requests
import psycopg2
import os
import sys
import json

BASE = "http://localhost:5080"
DB_CONF = dict(
    host=os.environ.get("POSTGRES_HOST", "postgres"),
    port=5432,
    dbname="aurora_db",
    user="aurora",
    password=os.environ.get("POSTGRES_PASSWORD", ""),
)

USERS = {
    "alice": ("alice@acme.com", "a1000000-0000-0000-0000-000000000001"),
    "bob":   ("bob@acme.com",   "b2000000-0000-0000-0000-000000000002"),
    "carol": ("carol@acme.com", "c3000000-0000-0000-0000-000000000003"),
    "dave":  ("dave@acme.com",  "d4000000-0000-0000-0000-000000000004"),
    "eve":   ("eve@acme.com",   "e5000000-0000-0000-0000-000000000005"),
}
DEFAULT_ORG = "d0000000-0000-0000-0000-000000000001"

errors = []

def fail(msg):
    errors.append(msg)
    print(f"  FAIL: {msg}")

def ok(msg):
    print(f"  OK: {msg}")

def login(name):
    email, uid = USERS[name]
    r = requests.post(f"{BASE}/api/auth/login", json={"email": email, "password": "password123"})
    if r.status_code != 200:
        fail(f"{name} login failed: {r.status_code} {r.text}")
        return None, None
    data = r.json()
    return data.get("id"), data.get("orgId")

def headers(uid, org_id):
    return {"X-User-ID": uid, "X-Org-ID": org_id or ""}

def db_query(sql, params=None):
    conn = psycopg2.connect(**DB_CONF)
    cur = conn.cursor()
    cur.execute(sql, params or ())
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows

def count_user_data(uid):
    incidents = db_query("SELECT count(*) FROM incidents WHERE user_id=%s", (uid,))[0][0]
    conns = db_query("SELECT count(*) FROM user_connections WHERE user_id=%s", (uid,))[0][0]
    tokens = db_query("SELECT count(*) FROM user_tokens WHERE user_id=%s", (uid,))[0][0]
    chats = db_query("SELECT count(*) FROM chat_sessions WHERE user_id=%s", (uid,))[0][0]
    return {"incidents": incidents, "connections": conns, "tokens": tokens, "chats": chats}

def count_user_data_in_org(uid, org_id):
    incidents = db_query("SELECT count(*) FROM incidents WHERE user_id=%s AND org_id=%s", (uid, org_id))[0][0]
    conns = db_query("SELECT count(*) FROM user_connections WHERE user_id=%s AND org_id=%s", (uid, org_id))[0][0]
    tokens = db_query("SELECT count(*) FROM user_tokens WHERE user_id=%s AND org_id=%s", (uid, org_id))[0][0]
    chats = db_query("SELECT count(*) FROM chat_sessions WHERE user_id=%s AND org_id=%s", (uid, org_id))[0][0]
    return {"incidents": incidents, "connections": conns, "tokens": tokens, "chats": chats}

def user_org(uid):
    rows = db_query("SELECT org_id FROM users WHERE id=%s", (uid,))
    return rows[0][0] if rows else None

def org_exists(org_id):
    rows = db_query("SELECT count(*) FROM organizations WHERE id=%s", (org_id,))
    return rows[0][0] > 0

def org_member_count(org_id):
    rows = db_query("SELECT count(*) FROM users WHERE org_id=%s", (org_id,))
    return rows[0][0]

def print_separator(title):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


# ============================================================
# STEP 1: Alice creates "Acme Corp"
# ============================================================
print_separator("STEP 1: Alice creates Acme Corp")

alice_uid, alice_org = login("alice")
print(f"  Alice logged in: uid={alice_uid}, org={alice_org}")

# She should be in Default Org
if alice_org != DEFAULT_ORG:
    fail(f"Alice should be in Default Org but is in {alice_org}")
else:
    ok("Alice is in Default Organization")

# Create Acme Corp
r = requests.post(
    f"{BASE}/api/auth/setup-org",
    json={"org_name": "Acme Corp"},
    headers=headers(alice_uid, alice_org),
)
print(f"  setup-org response: {r.status_code} {r.text[:200]}")

if r.status_code not in (200, 201):
    fail(f"Alice failed to create Acme Corp: {r.status_code}")
else:
    ok("Alice created Acme Corp")

# Get Alice's new org
acme_org = user_org(alice_uid)
print(f"  Alice new org: {acme_org}")

if acme_org == DEFAULT_ORG:
    fail("Alice is still in Default Org after creating Acme Corp!")

# Verify Alice's data migrated to new org
alice_data = count_user_data_in_org(alice_uid, acme_org)
print(f"  Alice data in Acme Corp: {alice_data}")

if alice_data["incidents"] != 3:
    fail(f"Alice should have 3 incidents in Acme Corp, got {alice_data['incidents']}")
else:
    ok("Alice has 3 incidents in Acme Corp")

if alice_data["connections"] != 1:
    fail(f"Alice should have 1 connection (GCP) in Acme Corp, got {alice_data['connections']}")
else:
    ok("Alice has 1 GCP connection in Acme Corp (migrated with her)")

if alice_data["tokens"] != 1:
    fail(f"Alice should have 1 token in Acme Corp, got {alice_data['tokens']}")
else:
    ok("Alice has 1 GCP token in Acme Corp")

if alice_data["chats"] != 2:
    fail(f"Alice should have 2 chats in Acme Corp, got {alice_data['chats']}")
else:
    ok("Alice has 2 chat sessions in Acme Corp")

# Verify Default Org still has other 4 users
default_members = org_member_count(DEFAULT_ORG)
print(f"  Default Org members remaining: {default_members}")
if default_members != 4:
    fail(f"Default Org should have 4 members, got {default_members}")
else:
    ok("Default Org has 4 remaining members (Bob, Carol, Dave, Eve)")


# ============================================================
# STEP 2: Alice invites Bob, Bob accepts
# ============================================================
print_separator("STEP 2: Alice invites Bob -> Bob accepts")

# Re-login Alice with new org
alice_uid, alice_org = login("alice")
print(f"  Alice logged in: uid={alice_uid}, org={alice_org}")

# Invite Bob
r = requests.post(
    f"{BASE}/api/admin/users",
    json={"email": "bob@acme.com", "role": "viewer"},
    headers=headers(alice_uid, alice_org),
)
print(f"  Invite Bob: {r.status_code} {r.text[:200]}")
if r.status_code not in (200, 201):
    fail(f"Failed to invite Bob: {r.status_code}")
else:
    ok("Alice invited Bob to Acme Corp")

# Bob's data BEFORE join
bob_uid = USERS["bob"][1]
bob_before = count_user_data(bob_uid)
print(f"  Bob data BEFORE join: {bob_before}")

# Get Bob's invitation
bob_uid_login, bob_org = login("bob")
r = requests.get(
    f"{BASE}/api/orgs/my-invitations",
    headers=headers(bob_uid_login, bob_org),
)
print(f"  Bob invitations: {r.status_code}")
inv_data = r.json() if r.status_code == 200 else {}
invitations = inv_data.get("invitations", [])
print(f"  Found {len(invitations)} invitations")

if not invitations:
    fail("Bob has no invitations!")
else:
    invite = invitations[0]
    invite_id = invite.get("id")
    print(f"  Accepting invitation {invite_id}...")

    r = requests.post(
        f"{BASE}/api/orgs/join",
        json={"invitation_id": invite_id},
        headers=headers(bob_uid_login, bob_org),
    )
    print(f"  Join response: {r.status_code} {r.text[:200]}")
    if r.status_code != 200:
        fail(f"Bob failed to join: {r.status_code}")
    else:
        ok("Bob accepted invitation to Acme Corp")

# Verify Bob's new org
bob_new_org = user_org(bob_uid)
print(f"  Bob new org: {bob_new_org}")
if bob_new_org != acme_org:
    fail(f"Bob should be in Acme Corp ({acme_org}) but is in {bob_new_org}")
else:
    ok("Bob is now in Acme Corp")

# Bob's incidents should have migrated
bob_acme = count_user_data_in_org(bob_uid, acme_org)
print(f"  Bob data in Acme Corp: {bob_acme}")

if bob_acme["incidents"] != 2:
    fail(f"Bob should have 2 incidents in Acme Corp, got {bob_acme['incidents']}")
else:
    ok("Bob has 2 incidents in Acme Corp (migrated)")

if bob_acme["chats"] != 1:
    fail(f"Bob should have 1 chat in Acme Corp, got {bob_acme['chats']}")
else:
    ok("Bob has 1 chat session in Acme Corp (migrated)")

# Bob's connections/tokens should be DELETED (not migrated)
if bob_acme["connections"] != 0:
    fail(f"Bob should have 0 connections in Acme Corp (deleted), got {bob_acme['connections']}")
else:
    ok("Bob has 0 connections in Acme Corp (connectors deleted, not migrated)")

if bob_acme["tokens"] != 0:
    fail(f"Bob should have 0 tokens in Acme Corp (deleted), got {bob_acme['tokens']}")
else:
    ok("Bob has 0 tokens in Acme Corp (tokens deleted, not migrated)")

# Bob's old connections should also be gone from Default Org
bob_default = count_user_data_in_org(bob_uid, DEFAULT_ORG)
print(f"  Bob data in Default Org: {bob_default}")
if bob_default["connections"] != 0:
    fail(f"Bob should have 0 connections in Default Org, got {bob_default['connections']}")
else:
    ok("Bob has 0 connections in Default Org (deleted)")

if bob_default["tokens"] != 0:
    fail(f"Bob should have 0 tokens in Default Org, got {bob_default['tokens']}")
else:
    ok("Bob has 0 tokens in Default Org (deleted)")

# Bob's old incidents should not be in Default Org
if bob_default["incidents"] != 0:
    fail(f"Bob should have 0 incidents in Default Org (migrated away), got {bob_default['incidents']}")
else:
    ok("Bob has 0 incidents in Default Org (migrated to Acme)")

# Default Org member count
default_members = org_member_count(DEFAULT_ORG)
print(f"  Default Org members remaining: {default_members}")
if default_members != 3:
    fail(f"Default Org should have 3 members, got {default_members}")
else:
    ok("Default Org has 3 remaining members (Carol, Dave, Eve)")


# ============================================================
# STEP 3: Alice invites Carol, Carol accepts
# ============================================================
print_separator("STEP 3: Alice invites Carol -> Carol accepts")

r = requests.post(
    f"{BASE}/api/admin/users",
    json={"email": "carol@acme.com", "role": "viewer"},
    headers=headers(alice_uid, alice_org),
)
print(f"  Invite Carol: {r.status_code}")
if r.status_code not in (200, 201):
    fail(f"Failed to invite Carol: {r.status_code}")
else:
    ok("Alice invited Carol to Acme Corp")

carol_uid = USERS["carol"][1]
carol_before = count_user_data(carol_uid)
print(f"  Carol data BEFORE join: {carol_before}")

carol_uid_login, carol_org = login("carol")
r = requests.get(f"{BASE}/api/orgs/my-invitations", headers=headers(carol_uid_login, carol_org))
inv_data = r.json() if r.status_code == 200 else {}
invitations = inv_data.get("invitations", [])
print(f"  Carol has {len(invitations)} invitation(s)")

if not invitations:
    fail("Carol has no invitations!")
else:
    invite_id = invitations[0].get("id")
    r = requests.post(
        f"{BASE}/api/orgs/join",
        json={"invitation_id": invite_id},
        headers=headers(carol_uid_login, carol_org),
    )
    print(f"  Join response: {r.status_code} {r.text[:200]}")
    if r.status_code != 200:
        fail(f"Carol failed to join: {r.status_code}")
    else:
        ok("Carol accepted invitation to Acme Corp")

carol_new_org = user_org(carol_uid)
if carol_new_org != acme_org:
    fail(f"Carol should be in Acme Corp but is in {carol_new_org}")
else:
    ok("Carol is now in Acme Corp")

carol_acme = count_user_data_in_org(carol_uid, acme_org)
print(f"  Carol data in Acme Corp: {carol_acme}")

if carol_acme["incidents"] != 2:
    fail(f"Carol should have 2 incidents, got {carol_acme['incidents']}")
else:
    ok("Carol has 2 incidents in Acme Corp (migrated)")

if carol_acme["chats"] != 1:
    fail(f"Carol should have 1 chat, got {carol_acme['chats']}")
else:
    ok("Carol has 1 chat in Acme Corp (migrated)")

if carol_acme["connections"] != 0:
    fail(f"Carol should have 0 connections (Azure+Datadog DELETED), got {carol_acme['connections']}")
else:
    ok("Carol has 0 connections in Acme Corp (Azure+Datadog deleted)")

if carol_acme["tokens"] != 0:
    fail(f"Carol should have 0 tokens, got {carol_acme['tokens']}")
else:
    ok("Carol has 0 tokens in Acme Corp (deleted)")

carol_default = count_user_data_in_org(carol_uid, DEFAULT_ORG)
print(f"  Carol data in Default Org: {carol_default}")
if carol_default["connections"] != 0 or carol_default["tokens"] != 0:
    fail(f"Carol still has ghost data in Default Org: {carol_default}")
else:
    ok("Carol has no ghost data in Default Org")

default_members = org_member_count(DEFAULT_ORG)
print(f"  Default Org members remaining: {default_members}")
if default_members != 2:
    fail(f"Default Org should have 2 members, got {default_members}")
else:
    ok("Default Org has 2 remaining members (Dave, Eve)")


# ============================================================
# STEP 4: Dave creates "Dave Corp"
# ============================================================
print_separator("STEP 4: Dave creates Dave Corp")

dave_uid_login, dave_org = login("dave")
print(f"  Dave logged in: uid={dave_uid_login}, org={dave_org}")

dave_uid = USERS["dave"][1]
dave_before = count_user_data(dave_uid)
print(f"  Dave data BEFORE: {dave_before}")

r = requests.post(
    f"{BASE}/api/auth/setup-org",
    json={"org_name": "Dave Corp"},
    headers=headers(dave_uid_login, dave_org),
)
print(f"  setup-org: {r.status_code} {r.text[:200]}")
if r.status_code not in (200, 201):
    fail(f"Dave failed to create Dave Corp: {r.status_code}")
else:
    ok("Dave created Dave Corp")

dave_new_org = user_org(dave_uid)
print(f"  Dave new org: {dave_new_org}")
if dave_new_org == DEFAULT_ORG:
    fail("Dave is still in Default Org!")

dave_data = count_user_data_in_org(dave_uid, dave_new_org)
print(f"  Dave data in Dave Corp: {dave_data}")

if dave_data["incidents"] != 1:
    fail(f"Dave should have 1 incident, got {dave_data['incidents']}")
else:
    ok("Dave has 1 incident in Dave Corp (migrated)")

if dave_data["connections"] != 1:
    fail(f"Dave should have 1 connection (GCP), got {dave_data['connections']}")
else:
    ok("Dave has 1 GCP connection in Dave Corp (fully migrated as creator)")

if dave_data["tokens"] != 1:
    fail(f"Dave should have 1 token, got {dave_data['tokens']}")
else:
    ok("Dave has 1 GCP token in Dave Corp (fully migrated as creator)")

if dave_data["chats"] != 1:
    fail(f"Dave should have 1 chat, got {dave_data['chats']}")
else:
    ok("Dave has 1 chat in Dave Corp")

default_members = org_member_count(DEFAULT_ORG)
print(f"  Default Org members remaining: {default_members}")
if default_members != 1:
    fail(f"Default Org should have 1 member (Eve), got {default_members}")
else:
    ok("Default Org has 1 remaining member (Eve)")


# ============================================================
# STEP 5: Eve creates "Eve Corp" (last person -> Default Org cleaned up)
# ============================================================
print_separator("STEP 5: Eve creates Eve Corp (last in Default Org)")

eve_uid_login, eve_org = login("eve")
print(f"  Eve logged in: uid={eve_uid_login}, org={eve_org}")

eve_uid = USERS["eve"][1]
eve_before = count_user_data(eve_uid)
print(f"  Eve data BEFORE: {eve_before}")

r = requests.post(
    f"{BASE}/api/auth/setup-org",
    json={"org_name": "Eve Corp"},
    headers=headers(eve_uid_login, eve_org),
)
print(f"  setup-org: {r.status_code} {r.text[:200]}")
if r.status_code not in (200, 201):
    fail(f"Eve failed to create Eve Corp: {r.status_code}")
else:
    ok("Eve created Eve Corp")

eve_new_org = user_org(eve_uid)
print(f"  Eve new org: {eve_new_org}")

eve_data = count_user_data_in_org(eve_uid, eve_new_org)
print(f"  Eve data in Eve Corp: {eve_data}")

if eve_data["incidents"] != 2:
    fail(f"Eve should have 2 incidents, got {eve_data['incidents']}")
else:
    ok("Eve has 2 incidents in Eve Corp")

if eve_data["connections"] != 1:
    fail(f"Eve should have 1 connection (Splunk), got {eve_data['connections']}")
else:
    ok("Eve has 1 Splunk connection in Eve Corp (migrated as creator)")

if eve_data["tokens"] != 1:
    fail(f"Eve should have 1 token (Grafana), got {eve_data['tokens']}")
else:
    ok("Eve has 1 Grafana token in Eve Corp (migrated as creator)")

if eve_data["chats"] != 2:
    fail(f"Eve should have 2 chats, got {eve_data['chats']}")
else:
    ok("Eve has 2 chats in Eve Corp")

# Default Org should be cleaned up (0 members -> deleted)
if org_exists(DEFAULT_ORG):
    remaining = org_member_count(DEFAULT_ORG)
    if remaining == 0:
        fail("Default Org exists with 0 members (should have been cleaned up!)")
    else:
        fail(f"Default Org still exists with {remaining} members")
else:
    ok("Default Organization was cleaned up (deleted after last member left)")


# ============================================================
# STEP 6: Final audit
# ============================================================
print_separator("FINAL AUDIT")

# Check no data remains in Default Org
for label, tbl in [("incidents","incidents"), ("connections","user_connections"), ("tokens","user_tokens"), ("chats","chat_sessions")]:
    rows = db_query(f"SELECT count(*) FROM {tbl} WHERE org_id=%s", (DEFAULT_ORG,))
    cnt = rows[0][0]
    if cnt > 0:
        fail(f"{cnt} orphan {label} still in Default Org!")
    else:
        ok(f"No orphan {label} in Default Org")

# Check Acme Corp totals
acme_incidents = db_query("SELECT count(*) FROM incidents WHERE org_id=%s", (acme_org,))[0][0]
acme_conns = db_query("SELECT count(*) FROM user_connections WHERE org_id=%s", (acme_org,))[0][0]
acme_tokens = db_query("SELECT count(*) FROM user_tokens WHERE org_id=%s", (acme_org,))[0][0]
acme_chats = db_query("SELECT count(*) FROM chat_sessions WHERE org_id=%s", (acme_org,))[0][0]
print(f"\n  Acme Corp totals: incidents={acme_incidents}, conns={acme_conns}, tokens={acme_tokens}, chats={acme_chats}")
# Alice(3)+Bob(2)+Carol(2) = 7 incidents
if acme_incidents != 7:
    fail(f"Acme Corp should have 7 incidents (3+2+2), got {acme_incidents}")
else:
    ok("Acme Corp: 7 incidents (Alice:3 + Bob:2 + Carol:2)")
# Only Alice's GCP conn migrated, Bob+Carol connectors deleted
if acme_conns != 1:
    fail(f"Acme Corp should have 1 connection (Alice GCP only), got {acme_conns}")
else:
    ok("Acme Corp: 1 connection (only Alice's GCP)")
if acme_tokens != 1:
    fail(f"Acme Corp should have 1 token (Alice GCP only), got {acme_tokens}")
else:
    ok("Acme Corp: 1 token (only Alice's GCP)")
# Alice(2)+Bob(1)+Carol(1) = 4 chats
if acme_chats != 4:
    fail(f"Acme Corp should have 4 chats (2+1+1), got {acme_chats}")
else:
    ok("Acme Corp: 4 chat sessions (Alice:2 + Bob:1 + Carol:1)")

# All orgs
all_orgs = db_query("SELECT name FROM organizations ORDER BY name")
print(f"\n  All organizations: {[r[0] for r in all_orgs]}")

# Connector status check: simulate what the API would return for each user
print(f"\n  --- Connector status (ghost reference check) ---")
for name in ["alice", "bob", "carol", "dave", "eve"]:
    uid = USERS[name][1]
    org = user_org(uid)
    r = requests.get(f"{BASE}/api/connectors/status", headers=headers(uid, org))
    if r.status_code == 200:
        connectors = r.json().get("connectors", {})
        connected = [k for k, v in connectors.items() if v.get("connected")]
        print(f"  {name}: connected={connected}")
    else:
        print(f"  {name}: status check failed {r.status_code}")


# ============================================================
# RESULTS
# ============================================================
print_separator("RESULTS")
if errors:
    print(f"\n  {len(errors)} FAILURES:")
    for e in errors:
        print(f"    - {e}")
    sys.exit(1)
else:
    print("\n  ALL TESTS PASSED!")
    sys.exit(0)
