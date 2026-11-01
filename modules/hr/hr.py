"""
HR module, v1: employee records and leave. Standalone: stdlib sqlite3 only,
nothing from operations_nerd/, no LLM calls. Built in the same way as the
logistics stock engine (modules/logistics/ on jasmeet/logistics-stock).

Each business has its own private database, so nothing here takes a business
id: a connection is one business's people.

Employees. An employee's history is a ledger, hr_employment_events. Every
command that changes an employee appends one event carrying the full record
after the change, and a database trigger copies it onto hr_employees. No
caller writes hr_employees, and its triggers refuse any row that doesn't match
its latest event. Terminated employees are frozen until they are rehired,
which is another event on the same record (same id, number and email; the
history stays whole). Events are dated in order.

Leave. A balance is a ledger, hr_leave_ledger: accruals and adjustments add or
remove, approving a request debits it, cancelling an approved request credits
it back, and requesting, rejecting or cancelling a pending request writes a
zero entry. balance = sum of the ledger; pending requests are held against it,
so available = balance - pending. Triggers keep a balance from going negative,
pending requests within the balance, and a request's status in step with its
ledger. Amounts are stored as whole half-days; commands take and return days.
A half day is the morning ('am') or the afternoon ('pm') of one date.

Every command runs in one BEGIN IMMEDIATE transaction and takes an idempotency
key that is unique across all commands. Replaying a key with the same command
and payload returns the original result and writes nothing; the same key with
a different command or payload raises IdempotencyConflict. A command that fails
records nothing, so its key stays free.

Open connections with connect(); it sets autocommit mode, which the explicit
BEGIN IMMEDIATE relies on, and turns on foreign keys. init_db() brings the
schema up to date through the versioned migrations in migrations/ (see migrate.py).

Dates are 'YYYY-MM-DD' and come from the caller (start_date, end_date,
effective_date, as_of). The one exception is cancel_leave, which needs to know
what day it is to refuse cancelling leave that has already started; it takes
`today` and only falls back to the clock when it isn't given.

Text. Employee numbers, names and other employee text are stored NFC-normalised
with surrounding whitespace trimmed and inner runs of whitespace collapsed to
one space; work emails are also lowercased. Every text input has a length cap
(MAX_TEXT, or MAX_LONG_TEXT for reasons and notes).

Trust boundary. The triggers are the rules for anyone who writes through
SQLite, but whoever can write the database file can also drop them. They are
not a defence against that person. verify_schema() compares the triggers and
indexes with what the migrations create and raises SchemaTampered on any
difference. init_db() runs it, and the API runs it on every request, so a
tampered schema stops the module (fails closed) instead of running without
its rules. It can't detect rows that were changed while a rule was missing.
"""

import datetime
import functools
import json
import re
import sqlite3
import unicodedata
from pathlib import Path

import migrate as _migrate

MODULE = "hr"
MIGRATIONS = Path(__file__).resolve().with_name("migrations")
BUSY_TIMEOUT_SECONDS = 10
# The largest id SQLite can store; a bigger number can't name a row (and sqlite3 raises OverflowError on it).
MAX_ID = 2 ** 63 - 1
# A cap on any one amount of leave (an allowance or an adjustment), far above any real one, so that amounts and
# their sums always fit SQLite's 64-bit integers.
MAX_LEAVE_DAYS = 100_000
# Length caps, in characters: names, numbers, emails, titles, codes and keys; and reasons and notes.
MAX_TEXT = 200
MAX_LONG_TEXT = 2000
HALVES = ("am", "pm")

EMPLOYMENT_TYPES = ("full_time", "part_time", "contractor", "temporary", "intern")
# Fields add_employee takes. Pay, tax, bank, national id and other sensitive data are left out on purpose (README).
EMPLOYEE_FIELDS = ("employee_number", "first_name", "last_name", "preferred_name", "work_email", "work_phone",
                   "job_title", "department", "manager_id", "employment_type", "work_location", "start_date")
REQUIRED_FIELDS = ("employee_number", "first_name", "job_title", "start_date")
# Fields update_employee may change. The number and start date are fixed; status changes only by terminate.
UPDATABLE_FIELDS = ("first_name", "last_name", "preferred_name", "work_email", "work_phone", "job_title",
                    "department", "manager_id", "employment_type", "work_location")
_SNAPSHOT_FIELDS = EMPLOYEE_FIELDS + ("status", "end_date", "termination_reason")


class HRError(Exception):
    pass


class IdempotencyConflict(HRError):
    """The key was already used for a different command or payload."""


class NotFound(HRError):
    """No such employee, leave type or leave request."""


class InvalidState(HRError):
    """The employee or request is not in a state for this command (terminated, already decided, still has reports...)."""


class InsufficientBalance(HRError):
    """The leave would take a balance below zero, or past what pending requests already hold."""


class Overlap(HRError):
    """The leave overlaps another pending or approved request of the same employee."""


class SchemaTampered(HRError):
    """The database's HR triggers or indexes aren't the ones the migrations create (see verify_schema)."""


