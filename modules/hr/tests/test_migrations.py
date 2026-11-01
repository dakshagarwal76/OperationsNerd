"""
HR module: versioned migrations and the automatic backup before an upgrade
(migrate.py). Each test gets its own throwaway database and, where it needs
broken or extra migration files, its own copy of migrations/. Run from the
repo root with the venv (pytest also collects these):

    python -B modules/hr/tests/test_migrations.py
"""

import os
import shutil
import sqlite3
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import hr as h
import migrate as m
from test_employees import rejected

TMP = tempfile.TemporaryDirectory(prefix="hr_migrations_", ignore_cleanup_errors=True)
LATEST = len(m.migrations(h.MIGRATIONS))


def fresh(name: str) -> tuple[sqlite3.Connection, str]:
    path = os.path.join(TMP.name, name + ".db")
    return h.connect(path), path


def tables(conn) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def folder_copy(name: str) -> Path:
    dest = Path(TMP.name) / f"{name}_migrations"
    shutil.copytree(h.MIGRATIONS, dest)
    return dest


def test_a_new_database_gets_every_version_and_no_backup():
    conn, path = fresh("new")
    out = h.init_db(conn)
    assert out == {"module": "hr", "from_version": 0, "to_version": LATEST, "applied": list(range(1, LATEST + 1)),
                   "backup": None}
    assert h.schema_version(conn) == LATEST == h.latest_version() == 4
    assert {"hr_employees", "hr_employment_events", "hr_leave_ledger", "hr_leave_requests"} <= tables(conn)
    names = [tuple(r) for r in conn.execute("SELECT module, version, name FROM module_migrations ORDER BY version")]
    assert names == [("hr", 1, "employees"), ("hr", 2, "leave"), ("hr", 3, "rehire_and_half_days"),
                     ("hr", 4, "calendar_day_guards")]
    assert not [f for f in os.listdir(TMP.name) if f.startswith("new.db.") and f.endswith(".bak")]


def test_running_it_again_changes_nothing():
    conn, _ = fresh("again")
    h.init_db(conn)
    assert h.init_db(conn) == {"module": "hr", "from_version": LATEST, "to_version": LATEST, "applied": [],
                               "backup": None}


