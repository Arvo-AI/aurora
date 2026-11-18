"""Log templating: stable templates, HTTP status kept as a class, failures recognised."""

from services.watch.logs import normalize


def test_status_class_survives_masking_so_200_and_502_differ():
    ok = normalize('10.244.0.9 - - [17/Nov/2026:21:44:51 +0000] "GET /api/items HTTP/1.1" 200 38 "-" "Wget" "-"')
    bad = normalize('10.244.0.9 - - [17/Nov/2026:21:44:52 +0000] "GET /api/items HTTP/1.1" 502 157 "-" "Wget" "-"')
    assert ok.fingerprint != bad.fingerprint
    assert (ok.http_class, ok.is_error) == ("2xx", False)
    assert (bad.http_class, bad.is_error, bad.is_request_failure) == ("5xx", True, True)


def test_varying_parts_are_masked():
    a = normalize("2026-11-17T22:00:14Z GET /api/items -> timeout")
    b = normalize("2026-11-17T22:09:51Z GET /api/items -> timeout")
    assert a.fingerprint == b.fingerprint
    c = normalize("connect() failed (111: Connection refused) upstream: http://10.96.1.2:8080/items pod api-5bdfdd9bf-488ds")
    assert "<ip>" in c.template and "api-<pod>" in c.template and "10.96" not in c.template


def test_a_count_is_not_an_http_status():
    t = normalize("processed batch of 500 items")
    assert t.http_class is None and not t.is_error


def test_client_timeouts_and_proxy_errors_are_request_failures():
    assert normalize("GET /api/items -> timeout").is_request_failure
    assert normalize("[error] 29#29: *1 connect() failed (111: Connection refused) while connecting to upstream").is_request_failure


def test_internal_errors_are_errors_but_not_request_failures():
    t = normalize("ERROR metrics: push to pushgateway:9091 failed: [Errno -2] Name does not resolve")
    assert t.is_error and not t.is_request_failure


def test_warnings_are_not_errors():
    assert not normalize("WARNING py.warnings: DeprecationWarning: StrictRedis is deprecated").is_error


def test_blank_lines_are_skipped():
    assert normalize("   ") is None


def test_a_huge_hostile_line_is_cheap():
    import time

    line = "a-" * 500_000 + "x" * 9 + "-" + "y" * 5 + " error"
    start = time.perf_counter()
    for _ in range(20):
        normalize(line)
    assert time.perf_counter() - start < 2.0
