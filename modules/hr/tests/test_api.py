"""
HR module: the FastAPI router (api.py) over a throwaway database, and the
additive hook in operations_nerd/main.py that mounts it only when
HR_MODULE_ENABLED=1. Run from the repo root with the venv (pytest also
collects these):

    python -B modules/hr/tests/test_api.py
"""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(HERE.parent))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import api  # noqa: E402

TMP = tempfile.TemporaryDirectory(prefix="hr_api_", ignore_cleanup_errors=True)
# Cancelling approved leave depends on the date (see hr.cancel_leave); pin it so these tests don't age.
api.today = lambda: "2026-10-26"


def client(name: str) -> TestClient:
    os.environ["HR_DATABASE_PATH"] = os.path.join(TMP.name, name + ".db")
    app = FastAPI()
    app.include_router(api.router)
    return TestClient(app)


class Calls:
    """Each write gets a fresh Idempotency-Key unless the test passes one."""

    def __init__(self, c: TestClient):
        self.c, self.n = c, 0

    def write(self, method, url, body, key=None, expect=None):
        self.n += 1
        r = self.c.request(method, url, json=body, headers={"Idempotency-Key": key or f"k{self.n}"})
        if expect is not None:
            assert r.status_code == expect, (r.status_code, r.text)
        return r

    def get(self, url, expect=200):
        r = self.c.get(url)
        assert r.status_code == expect, (r.status_code, r.text)
        return r.json()


def seeded(name: str) -> Calls:
    s = Calls(client(name))
    s.write("POST", "/hr/employees", {"employee_number": "E001", "first_name": "Maya", "job_title": "Owner",
                                      "start_date": "2024-01-08"}, expect=201)
    s.write("POST", "/hr/employees", {"employee_number": "E002", "first_name": "Sam", "job_title": "Shop assistant",
                                      "start_date": "2025-03-03", "manager_id": 1, "employment_type": "part_time"},
            expect=201)
    s.write("POST", "/hr/leave/types", {"code": "annual", "name": "Annual leave", "annual_allowance_days": 20},
            expect=201)
    s.write("POST", "/hr/leave/accruals", {"employee_id": 2, "leave_type": "annual", "year": 2026}, expect=201)
    return s


def test_employee_endpoints():
    s = seeded("employees")
    assert s.get("/hr/schema") == {"module": "hr", "version": 4}
    sam = s.get("/hr/employees/2")
    assert sam["manager_name"] == "Maya" and sam["employment_type"] == "part_time" and sam["status"] == "active"
    assert [e["employee_number"] for e in s.get("/hr/employees")] == ["E001", "E002"]
    r = s.write("PATCH", "/hr/employees/2", {"changes": {"job_title": "Senior shop assistant"},
                                            "effective_date": "2026-06-01", "reason": "promotion"}, expect=200)
    assert r.json()["job_title"] == "Senior shop assistant" and r.json()["version"] == 2
    out = s.write("POST", "/hr/employees/2/terminate", {"end_date": "2026-11-30", "reason": "moved away"},
                  expect=200).json()
    assert out["status"] == "terminated" and out["reassigned_reports"] == [] and out["cancelled_leave"] == []
    assert [e["event"] for e in s.get("/hr/employees/2/history")] == ["hired", "updated", "terminated"]
    assert [e["employee_number"] for e in s.get("/hr/employees?status=active")] == ["E001"]
    s.get("/hr/employees/9", expect=404)