def connect(path: str, check_same_thread: bool = True, create: bool = True) -> sqlite3.Connection:
    """check_same_thread=False is for the API, where FastAPI may open and use one request's connection on two
    worker threads; the connection is still used by one request at a time. create=False opens only a database
    file that exists (sqlite3.OperationalError otherwise) instead of creating an empty one."""
    if create or path == ":memory:":
        conn = sqlite3.connect(path, isolation_level=None, timeout=BUSY_TIMEOUT_SECONDS,
                               check_same_thread=check_same_thread)
    else:
        conn = sqlite3.connect(Path(path).absolute().as_uri() + "?mode=rw", uri=True, isolation_level=None,
                               timeout=BUSY_TIMEOUT_SECONDS, check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db(conn: sqlite3.Connection, backup_dir: str | None = None) -> dict:
    """Applies any missing HR migrations, after backing up a database that already has tables, then checks the
    schema with verify_schema() (SchemaTampered if it was changed outside the migrations)."""
    result = _migrate.migrate(conn, MODULE, MIGRATIONS, backup_dir=backup_dir)
    verify_schema(conn)
    return result


def schema_version(conn) -> int:
    return _migrate.current_version(conn, MODULE)


def latest_version() -> int:
    """The version the migrations in this code bring a database to."""
    return len(_migrate.migrations(MIGRATIONS))


def verify_schema(conn) -> None:
    """Raises SchemaTampered unless the database's HR triggers and indexes are exactly the ones the migrations
    up to its version create (same names, same SQL up to whitespace), and its HR tables are the ones they
    create. Changed or dropped rules, and extra hr_ objects, all count."""
    version = schema_version(conn)
    expected = _expected_schema(version)
    actual = {r["name"]: (r["type"], _normalise_sql(r["sql"]))
              for r in conn.execute("""SELECT type, name, sql FROM sqlite_master
                                        WHERE type IN ('table', 'index', 'trigger', 'view')
                                          AND name NOT LIKE 'sqlite_%'
                                          AND (name LIKE 'hr\\_%' ESCAPE '\\' OR tbl_name LIKE 'hr\\_%' ESCAPE '\\')""")}
    problems = []
    for name, (kind, sql) in sorted(expected.items()):
        if name not in actual:
            problems.append(f"{kind} {name} is missing")
        elif actual[name][0] != kind:
            problems.append(f"{name} is a {actual[name][0]}, not a {kind}")
        elif kind != "table" and actual[name][1] != sql:
            problems.append(f"{kind} {name} was changed")
    problems += [f"unexpected {kind} {name}" for name, (kind, _) in sorted(actual.items()) if name not in expected]
    if problems:
        raise SchemaTampered(f"the HR schema (version {version}) isn't what its migrations create: "
                             + "; ".join(problems))


@functools.lru_cache(maxsize=None)
def _expected_schema(version: int) -> dict:
    """{name: (type, normalised sql)} of the hr_ objects migrations 1..version leave behind. Tables are
    compared by name only: SQLite rewrites a table's SQL on ALTER TABLE."""
    out = {}
    for _, _, path in _migrate.migrations(MIGRATIONS)[:version]:
        for sql in _migrate._statements(path.read_text()):
            text = _normalise_sql(sql)
            m = re.match(r"CREATE (?:UNIQUE )?(TABLE|INDEX|TRIGGER|VIEW) (?:IF NOT EXISTS )?(\w+)", text, re.I)
            if m:
                out[m.group(2)] = (m.group(1).lower(), text)
                continue
            m = re.match(r"DROP (TABLE|INDEX|TRIGGER|VIEW) (?:IF EXISTS )?(\w+)", text, re.I)
            if m:
                out.pop(m.group(2), None)
                if m.group(1).upper() == "TABLE":     # its indexes and triggers go with it
                    out = {k: v for k, v in out.items()
                           if not (v[0] in ("index", "trigger")
                                   and re.search(rf"\bON {re.escape(m.group(2))}\b", v[1], re.I))}
                continue
            m = re.match(r"ALTER TABLE (\w+) RENAME TO (\w+)", text, re.I)
            if m:
                out[m.group(2)] = out.pop(m.group(1))
    return out


def _normalise_sql(sql) -> str:
    """The statement as sqlite_master keeps it: from CREATE on, no trailing ';', whitespace collapsed."""
    lines = (sql or "").strip().splitlines()
    while lines and (not lines[0].strip() or lines[0].lstrip().startswith("--")):
        lines.pop(0)
    return " ".join("\n".join(lines).strip().rstrip(";").split())


# ---------------------------------------------------------------- employee commands

def add_employee(conn, idempotency_key: str, **fields) -> dict:
    """Hires someone: writes the 'hired' event (version 1), which creates the employee. Returns employee().

    Takes EMPLOYEE_FIELDS as keywords. employee_number, first_name, job_title and start_date are
    required; employment_type defaults to full_time. manager_id must be an active employee.
    effective_date of the hire is the start_date.
    """
    unknown = set(fields) - set(EMPLOYEE_FIELDS)
    if unknown:
        raise ValueError(f"unknown employee fields: {', '.join(sorted(unknown))}")
    for name in REQUIRED_FIELDS:
        if fields.get(name) is None:
            raise ValueError(f"{name} is required")
    record = {name: _clean_field(name, fields.get(name)) for name in EMPLOYEE_FIELDS}
    if record["employment_type"] is None:
        record["employment_type"] = "full_time"
    _check_employee_fields(record)
    record.update(status="active", end_date=None, termination_reason=None)

    def work():
        taken = conn.execute("SELECT id FROM hr_employees WHERE employee_number = ?",
                             (record["employee_number"],)).fetchone()
        if taken:
            raise InvalidState(f"employee number {record['employee_number']!r} is already employee {taken['id']}")
        _check_email_free(conn, record["work_email"], None)
        if record["manager_id"] is not None:
            _active_employee(conn, record["manager_id"], "manager")
        # The event comes first and creates the employee, so the id is chosen here. BEGIN IMMEDIATE
        # holds the write lock, so no other writer can take it meanwhile.
        employee_id = conn.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM hr_employees").fetchone()[0]
        changes = {k: [None, v] for k, v in record.items() if v is not None}
        _insert_event(conn, employee_id, 1, "hired", record["start_date"], changes, record, None, idempotency_key)
        return employee(conn, employee_id)

    return _run(conn, idempotency_key, "add_employee", record, work)


def update_employee(conn, employee_id: int, changes: dict, effective_date: str, idempotency_key: str,
                    reason: str | None = None) -> dict:
    """Changes UPDATABLE_FIELDS of an active employee, as one 'updated' event. Returns employee().

    Fields set to None are cleared (first_name, job_title and employment_type can't be). A change that
    changes nothing is a ValueError. A new manager must be active and not report to this employee,
    directly or through others.
    """
    _positive_int("employee_id", employee_id)
    if not isinstance(changes, dict) or not changes:
        raise ValueError("changes must be a non-empty dict of fields")
    unknown = set(changes) - set(UPDATABLE_FIELDS)
    if unknown:
        raise ValueError(f"these fields can't be updated: {', '.join(sorted(unknown))}")
    _date("effective_date", effective_date)
    if reason is not None:
        _text("reason", reason, MAX_LONG_TEXT)
    changes = {k: _clean_field(k, v) for k, v in changes.items()}

    def work():
        current = _active_employee(conn, employee_id)
        record = {k: current[k] for k in _SNAPSHOT_FIELDS}
        record.update(changes)
        _check_employee_fields(record)
        diff = {k: [current[k], v] for k, v in changes.items() if current[k] != v}
        if not diff:
            raise ValueError(f"nothing changes for employee {employee_id}")
        if effective_date < current["start_date"]:
            raise ValueError(f"effective_date {effective_date} is before the start date {current['start_date']}")
        _check_after_last_event(conn, employee_id, "effective_date", effective_date)
        if "work_email" in diff:
            _check_email_free(conn, record["work_email"], employee_id)
        if "manager_id" in diff and record["manager_id"] is not None:
            _check_manager(conn, employee_id, record["manager_id"])
        _insert_event(conn, employee_id, current["version"] + 1, "updated", effective_date, diff, record, reason,
                      idempotency_key)
        return employee(conn, employee_id)

    payload = {"employee_id": employee_id, "changes": changes, "effective_date": effective_date, "reason": reason}
    return _run(conn, idempotency_key, "update_employee", payload, work)


def terminate_employee(conn, employee_id: int, end_date: str, reason: str, idempotency_key: str,
                       reassign_reports_to: int | None = None) -> dict:
    """Ends an active employee's employment on end_date (their last day). Returns employee() plus what it changed.

    In the same transaction:
    - Active direct reports move to reassign_reports_to, one 'updated' event each. Without it, an
      employee who still has active reports can't be terminated (InvalidState).
    - Every pending leave request is cancelled, and every approved one that starts after end_date is
      cancelled and credited back. An approved leave that runs past end_date is charged only up to it:
      the request is cancelled (credited in full) and replaced by an approved request from its start
      to end_date, debited again ("shortened_leave"). One that ends by end_date stays.
    Then the 'terminated' event freezes the employee. Balances stay readable; nothing more is written to
    their leave ledger (no accrual, adjustment, request, decision or cancel) unless they are rehired.
    end_date can't be before their start date or their latest employment event.
    """
    _positive_int("employee_id", employee_id)
    _date("end_date", end_date)
    _text("reason", reason, MAX_LONG_TEXT)
    if reassign_reports_to is not None:
        _positive_int("reassign_reports_to", reassign_reports_to)

    def work():
        current = _active_employee(conn, employee_id)
        if end_date < current["start_date"]:
            raise ValueError(f"end_date {end_date} is before the start date {current['start_date']}")
        _check_after_last_event(conn, employee_id, "end_date", end_date)
        reports = [r["id"] for r in conn.execute(
            "SELECT id FROM hr_employees WHERE manager_id = ? AND status = 'active' ORDER BY id", (employee_id,))]
        if reports and reassign_reports_to is None:
            raise InvalidState(f"employee {employee_id} still manages {reports}: pass reassign_reports_to")
        if reassign_reports_to is not None:
            if reassign_reports_to == employee_id:
                raise ValueError("can't reassign reports to the employee being terminated")
            _active_employee(conn, reassign_reports_to, "manager")
            if employee_id in _management_chain(conn, reassign_reports_to):
                raise ValueError(f"employee {reassign_reports_to} reports to employee {employee_id}; "
                                 f"reassign to someone outside their team")
        to_cancel = conn.execute("""SELECT id, leave_type_id, status, half_days FROM hr_leave_requests
                                    WHERE employee_id = ? AND (status = 'pending'
                                          OR (status = 'approved' AND start_date > ?))
                                    ORDER BY id""", (employee_id, end_date)).fetchall()
        to_shorten = conn.execute("""SELECT * FROM hr_leave_requests
                                     WHERE employee_id = ? AND status = 'approved' AND start_date <= ?
                                       AND end_date > ? ORDER BY id""", (employee_id, end_date, end_date)).fetchall()
        derived_keys = ([f"{idempotency_key}/report:{r}" for r in reports]
                        + [f"{idempotency_key}/cancel:{req['id']}" for req in to_cancel])
        for req in to_shorten:
            derived_keys += [f"{idempotency_key}/shorten:{req['id']}{step}" for step in ("", "/request", "/approve")]
        for derived in derived_keys:
            if _key_taken(conn, derived):
                raise IdempotencyConflict(f"key {derived!r}, derived from {idempotency_key!r}, was already used")
        for report_id in reports:
            r = _employee_row(conn, report_id)
            record = {k: r[k] for k in _SNAPSHOT_FIELDS} | {"manager_id": reassign_reports_to}
            # Dated end_date, or later if the report's own history already is.
            dated = max(end_date, _last_event_date(conn, report_id))
            _insert_event(conn, report_id, r["version"] + 1, "updated", dated,
                          {"manager_id": [employee_id, reassign_reports_to]}, record,
                          f"manager {employee_id} terminated", f"{idempotency_key}/report:{report_id}")
        cancelled = []
        for req in to_cancel:
            credit = req["half_days"] if req["status"] == "approved" else 0
            _insert_ledger(conn, employee_id, req["leave_type_id"], "cancel", credit,
                           f"{idempotency_key}/cancel:{req['id']}", request_id=req["id"], as_of=end_date,
                           reason="employment ended")
            cancelled.append({"request_id": req["id"], "was": req["status"], "credited_days": credit / 2})
        shortened = []
        for req in to_shorten:
            # Credit the whole request back, then charge again only the days up to end_date. A request can't be
            # edited (its rows are fixed), so the charged part is a new approved request.
            base = f"{idempotency_key}/shorten:{req['id']}"
            _insert_ledger(conn, employee_id, req["leave_type_id"], "cancel", req["half_days"], base,
                           request_id=req["id"], as_of=end_date, reason=f"employment ended on {end_date}")
            kept = 2 * working_days(req["start_date"], end_date)
            replacement = None
            if kept:
                replacement = conn.execute("""INSERT INTO hr_leave_requests (employee_id, leave_type_id, start_date,
                                                  end_date, half_days, note, idempotency_key, half)
                                              VALUES (?, ?, ?, ?, ?, ?, ?, NULL)""",
                                           (employee_id, req["leave_type_id"], req["start_date"], end_date, kept,
                                            req["note"], base + "/request")).lastrowid
                _insert_ledger(conn, employee_id, req["leave_type_id"], "request", 0, base + "/request",
                               request_id=replacement, as_of=end_date,
                               reason=f"the part of request {req['id']} up to {end_date}")
                _insert_ledger(conn, employee_id, req["leave_type_id"], "approve", -kept, base + "/approve",
                               request_id=replacement, as_of=end_date,
                               reason=f"the part of request {req['id']} up to {end_date}")
            shortened.append({"request_id": req["id"], "replaced_by": replacement, "kept_days": kept / 2,
                              "credited_days": (req["half_days"] - kept) / 2})
        record = {k: current[k] for k in _SNAPSHOT_FIELDS} | {"status": "terminated", "end_date": end_date,
                                                               "termination_reason": reason}
        _insert_event(conn, employee_id, current["version"] + 1, "terminated", end_date,
                      {"status": ["active", "terminated"], "end_date": [None, end_date],
                       "termination_reason": [None, reason]}, record, reason, idempotency_key)
        return employee(conn, employee_id) | {"reassigned_reports": reports, "cancelled_leave": cancelled,
                                              "shortened_leave": shortened}

    payload = {"employee_id": employee_id, "end_date": end_date, "reason": reason,
               "reassign_reports_to": reassign_reports_to}
    return _run(conn, idempotency_key, "terminate_employee", payload, work)


def rehire_employee(conn, employee_id: int, rehire_date: str, idempotency_key: str, changes: dict | None = None,
                    reason: str | None = None) -> dict:
    """Brings a terminated employee back from rehire_date (after their end date), as a 'rehired' event on the
    same record: same id, employee number and work email, and the whole history kept. Returns employee().

    changes may set UPDATABLE_FIELDS for the new employment (a new job title, say). Their manager stays
    unless changed, and must be active: if the old one has left, pass changes={"manager_id": ...} (or None).
    Their work email must still be free. start_date stays the first hire date; employment_start_date
    becomes rehire_date, and leave is requested and accrued from it. The leave balance carries over:
    termination settled it, and an accrual already made for a year isn't repeated.
    """
    _positive_int("employee_id", employee_id)
    _date("rehire_date", rehire_date)
    if changes is None:
        changes = {}
    if not isinstance(changes, dict):
        raise ValueError("changes must be a dict of fields")
    unknown = set(changes) - set(UPDATABLE_FIELDS)
    if unknown:
        raise ValueError(f"these fields can't be set on rehire: {', '.join(sorted(unknown))}")
    if reason is not None:
        _text("reason", reason, MAX_LONG_TEXT)
    changes = {k: _clean_field(k, v) for k, v in changes.items()}

    def work():
        current = _employee_row(conn, employee_id)
        if current["status"] != "terminated":
            raise InvalidState(f"employee {employee_id} is {current['status']}; only a terminated employee is rehired")
        if rehire_date <= current["end_date"]:
            raise ValueError(f"rehire_date {rehire_date} must be after the end date {current['end_date']}")
        _check_after_last_event(conn, employee_id, "rehire_date", rehire_date)
        record = {k: current[k] for k in _SNAPSHOT_FIELDS} | changes
        record.update(status="active", end_date=None, termination_reason=None)
        _check_employee_fields(record)
        _check_email_free(conn, record["work_email"], employee_id)
        if record["manager_id"] is not None:
            try:
                _check_manager(conn, employee_id, record["manager_id"])
            except InvalidState as e:
                raise InvalidState(f"{e}: pass changes={{'manager_id': ...}} with someone active, or None") from None
        diff = {k: [current[k], record[k]] for k in _SNAPSHOT_FIELDS if current[k] != record[k]}
        _insert_event(conn, employee_id, current["version"] + 1, "rehired", rehire_date, diff, record, reason,
                      idempotency_key)
        return employee(conn, employee_id)

    payload = {"employee_id": employee_id, "rehire_date": rehire_date, "changes": changes, "reason": reason}
    return _run(conn, idempotency_key, "rehire_employee", payload, work)


# ---------------------------------------------------------------- leave commands

def create_leave_type(conn, code: str, name: str, idempotency_key: str, paid: bool = True,
                      tracks_balance: bool = True, annual_allowance_days: float = 0) -> dict:
    """Adds a leave type. code is lowercase letters, digits and '_'. A type that doesn't track a balance
    (unpaid leave, say) has no allowance and no accruals; its requests are only checked for overlaps.
    A tracked type's starting allowance is recorded as its first policy change."""
    _code("code", code)
    _text("name", name)
    _bool("paid", paid)
    _bool("tracks_balance", tracks_balance)
    allowance = _half_days("annual_allowance_days", annual_allowance_days, allow_zero=True)
    if not tracks_balance and allowance:
        raise ValueError("a leave type without a balance has no allowance")

    def work():
        if conn.execute("SELECT 1 FROM hr_leave_types WHERE code = ?", (code,)).fetchone():
            raise InvalidState(f"leave type {code!r} already exists")
        type_id = conn.execute("""INSERT INTO hr_leave_types (code, name, paid, tracks_balance,
                                                              annual_allowance_half_days, idempotency_key)
                                  VALUES (?, ?, ?, ?, ?, ?)""",
                               (code, name, int(paid), int(tracks_balance), allowance, idempotency_key)).lastrowid
        if tracks_balance:
            conn.execute("""INSERT INTO hr_leave_policy_changes (leave_type_id, annual_allowance_half_days, reason,
                                                                 idempotency_key) VALUES (?, ?, ?, ?)""",
                         (type_id, allowance, "initial allowance", idempotency_key))
        return leave_type(conn, code)

    payload = {"code": code, "name": name, "paid": paid, "tracks_balance": tracks_balance,
               "annual_allowance_half_days": allowance}
    return _run(conn, idempotency_key, "create_leave_type", payload, work)


def set_leave_allowance(conn, code: str, annual_allowance_days: float, reason: str, idempotency_key: str) -> dict:
    """Changes a tracked leave type's annual allowance. Accruals already made keep their amounts."""
    _code("code", code)
    allowance = _half_days("annual_allowance_days", annual_allowance_days, allow_zero=True)
    _text("reason", reason, MAX_LONG_TEXT)

    def work():
        lt = _leave_type_row(conn, code)
        if not lt["tracks_balance"]:
            raise InvalidState(f"leave type {code!r} has no balance, so no allowance")
        if lt["annual_allowance_half_days"] == allowance:
            raise ValueError(f"leave type {code!r} already allows {allowance / 2} days")
        conn.execute("""INSERT INTO hr_leave_policy_changes (leave_type_id, annual_allowance_half_days, reason,
                                                             idempotency_key) VALUES (?, ?, ?, ?)""",
                     (lt["id"], allowance, reason, idempotency_key))
        return leave_type(conn, code)

    payload = {"code": code, "annual_allowance_half_days": allowance, "reason": reason}
    return _run(conn, idempotency_key, "set_leave_allowance", payload, work)


def accrue_annual_leave(conn, employee_id: int, code: str, year: int, idempotency_key: str) -> dict:
    """Grants an active employee a tracked type's annual allowance for year, once per employee, type and year.

    The grant is front-loaded: the whole year at once. Someone who started during the year gets the
    allowance prorated by calendar days employed from their start date to 31 December, rounded down to a
    half-day. A year before their start year is a ValueError. For a rehired employee the start is the
    rehire date (employment_start_date).
    """
    _positive_int("employee_id", employee_id)
    _code("code", code)
    if type(year) is not int or not 1900 <= year <= 9999:
        raise ValueError(f"year must be an integer year, got {year!r}")

    def work():
        emp = _active_employee(conn, employee_id)
        lt = _leave_type_row(conn, code)
        if not lt["tracks_balance"]:
            raise InvalidState(f"leave type {code!r} has no balance")
        year_start, year_end = datetime.date(year, 1, 1), datetime.date(year, 12, 31)
        began = _employment_start(conn, emp)
        start = datetime.date.fromisoformat(began)
        if start > year_end:
            raise ValueError(f"employee {employee_id} starts on {began}, after {year}")
        if conn.execute("""SELECT 1 FROM hr_leave_ledger WHERE employee_id = ? AND leave_type_id = ?
                           AND entry_type = 'accrual' AND accrual_year = ?""", (employee_id, lt["id"], year)).fetchone():
            raise InvalidState(f"employee {employee_id} already accrued {code!r} for {year}")
        employed = (year_end - max(start, year_start)).days + 1
        in_year = (year_end - year_start).days + 1
        amount = lt["annual_allowance_half_days"] * employed // in_year
        entry_id = _insert_ledger(conn, employee_id, lt["id"], "accrual", amount, idempotency_key, accrual_year=year,
                                  as_of=max(start, year_start).isoformat())
        return {"entry_id": entry_id, "employee_id": employee_id, "leave_type": code, "year": year,
                "days": amount / 2, "prorated": employed < in_year, "balance": leave_balance(conn, employee_id, code)}

    payload = {"employee_id": employee_id, "code": code, "year": year}
    return _run(conn, idempotency_key, "accrue_annual_leave", payload, work)


def adjust_leave(conn, employee_id: int, code: str, days: float, reason: str, idempotency_key: str,
                 as_of: str | None = None) -> dict:
    """A manual correction to a tracked balance, positive or negative, with a reason. A negative one
    can't take the balance below what pending requests hold."""
    _positive_int("employee_id", employee_id)
    _code("code", code)
    amount = _half_days("days", days, allow_negative=True)
    _text("reason", reason, MAX_LONG_TEXT)
    if as_of is not None:
        _date("as_of", as_of)

    def work():
        _active_employee(conn, employee_id)
        lt = _leave_type_row(conn, code)
        if not lt["tracks_balance"]:
            raise InvalidState(f"leave type {code!r} has no balance")
        if amount < 0:
            bal = leave_balance(conn, employee_id, code)
            if bal["available"] * 2 + amount < 0:
                raise InsufficientBalance(f"can't remove {-amount / 2} days: employee {employee_id} has "
                                          f"{bal['available']} {code!r} days available")
        entry_id = _insert_ledger(conn, employee_id, lt["id"], "adjustment", amount, idempotency_key, as_of=as_of,
                                  reason=reason)
        return {"entry_id": entry_id, "employee_id": employee_id, "leave_type": code, "days": amount / 2,
                "balance": leave_balance(conn, employee_id, code)}

    payload = {"employee_id": employee_id, "code": code, "half_days": amount, "reason": reason, "as_of": as_of}
    return _run(conn, idempotency_key, "adjust_leave", payload, work)


def request_leave(conn, employee_id: int, code: str, start_date: str, end_date: str, idempotency_key: str,
                  half_day: bool = False, note: str | None = None, as_of: str | None = None,
                  half: str | None = None) -> dict:
    """Requests leave from start_date to end_date inclusive. Returns leave_request().

    The amount is the working days (Monday to Friday) in the range; half_day=True asks for half of a
    single working day: the morning (half='am', the default) or the afternoon (half='pm'). There is no
    public holiday calendar. A range with no working day is a ValueError.
    For a tracked type, the amount plus what pending requests hold must fit in the balance
    (InsufficientBalance). It can't overlap another pending or approved request of the employee, of any
    type (Overlap), though the morning and the afternoon of one date don't overlap; and it can't start
    before their employment_start_date. Nothing is debited until approve_leave.
    """
    _positive_int("employee_id", employee_id)
    _code("code", code)
    _date("start_date", start_date)
    _date("end_date", end_date)
    _bool("half_day", half_day)
    if half is not None and half not in HALVES:
        raise ValueError(f"half must be 'am' or 'pm', got {half!r}")
    if half is not None and not half_day:
        raise ValueError("half ('am' or 'pm') is for a half_day request")
    if half_day and half is None:
        half = "am"
    if end_date < start_date:
        raise ValueError(f"end_date {end_date} is before start_date {start_date}")
    if half_day and start_date != end_date:
        raise ValueError("half_day is for a single day: start_date and end_date must be the same")
    if note is not None:
        _text("note", note, MAX_LONG_TEXT)
    if as_of is not None:
        _date("as_of", as_of)
    amount = 2 * working_days(start_date, end_date)
    if amount == 0:
        raise ValueError(f"{start_date} to {end_date} has no working day")
    if half_day:
        amount = 1

    def work():
        emp = _active_employee(conn, employee_id)
        lt = _leave_type_row(conn, code)
        began = _employment_start(conn, emp)
        if start_date < began:
            raise ValueError(f"leave starts before the employee's start date {began}")
        clash = conn.execute("""SELECT id, start_date, end_date, status FROM hr_leave_requests
                                WHERE employee_id = ? AND status IN ('pending', 'approved')
                                  AND start_date <= ? AND end_date >= ?
                                  AND NOT (half IS NOT NULL AND ? IS NOT NULL AND half <> ?)
                                ORDER BY id LIMIT 1""",
                             (employee_id, end_date, start_date, half, half)).fetchone()
        if clash:
            raise Overlap(f"overlaps {clash['status']} request {clash['id']} ({clash['start_date']} to "
                          f"{clash['end_date']})")
        if lt["tracks_balance"]:
            bal = leave_balance(conn, employee_id, code)
            if amount > bal["available"] * 2:
                raise InsufficientBalance(f"asks for {amount / 2} {code!r} days, employee {employee_id} has "
                                          f"{bal['available']} available ({bal['balance']} balance, "
                                          f"{bal['pending']} pending)")
        request_id = conn.execute("""INSERT INTO hr_leave_requests (employee_id, leave_type_id, start_date, end_date,
                                                                    half_days, note, idempotency_key, half)
                                     VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                                  (employee_id, lt["id"], start_date, end_date, amount, note,
                                   idempotency_key, half)).lastrowid
        _insert_ledger(conn, employee_id, lt["id"], "request", 0, idempotency_key, request_id=request_id, as_of=as_of)
        return leave_request(conn, request_id)

    payload = {"employee_id": employee_id, "code": code, "start_date": start_date, "end_date": end_date,
               "half_day": half_day, "note": note, "as_of": as_of}
    if half == "pm":      # left out otherwise, so that requests made before halves existed replay unchanged
        payload["half"] = half
    return _run(conn, idempotency_key, "request_leave", payload, work)


def approve_leave(conn, request_id: int, idempotency_key: str, approver: str | None = None,
                  as_of: str | None = None) -> dict:
    """Approves a pending request and debits it from the balance. Returns leave_request()."""
    return _decide(conn, request_id, "approve", idempotency_key, approver, None, as_of)


def reject_leave(conn, request_id: int, reason: str, idempotency_key: str, approver: str | None = None,
                 as_of: str | None = None) -> dict:
    """Rejects a pending request, with a reason. Nothing is debited. Returns leave_request()."""
    _text("reason", reason, MAX_LONG_TEXT)
    return _decide(conn, request_id, "reject", idempotency_key, approver, reason, as_of)


def cancel_leave(conn, request_id: int, idempotency_key: str, reason: str | None = None, actor: str | None = None,
                 as_of: str | None = None, today: str | None = None) -> dict:
    """Cancels a pending or approved request of an active employee. Returns leave_request().

    A pending request can always be cancelled. An approved one is credited back in full, so it can only be
    cancelled before it starts: once its start_date is today or past, cancelling is refused (InvalidState);
    correct days already taken with adjust_leave instead. today is 'YYYY-MM-DD' and defaults to the
    machine's date; it isn't part of the command, so a replay on a later day still returns the first result.
    Leave of someone who has left isn't cancelled (termination settled it), nor leave from an earlier
    employment of someone rehired.
    """
    today = datetime.date.today().isoformat() if today is None else today
    _date("today", today)
    return _decide(conn, request_id, "cancel", idempotency_key, actor, reason, as_of, today)


# ---------------------------------------------------------------- queries

def employee(conn, employee_id: int) -> dict:
    """The employee's current record, with manager_name, how many active direct reports they have, and
    employment_start_date (start_date, or the latest rehire date)."""
    _lookup_id("employee", employee_id)
    row = conn.execute(f"SELECT {_EMPLOYEE_COLUMNS} FROM hr_employees e WHERE e.id = ?", (employee_id,)).fetchone()
    if row is None:
        raise NotFound(f"no employee {employee_id}")
    out = _employee_dict(row)
    out["direct_reports"] = conn.execute("SELECT COUNT(*) FROM hr_employees WHERE manager_id = ? AND status = 'active'",
                                         (employee_id,)).fetchone()[0]
    out["employment_start_date"] = _employment_start(conn, row)
    return out


def employees(conn, status: str | None = None, department: str | None = None) -> list[dict]:
    """Employees ordered by id, optionally only one status ('active' or 'terminated') or department."""
    if status is not None and status not in ("active", "terminated"):
        raise ValueError(f"status must be 'active' or 'terminated', got {status!r}")
    sql, args = f"SELECT {_EMPLOYEE_COLUMNS} FROM hr_employees e WHERE 1 = 1", []
    if status is not None:
        sql, args = sql + " AND e.status = ?", args + [status]
    if department is not None:
        if not isinstance(department, str):
            raise ValueError(f"department must be a string, got {department!r}")
        sql, args = sql + " AND e.department = ?", args + [_clean(department)]
    return [_employee_dict(r) for r in conn.execute(sql + " ORDER BY e.id", args)]


def employment_history(conn, employee_id: int) -> list[dict]:
    """Every employment event of the employee, oldest first: version, event, effective_date, changes
    ({field: [old, new]}), reason."""
    _employee_row(conn, employee_id)
    rows = conn.execute("""SELECT version, event, effective_date, changes, reason, created_at
                           FROM hr_employment_events WHERE employee_id = ? ORDER BY version""", (employee_id,))
    return [dict(r) | {"changes": json.loads(r["changes"])} for r in rows]


def leave_type(conn, code: str) -> dict:
    return _leave_type_dict(_leave_type_row(conn, code))


def leave_types(conn) -> list[dict]:
    return [_leave_type_dict(r) for r in conn.execute("SELECT * FROM hr_leave_types ORDER BY id")]


def leave_balance(conn, employee_id: int, code: str) -> dict:
    """Days for one employee and leave type: accrued (accruals), adjusted, taken (approved, net of cancels),
    balance (the ledger sum), pending (held by pending requests) and available (balance - pending).
    For a type without a balance, balance and available are None."""
    _employee_row(conn, employee_id)
    lt = _leave_type_row(conn, code)
    sums = conn.execute("""SELECT COALESCE(SUM(CASE WHEN entry_type = 'accrual' THEN half_days END), 0),
                                  COALESCE(SUM(CASE WHEN entry_type = 'adjustment' THEN half_days END), 0),
                                  COALESCE(-SUM(CASE WHEN entry_type IN ('approve', 'cancel') THEN half_days END), 0),
                                  COALESCE(SUM(half_days), 0)
                           FROM hr_leave_ledger WHERE employee_id = ? AND leave_type_id = ?""",
                        (employee_id, lt["id"])).fetchone()
    pending = conn.execute("""SELECT COALESCE(SUM(half_days), 0) FROM hr_leave_requests
                              WHERE employee_id = ? AND leave_type_id = ? AND status = 'pending'""",
                           (employee_id, lt["id"])).fetchone()[0]
    tracked = bool(lt["tracks_balance"])
    return {"employee_id": employee_id, "leave_type": code, "accrued": sums[0] / 2, "adjusted": sums[1] / 2,
            "taken": sums[2] / 2, "balance": sums[3] / 2 if tracked else None, "pending": pending / 2,
            "available": (sums[3] - pending) / 2 if tracked else None}


def leave_balances(conn, employee_id: int) -> list[dict]:
    _employee_row(conn, employee_id)
    return [leave_balance(conn, employee_id, lt["code"])
            for lt in conn.execute("SELECT code FROM hr_leave_types ORDER BY id")]


def leave_request(conn, request_id: int) -> dict:
    _lookup_id("leave request", request_id)
    row = conn.execute(f"""SELECT {_REQUEST_COLUMNS} FROM hr_leave_requests r
                           JOIN hr_leave_types t ON t.id = r.leave_type_id WHERE r.id = ?""",
                       (request_id,)).fetchone()
    if row is None:
        raise NotFound(f"no leave request {request_id}")
    return _request_dict(row)


def leave_requests(conn, employee_id: int | None = None, status: str | None = None) -> list[dict]:
    sql, args = f"""SELECT {_REQUEST_COLUMNS} FROM hr_leave_requests r
                    JOIN hr_leave_types t ON t.id = r.leave_type_id WHERE 1 = 1""", []
    if employee_id is not None:
        try:
            _lookup_id("employee", employee_id)
        except NotFound:
            return []
        sql, args = sql + " AND r.employee_id = ?", args + [employee_id]
    if status is not None:
        if status not in ("pending", "approved", "rejected", "cancelled"):
            raise ValueError(f"status must be pending, approved, rejected or cancelled, got {status!r}")
        sql, args = sql + " AND r.status = ?", args + [status]
    return [_request_dict(r) for r in conn.execute(sql + " ORDER BY r.start_date, r.id", args)]


def leave_out(conn, start_date: str, end_date: str, include_pending: bool = False) -> list[dict]:
    """Who is away: leave requests that touch start_date..end_date (both inclusive), with the person.

    Approved requests only, unless include_pending is true, which adds pending ones (each row carries its
    status so a caller can tell them apart). A request counts if any part of it falls in the range, so a
    request that starts before the range or ends after it is included whole. It is a read only query: the
    employee's current name and department come from their current record, and rejected or cancelled
    requests never show. Ordered by start date, then employee, then request id.
    """
    _check_date_range(start_date, end_date)
    _bool("include_pending", include_pending)
    statuses = ("approved", "pending") if include_pending else ("approved",)
    marks = ", ".join("?" for _ in statuses)
    rows = conn.execute(f"""SELECT {_REQUEST_COLUMNS},
                                   TRIM(e.first_name || ' ' || COALESCE(e.last_name, '')) AS employee_name,
                                   e.department AS department
                            FROM hr_leave_requests r
                            JOIN hr_leave_types t ON t.id = r.leave_type_id
                            JOIN hr_employees e ON e.id = r.employee_id
                            WHERE r.status IN ({marks}) AND r.start_date <= ? AND r.end_date >= ?
                            ORDER BY r.start_date, r.employee_id, r.id""",
                        (*statuses, end_date, start_date)).fetchall()
    return [_request_dict(r) for r in rows]


def _check_date_range(start_date, end_date):
    _date("start_date", start_date)
    _date("end_date", end_date)
    if end_date < start_date:
        raise ValueError(f"end_date {end_date} is before start_date {start_date}")


def leave_ledger(conn, employee_id: int, code: str | None = None) -> list[dict]:
    """The employee's leave ledger, oldest first, with each entry's days."""
    _employee_row(conn, employee_id)
    sql, args = """SELECT l.id AS entry_id, t.code AS leave_type, l.entry_type, l.half_days, l.request_id,
                          l.accrual_year, l.as_of, l.actor, l.reason
                   FROM hr_leave_ledger l JOIN hr_leave_types t ON t.id = l.leave_type_id
                   WHERE l.employee_id = ?""", [employee_id]
    if code is not None:
        sql, args = sql + " AND t.code = ?", args + [code]
    out = []
    for r in conn.execute(sql + " ORDER BY l.id", args):
        d = dict(r)
        d["days"] = d.pop("half_days") / 2
        out.append(d)
    return out


def working_days(start_date: str, end_date: str) -> int:
    """Monday-to-Friday days from start_date to end_date inclusive."""
    start, end = datetime.date.fromisoformat(start_date), datetime.date.fromisoformat(end_date)
    days = (end - start).days + 1
    if days <= 0:
        return 0
    full_weeks, rest = divmod(days, 7)
    return full_weeks * 5 + sum(1 for i in range(rest) if (start.weekday() + i) % 7 < 5)


# ---------------------------------------------------------------- internals

_EMPLOYEE_COLUMNS = (", ".join(f"e.{c}" for c in _SNAPSHOT_FIELDS) + ", e.id, e.version, "
                     "(SELECT TRIM(m.first_name || ' ' || COALESCE(m.last_name, '')) FROM hr_employees m "
                     "WHERE m.id = e.manager_id) AS manager_name")
_REQUEST_COLUMNS = ("r.id AS request_id, r.employee_id, t.code AS leave_type, r.start_date, r.end_date, "
                    "r.half_days, r.half, r.note, r.status")


def _run(conn, idempotency_key, command, payload, work):
    """Runs work() in one BEGIN IMMEDIATE transaction, or returns the stored result of an earlier run of this key."""
    _text("idempotency_key", idempotency_key)
    body = json.dumps(payload, sort_keys=True)
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT command, payload, result FROM hr_commands WHERE idempotency_key = ?",
                           (idempotency_key,)).fetchone()
        if row is not None:
            if (row["command"], row["payload"]) != (command, body):
                raise IdempotencyConflict(f"key {idempotency_key!r} was already used for {row['command']} {row['payload']}")
            result = row["result"]
        else:
            # Termination writes derived keys (key/report:N, key/cancel:N, key/shorten:N...) into other tables
            # without an hr_commands row of their own; a later command can't reuse one of them.
            if _key_taken(conn, idempotency_key):
                raise IdempotencyConflict(f"key {idempotency_key!r} was already used inside an earlier command")
            result = json.dumps(work(), sort_keys=True)
            conn.execute("INSERT INTO hr_commands (idempotency_key, command, payload, result) VALUES (?, ?, ?, ?)",
                         (idempotency_key, command, body, result))
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    return json.loads(result)


def _key_taken(conn, key) -> bool:
    """Whether a command, or a step derived from one, already used key. Only the tables this schema version has
    are looked at (hr_leave_ledger and hr_leave_requests arrive with migration 0002)."""
    tables = [t for (t,) in conn.execute("""SELECT name FROM sqlite_master WHERE type = 'table'
                                             AND name IN ('hr_commands', 'hr_employment_events', 'hr_leave_ledger',
                                                          'hr_leave_requests')""")]
    return any(conn.execute(f"SELECT 1 FROM {t} WHERE idempotency_key = ?", (key,)).fetchone() for t in tables)


def _insert_event(conn, employee_id, version, event, effective_date, changes, record, reason, idempotency_key):
    snapshot = {k: record[k] for k in _SNAPSHOT_FIELDS}
    return conn.execute("""INSERT INTO hr_employment_events (employee_id, version, event, effective_date, changes,
                                                             snapshot, reason, idempotency_key)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (employee_id, version, event, effective_date, json.dumps(changes, sort_keys=True),
                         json.dumps(snapshot, sort_keys=True), reason, idempotency_key)).lastrowid


def _insert_ledger(conn, employee_id, leave_type_id, entry_type, half_days, idempotency_key, request_id=None,
                   accrual_year=None, as_of=None, actor=None, reason=None):
    return conn.execute("""INSERT INTO hr_leave_ledger (employee_id, leave_type_id, entry_type, half_days, request_id,
                                                        accrual_year, as_of, actor, reason, idempotency_key)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (employee_id, leave_type_id, entry_type, half_days, request_id, accrual_year, as_of, actor,
                         reason, idempotency_key)).lastrowid


def _decide(conn, request_id, step, idempotency_key, actor, reason, as_of, today=None):
    _positive_int("request_id", request_id)
    if actor is not None:
        _text("actor", actor)
    if reason is not None:
        _text("reason", reason, MAX_LONG_TEXT)
    if as_of is not None:
        _date("as_of", as_of)

    def work():
        req = conn.execute("SELECT * FROM hr_leave_requests WHERE id = ?", (request_id,)).fetchone()
        if req is None:
            raise NotFound(f"no leave request {request_id}")
        allowed = ("pending", "approved") if step == "cancel" else ("pending",)
        if req["status"] not in allowed:
            raise InvalidState(f"leave request {request_id} is {req['status']}")
        emp = _active_employee(conn, req["employee_id"])
        if step == "cancel":
            if req["start_date"] < _employment_start(conn, emp):
                raise InvalidState(f"leave request {request_id} is from an earlier employment")
            if req["status"] == "approved" and req["start_date"] <= today:
                raise InvalidState(f"leave request {request_id} started on {req['start_date']} (today is {today}): "
                                   f"approved leave is cancelled only before it starts; use adjust_leave")
        amount = {"approve": -req["half_days"], "reject": 0,
                  "cancel": req["half_days"] if req["status"] == "approved" else 0}[step]
        _insert_ledger(conn, req["employee_id"], req["leave_type_id"], step, amount, idempotency_key,
                       request_id=request_id, as_of=as_of, actor=actor, reason=reason)
        return leave_request(conn, request_id)

    command = {"approve": "approve_leave", "reject": "reject_leave", "cancel": "cancel_leave"}[step]
    payload = {"request_id": request_id, "actor": actor, "reason": reason, "as_of": as_of}
    return _run(conn, idempotency_key, command, payload, work)


def _employee_row(conn, employee_id):
    _lookup_id("employee", employee_id)
    row = conn.execute("SELECT * FROM hr_employees WHERE id = ?", (employee_id,)).fetchone()
    if row is None:
        raise NotFound(f"no employee {employee_id}")
    return row


def _active_employee(conn, employee_id, role="employee"):
    row = conn.execute("SELECT * FROM hr_employees WHERE id = ?", (employee_id,)).fetchone()
    if row is None:
        raise NotFound(f"no {role} {employee_id}")
    if row["status"] != "active":
        raise InvalidState(f"{role} {employee_id} is {row['status']}")
    return row


def _employment_start(conn, emp) -> str:
    """When the employee's current (or last) employment began: the latest rehire date, else start_date."""
    if not conn.execute("SELECT 1 FROM module_migrations WHERE module = ? AND version >= 3", (MODULE,)).fetchone():
        return emp["start_date"]
    row = conn.execute("""SELECT MAX(effective_date) FROM hr_employment_events
                          WHERE employee_id = ? AND event = 'rehired'""", (emp["id"],)).fetchone()
    return row[0] or emp["start_date"]


def _last_event_date(conn, employee_id) -> str:
    return conn.execute("""SELECT effective_date FROM hr_employment_events WHERE employee_id = ?
                           ORDER BY version DESC LIMIT 1""", (employee_id,)).fetchone()[0]


def _check_after_last_event(conn, employee_id, name, value):
    last = _last_event_date(conn, employee_id)
    if value < last:
        raise ValueError(f"{name} {value} is before employee {employee_id}'s latest employment event ({last})")


def _management_chain(conn, employee_id) -> list[int]:
    """The employee's managers, nearest first."""
    chain, current = [], conn.execute("SELECT manager_id FROM hr_employees WHERE id = ?", (employee_id,)).fetchone()
    while current is not None and current["manager_id"] is not None and current["manager_id"] not in chain:
        chain.append(current["manager_id"])
        current = conn.execute("SELECT manager_id FROM hr_employees WHERE id = ?", (current["manager_id"],)).fetchone()
    return chain


def _check_manager(conn, employee_id, manager_id):
    if manager_id == employee_id:
        raise ValueError("an employee can't be their own manager")
    _active_employee(conn, manager_id, "manager")
    if employee_id in _management_chain(conn, manager_id):
        raise ValueError(f"employee {manager_id} reports to employee {employee_id}: that would be a loop")


def _check_email_free(conn, email, employee_id):
    if email is None:
        return
    taken = conn.execute("SELECT id FROM hr_employees WHERE lower(work_email) = lower(?) AND id IS NOT ?",
                         (email, employee_id)).fetchone()
    if taken:
        raise InvalidState(f"work_email {email!r} is already employee {taken['id']}'s")


_CLEANED_FIELDS = ("employee_number", "first_name", "last_name", "preferred_name", "work_email", "work_phone",
                   "job_title", "department", "work_location")


def _clean(value):
    """NFC, surrounding whitespace trimmed and inner whitespace collapsed to one space."""
    return " ".join(unicodedata.normalize("NFC", value).split())


def _clean_field(name, value):
    if name not in _CLEANED_FIELDS or not isinstance(value, str):
        return value              # left for _check_employee_fields to refuse
    value = _clean(value)
    return value.lower() if name == "work_email" else value


def _check_employee_fields(record):
    for name in ("employee_number", "first_name", "job_title"):
        _text(name, record[name])
    for name in ("last_name", "preferred_name", "work_phone", "department", "work_location"):
        if record[name] is not None:
            _text(name, record[name])
    email = record["work_email"]
    if email is not None:
        _text("work_email", email)
        local, _, domain = email.partition("@")
        if not local or not domain or " " in email:
            raise ValueError(f"work_email must look like name@domain, got {email!r}")
    if record["employment_type"] not in EMPLOYMENT_TYPES:
        raise ValueError(f"employment_type must be one of {', '.join(EMPLOYMENT_TYPES)}, "
                         f"got {record['employment_type']!r}")
    if record["manager_id"] is not None:
        _positive_int("manager_id", record["manager_id"])
    _date("start_date", record["start_date"])


def _leave_type_row(conn, code):
    row = conn.execute("SELECT * FROM hr_leave_types WHERE code = ?", (code,)).fetchone()
    if row is None:
        raise NotFound(f"no leave type {code!r}")
    return row


def _leave_type_dict(row) -> dict:
    return {"code": row["code"], "name": row["name"], "paid": bool(row["paid"]),
            "tracks_balance": bool(row["tracks_balance"]),
            "annual_allowance_days": row["annual_allowance_half_days"] / 2}


def _employee_dict(row) -> dict:
    return ({"employee_id": row["id"]} | {k: row[k] for k in _SNAPSHOT_FIELDS}
            | {"version": row["version"], "manager_name": row["manager_name"]})


def _request_dict(row) -> dict:
    d = dict(row)
    d["days"] = d.pop("half_days") / 2
    return d


def _positive_int(name, value):
    if type(value) is not int or not 0 < value <= MAX_ID:
        raise ValueError(f"{name} must be a positive integer, got {value!r}")


def _bool(name, value):
    if type(value) is not bool:
        raise ValueError(f"{name} must be true or false, got {value!r}")


def _text(name, value, limit=MAX_TEXT):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string, got {value!r}")
    if len(value) > limit:
        raise ValueError(f"{name} must be at most {limit} characters, got {len(value)}")


def _lookup_id(what, value):
    """An id to read by: not an integer is a ValueError; one no row can have (0, negative, past 2**63-1) is
    NotFound, never sqlite3's OverflowError."""
    if type(value) is not int:
        raise ValueError(f"{what} id must be an integer, got {value!r}")
    if not 0 < value <= MAX_ID:
        raise NotFound(f"no {what} {value}")


def _code(name, value):
    _text(name, value)
    if not all(c.isascii() and (c.islower() or c.isdigit() or c == "_") for c in value):
        raise ValueError(f"{name} must be lowercase letters, digits and '_', got {value!r}")


def _half_days(name, days, allow_zero=False, allow_negative=False) -> int:
    """days (a whole or half number) as an integer count of half-days."""
    if type(days) not in (int, float) or days != days or days in (float("inf"), float("-inf")):
        raise ValueError(f"{name} must be a number of days, got {days!r}")
    if abs(days) > MAX_LEAVE_DAYS:
        raise ValueError(f"{name} must be at most {MAX_LEAVE_DAYS} days, got {days!r}")
    half = days * 2
    if half != int(half):
        raise ValueError(f"{name} must be a whole or half day, got {days!r}")
    half = int(half)
    if half < 0 and not allow_negative or half == 0 and not allow_zero:
        raise ValueError(f"{name} must be {'non-zero' if allow_negative else 'positive'}, got {days!r}")
    return half


def _date(name, value):
    try:
        ok = isinstance(value, str) and datetime.date.fromisoformat(value).isoformat() == value
    except ValueError:
        ok = False
    if not ok:
        raise ValueError(f"{name} must be a date as 'YYYY-MM-DD', got {value!r}")
