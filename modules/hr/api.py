"""
HTTP API for the HR module: a FastAPI router over hr.py's commands and queries.

Every POST/PATCH/PUT needs an Idempotency-Key header, which becomes the command's
idempotency key, so a client can retry safely. Errors map to status codes:
ValueError 422, NotFound 404, IdempotencyConflict / InvalidState / Overlap /
InsufficientBalance and database rule violations 409; a database that is missing,
unreadable or whose schema was tampered with (hr.SchemaTampered) 503.

Every string field and the Idempotency-Key header have a length cap (hr.MAX_TEXT,
or hr.MAX_LONG_TEXT for reasons and notes); hr.py checks the same caps.

Each request checks the database before using it: a file that was set up before
and has since disappeared is a 503 (it is not quietly recreated empty), one
whose schema is behind this code is migrated, and verify_schema() runs every
time.

The database is the file named by HR_DATABASE_PATH. There is no per-business
database routing or auth on main yet, so this router is a local development
surface only: main.py mounts it when HR_MODULE_ENABLED=1 and never when
APP_ENV=production. Once the per-business database and login land, the router
should take the signed-in business's connection instead (see README).
"""

import datetime
import os
import sqlite3
import sys
import threading
from pathlib import Path as _FilePath
from typing import Annotated, Literal, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query
from pydantic import BaseModel, ConfigDict, Field

sys.path.insert(0, str(_FilePath(__file__).resolve().parent))
import hr  # noqa: E402

router = APIRouter(prefix="/hr", tags=["hr"])

_ready: set[str] = set()
_ready_lock = threading.Lock()


def database_path() -> str:
    path = os.environ.get("HR_DATABASE_PATH")
    if not path:
        raise HTTPException(503, "HR_DATABASE_PATH is not set")
    return path


def today() -> str:
    """The date cancel_leave is given. A function so that tests (and a deployment with a business time zone) can
    replace it."""
    return datetime.date.today().isoformat()


def get_conn():
    path = database_path()
    with _ready_lock:
        known = path in _ready
    try:
        # A database this process has already set up is opened without create, so a file that has been deleted
        # (or moved away) is a 503 until it is put back, instead of a new empty database.
        conn = hr.connect(path, check_same_thread=False, create=not known)
    except sqlite3.DatabaseError as e:
        raise HTTPException(503, f"the HR database can't be opened: {e}")
    try:
        _prepare(conn, path, known)
    except BaseException:
        conn.close()
        raise
    try:
        yield conn
    finally:
        conn.close()


def _prepare(conn, path, known):
    try:
        if not known or hr.schema_version(conn) != hr.latest_version():
            with _ready_lock:
                hr.init_db(conn)                     # migrates if needed, then verify_schema()
                _ready.add(path)
        else:
            hr.verify_schema(conn)
    except hr.SchemaTampered as e:
        raise HTTPException(503, f"SchemaTampered: {e}")
    except sqlite3.DatabaseError as e:
        raise HTTPException(503, f"the HR database can't be used: {e}")


def _key(idempotency_key: str = Header(..., alias="Idempotency-Key", max_length=hr.MAX_TEXT)) -> str:
    return idempotency_key


def _call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except hr.NotFound as e:
        raise HTTPException(404, str(e))
    except (hr.IdempotencyConflict, hr.InvalidState, hr.Overlap, hr.InsufficientBalance) as e:
        raise HTTPException(409, f"{type(e).__name__}: {e}")
    except ValueError as e:
        raise HTTPException(422, str(e))
    except OverflowError:
        # A number too big for SQLite's 64-bit integers that hr.py didn't catch first.
        raise HTTPException(422, "a number in the request is too large")
    except sqlite3.IntegrityError as e:
        raise HTTPException(409, f"rejected by the database: {e}")
    except hr.SchemaTampered as e:
        raise HTTPException(503, f"SchemaTampered: {e}")
    except sqlite3.DatabaseError as e:
        # The file went away, became unreadable or stayed locked: not the client's fault, and not a crash.
        raise HTTPException(503, f"the HR database can't be used: {e}")


class _Body(BaseModel):
    # strict: no silent coercion, so {"manager_id": true} or {"employee_id": "2"} is a 422 instead of
    # becoming employee 1 or 2 (hr.py refuses bools for ids too). JSON whole numbers still fill float fields.
    model_config = ConfigDict(extra="forbid", strict=True)