def test_leave_endpoints_and_balance_enforcement():
    s = seeded("leave")
    r = s.write("POST", "/hr/leave/requests", {"employee_id": 2, "leave_type": "annual", "start_date": "2026-11-02",
                                               "end_date": "2026-11-06"}, expect=201).json()
    assert r["days"] == 5 and r["status"] == "pending"
    s.write("POST", f"/hr/leave/requests/{r['request_id']}/approve", {"approver": "Maya"}, expect=200)
    balance = s.get("/hr/employees/2/leave/balances")[0]
    assert (balance["balance"], balance["taken"], balance["available"]) == (15, 5, 15)
    big = s.write("POST", "/hr/leave/requests", {"employee_id": 2, "leave_type": "annual",
                                                 "start_date": "2026-12-01", "end_date": "2026-12-31"}, expect=409)
    assert "InsufficientBalance" in big.json()["detail"]
    overlap = s.write("POST", "/hr/leave/requests", {"employee_id": 2, "leave_type": "annual",
                                                     "start_date": "2026-11-06", "end_date": "2026-11-06"}, expect=409)
    assert "Overlap" in overlap.json()["detail"]
    r2 = s.write("POST", "/hr/leave/requests", {"employee_id": 2, "leave_type": "annual", "start_date": "2026-11-16",
                                                "end_date": "2026-11-16", "half_day": True}, expect=201).json()
    s.write("POST", f"/hr/leave/requests/{r2['request_id']}/reject", {}, expect=422)               # reason required
    s.write("POST", f"/hr/leave/requests/{r2['request_id']}/reject", {"reason": "stocktake"}, expect=200)
    s.write("POST", f"/hr/leave/requests/{r['request_id']}/cancel", {"reason": "trip off"}, expect=200)
    assert s.get("/hr/employees/2/leave/balances")[0]["available"] == 20
    assert [e["entry_type"] for e in s.get("/hr/employees/2/leave/ledger")] == [
        "accrual", "request", "approve", "request", "reject", "cancel"]
    assert {q["request_id"]: q["status"] for q in s.get("/hr/leave/requests?employee_id=2")} == {
        r["request_id"]: "cancelled", r2["request_id"]: "rejected"}
    assert s.get("/hr/leave/requests?status=pending") == []
    s.write("PUT", "/hr/leave/types/annual/allowance", {"annual_allowance_days": 22, "reason": "policy"}, expect=200)
    assert s.get("/hr/leave/types")[0]["annual_allowance_days"] == 22
    s.write("POST", "/hr/leave/adjustments", {"employee_id": 2, "leave_type": "annual", "days": 1.5,
                                              "reason": "carried over"}, expect=201)
    assert s.get("/hr/employees/2/leave/balances")[0]["available"] == 21.5
    s.get("/hr/leave/requests/99", expect=404)


def test_writes_need_an_idempotency_key_and_replay_by_it():
    s = seeded("idempotency")
    body = {"employee_number": "E003", "first_name": "Ana", "job_title": "Cashier", "start_date": "2026-02-02"}
    no_key = s.c.post("/hr/employees", json=body)
    assert no_key.status_code == 422
    first = s.write("POST", "/hr/employees", body, key="hire-ana", expect=201).json()
    again = s.write("POST", "/hr/employees", body, key="hire-ana", expect=201).json()
    assert again == first and len(s.get("/hr/employees")) == 3
    clash = s.write("POST", "/hr/employees", dict(body, first_name="Anna"), key="hire-ana", expect=409)
    assert "IdempotencyConflict" in clash.json()["detail"]


def test_bad_input_maps_to_422_and_unknown_fields_are_refused():
    s = seeded("bad_input")
    s.write("POST", "/hr/employees", {"employee_number": "E009", "first_name": "X", "job_title": "Y",
                                      "start_date": "2026-02-30"}, expect=422)
    s.write("POST", "/hr/employees", {"employee_number": "E009", "first_name": "X", "job_title": "Y",
                                      "start_date": "2026-02-02", "salary": 50000}, expect=422)
    s.write("POST", "/hr/employees", {"employee_number": "E009", "first_name": "X", "job_title": "Y",
                                      "start_date": "2026-02-02", "employment_type": "boss"}, expect=422)
    s.write("PATCH", "/hr/employees/2", {"changes": {"bank_account": "123"}, "effective_date": "2026-06-01"},
            expect=422)
    s.write("POST", "/hr/employees", {"employee_number": "E001", "first_name": "Dup", "job_title": "Y",
                                      "start_date": "2026-02-02"}, expect=409)


def test_no_database_configured_is_a_503():
    c = client("unused")
    os.environ.pop("HR_DATABASE_PATH")
    assert c.get("/hr/employees").status_code == 503


