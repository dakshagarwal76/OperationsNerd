# HR module: employee records and leave

Employee records with a full employment history, plus leave/PTO with an
append-only ledger. Built in the same way as the logistics stock engine
(`modules/logistics/`, on `jasmeet/logistics-stock`). It uses stdlib `sqlite3`
only, imports nothing from `operations_nerd/` and makes no LLM calls.

No minimum SQLite version is needed. `date()` before SQLite 3.45 lets
impossible days such as 2026-02-30 through (3.37 and 3.40 were checked), so each
date CHECK also compares the day with the last day of its month; the rules are
the same on every version.

```
modules/hr/
  hr.py                  commands and queries (the API other code calls)
  migrate.py             versioned migrations + automatic backup (usable by any module)
  migrations/0001_employees.sql
  migrations/0002_leave.sql
  migrations/0003_rehire_and_half_days.sql
  migrations/0004_calendar_day_guards.sql
  api.py                 FastAPI router, /hr/... (local dev only, see below)
  demo_toy_shop.py       walkthrough for a small toy shop
  tests/                 plain scripts, also collected by pytest
```

Each business has its own private database, so nothing takes a business id: a
connection is one business's people. Every table is prefixed `hr_`.

## Design

**Employees are event-sourced.** `hr_employment_events` is the ledger: every
command that changes an employee appends one event (`hired`, `updated`,
`terminated`, `rehired`) carrying the change (`changes`: field -> [old, new]) and the full
record after it (`snapshot`). A trigger projects the snapshot onto
`hr_employees`. No caller writes `hr_employees`, and its triggers refuse any
row that doesn't match its latest event, so the current record and its
history can't drift apart (the lesson from logistics, where config quantities
drifted from the ledger).

**Leave balances are a ledger.** `hr_leave_ledger` rows are signed half-days:

| entry       | amount                          | when |
|-------------|---------------------------------|------|
| accrual     | + allowance (prorated year 1)   | once per employee, type and year |
| adjustment  | + or -, needs a reason          | manual correction |
| request     | 0                               | a request is made (holds days as pending) |
| approve     | - request amount                | request approved |
| reject      | 0                               | request rejected |
| cancel      | + amount if it was approved, else 0 | request cancelled |

`balance = sum(ledger)`, `pending = sum(pending requests)`,
`available = balance - pending`. A request must fit in `available`, so two
pending requests can't both spend the same days. Amounts are whole half-days in
the database (exact arithmetic); commands take and return days.

**Commands** each run in one `BEGIN IMMEDIATE` transaction with an idempotency
key unique across all commands (`hr_commands`). Replaying a key with the same
command and payload returns the stored result and writes nothing; the same key
with anything else raises `IdempotencyConflict`; a failed command records
nothing, so its key stays free. Dates come from the caller; the one exception
is `cancel_leave`'s `today` (see below), which falls back to the clock only
when it isn't given and is never stored in the command's payload.

**Text is normalised before it's stored.** Every identity and text field
(employee number, names, email, phone, job title, department, location) is
NFC-normalised, trimmed, and inner runs of whitespace become one space; work
email is also lowercased. So `" E001 "` and `"E001"` are the same number, and
`Maya@Shop.test` clashes with `maya@shop.test`. Text inputs are capped at
`hr.MAX_TEXT` (200) characters, reasons and notes at `hr.MAX_LONG_TEXT` (2000),
idempotency keys at 200; longer is a `ValueError` (422 in the API).

### Tables