Text = Annotated[str, Field(max_length=hr.MAX_TEXT)]
LongText = Annotated[str, Field(max_length=hr.MAX_LONG_TEXT)]
Date = Annotated[str, Field(max_length=10)]


class NewEmployee(_Body):
    employee_number: Text
    first_name: Text
    job_title: Text
    start_date: Date
    last_name: Optional[Text] = None
    preferred_name: Optional[Text] = None
    work_email: Optional[Text] = None
    work_phone: Optional[Text] = None
    department: Optional[Text] = None
    manager_id: Optional[int] = None
    employment_type: Literal["full_time", "part_time", "contractor", "temporary", "intern"] = "full_time"
    work_location: Optional[Text] = None


class EmployeeChanges(_Body):
    changes: dict                               # its values are checked (and capped) by hr.update_employee
    effective_date: Date
    reason: Optional[LongText] = None


class Termination(_Body):
    end_date: Date
    reason: LongText
    reassign_reports_to: Optional[int] = None


class Rehire(_Body):
    rehire_date: Date
    changes: Optional[dict] = None              # checked by hr.rehire_employee, like EmployeeChanges.changes
    reason: Optional[LongText] = None


class NewLeaveType(_Body):
    code: Text
    name: Text
    paid: bool = True
    tracks_balance: bool = True
    annual_allowance_days: float = 0


class Allowance(_Body):
    annual_allowance_days: float
    reason: LongText


class Accrual(_Body):
    employee_id: int
    leave_type: Text
    year: int


class Adjustment(_Body):
    employee_id: int
    leave_type: Text
    days: float
    reason: LongText
    as_of: Optional[Date] = None


class NewLeaveRequest(_Body):
    employee_id: int
    leave_type: Text
    start_date: Date
    end_date: Date
    half_day: bool = False
    half: Optional[Literal["am", "pm"]] = None
    note: Optional[LongText] = None
    as_of: Optional[Date] = None


class Decision(_Body):
    approver: Optional[Text] = None
    reason: Optional[LongText] = None
    as_of: Optional[Date] = None


_NO_DECISION = Decision()


def _days(value: float):
    return int(value) if float(value).is_integer() else value


# ---------------------------------------------------------------- employees

@router.get("/employees")
def list_employees(status: Optional[Literal["active", "terminated"]] = None,
                   department: Optional[str] = Query(None, max_length=hr.MAX_TEXT), conn=Depends(get_conn)):
    return _call(hr.employees, conn, status=status, department=department)


@router.post("/employees", status_code=201)
def add_employee(body: NewEmployee, key: str = Depends(_key), conn=Depends(get_conn)):
    return _call(hr.add_employee, conn, key, **body.model_dump())


@router.get("/employees/{employee_id}")
def get_employee(employee_id: int, conn=Depends(get_conn)):
    return _call(hr.employee, conn, employee_id)


@router.patch("/employees/{employee_id}")
def update_employee(employee_id: int, body: EmployeeChanges, key: str = Depends(_key), conn=Depends(get_conn)):
    return _call(hr.update_employee, conn, employee_id, body.changes, body.effective_date, key, reason=body.reason)


@router.post("/employees/{employee_id}/terminate")
def terminate_employee(employee_id: int, body: Termination, key: str = Depends(_key), conn=Depends(get_conn)):
    return _call(hr.terminate_employee, conn, employee_id, body.end_date, body.reason, key,
                 reassign_reports_to=body.reassign_reports_to)


@router.post("/employees/{employee_id}/rehire")
def rehire_employee(employee_id: int, body: Rehire, key: str = Depends(_key), conn=Depends(get_conn)):
    return _call(hr.rehire_employee, conn, employee_id, body.rehire_date, key, changes=body.changes,
                 reason=body.reason)


@router.get("/employees/{employee_id}/history")
def employment_history(employee_id: int, conn=Depends(get_conn)):
    return _call(hr.employment_history, conn, employee_id)


@router.get("/employees/{employee_id}/leave/balances")
def leave_balances(employee_id: int, conn=Depends(get_conn)):
    return _call(hr.leave_balances, conn, employee_id)