def _routes_of_main(enabled: str) -> dict:
    env = dict(os.environ, LLM_PROVIDER="stub", HR_MODULE_ENABLED=enabled, APP_ENV="development",
               DATABASE_PATH=os.path.join(TMP.name, f"core_{enabled}.db"),
               HR_DATABASE_PATH=os.path.join(TMP.name, f"main_{enabled}.db"))
    code = ("import json, main\n"
            "from fastapi.testclient import TestClient\n"
            "paths = sorted(p for p in main.app.openapi()['paths'] if p.startswith('/hr'))\n"
            "out = {'paths': paths}\n"
            "if paths:\n"
            "    out['schema'] = TestClient(main.app).get('/hr/schema').json()\n"
            "print(json.dumps(out))\n")
    done = subprocess.run([sys.executable, "-B", "-c", code], cwd=REPO / "operations_nerd", env=env,
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_main_mounts_the_router_only_when_enabled():
    on = _routes_of_main("1")
    assert "/hr/employees" in on["paths"] and "/hr/leave/requests/{request_id}/approve" in on["paths"]
    assert on["schema"] == {"module": "hr", "version": 4}
    assert _routes_of_main("0") == {"paths": []}


def test_no_api_body_carries_sensitive_fields():
    """Payroll and sensitive data are deferred (README): no request body has a field for them."""
    banned = {"salary", "wage", "wages", "pay", "payroll", "rate", "tax", "ssn", "sin", "national", "bank", "iban",
              "account", "routing", "dob", "birth", "birthday", "passport", "address", "medical", "health", "gender"}
    models = [m for m in vars(api).values() if isinstance(m, type) and issubclass(m, api._Body) and m is not api._Body]
    assert len(models) == 10
    for model in models:
        for name in model.model_fields:
            assert not banned & set(name.split("_")), (model.__name__, name)



def test_numbers_too_big_for_sqlite_are_a_422_not_a_500():
    """Regression: these used to raise OverflowError inside sqlite3 and come back as a 500. A read by an id no row
    can have is now a plain 404 (hr.py answers NotFound); a write with one is still a 422."""
    s = seeded("too_big")
    huge = 2 ** 70
    s.get(f"/hr/employees/{huge}", expect=404)
    s.get(f"/hr/leave/requests/{huge}", expect=404)
    s.get(f"/hr/employees/{huge}/leave/balances", expect=404)
    s.get(f"/hr/employees/{huge}/history", expect=404)
    assert s.get(f"/hr/leave/requests?employee_id={huge}") == []
    s.write("POST", "/hr/leave/types", {"code": "big", "name": "Big", "annual_allowance_days": 1e300}, expect=422)
    s.write("POST", "/hr/leave/adjustments", {"employee_id": 2, "leave_type": "annual", "days": 1e20,
                                              "reason": "typo"}, expect=422)
    s.write("POST", "/hr/leave/requests", {"employee_id": huge, "leave_type": "annual", "start_date": "2026-11-02",
                                           "end_date": "2026-11-02"}, expect=422)
    s.write("POST", f"/hr/leave/requests/{huge}/approve", {}, expect=422)
    assert s.get("/hr/employees/2/leave/balances")[0]["balance"] == 20



def test_wrong_json_types_are_refused_not_coerced():
    """Regression: pydantic's lax mode turned {"manager_id": true} into manager 1, "2" into 2 and "yes" into True."""
    s = seeded("strict_types")
    hire = {"employee_number": "E003", "first_name": "Lee", "job_title": "Helper", "start_date": "2025-06-02"}
    for bad in ({"manager_id": True}, {"manager_id": "1"}, {"manager_id": 1.0}):
        s.write("POST", "/hr/employees", hire | bad, expect=422)
    request = {"employee_id": 2, "leave_type": "annual", "start_date": "2026-11-02", "end_date": "2026-11-02"}
    for bad in ({"employee_id": True}, {"employee_id": "2"}, {"half_day": "yes"}, {"half_day": 1}):
        s.write("POST", "/hr/leave/requests", request | bad, expect=422)
    s.write("POST", "/hr/leave/adjustments", {"employee_id": 2, "leave_type": "annual", "days": True,
                                              "reason": "x"}, expect=422)
    s.write("POST", "/hr/leave/accruals", {"employee_id": 2, "leave_type": "annual", "year": "2027"}, expect=422)
    assert len(s.get("/hr/employees")) == 2 and s.get("/hr/leave/requests") == []
    # Whole numbers still fill a days field, and the right types still work.
    s.write("POST", "/hr/leave/adjustments", {"employee_id": 2, "leave_type": "annual", "days": 2,
                                              "reason": "bonus"}, expect=201)
    assert s.write("POST", "/hr/employees", hire | {"manager_id": 1}, expect=201).json()["manager_id"] == 1


def test_approve_reject_and_cancel_take_an_empty_body():
    s = seeded("empty_body")
    reqs = [s.write("POST", "/hr/leave/requests", {"employee_id": 2, "leave_type": "annual", "start_date": d,
                                                   "end_date": d}, expect=201).json()["request_id"]
            for d in ("2026-11-02", "2026-11-03", "2026-11-04")]
    for rid, step in zip(reqs, ("approve", "cancel")):
        r = s.c.post(f"/hr/leave/requests/{rid}/{step}", headers={"Idempotency-Key": f"nobody:{rid}"})
        assert r.status_code == 200, r.text                                        # no body at all
    r = s.c.post(f"/hr/leave/requests/{reqs[2]}/reject", headers={"Idempotency-Key": "nobody:rej"})
    assert r.status_code == 422 and "reason is required" in r.text               # reject still needs a reason
    r = s.c.post(f"/hr/leave/requests/{reqs[2]}/approve", content=b"",
                 headers={"Idempotency-Key": "empty", "Content-Type": "application/json"})
    assert r.status_code == 200, r.text                                            # an empty JSON body
    s.write("POST", f"/hr/leave/requests/{reqs[0]}/cancel", {}, expect=200)       # {} as before
    s.write("POST", f"/hr/leave/requests/{reqs[1]}/approve", {"approver": 5}, expect=422)


def test_a_database_that_disappears_is_a_503_and_recovers_when_put_back():
    s = seeded("vanishing")
    path = os.environ["HR_DATABASE_PATH"]
    for suffix in ("-wal", "-shm"):
        if os.path.exists(path + suffix):
            os.remove(path + suffix)
    os.rename(path, path + ".away")
    try:
        r = s.c.get("/hr/employees")
        assert r.status_code == 503 and "can't be opened" in r.text, r.text
        s.write("POST", "/hr/employees", {"employee_number": "E9", "first_name": "A", "job_title": "B",
                                          "start_date": "2026-01-05"}, expect=503)
        assert not os.path.exists(path)                                             # not recreated empty
    finally:
        os.rename(path + ".away", path)
    assert [e["employee_number"] for e in s.get("/hr/employees")] == ["E001", "E002"]


def test_a_tampered_or_older_schema_is_handled_per_request():
    s = seeded("tampered")
    path = os.environ["HR_DATABASE_PATH"]
    raw = sqlite3.connect(path)
    raw.execute("DROP TRIGGER hr_leave_ledger_balance_not_negative")
    raw.commit()
    r = s.c.get("/hr/employees")
    assert r.status_code == 503 and "SchemaTampered" in r.text and "balance_not_negative is missing" in r.text
    # Put the rule back exactly as the migration wrote it: the module works again.
    sql = next(q for q in __import__("migrate")._statements((api.hr.MIGRATIONS / "0002_leave.sql").read_text())
               if "CREATE TRIGGER hr_leave_ledger_balance_not_negative" in q)
    raw.execute(sql)
    raw.commit()
    raw.close()
    assert len(s.get("/hr/employees")) == 2


def test_rehire_and_half_day_endpoints():
    s = seeded("rehire_api")
    s.write("POST", "/hr/employees/2/terminate", {"end_date": "2026-01-30", "reason": "left"}, expect=200)
    s.write("POST", "/hr/employees/2/rehire", {"rehire_date": "2026-01-30"}, expect=422)
    s.write("POST", "/hr/employees/2/rehire", {"rehire_date": "2026-03-02", "salary": 1}, expect=422)
    s.write("POST", "/hr/employees/1/rehire", {"rehire_date": "2026-03-02"}, expect=409)        # 1 is active
    back = s.write("POST", "/hr/employees/2/rehire", {"rehire_date": "2026-03-02", "changes": {"job_title": "Lead"},
                                                      "reason": "came back"}, expect=200).json()
    assert back["status"] == "active" and back["employment_start_date"] == "2026-03-02"
    assert [e["event"] for e in s.get("/hr/employees/2/history")] == ["hired", "terminated", "rehired"]
    none = s.write("POST", "/hr/leave/requests", {"employee_id": 1, "leave_type": "annual", "half_day": True,
                                                  "start_date": "2026-11-02", "end_date": "2026-11-02", "half": "pm"},
                   expect=409)                    # employee 1 has no annual balance yet (seeded accrues 2 only)
    assert "InsufficientBalance" in none.text
    s.write("POST", "/hr/leave/accruals", {"employee_id": 1, "leave_type": "annual", "year": 2026}, expect=201)
    am = s.write("POST", "/hr/leave/requests", {"employee_id": 1, "leave_type": "annual", "start_date": "2026-11-02",
                                                "end_date": "2026-11-02", "half_day": True}, expect=201).json()
    pm = s.write("POST", "/hr/leave/requests", {"employee_id": 1, "leave_type": "annual", "start_date": "2026-11-02",
                                                "end_date": "2026-11-02", "half_day": True, "half": "pm"},
                 expect=201).json()
    assert (am["half"], pm["half"]) == ("am", "pm")
    s.write("POST", "/hr/leave/requests", {"employee_id": 1, "leave_type": "annual", "start_date": "2026-11-03",
                                           "end_date": "2026-11-03", "half_day": True, "half": "noon"}, expect=422)


def test_every_string_and_the_key_have_a_length_limit():
    s = seeded("limits_api")
    long, longer = "x" * 201, "x" * 2001
    hire = {"employee_number": "E9", "first_name": "A", "job_title": "B", "start_date": "2026-01-05"}
    for field in ("employee_number", "first_name", "job_title", "last_name", "preferred_name", "work_email",
                  "work_phone", "department", "work_location"):
        r = s.write("POST", "/hr/employees", hire | {field: long}, expect=422)
        assert "at most 200" in r.text, r.text
    s.write("POST", "/hr/employees", hire | {"start_date": "2026-01-05" * 2}, expect=422)
    s.write("POST", "/hr/employees", hire, key="k" * 201, expect=422)
    s.write("PATCH", "/hr/employees/2", {"changes": {"job_title": long}, "effective_date": "2026-06-01"}, expect=422)
    s.write("PATCH", "/hr/employees/2", {"changes": {"job_title": "X"}, "effective_date": "2026-06-01",
                                         "reason": longer}, expect=422)
    s.write("POST", "/hr/leave/requests", {"employee_id": 2, "leave_type": "annual", "start_date": "2026-11-02",
                                           "end_date": "2026-11-02", "note": longer}, expect=422)
    s.write("POST", "/hr/leave/requests/1/reject", {"reason": longer}, expect=422)
    s.write("PUT", f"/hr/leave/types/{'a' * 201}/allowance", {"annual_allowance_days": 1, "reason": "x"}, expect=422)
    s.get(f"/hr/employees?department={long}", expect=422)
    s.get(f"/hr/employees/2/leave/ledger?leave_type={long}", expect=422)
    # Right at the limits is accepted.
    s.write("POST", "/hr/employees", hire | {"first_name": "y" * 200}, key="k" * 200, expect=201)
    s.write("POST", "/hr/leave/requests", {"employee_id": 2, "leave_type": "annual", "start_date": "2026-11-02",
                                           "end_date": "2026-11-02", "note": "n" * 2000}, expect=201)


if __name__ == "__main__":
    print(f"Python {sys.version.split()[0]}, SQLite {sqlite3.sqlite_version}")
    tests = [(n, f) for n, f in globals().items() if n.startswith("test_") and callable(f)]
    for name, fn in tests:
        fn()
        print(f"PASS {name}")
    print(f"\n{len(tests)} tests passed")