| table | what | rules (enforced by the database) |
|---|---|---|
| `hr_command_names` | allowed command names | each migration adds its own |
| `hr_commands` | idempotency log | immutable |
| `hr_employees` | current record (projection) | written only by an event; born active at v1; version +1 per change; terminated is frozen until a `rehired` event; a rehire's manager must be active; id, number and start date never change; manager must be an active employee (not themself); can't terminate someone with active reports; never deleted; unique number, unique work email (case-insensitive) |
| `hr_employment_events` | employment history | immutable; in version order; dated on or after the previous event; `hired` exactly at v1; `terminated` exactly when the snapshot is terminated, with end_date = effective_date; `rehired` only for a terminated employee, after their end date; a hire's employee number is trimmed and any email written is trimmed and lowercase |
| `hr_leave_types` | `code`, `name`, `paid`, `tracks_balance`, annual allowance | only the allowance changes, and only through a policy change; an untracked type has no allowance; never deleted |
| `hr_leave_policy_changes` | allowance history | immutable; tracked types only |
| `hr_leave_requests` | requests and their status | born pending; employee active; on/after the start of their current employment; no overlap with their pending/approved requests of any type (an `am` and a `pm` half of one day don't overlap; a whole day or an old half day with no `half` blocks both); `half` only on a single-day half-day request; within available (tracked types); status moves only with the ledger; nothing else changes; never deleted |
| `hr_leave_ledger` | the balance ledger | immutable; accrual/adjustment on tracked types only; employee active for every entry (termination settles their leave before the `terminated` event, in the same transaction); one accrual per year; each step follows the request's state; amounts match the request; balance never negative; adjustment within available |
| `module_migrations` | applied schema versions per module | shared by all modules (see Migrations) |

Termination cleans up leave in its own transaction; afterwards nothing more is
written to a terminated employee's leave (a later cancel is an `InvalidState`),
and their balances stay readable.

### Fields, and what was reused from PR #21

The fields come from Harsh's HR config pack (PR #21, `packs/hr/entity_schemas.yaml`
and `questionnaire.yaml`), thanks Harsh:

| PR #21 | here |
|---|---|
| `crm_employee.first_name`, `last_name` | same |
| `email` | `work_email` |
| `role` | `job_title` |
| `department` | same |
| `manager` | `manager_id`, checked against active employees |
| `start_date` | same, fixed once set |
| `pto_balance_days` | not stored: derived from the leave ledger (`leave_balance`) |
| setting `pto_annual_days` | a leave type's `annual_allowance_days` (the demo's `pto` type) |
| setting `leave_approver_role` | the `approver` recorded on approve/reject (no role check yet) |

Added: `employee_number` (required, unique), `preferred_name`, `work_phone`,
`employment_type` (full_time, part_time, contractor, temporary, intern),
`work_location`, `status`, `end_date`, `termination_reason`.

Customizable fields are **not** wired into the chat spec. That would mean
changing `operations_nerd/agentic/`, which CONTRIBUTING says not to touch
without agreement, and there's no module interface yet. Until then an owner can
keep extra, non-sensitive fields on a CRM "Employee" entity in the chat app,
linked by `employee_number`. When the module interface exists, the spec can
point at `hr.add_employee` / `hr.update_employee`; the field list is in
`hr.EMPLOYEE_FIELDS` / `hr.UPDATABLE_FIELDS`.

### No payroll or sensitive fields

There are no salary, pay rate, bank, tax ID, national ID, date of birth, home
address or health fields, and a test fails if one appears in a table column, the employee field
lists or an API request body. Why:

- The databases are plain SQLite files with automatic backup copies. There is
  no encryption at rest, no field-level encryption, and no access control
  inside a business (everyone with the app sees everything).
- Leaked pay, bank or ID data does real harm and carries legal duties
  (data-protection and breach-notification rules) that we can't meet yet.
- Payroll also needs tax rules per country, which is its own project.

Add them only after encryption, per-role access and an audit of who read what.
Sick leave is a leave type with no medical detail: a `note` is optional and
should stay non-medical.

## Commands and queries (`hr.py`)

```python
import hr
conn = hr.connect("business.db")
hr.init_db(conn)                       # migrates to the latest version, backing up first

hr.add_employee(conn, key, employee_number=..., first_name=..., job_title=..., start_date=..., **optional)
hr.update_employee(conn, employee_id, {"job_title": ...}, effective_date, key, reason=None)
hr.terminate_employee(conn, employee_id, end_date, reason, key, reassign_reports_to=None)
hr.rehire_employee(conn, employee_id, rehire_date, key, changes=None, reason=None)

hr.create_leave_type(conn, code, name, key, paid=True, tracks_balance=True, annual_allowance_days=0)
hr.set_leave_allowance(conn, code, days, reason, key)
hr.accrue_annual_leave(conn, employee_id, code, year, key)
hr.adjust_leave(conn, employee_id, code, days, reason, key, as_of=None)
hr.request_leave(conn, employee_id, code, start_date, end_date, key, half_day=False, note=None, as_of=None,
                 half=None)            # 'am' (the default for a half day) or 'pm'
hr.approve_leave(conn, request_id, key, approver=None, as_of=None)
hr.reject_leave(conn, request_id, reason, key, approver=None, as_of=None)
hr.cancel_leave(conn, request_id, key, reason=None, actor=None, as_of=None, today=None)

hr.employee / employees / employment_history      # employee() includes employment_start_date
hr.verify_schema(conn)                 # raises SchemaTampered (init_db calls it)
hr.leave_type / leave_types / leave_balance / leave_balances / leave_request / leave_requests / leave_ledger
```