def test_an_upgrade_backs_up_the_database_first_and_keeps_the_data():
    conn, path = fresh("upgrade")
    assert m.migrate(conn, "hr", h.MIGRATIONS, target=1)["applied"] == [1]
    assert "hr_leave_ledger" not in tables(conn)
    h.add_employee(conn, "hire", employee_number="E001", first_name="Maya", job_title="Owner", start_date="2025-01-06")
    backups = Path(TMP.name) / "upgrade_backups"
    out = h.init_db(conn, backup_dir=str(backups))
    assert out["from_version"] == 1 and out["applied"] == [2, 3, 4] and out["to_version"] == 4
    assert out["backup"] and Path(out["backup"]).parent == backups and "hr-v0001" in out["backup"]
    copy = sqlite3.connect(out["backup"])
    assert copy.execute("SELECT first_name FROM hr_employees").fetchall() == [("Maya",)]
    assert "hr_leave_ledger" not in {r[0] for r in copy.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert copy.execute("SELECT MAX(version) FROM module_migrations WHERE module = 'hr'").fetchone()[0] == 1
    copy.close()
    assert h.employee(conn, 1)["first_name"] == "Maya"
    h.create_leave_type(conn, "annual", "Annual leave", "lt", annual_allowance_days=20)


def test_a_failing_migration_rolls_back_and_leaves_the_previous_version():
    conn, _ = fresh("failing")
    folder = folder_copy("failing")
    broken = folder / f"{LATEST + 1:04d}_broken.sql"
    broken.write_text("CREATE TABLE hr_half_done (id INTEGER PRIMARY KEY);\n"
                      "INSERT INTO no_such_table VALUES (1);\n")
    h.init_db(conn)
    rejected(lambda: m.migrate(conn, "hr", folder), "no such table: no_such_table", sqlite3.OperationalError)
    assert h.schema_version(conn) == LATEST and "hr_half_done" not in tables(conn)
    assert not conn.in_transaction
    broken.write_text("CREATE TABLE hr_half_done (id INTEGER PRIMARY KEY);\n")
    out = m.migrate(conn, "hr", folder)
    assert out["applied"] == [LATEST + 1] and out["backup"] and "hr_half_done" in tables(conn)


def test_a_failing_version_keeps_the_versions_before_it_from_the_same_run():
    """One run holds one lock, but each version is its own savepoint: 1..LATEST stay when LATEST+1 fails."""
    conn, _ = fresh("failing_late")
    folder = folder_copy("failing_late")
    (folder / f"{LATEST + 1:04d}_broken.sql").write_text("CREATE TABLE hr_half_done (id INTEGER PRIMARY KEY);\n"
                                                         "INSERT INTO no_such_table VALUES (1);\n")
    rejected(lambda: m.migrate(conn, "hr", folder), "no such table: no_such_table", sqlite3.OperationalError)
    assert h.schema_version(conn) == LATEST and "hr_half_done" not in tables(conn) and not conn.in_transaction
    h.verify_schema(conn)


def test_migration_files_must_be_numbered_without_gaps_and_only_go_forward():
    conn, _ = fresh("rules")
    folder = folder_copy("gap")
    (folder / f"{LATEST + 2:04d}_skip.sql").write_text("SELECT 1;\n")
    rejected(lambda: m.migrate(conn, "hr", folder), f"expected version {LATEST + 1:04d}", m.MigrationError)
    folder2 = folder_copy("badname")
    (folder2 / "3_bad.sql").write_text("SELECT 1;\n")
    rejected(lambda: m.migrate(conn, "hr", folder2), "is not named NNNN_name.sql", m.MigrationError)
    h.init_db(conn)
    rejected(lambda: m.migrate(conn, "hr", h.MIGRATIONS, target=1), "migrations only go forward", m.MigrationError)
    rejected(lambda: m.migrate(conn, "hr", h.MIGRATIONS, target=7), "no migration 7", m.MigrationError)
    conn.execute("INSERT INTO module_migrations (module, version, name) VALUES ('hr', ?, 'from_the_future')",
                 (LATEST + 1,))
    rejected(lambda: h.init_db(conn), "newer than this code", m.MigrationError)


def test_modules_share_a_business_database_without_touching_each_other():
    """One business database holds every module's tables: logistics' stock tables are left alone."""
    conn, path = fresh("shared")
    conn.execute("CREATE TABLE stock_movements (id INTEGER PRIMARY KEY, quantity INTEGER NOT NULL)")
    conn.execute("INSERT INTO stock_movements (quantity) VALUES (5)")
    conn.execute("PRAGMA user_version = 7")                                   # whatever another tool keeps there
    out = h.init_db(conn)
    assert out["applied"] == list(range(1, LATEST + 1)) and out["backup"]     # not empty, so backed up first
    assert [tuple(r) for r in conn.execute("SELECT quantity FROM stock_movements")] == [(5,)]
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 7
    assert m.current_version(conn, "logistics") == 0
    for name in tables(conn) - {"stock_movements", "module_migrations"}:
        assert name.startswith("hr_"), name                                    # every HR table is prefixed


def test_two_processes_migrating_at_once_apply_each_version_once():
    _, path = fresh("race")
    errors, barrier = [], threading.Barrier(4)

    def worker():
        conn = h.connect(path)
        try:
            barrier.wait()
            h.init_db(conn)
        except Exception as e:                                                   # pragma: no cover
            errors.append(e)
        finally:
            conn.close()

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    conn = h.connect(path)
    assert [r[0] for r in conn.execute("SELECT version FROM module_migrations WHERE module = 'hr'")] == list(
        range(1, LATEST + 1))


def test_concurrent_migrators_take_one_backup():
    """Four processes upgrade the same v1 database at once: one backs it up and migrates, the others wait for its
    lock, see the new version and do nothing (no second backup of the already-migrated file)."""
    conn, path = fresh("race_backup")
    m.migrate(conn, "hr", h.MIGRATIONS, target=1)
    h.add_employee(conn, "hire", employee_number="E001", first_name="Maya", job_title="Owner", start_date="2025-01-06")
    conn.close()
    backups = Path(TMP.name) / "race_backups"
    results, errors, barrier = [], [], threading.Barrier(4)

    def worker():
        c = h.connect(path)
        try:
            barrier.wait()
            results.append(h.init_db(c, backup_dir=str(backups)))
        except Exception as e:                                                   # pragma: no cover
            errors.append(e)
        finally:
            c.close()

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    assert len(os.listdir(backups)) == 1, os.listdir(backups)
    assert sorted(len(r["applied"]) for r in results) == [0, 0, 0, LATEST - 1]
    assert [r["backup"] for r in results if r["backup"]] == [str(backups / os.listdir(backups)[0])]
    copy = sqlite3.connect(backups / os.listdir(backups)[0])
    assert copy.execute("SELECT MAX(version) FROM module_migrations").fetchone()[0] == 1
    copy.close()


def test_upgrading_to_3_rebuilds_the_event_table_and_keeps_every_row_and_rule():
    """0003 copies hr_employment_events into a new table (to allow 'rehired'); history and rules survive."""
    conn, path = fresh("to_v3")
    m.migrate(conn, "hr", h.MIGRATIONS, target=2)
    h.add_employee(conn, "h1", employee_number="E001", first_name="Maya", job_title="Owner", start_date="2025-01-06")
    h.add_employee(conn, "h2", employee_number="E002", first_name="Sam", job_title="Clerk", start_date="2025-02-03",
                   manager_id=1)
    h.update_employee(conn, 2, {"job_title": "Lead"}, "2025-06-02", "u2")
    h.terminate_employee(conn, 2, "2025-09-30", "left", "t2")
    h.create_leave_type(conn, "annual", "Annual leave", "lt", annual_allowance_days=20)
    h.accrue_annual_leave(conn, 1, "annual", 2025, "acc")
    # A request as the v2 code wrote it (this code expects the v3 'half' column, so it writes v2 rows by hand).
    conn.execute("""INSERT INTO hr_leave_requests (employee_id, leave_type_id, start_date, end_date, half_days,
                                                   idempotency_key) VALUES (1, 1, '2025-11-03', '2025-11-04', 4, 'req')""")
    conn.execute("""INSERT INTO hr_leave_ledger (employee_id, leave_type_id, entry_type, half_days, request_id,
                                                 idempotency_key) VALUES (1, 1, 'request', 0, 1, 'req')""")
    r = {"request_id": 1}
    before = [tuple(row) for row in conn.execute("SELECT * FROM hr_employment_events ORDER BY id")]
    out = h.init_db(conn)
    assert out["applied"] == [3, 4] and out["backup"]
    assert [tuple(row) for row in conn.execute("SELECT * FROM hr_employment_events ORDER BY id")] == before
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert h.leave_request(conn, r["request_id"])["half"] is None
    h.verify_schema(conn)
    rejected(lambda: conn.execute("DELETE FROM hr_employment_events"), "rows are immutable")
    rejected(lambda: conn.execute("UPDATE hr_employees SET job_title = 'X', version = version + 1 WHERE id = 1"),
             "employee rows are written only by an employment event")
    assert h.employment_history(conn, 2)[-1]["event"] == "terminated"
    assert h.rehire_employee(conn, 2, "2026-01-05", "back")["status"] == "active"


AFTER = {"2026-02-30": "2026-03-02", "2026-04-31": "2026-05-02", "2025-02-29": "2025-03-02"}
IMPOSSIBLE_DATES = ("2026-02-30", "2026-04-31", "2025-02-29")
REAL_DATES = ("2024-02-29", "2026-01-31", "2026-12-31", "2026-02-28", "2026-04-30")


def date_checks() -> list[tuple[str, str, str]]:
    """(file, column, expression) for every `CHECK (col IS date(col) ...)` in migrations/."""
    import re
    out = []
    for path in sorted(Path(h.MIGRATIONS).glob("*.sql")):
        sql = path.read_text()
        for check in re.finditer(r"CHECK\s*\(", sql):
            depth, j = 1, check.end()
            while depth:
                depth += {"(": 1, ")": -1}.get(sql[j], 0)
                j += 1
            expr = sql[check.end():j - 1]
            col = re.match(r"\s*(\w+) IS date\(\1\)", expr)
            if col:
                out.append((path.name, col.group(1), expr))
    return out


def test_every_date_check_refuses_impossible_days_whatever_the_sqlite_version():
    """`x IS date(x)` alone lets SQLite < 3.45 store 2026-02-30; each date CHECK also compares the day with the
    month's last day. The guard is evaluated on its own too, so this holds on a SQLite whose date() is lenient."""
    import re
    checks = date_checks()
    assert sorted(c for _, c, _ in checks) == sorted(["start_date", "end_date", "effective_date", "start_date",
                                                     "end_date", "as_of", "effective_date"]), checks
    conn = sqlite3.connect(":memory:")
    for name, col, expr in checks:
        assert " AND substr(" in expr, (name, col, "date CHECK without the day-of-month guard")
        guard = expr.split(" AND ", 1)[1]
        full, day_only = (re.sub(rf"\b{col}\b", ":d", e) for e in (expr, guard))
        for d in IMPOSSIBLE_DATES:
            assert conn.execute(f"SELECT {full}", {"d": d}).fetchone()[0] == 0, (name, col, d)
            assert conn.execute(f"SELECT {day_only}", {"d": d}).fetchone()[0] == 0, (name, col, d)
        for d in REAL_DATES:
            assert conn.execute(f"SELECT {full}", {"d": d}).fetchone()[0] == 1, (name, col, d)
        # NULL: the CHECK is NULL (passes), as it was with `x IS date(x)` alone; NOT NULL columns refuse it anyway.
        assert conn.execute(f"SELECT {full}", {"d": None}).fetchone()[0] in (None, 1), (name, col)
        for d in ("2026-1-05", "2026-01-05 10:00", "05/01/2026", "", "2026-13-01"):
            assert conn.execute(f"SELECT {full}", {"d": d}).fetchone()[0] == 0, (name, col, d)


# ---- 0004: a database that an earlier release already migrated to version 3 (CHECKs without the day guard)

_GUARD = None


def legacy_v3(name: str):
    """A database at version 3 as the release before the day guard left it: the 0001-0003 files with the
    day-of-month guard taken out of every date CHECK."""
    import re
    folder = Path(TMP.name) / f"{name}_legacy_migrations"
    folder.mkdir()
    for path in sorted(Path(h.MIGRATIONS).glob("000[123]_*.sql")):
        text = re.sub(r" AND substr\((\w+), 9, 2\) <= substr\(date\(substr\(\1, 1, 8\) \|\| '01', '\+1 month', '-1 day'\), 9, 2\)",
                      "", path.read_text())
        assert not [l for l in text.splitlines() if "'-1 day'" in l and not l.lstrip().startswith("--")], path.name
        (folder / path.name).write_text(text)
    path = os.path.join(TMP.name, name + ".db")
    conn = h.connect(path)
    assert m.migrate(conn, "hr", folder)["to_version"] == 3
    return conn, path


def raw_hire(conn, number, start_date, key, effective="2025-03-03", employee_id=None):
    """Writes a hire event straight to SQL, the way a script or another tool could, around the Python date check."""
    from test_employees import raw_event
    snap = {k: None for k in h._SNAPSHOT_FIELDS}
    snap.update(employee_number=number, first_name="Raw", job_title="Clerk", start_date=start_date, status="active",
                employment_type=snap.get("employment_type") or "full_time")
    eid = employee_id or conn.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM hr_employees").fetchone()[0]
    return raw_event(conn, eid, 1, "hired", snap, key, effective)


def seed_legacy(conn):
    h.add_employee(conn, "h1", employee_number="E001", first_name="Maya", job_title="Owner", start_date="2025-01-06")
    h.add_employee(conn, "h2", employee_number="E002", first_name="Sam", job_title="Clerk", start_date="2025-02-03",
                   manager_id=1)
    h.create_leave_type(conn, "annual", "Annual leave", "lt", annual_allowance_days=20)
    h.accrue_annual_leave(conn, 1, "annual", 2025, "acc")


def test_the_legacy_v3_fixture_really_accepts_an_impossible_day_on_this_sqlite():
    conn, _ = legacy_v3("legacy_proof")
    seed_legacy(conn)
    sqlite_has_strict_date = sqlite3.sqlite_version_info >= (3, 45)
    try:
        raw_hire(conn, "E003", "2026-02-30", "h3")
        accepted = True
    except sqlite3.IntegrityError:
        accepted = False
    assert accepted == (not sqlite_has_strict_date), (sqlite3.sqlite_version, accepted)


def test_upgrading_a_v3_database_to_4_adds_six_triggers_and_keeps_every_row():
    conn, _ = legacy_v3("to_v4_rows")
    seed_legacy(conn)
    h.update_employee(conn, 2, {"job_title": "Lead"}, "2025-06-02", "u2")
    before = {t: [tuple(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY 1")]
              for t in ("hr_employees", "hr_employment_events", "hr_leave_ledger", "hr_leave_types")}
    triggers = lambda: {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger' AND name LIKE '%_real_day'")}
    assert triggers() == set()
    out = h.init_db(conn)
    assert out["from_version"] == 3 and out["applied"] == [4] and out["to_version"] == 4 and out["backup"]
    assert len(triggers()) == 6
    after = {t: [tuple(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY 1")] for t in before}
    assert after == before
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    h.verify_schema(conn)


def rejected_day(fn, *columns, old_message="is not a real calendar day"):
    """The write must be refused because of one of these date columns, whichever rule fires first.

    SQLite before 3.45 lets `x IS date(x)` accept impossible days, so the refusal comes from the 0004 trigger and
    carries its message. From 3.45 the old CHECK already refuses them and raises its own message, naming the column.
    Either way a write that goes through, or one refused for another column, fails the test."""
    if sqlite3.sqlite_version_info < (3, 45):
        return rejected(fn, old_message)
    try:
        fn()
    except sqlite3.IntegrityError as e:
        assert any(f"CHECK constraint failed: {c} IS date({c})" in str(e) for c in columns), \
            f"refused for the wrong reason: {e!r}, wanted a date CHECK on one of {columns}"
    else:
        raise AssertionError(f"an impossible day was accepted (columns {columns})")


def test_after_the_upgrade_an_employee_cannot_start_or_end_on_an_impossible_day():
    conn, _ = legacy_v3("v4_employee")
    seed_legacy(conn)
    h.init_db(conn)
    for i, day in enumerate(IMPOSSIBLE_DATES):
        rejected_day(lambda: raw_hire(conn, f"X{i}", day, f"x{i}"), "start_date", "effective_date")
    assert conn.execute("SELECT COUNT(*) FROM hr_employees").fetchone()[0] == 2
    from test_employees import raw_event, snapshot_of
    for i, day in enumerate(IMPOSSIBLE_DATES):
        gone = snapshot_of(conn, 2) | {"status": "terminated", "end_date": day, "termination_reason": "left"}
        rejected_day(lambda: raw_event(conn, 2, 2, "terminated", gone, f"t{i}", day), "end_date", "effective_date")
    assert h.employee(conn, 2)["status"] == "active"


def test_after_the_upgrade_an_employment_event_refuses_an_impossible_day():
    conn, _ = legacy_v3("v4_event")
    seed_legacy(conn)
    h.init_db(conn)
    from test_employees import raw_event, snapshot_of
    for i, day in enumerate(IMPOSSIBLE_DATES):
        rejected_day(lambda: raw_event(conn, 2, 2, "updated", snapshot_of(conn, 2) | {"job_title": "Lead"}, f"u{i}", day),
                     "effective_date")
    assert h.employee(conn, 2)["job_title"] == "Clerk" and len(h.employment_history(conn, 2)) == 1


def test_after_the_upgrade_a_leave_request_refuses_an_impossible_start_or_end():
    conn, _ = legacy_v3("v4_leave")
    seed_legacy(conn)
    h.init_db(conn)
    from test_leave import raw_request
    for i, day in enumerate(IMPOSSIBLE_DATES):
        rejected_day(lambda: raw_request(conn, 1, 1, day, AFTER[day], 2, f"s{i}"), "start_date",
                     old_message="start_date is not a real calendar day")
        rejected_day(lambda: raw_request(conn, 1, 1, "2025-02-26", day, 8, f"e{i}"), "end_date",
                     old_message="end_date is not a real calendar day")
    assert conn.execute("SELECT COUNT(*) FROM hr_leave_requests").fetchone()[0] == 0


def test_after_the_upgrade_a_ledger_as_of_date_refuses_an_impossible_day():
    conn, _ = legacy_v3("v4_ledger")
    seed_legacy(conn)
    h.init_db(conn)
    before = conn.execute("SELECT COUNT(*) FROM hr_leave_ledger").fetchone()[0]
    for i, day in enumerate(IMPOSSIBLE_DATES):
        rejected_day(lambda: conn.execute("""INSERT INTO hr_leave_ledger (employee_id, leave_type_id, entry_type, half_days,
                                         reason, as_of, idempotency_key) VALUES (1, 1, 'adjustment', 1, 'x', ?, ?)""", (day, f"l{i}")),
                     "as_of")
    assert conn.execute("SELECT COUNT(*) FROM hr_leave_ledger").fetchone()[0] == before


def test_after_the_upgrade_real_month_ends_are_still_accepted():
    conn, _ = legacy_v3("v4_real")
    seed_legacy(conn)
    h.init_db(conn)
    for i, day in enumerate(("2024-02-29", "2026-01-31", "2026-12-31", "2026-02-28", "2026-04-30")):
        raw_hire(conn, f"R{i}", day, f"r{i}", effective=day)
    assert conn.execute("SELECT COUNT(*) FROM hr_employees").fetchone()[0] == 7


def test_the_same_upgrade_is_a_no_op_the_second_time_and_a_new_database_has_the_guards_too():
    conn, _ = legacy_v3("v4_again")
    h.init_db(conn)
    assert h.init_db(conn)["applied"] == []
    fresh_conn, _ = fresh("v4_fresh")
    h.init_db(fresh_conn)
    names = {r[0] for r in fresh_conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger' AND name LIKE '%_real_day'")}
    assert len(names) == 6
    h.verify_schema(fresh_conn)


def test_a_failing_0004_rolls_back_and_leaves_the_database_at_version_3():
    conn, _ = legacy_v3("v4_fail")
    seed_legacy(conn)
    folder = folder_copy("v4_broken")
    (folder / "0004_calendar_day_guards.sql").write_text(
        (folder / "0004_calendar_day_guards.sql").read_text() + "\nCREATE TRIGGER hr_employees_start_date_real_day BEFORE INSERT ON hr_employees BEGIN SELECT 1; END;\n")
    try:
        m.migrate(conn, "hr", folder)
    except sqlite3.OperationalError:
        pass
    else:
        raise AssertionError("expected the duplicate trigger to fail")
    assert h.schema_version(conn) == 3
    assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name LIKE '%_real_day'").fetchone()
    assert h.init_db(conn)["to_version"] == 4


def test_an_existing_impossible_day_in_a_v3_database_does_not_block_the_upgrade():
    """The triggers guard new inserts only. A row an earlier release already stored is kept, not rewritten."""
    if sqlite3.sqlite_version_info >= (3, 45):
        return
    conn, _ = legacy_v3("v4_existing")
    seed_legacy(conn)
    raw_hire(conn, "E099", "2026-02-30", "bad")
    out = h.init_db(conn)
    assert out["to_version"] == 4
    assert conn.execute("SELECT start_date FROM hr_employees WHERE employee_number = 'E099'").fetchone()[0] == "2026-02-30"


def test_an_in_memory_database_migrates_without_a_backup():
    conn = h.connect(":memory:")
    assert h.init_db(conn)["backup"] is None and h.schema_version(conn) == LATEST


if __name__ == "__main__":
    print(f"Python {sys.version.split()[0]}, SQLite {sqlite3.sqlite_version}")
    tests = [(n, f) for n, f in globals().items() if n.startswith("test_") and callable(f)]
    for name, fn in tests:
        fn()
        print(f"PASS {name}")
    print(f"\n{len(tests)} tests passed")