@router.get("/employees/{employee_id}/leave/ledger")
def leave_ledger(employee_id: int, leave_type: Optional[str] = Query(None, max_length=hr.MAX_TEXT),
                 conn=Depends(get_conn)):
    return _call(hr.leave_ledger, conn, employee_id, leave_type)


# ---------------------------------------------------------------- leave

@router.get("/leave/types")
def leave_types(conn=Depends(get_conn)):
    return _call(hr.leave_types, conn)


@router.post("/leave/types", status_code=201)
def create_leave_type(body: NewLeaveType, key: str = Depends(_key), conn=Depends(get_conn)):
    return _call(hr.create_leave_type, conn, body.code, body.name, key, paid=body.paid,
                 tracks_balance=body.tracks_balance, annual_allowance_days=_days(body.annual_allowance_days))


@router.put("/leave/types/{code}/allowance")
def set_leave_allowance(body: Allowance, code: str = Path(max_length=hr.MAX_TEXT), key: str = Depends(_key),
                        conn=Depends(get_conn)):
    return _call(hr.set_leave_allowance, conn, code, _days(body.annual_allowance_days), body.reason, key)


@router.post("/leave/accruals", status_code=201)
def accrue(body: Accrual, key: str = Depends(_key), conn=Depends(get_conn)):
    return _call(hr.accrue_annual_leave, conn, body.employee_id, body.leave_type, body.year, key)


@router.post("/leave/adjustments", status_code=201)
def adjust(body: Adjustment, key: str = Depends(_key), conn=Depends(get_conn)):
    return _call(hr.adjust_leave, conn, body.employee_id, body.leave_type, _days(body.days), body.reason, key,
                 as_of=body.as_of)


@router.get("/leave/requests")
def list_requests(employee_id: Optional[int] = None,
                  status: Optional[Literal["pending", "approved", "rejected", "cancelled"]] = Query(None),
                  conn=Depends(get_conn)):
    return _call(hr.leave_requests, conn, employee_id=employee_id, status=status)


@router.get("/leave/out")
def who_is_out(start_date: str, end_date: str, include_pending: bool = False, conn=Depends(get_conn)):
    """Leave requests touching start_date..end_date, approved only unless include_pending=true."""
    return _call(hr.leave_out, conn, start_date, end_date, include_pending=include_pending)


@router.post("/leave/requests", status_code=201)
def request_leave(body: NewLeaveRequest, key: str = Depends(_key), conn=Depends(get_conn)):
    return _call(hr.request_leave, conn, body.employee_id, body.leave_type, body.start_date, body.end_date, key,
                 half_day=body.half_day, note=body.note, as_of=body.as_of, half=body.half)


@router.get("/leave/requests/{request_id}")
def get_request(request_id: int, conn=Depends(get_conn)):
    return _call(hr.leave_request, conn, request_id)


# The decision body is optional: no body at all is the same as {}.

@router.post("/leave/requests/{request_id}/approve")
def approve(request_id: int, body: Optional[Decision] = None, key: str = Depends(_key), conn=Depends(get_conn)):
    body = body or _NO_DECISION
    return _call(hr.approve_leave, conn, request_id, key, approver=body.approver, as_of=body.as_of)


@router.post("/leave/requests/{request_id}/reject")
def reject(request_id: int, body: Optional[Decision] = None, key: str = Depends(_key), conn=Depends(get_conn)):
    body = body or _NO_DECISION
    if body.reason is None:
        raise HTTPException(422, "reason is required to reject")
    return _call(hr.reject_leave, conn, request_id, body.reason, key, approver=body.approver, as_of=body.as_of)


@router.post("/leave/requests/{request_id}/cancel")
def cancel(request_id: int, body: Optional[Decision] = None, key: str = Depends(_key), conn=Depends(get_conn)):
    """Approved leave is cancelled only before it starts (409 from its first day on; see hr.cancel_leave)."""
    body = body or _NO_DECISION
    return _call(hr.cancel_leave, conn, request_id, key, reason=body.reason, actor=body.approver, as_of=body.as_of,
                 today=today())


@router.get("/schema")
def schema(conn=Depends(get_conn)):
    return {"module": hr.MODULE, "version": hr.schema_version(conn)}
