-- Real calendar days on databases that were migrated before the day guard existed.
--
-- A release of 0001-0003 stored dates with `x IS date(x)` alone. Before SQLite 3.45, date() accepts days that do
-- not exist (2026-02-30, 2026-04-31, 2025-02-29), so those CHECKs let them through. The CHECKs in 0001-0003 now also
-- compare the day with the last day of the month, but a file that was already applied is never run again, and
-- SQLite cannot change a CHECK in place. A database migrated to version 3 by the earlier release therefore still
-- accepts impossible days. These triggers close that gap: one per date column, refusing an insert whose day is
-- past the end of its month. A NULL date stays allowed where the column allows it; the CHECKs still refuse
-- anything that is not an ISO date. The triggers run AFTER the insert, so on a new database the CHECK refuses the
-- day first with its own message and these never fire; on an upgraded one they are the only guard.

CREATE TRIGGER hr_employees_start_date_real_day AFTER INSERT ON hr_employees
WHEN NEW.start_date IS NOT NULL
 AND substr(NEW.start_date, 9, 2) > substr(date(substr(NEW.start_date, 1, 8) || '01', '+1 month', '-1 day'), 9, 2)
BEGIN SELECT RAISE(ABORT, 'hr_employees.start_date is not a real calendar day'); END;

CREATE TRIGGER hr_employees_end_date_real_day AFTER INSERT ON hr_employees
WHEN NEW.end_date IS NOT NULL
 AND substr(NEW.end_date, 9, 2) > substr(date(substr(NEW.end_date, 1, 8) || '01', '+1 month', '-1 day'), 9, 2)
BEGIN SELECT RAISE(ABORT, 'hr_employees.end_date is not a real calendar day'); END;

CREATE TRIGGER hr_employment_events_effective_date_real_day AFTER INSERT ON hr_employment_events
WHEN NEW.effective_date IS NOT NULL
 AND substr(NEW.effective_date, 9, 2) > substr(date(substr(NEW.effective_date, 1, 8) || '01', '+1 month', '-1 day'), 9, 2)
BEGIN SELECT RAISE(ABORT, 'hr_employment_events.effective_date is not a real calendar day'); END;

CREATE TRIGGER hr_leave_requests_start_date_real_day AFTER INSERT ON hr_leave_requests
WHEN NEW.start_date IS NOT NULL
 AND substr(NEW.start_date, 9, 2) > substr(date(substr(NEW.start_date, 1, 8) || '01', '+1 month', '-1 day'), 9, 2)
BEGIN SELECT RAISE(ABORT, 'hr_leave_requests.start_date is not a real calendar day'); END;

CREATE TRIGGER hr_leave_requests_end_date_real_day AFTER INSERT ON hr_leave_requests
WHEN NEW.end_date IS NOT NULL
 AND substr(NEW.end_date, 9, 2) > substr(date(substr(NEW.end_date, 1, 8) || '01', '+1 month', '-1 day'), 9, 2)
BEGIN SELECT RAISE(ABORT, 'hr_leave_requests.end_date is not a real calendar day'); END;

CREATE TRIGGER hr_leave_ledger_as_of_real_day AFTER INSERT ON hr_leave_ledger
WHEN NEW.as_of IS NOT NULL
 AND substr(NEW.as_of, 9, 2) > substr(date(substr(NEW.as_of, 1, 8) || '01', '+1 month', '-1 day'), 9, 2)
BEGIN SELECT RAISE(ABORT, 'hr_leave_ledger.as_of is not a real calendar day'); END;
