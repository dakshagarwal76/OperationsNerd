"""
HR module: the "who is away" query, hr.leave_out and GET /hr/leave/out. It is
read only, so these tests also check that it changes nothing. Run from the repo
root with the venv (pytest also collects these):

    python -B modules/hr/tests/test_leave_out.py
"""

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import hr as h
from test_employees import dump, hire, rejected
from test_leave import MON, TUE, WED, THU, FRI, TODAY, shop
import test_api as ta

NEXT_MON, NEXT_TUE = "2026-11-09", "2026-11-10"


def request(conn, emp, start, end, key, approve=True, **kw):
    r = h.request_leave(conn, emp, "unpaid", start, end, key, as_of="2026-10-26", **kw)
    if approve:
        h.approve_leave(conn, r["request_id"], f"ok:{key}", as_of="2026-10-26")
    return r["request_id"]


def out(conn, start, end, **kw):
    return [(r["employee_id"], r["start_date"], r["end_date"], r["status"]) for r in h.leave_out(conn, start, end, **kw)]


def test_an_approved_request_inside_the_range_is_listed_with_the_person():
    conn, _ = shop("out_basic")
    hire(conn, "E003", first_name="Ana", last_name="Diaz", department="Shop floor")
    rid = request(conn, 3, TUE, WED, "r1")
    rows = h.leave_out(conn, MON, FRI)
    assert [r["request_id"] for r in rows] == [rid]
    assert rows[0]["employee_name"] == "Ana Diaz" and rows[0]["department"] == "Shop floor"
    assert rows[0]["leave_type"] == "unpaid" and rows[0]["days"] == 2 and rows[0]["status"] == "approved"


def test_pending_requests_show_only_when_asked_for():
    conn, _ = shop("out_pending")
    request(conn, 2, TUE, TUE, "p1", approve=False)
    assert out(conn, MON, FRI) == []
    assert out(conn, MON, FRI, include_pending=True) == [(2, TUE, TUE, "pending")]


def test_rejected_and_cancelled_requests_never_show():
    conn, _ = shop("out_gone")
    a = request(conn, 2, TUE, TUE, "a", approve=False)
    b = request(conn, 2, WED, WED, "b", approve=False)
    h.reject_leave(conn, a, "no", "rej:a", as_of="2026-10-26")
    h.cancel_leave(conn, b, "can:b", as_of=TODAY)
    assert out(conn, MON, FRI, include_pending=True) == []


def test_a_request_that_only_touches_the_range_is_listed_whole():
    conn, _ = shop("out_touch")
    request(conn, 2, MON, WED, "t1")
    assert out(conn, WED, FRI) == [(2, MON, WED, "approved")]     # ends on the first day of the range
    assert out(conn, MON, MON) == [(2, MON, WED, "approved")]     # a one day range inside the request
    assert out(conn, THU, FRI) == []                              # starts after the request ends


def test_the_edges_of_the_range_are_inclusive():
    conn, _ = shop("out_edges")
    request(conn, 2, THU, THU, "e1")
    assert out(conn, THU, THU) == [(2, THU, THU, "approved")]
    assert out(conn, WED, WED) == [] and out(conn, FRI, FRI) == []


def test_rows_are_ordered_by_start_date_then_employee_then_request():
    conn, _ = shop("out_order")
    hire(conn, "E003")
    request(conn, 3, TUE, TUE, "o3")
    request(conn, 2, MON, MON, "o1")
    request(conn, 2, WED, WED, "o4")
    assert [(r[0], r[1]) for r in out(conn, MON, FRI)] == [(2, MON), (3, TUE), (2, WED)]


def test_half_day_requests_report_half_a_day():
    conn, _ = shop("out_half")
    rid = request(conn, 2, TUE, TUE, "h1", half_day=True, half="pm")
    row = h.leave_out(conn, TUE, TUE)[0]
    assert row["request_id"] == rid and row["days"] == 0.5 and row["half"] == "pm"


def test_a_terminated_employees_cancelled_leave_is_not_listed():
    conn, _ = shop("out_term")
    request(conn, 2, NEXT_MON, NEXT_TUE, "x1")
    h.terminate_employee(conn, 2, "2026-11-04", "left", "term:2")
    assert out(conn, NEXT_MON, NEXT_TUE) == []


def test_a_range_with_nothing_in_it_is_an_empty_list():
    conn, _ = shop("out_empty")
    assert h.leave_out(conn, MON, FRI) == []


def test_bad_ranges_and_flags_are_refused():
    conn, _ = shop("out_bad")
    rejected(lambda: h.leave_out(conn, FRI, MON), "before start_date", ValueError)
    rejected(lambda: h.leave_out(conn, "2026-02-30", MON), "YYYY-MM-DD", ValueError)
    rejected(lambda: h.leave_out(conn, MON, None), "YYYY-MM-DD", ValueError)
    rejected(lambda: h.leave_out(conn, MON, FRI, include_pending="yes"), "true or false", ValueError)


def test_the_query_changes_nothing():
    conn, _ = shop("out_readonly")
    request(conn, 2, TUE, WED, "ro1")
    before = dump(conn)
    h.leave_out(conn, MON, FRI, include_pending=True)
    assert dump(conn) == before


def test_the_api_lists_who_is_out_and_refuses_bad_input():
    s = ta.seeded("out_api")
    s.write("POST", "/hr/leave/types", {"code": "unpaid", "name": "Unpaid", "paid": False, "tracks_balance": False}, expect=201)
    r = s.write("POST", "/hr/leave/requests", {"employee_id": 1, "leave_type": "unpaid", "start_date": TUE,
                                               "end_date": TUE, "as_of": "2026-10-26"}, expect=201).json()
    assert s.get(f"/hr/leave/out?start_date={MON}&end_date={FRI}") == []
    got = s.get(f"/hr/leave/out?start_date={MON}&end_date={FRI}&include_pending=true")
    assert [g["request_id"] for g in got] == [r["request_id"]] and got[0]["status"] == "pending"
    s.write("POST", f"/hr/leave/requests/{r['request_id']}/approve", {}, expect=200)
    assert len(s.get(f"/hr/leave/out?start_date={MON}&end_date={FRI}")) == 1
    s.get(f"/hr/leave/out?start_date={FRI}&end_date={MON}", expect=422)
    s.get("/hr/leave/out?start_date=2026-02-30&end_date=2026-03-01", expect=422)
    s.get(f"/hr/leave/out?start_date={MON}", expect=422)


if __name__ == "__main__":
    print(f"Python {sys.version.split()[0]}, SQLite {sqlite3.sqlite_version}")
    tests = [(n, f) for n, f in globals().items() if n.startswith("test_") and callable(f)]
    for name, fn in tests:
        fn()
        print(f"PASS {name}")
    print(f"\n{len(tests)} tests passed")
