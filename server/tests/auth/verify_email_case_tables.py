"""Print the verification tables for the email-case-insensitivity fix.

Run with: python3 -m pytest tests/auth/verify_email_case_tables.py -s -q

Lives as a pytest module (not a bare script) so it inherits the heavy-package
stubs in tests/conftest.py — casbin et al. aren't installed in a plain local
env.
"""

import bcrypt

ADMIN_PW, SELF_PW = "AdminChosenPw1", "OwnerChosenPw2"
LOWER, MIXED = "first.last@example.com", "First.Last@example.com"


def _mk(uid, email, pw, created):
    return (uid, email, bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode(), created)


def test_print_tables():
    from routes.auth_routes import _password_matches
    from utils.auth import normalize_email

    rows = [_mk("uid-admin", LOWER, ADMIN_PW, 1), _mk("uid-self", MIXED, SELF_PW, 2)]

    def login(typed, pw):
        raw = typed.strip()
        norm = normalize_email(raw)
        cands = sorted([r for r in rows if r[1].lower() == norm], key=lambda r: (r[1] != raw, r[3]))
        return next((c[0] for c in cands if _password_matches(pw, c[2])), None)

    ok = True
    print("\nTABLE 1 - candidate selection against a simulated duplicate pair")
    print(f"{'typed email':<32}{'password':<16}{'expected':<12}{'got':<12}{'result':<8}note")
    for typed, pw, exp, note in [
        (MIXED, SELF_PW, "uid-self", "owner pw + exact case"),
        (LOWER, SELF_PW, "uid-self", "owner pw + lowercased"),
        (MIXED.upper(), SELF_PW, "uid-self", "owner pw + ALL CAPS"),
        (LOWER, ADMIN_PW, "uid-admin", "admin pw + lowercase"),
        (MIXED, ADMIN_PW, "uid-admin", "admin pw + mixed case"),
        (MIXED.upper(), ADMIN_PW, "uid-admin", "admin pw + ALL CAPS"),
        (LOWER, "WrongPw", None, "wrong password -> 401"),
        ("nobody@nowhere.com", SELF_PW, None, "unknown email -> 401"),
    ]:
        got = login(typed, pw)
        res = "PASS" if got == exp else "FAIL"
        ok &= res == "PASS"
        print(f"{typed:<32}{pw:<16}{str(exp):<12}{str(got):<12}{res:<8}{note}")

    good = bcrypt.hashpw(b"correct-horse", bcrypt.gensalt()).decode()
    print("\nTABLE 2 - _password_matches hardening (must never raise)")
    print(f"{'password':<22}{'hash':<26}{'returns':<10}{'type':<8}result")
    for pw, h, exp in [
        ("correct-horse", good, True),
        ("wrong-horse", good, False),
        (12345, good, False),
        (None, good, False),
        ("correct-horse", "corrupt-hash", False),
        ("correct-horse", good[:20], False),
        ("correct-horse", "", False),
        ("correct-horse", None, False),
        ("x" * 200, good, False),
    ]:
        raised = False
        try:
            got = _password_matches(pw, h)
        except Exception as e:  # noqa: BLE001 — the point is that it must not happen
            got, raised = repr(e), True
        res = "PASS" if (not raised and got is exp) else "FAIL"
        ok &= res == "PASS"
        label = "<200-char str>" if isinstance(pw, str) and len(pw) > 30 else repr(pw)
        hlabel = (repr(h)[:24] if h else repr(h))
        print(f"{label:<22}{hlabel:<26}{str(got):<10}{type(got).__name__:<8}{res}")

    print("\nALL GREEN" if ok else "\nFAILURES PRESENT")
    assert ok