Errors: `ValueError` (bad input), `NotFound`, `InvalidState`, `Overlap`,
`InsufficientBalance`, `IdempotencyConflict`, `SchemaTampered` (all but
`ValueError` subclass `HRError`); a raw write that breaks a database rule raises
`sqlite3.IntegrityError`.

- **Accrual** is front-loaded: the year's allowance at once. In the start
  year it's prorated by calendar days from the start date to 31 December,
  rounded down to a half-day.
- **Requests** count Monday-Friday days between the dates (inclusive), or half
  a day with `half_day=True` on a single day: the morning (`half="am"`, the
  default) or the afternoon (`half="pm"`), so both halves of one day can be
  booked. An untracked type (unpaid leave) has no balance but is still checked
  for overlaps.
- **Cancelling:** a pending request can be cancelled any time. An approved one
  only before it starts: from its `start_date` on (`start_date <= today`) the
  cancel is an `InvalidState`, and days already taken are corrected with
  `adjust_leave`. `today` defaults to `date.today()` on the server (the API
  passes `api.today()`, which tests and a deployment with its own business
  time zone can replace). A request from an earlier employment (before a
  rehire) can't be cancelled.
- **Termination**, in one transaction: active reports move to
  `reassign_reports_to` (required if there are any), dated the end date or
  their own latest event if later; pending requests are cancelled; approved
  requests that start after the end date are cancelled and credited back;
  an approved request that spans the end date is cancelled in full and
  replaced by an approved request for its days up to the end date, so only
  those are charged (`shortened_leave` in the result lists each: `request_id`,
  `replaced_by` or null if no working day was left, `kept_days`,
  `credited_days`). Derived keys (`key/report:N`, `key/cancel:N`,
  `key/shorten:N`, `.../request`, `.../approve`) keep the whole thing
  idempotent; a key already taken by another command, or a later command
  reusing a derived key, is an `IdempotencyConflict`. Then the employee is
  frozen.
- **Rehire** brings back the same record: same id, employee number, work
  email (if nobody else has it now) and the whole history, with a `rehired`
  event after the end date. `changes` may set any updatable field (a new job
  title, or a new manager if the old one has left, which is required then).
  `start_date` stays the first hire date; `employment_start_date` is the
  latest rehire date, and accrual proration and "leave on/after start" use it.
  The balance carries over; a year already accrued isn't accrued again.
- **Effective dates never go back:** an update, termination or rehire dated
  before the employee's latest event is a `ValueError` (and a raw event is
  refused by the database).
- **Amounts** (allowance, adjustment) are capped at `MAX_LEAVE_DAYS`
  (100,000) days and ids at SQLite's 2^63 - 1, so a typo is a `ValueError`,
  not a crash. A read by an id no row can have (0, negative, past 2^63 - 1) is
  `NotFound`; a non-integer id is a `ValueError`.

### Trust boundary

The rules live in the database's triggers and CHECKs, so they protect against
any code that writes through SQLite, but not against someone who can change
the schema: anyone with write access to the file can drop a trigger. That
access is the trust boundary; the module doesn't try to defend against it.
What it does is notice: `init_db` (and the API, on every request) runs
`verify_schema`, which compares every `hr_` trigger, index and table with
what the migration files create and raises `SchemaTampered` (503 in the API)
on anything missing, changed or extra. Restoring the definition (or a
backup) clears it.

## HTTP API (`api.py`)

| method | path | |
|---|---|---|
| GET, POST | `/hr/employees` | list (`?status=`, `?department=`), hire |
| GET, PATCH | `/hr/employees/{id}` | read, update (`changes`, `effective_date`, `reason`) |
| POST | `/hr/employees/{id}/terminate` | `end_date`, `reason`, `reassign_reports_to` |
| POST | `/hr/employees/{id}/rehire` | `rehire_date`, `changes`, `reason` |
| GET | `/hr/employees/{id}/history` | employment events |
| GET | `/hr/employees/{id}/leave/balances` | every type |
| GET | `/hr/employees/{id}/leave/ledger` | `?leave_type=` |
| GET, POST | `/hr/leave/types` | list, create |
| PUT | `/hr/leave/types/{code}/allowance` | new allowance + reason |
| POST | `/hr/leave/accruals`, `/hr/leave/adjustments` | |
| GET, POST | `/hr/leave/requests` | list (`?employee_id=`, `?status=`), request (`half_day`, `half`) |
| GET | `/hr/leave/requests/{id}` | |
| POST | `/hr/leave/requests/{id}/approve`, `/reject` (reason required), `/cancel` | the body is optional |
| GET | `/hr/schema` | schema version |

Every POST, PATCH and PUT needs an `Idempotency-Key` header (it becomes the
command's key, so clients can retry). Status codes: 422 bad input (unknown
fields are refused), 404 not found, 409 conflicts (`InvalidState`, `Overlap`,
`InsufficientBalance`, `IdempotencyConflict`, database rule), 503 if
`HR_DATABASE_PATH` isn't set, if a database this process already set up has
disappeared (it is not quietly recreated empty; putting the file back
recovers), if the database can't be read, or on `SchemaTampered`. A database
behind the code's schema version is migrated on its next request. Every string
field, query parameter and the `Idempotency-Key` header is length-capped
(as above).

There's no login or per-business database routing on `main` yet, so the router
is a **local development surface only**: `main.py` mounts it when
`HR_MODULE_ENABLED=1`, never when `APP_ENV=production`, on the SQLite file in
`HR_DATABASE_PATH`. There is no UI (logistics has none either).

```
cd operations_nerd
LLM_PROVIDER=stub HR_MODULE_ENABLED=1 HR_DATABASE_PATH=/tmp/hr_dev.db uvicorn main:app
```

PowerShell: `$env:HR_MODULE_ENABLED="1"; $env:HR_DATABASE_PATH="$env:TEMP\hr_dev.db"; uvicorn main:app`.

## Migrations (`migrate.py`)

This is the hook for "one private database per business, versioned upgrades,
automatic backups":

- A module's schema is `migrations/NNNN_name.sql`, numbered from 0001 with no
  gaps. A released file never changes; a schema change is a new file (0003, ...).
- `module_migrations(module, version, name, applied_at)` records what each
  module has applied, so several modules share one business database.
  `PRAGMA user_version` is left alone (it's one number per file).
- `migrate(conn, module, folder, backup_dir=None, target=None)` applies the
  missing versions in order. Before it changes a database that already has
  tables it copies the whole file with SQLite's backup API to
  `<db>.<module>-vNNNN.<UTC time>.bak` (next to it, or in `backup_dir`).
  `.gitignore` now ignores `*.bak`.
- A run takes one `BEGIN IMMEDIATE` lock, re-reads the version inside it and
  only then backs up (through a second, read-only connection), so two
  processes migrating at once take one backup and apply each version once.
  Each version runs in its own savepoint with its `module_migrations` row: a
  failing version rolls back completely, while the versions before it in the
  same run are kept. It refuses to go backwards or to open a database newer
  than the code.
- 0003 rebuilds `hr_employment_events` (to allow `rehired`), keeping every
  row and id, and recreates the triggers it replaces under the same names.
  Half-day requests made before it have no `half` and still block the whole
  day.
- 0004 adds one AFTER INSERT trigger per date column (six) that refuses a day past the end of its month. It is
  for databases already at version 3 from a release whose CHECKs lacked the day guard: an applied file never
  runs again and SQLite cannot change a CHECK in place. On a new database the CHECK refuses first and the triggers
  do not fire. Rows already stored are left as they are.

`hr.init_db(conn, backup_dir=None)` is `migrate(conn, "hr", hr.MIGRATIONS)`.
Whoever builds the per-business database opener should call each module's
`init_db` when opening a business's file (and pick the backup directory and
retention, which nothing here prunes yet).

## Tests

From the repo root (the core runner now includes module tests too):

```
python scripts/run_tests.py modules/hr          # just HR
python -B modules/hr/tests/test_leave.py       # one script
python -m pytest modules/hr/tests              # if pytest is installed
python -B modules/hr/tests/mutation_check.py   # ~1 min, not run by CI
```

| script | tests | covers |
|---|---|---|
| `test_employees.py` | 17 | hiring, required/invalid fields, no sensitive fields, unique number/email, managers and loops, update diffs, termination and report reassignment, idempotent replay/conflict/failed key, 8 concurrent hires, raw SQL against every employee/event rule |
| `test_leave.py` | 23 | leave types and allowance policy, accrual + proration, adjustments, working days and half days, approve/reject/cancel with credit-back, balance enforcement with pending holds, untracked types, overlaps across types, racing requests, termination effects, idempotency, ledger immutability, raw SQL against every leave rule, impossible days (2026-02-30) refused in every leave date column, deletes on a connection with foreign keys off, amounts/ids too big for SQLite, termination's derived keys never colliding |
| `test_migrations.py` | 22 | fresh, rerun, stepwise upgrade with backup holding the old data, failing migration rolls back (keeping earlier versions of the same run), numbering rules, newer database refused, sharing a database with another module's tables, concurrent migrators taking one backup, upgrading a v2 database to 3 keeps every row and rule, every date CHECK refuses impossible days on any SQLite version, in-memory; a database already at version 3 without the day guard gains six triggers on upgrade, keeps every row, refuses impossible days in every date column, a failing 0004 rolls back to 3 |
| `test_hardening.py` | 19 | the cancel clock, termination with spanning leave (charged to the end date) and nothing written after it, rehire (same record, rules, old-employment leave), trimmed/normalised identity fields, length limits on every text input, event dates in order, huge or odd ids, am/pm halves, the startup schema check; raw SQL against each new rule |
| `test_api.py` | 14 | every endpoint, no sensitive fields in request bodies, 409/422/404/503 mapping, Idempotency-Key required and replayed, `main.py` mounts `/hr` only when enabled, too-big numbers, strict JSON types (no `"2"`→2 or `true`→1 coercion), empty decision bodies, a vanished database is a 503 and recovers, a tampered schema is a 503, rehire and half endpoints, length limits on bodies, header, query and path |
| `test_toy_shop_walkthrough.py` | 2 | the demo end to end, and replaying it writes nothing |

"Raw" tests write SQL directly, bypassing `hr.py`, so anything they see
rejected is rejected by the database itself.

`mutation_check.py` removes each database rule in turn (every trigger, unique
index and CHECK, where a current database gets it from) plus 20 pieces of
command logic, from a temp copy, and reruns the tests. Result: 121 removals,
103 caught, 10 redundant (implied by other rules; still rejected, with another
message), 8 shadowed (another rule always fires first, so no test can tell). Both lists are pinned in the script
with the rule that covers each.

## Walkthrough

`python -B modules/hr/demo_toy_shop.py` (a temp database, deleted afterwards;
`--db shop.db` keeps it): Tiny Tots Toys hires an owner, a part-time assistant
and a weekend helper, sets up `pto` (20 days, from PR #21's `pto_annual_days`)
and unpaid leave, accrues 2026, approves the assistant's 3 days, and refuses
the helper's 10-day request because only 6 prorated days are available.

## Known limits (v1)

- No carry-over, expiry, accrual schedules (monthly) or negative balances.
- No public-holiday calendar: requests count Monday-Friday.
- `start_date` and `employee_number` can't be corrected; a typo means a new record.
- Approver role isn't checked (no users/roles in the module yet).
- That every request has its `request` ledger entry is guaranteed by the
  command, not a trigger (SQLite can't check it at commit); a raw request
  without one can never be decided (tested).
- Backups aren't pruned.
- Someone with write access to the database file can still change it; the
  schema check notices changed rules, not changed data (see Trust boundary).
- Cancelling uses one calendar date; there's no per-business time zone yet
  (replace `api.today`).

## Notes for Amaan and the team

- **No conflicts with logistics.** HR touches only `modules/hr/` plus small
  additive edits to `operations_nerd/main.py`, `scripts/run_tests.py`,
  `documentation/CONTRIBUTING.md` and `.gitignore`; none of the open branches
  change those files. Merged together with `jasmeet/logistics-stock`, all the
  module test scripts passed (checked before 0003; not rechecked since) and both modules share one database fine.
- **The runner now runs module tests** (`modules/*/tests/test_*.py`, from the
  repo root). Once logistics merges, CI runs its 4 scripts too (checked: they
  pass, ~8 s).
- **Migration hook:** logistics creates its schema with `schema.sql` and no
  versions. It could adopt `migrate.py` as is (move `schema.sql` to
  `migrations/0001_stock.sql`). If both modules keep a top-level `migrate`
  import via `sys.path`, move it to a shared place first so they don't shadow
  each other.
- **Per-business database:** `api.py` reads one file from `HR_DATABASE_PATH`.
  When login and per-business routing land, replace `get_conn` with the
  signed-in business's connection and drop the production guard.
