"""
Accounting module: versioned migrations, the automatic backup before an upgrade
(migrate.py) and the schema check that notices a tampered database. Each test
gets its own throwaway database and, where it needs broken or extra migration
files, its own copy of migrations/. Run from the repo root with the venv
(pytest also collects these):

    python -B modules/accounting/tests/test_migrations.py
"""

import os
import re
import shutil
import sqlite3
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import accounting as a
from test_ledger import chart, rejected

m = a._migrate
TMP = tempfile.TemporaryDirectory(prefix="accounting_migrations_", ignore_cleanup_errors=True)
LATEST = len(m.migrations(a.MIGRATIONS))


def fresh(name: str) -> tuple[sqlite3.Connection, str]:
    path = os.path.join(TMP.name, name + ".db")
    return a.connect(path), path


def tables(conn) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def folder_copy(name: str) -> Path:
    dest = Path(TMP.name) / f"{name}_migrations"
    shutil.copytree(a.MIGRATIONS, dest)
    return dest


def test_the_migration_helper_is_loaded_under_its_own_name():
    """modules/hr/migrate.py is the same module under the same name; loading ours by path keeps a mounted
    HR router from quietly getting this copy, or the other way round."""
    assert m.__name__ == "accounting_migrate"
    assert "migrate" not in sys.modules or sys.modules["migrate"] is not m
    assert Path(m.__file__).parent == Path(a.__file__).parent
    assert a._load_migrate() is m                                   # loaded once, not on every call


def test_a_new_database_gets_every_version_and_no_backup():
    conn, path = fresh("new")
    out = a.init_db(conn)
    assert out == {"module": "accounting", "from_version": 0, "to_version": LATEST,
                   "applied": list(range(1, LATEST + 1)), "backup": None}
    assert a.schema_version(conn) == LATEST == a.latest_version() == 5
    assert {"acct_accounts", "acct_journal_entries", "acct_journal_lines", "acct_customers", "acct_suppliers",
            "acct_invoices", "acct_invoice_lines", "acct_invoice_payments", "acct_bills", "acct_bill_lines",
            "acct_bill_payments"} <= tables(conn)
    names = [tuple(r) for r in conn.execute("SELECT module, version, name FROM module_migrations ORDER BY version")]
    assert names == [("accounting", 1, "ledger"), ("accounting", 2, "sales_and_purchases"),
                     ("accounting", 3, "document_entries"), ("accounting", 4, "fund_accounting"), ("accounting", 5, "fund_capital_accounts")]
    assert "acct_funds" in tables(conn)
    assert conn.execute("SELECT fund_id FROM acct_journal_lines LIMIT 0").fetchone() is None
    assert not [f for f in os.listdir(TMP.name) if f.startswith("new.db.") and f.endswith(".bak")]


def test_running_it_again_changes_nothing():
    conn, _ = fresh("again")
    a.init_db(conn)
    assert a.init_db(conn) == {"module": "accounting", "from_version": LATEST, "to_version": LATEST,
                               "applied": [], "backup": None}


def test_an_upgrade_backs_up_the_database_first_and_keeps_the_data():
    conn, path = fresh("upgrade")
    assert m.migrate(conn, "accounting", a.MIGRATIONS, target=1)["applied"] == [1]
    assert "acct_invoices" not in tables(conn)
    chart(conn)
    a.post_journal_entry(conn, "e1", "2026-01-01", "Opening capital", [
        {"account_code": "1000", "debit_cents": 500_000}, {"account_code": "3000", "credit_cents": 500_000}])
    backups = Path(TMP.name) / "upgrade_backups"
    out = a.init_db(conn, backup_dir=str(backups))
    assert out["from_version"] == 1 and out["applied"] == [2, 3, 4, 5] and out["to_version"] == 5
    assert out["backup"] and Path(out["backup"]).parent == backups and "accounting-v0001" in out["backup"]
    copy = sqlite3.connect(out["backup"])
    assert copy.execute("SELECT code FROM acct_accounts ORDER BY code").fetchall()[0] == ("1000",)
    assert copy.execute("SELECT total_cents FROM acct_journal_entries").fetchall() == [(500_000,)]
    assert "acct_invoices" not in {r[0] for r in copy.execute("SELECT name FROM sqlite_master "
                                                              "WHERE type = 'table'")}
    assert copy.execute("SELECT MAX(version) FROM module_migrations WHERE module = 'accounting'"
                        ).fetchone()[0] == 1
    copy.close()
    # The upgraded database still has the old rows, and the new tables work.
    assert a.account_by_code(conn, "1000")["balance_cents"] == 500_000
    customer = a.add_customer(conn, "c", "Northgate Offices")
    draft = a.create_invoice(conn, "inv", customer["id"], "INV-001", "2026-03-01", [
        {"description": "Catering", "unit_price_cents": 10_000, "account_code": "4000"}])
    assert a.send_invoice(conn, draft["invoice_id"], "send")["status"] == "sent"
    assert a.trial_balance(conn, as_of="2026-03-01")["balanced"]


def test_version_1_is_a_usable_ledger_on_its_own():
    """0001 is the journal; invoices and bills arrive with 0002. A database stopped at 1 still keeps books."""
    conn, _ = fresh("v1")
    m.migrate(conn, "accounting", a.MIGRATIONS, target=1)
    chart(conn)
    a.post_journal_entry(conn, "e1", "2026-01-01", "Cash sale", [
        {"account_code": "1000", "debit_cents": 2_500}, {"account_code": "4000", "credit_cents": 2_500}])
    assert a.trial_balance(conn, as_of="2026-01-01")["balanced"]
    assert a.profit_and_loss(conn, "2026-01-01", "2026-12-31")["net_income_cents"] == 2_500
    assert a.balance_sheet(conn, as_of="2026-01-01")["balanced"]
    # The aging reports need the document tables, which this version hasn't got.
    rejected(lambda: a.ar_aging(conn, as_of="2026-01-01"), "no such table: acct_invoices",
             sqlite3.OperationalError)
    # 'payroll' is a known entry source from 0001 on, so the hook works before 0002 too.
    assert a.post_payroll_entry(conn, "pay", "2026-01-31", "Pay run", [
        {"account_code": "5000", "debit_cents": 100}, {"account_code": "1000", "credit_cents": 100}],
        source_ref="PR-1")["source"] == "payroll"


def test_a_failing_migration_rolls_back_and_leaves_the_previous_version():
    conn, _ = fresh("failing")
    folder = folder_copy("failing")
    broken = folder / f"{LATEST + 1:04d}_broken.sql"
    broken.write_text("CREATE TABLE acct_half_done (id INTEGER PRIMARY KEY);\n"
                      "INSERT INTO no_such_table VALUES (1);\n")
    a.init_db(conn)
    rejected(lambda: m.migrate(conn, "accounting", folder), "no such table: no_such_table",
             sqlite3.OperationalError)
    assert a.schema_version(conn) == LATEST and "acct_half_done" not in tables(conn)
    assert not conn.in_transaction
    broken.write_text("CREATE TABLE acct_half_done (id INTEGER PRIMARY KEY);\n")
    out = m.migrate(conn, "accounting", folder)
    assert out["applied"] == [LATEST + 1] and out["backup"] and "acct_half_done" in tables(conn)


def test_a_failing_version_keeps_the_versions_before_it_from_the_same_run():
    """One run holds one lock, but each version is its own savepoint: 1..LATEST stay when LATEST+1 fails."""
    conn, _ = fresh("failing_late")
    folder = folder_copy("failing_late")
    (folder / f"{LATEST + 1:04d}_broken.sql").write_text("CREATE TABLE acct_half_done (id INTEGER PRIMARY KEY);\n"
                                                         "INSERT INTO no_such_table VALUES (1);\n")
    rejected(lambda: m.migrate(conn, "accounting", folder), "no such table: no_such_table",
             sqlite3.OperationalError)
    assert a.schema_version(conn) == LATEST and "acct_half_done" not in tables(conn)
    assert not conn.in_transaction
    a.verify_schema(conn)


def test_migration_files_must_be_numbered_without_gaps_and_only_go_forward():
    conn, _ = fresh("rules")
    folder = folder_copy("gap")
    (folder / f"{LATEST + 2:04d}_skip.sql").write_text("SELECT 1;\n")
    rejected(lambda: m.migrate(conn, "accounting", folder), f"expected version {LATEST + 1:04d}",
             m.MigrationError)
    folder2 = folder_copy("badname")
    (folder2 / "3_bad.sql").write_text("SELECT 1;\n")
    rejected(lambda: m.migrate(conn, "accounting", folder2), "is not named NNNN_name.sql", m.MigrationError)
    a.init_db(conn)
    rejected(lambda: m.migrate(conn, "accounting", a.MIGRATIONS, target=1), "migrations only go forward",
             m.MigrationError)
    rejected(lambda: m.migrate(conn, "accounting", a.MIGRATIONS, target=7), "no migration 7", m.MigrationError)
    conn.execute("INSERT INTO module_migrations (module, version, name) VALUES ('accounting', ?, 'the_future')",
                 (LATEST + 1,))
    rejected(lambda: a.init_db(conn), "newer than this code", m.MigrationError)


def test_modules_share_a_business_database_without_touching_each_other():
    """One business database holds every module's tables: HR's and logistics' are left alone, and each
    module keeps its own version (so nothing here uses PRAGMA user_version)."""
    conn, path = fresh("shared")
    conn.execute("CREATE TABLE stock_movements (id INTEGER PRIMARY KEY, quantity INTEGER NOT NULL)")
    conn.execute("INSERT INTO stock_movements (quantity) VALUES (5)")
    conn.execute("CREATE TABLE hr_employees (id INTEGER PRIMARY KEY, first_name TEXT NOT NULL)")
    conn.execute("INSERT INTO hr_employees (first_name) VALUES ('Maya')")
    conn.execute("INSERT INTO module_migrations (module, version, name) VALUES ('hr', 1, 'employees')") \
        if "module_migrations" in tables(conn) else None
    conn.execute("PRAGMA user_version = 7")                                   # whatever another tool keeps there
    out = a.init_db(conn)
    assert out["applied"] == list(range(1, LATEST + 1)) and out["backup"]      # not empty, so backed up first
    assert [tuple(r) for r in conn.execute("SELECT quantity FROM stock_movements")] == [(5,)]
    assert [tuple(r) for r in conn.execute("SELECT first_name FROM hr_employees")] == [("Maya",)]
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 7
    assert m.current_version(conn, "hr") == 0 and m.current_version(conn, "logistics") == 0
    assert m.current_version(conn, "accounting") == LATEST
    for name in tables(conn) - {"stock_movements", "hr_employees", "module_migrations"}:
        assert name.startswith("acct_"), name                                  # every accounting table is prefixed
    a.verify_schema(conn)                                                      # the other modules' tables aren't ours


def test_two_processes_migrating_at_once_apply_each_version_once():
    _, path = fresh("race")
    errors, barrier = [], threading.Barrier(4)

    def worker():
        conn = a.connect(path)
        try:
            barrier.wait()
            a.init_db(conn)
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
    conn = a.connect(path)
    assert [r[0] for r in conn.execute("SELECT version FROM module_migrations WHERE module = 'accounting'")] \
        == list(range(1, LATEST + 1))


def test_concurrent_migrators_take_one_backup():
    """Four processes upgrade the same v1 database at once: one backs it up and migrates, the others wait for
    its lock, see the new version and do nothing (no second backup of the already-migrated file)."""
    conn, path = fresh("race_backup")
    m.migrate(conn, "accounting", a.MIGRATIONS, target=1)
    chart(conn)
    conn.close()
    backups = Path(TMP.name) / "race_backups"
    results, errors, barrier = [], [], threading.Barrier(4)

    def worker():
        c = a.connect(path)
        try:
            barrier.wait()
            results.append(a.init_db(c, backup_dir=str(backups)))
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


def test_an_in_memory_database_migrates_without_a_backup():
    conn = a.connect(":memory:")
    assert a.init_db(conn)["backup"] is None and a.schema_version(conn) == LATEST


# ---------------------------------------------------------------- the schema check

def test_a_dropped_or_changed_rule_is_noticed():
    conn, path = fresh("tampered")
    a.init_db(conn)
    a.verify_schema(conn)
    conn.execute("DROP TRIGGER acct_journal_entries_balanced")
    rejected(lambda: a.verify_schema(conn), "trigger acct_journal_entries_balanced is missing", a.SchemaTampered)
    # Put it back with its body changed, so it exists but no longer checks anything.
    conn.execute("""CREATE TRIGGER acct_journal_entries_balanced BEFORE INSERT ON acct_journal_entries
                    WHEN 0 BEGIN SELECT RAISE(ABORT, 'never'); END""")
    rejected(lambda: a.verify_schema(conn), "trigger acct_journal_entries_balanced was changed",
             a.SchemaTampered)
    conn.execute("DROP TRIGGER acct_journal_entries_balanced")
    conn.execute(_statement_for("acct_journal_entries_balanced"))
    a.verify_schema(conn)                                                       # the real one back again


def test_a_missing_index_or_an_extra_table_is_noticed():
    conn, _ = fresh("tampered2")
    a.init_db(conn)
    conn.execute("DROP INDEX acct_accounts_role")
    rejected(lambda: a.verify_schema(conn), "index acct_accounts_role is missing", a.SchemaTampered)
    conn.execute(_statement_for("acct_accounts_role"))
    a.verify_schema(conn)
    conn.execute("CREATE TABLE acct_shadow_ledger (id INTEGER PRIMARY KEY)")
    rejected(lambda: a.verify_schema(conn), "unexpected table acct_shadow_ledger", a.SchemaTampered)
    conn.execute("DROP TABLE acct_shadow_ledger")
    a.verify_schema(conn)


def test_init_db_and_the_api_refuse_to_run_on_a_tampered_schema():
    conn, path = fresh("tampered3")
    a.init_db(conn)
    conn.execute("DROP TRIGGER acct_invoices_no_delete")
    rejected(lambda: a.init_db(conn), "isn't what its migrations create", a.SchemaTampered)
    # Nothing in the module repairs it quietly: the version is still the latest, and reads still work.
    assert a.schema_version(conn) == LATEST
    assert a.accounts(conn) == []


def _statement_for(name: str) -> str:
    """The CREATE statement for one object, taken from the migration files."""
    for _, _, path in m.migrations(a.MIGRATIONS):
        for sql in m._statements(path.read_text()):
            # Normalised, so the comment lines a statement may start with don't get in the way.
            if re.match(rf"CREATE (?:UNIQUE )?(?:TABLE|INDEX|TRIGGER|VIEW) {name}\b", a._normalise_sql(sql),
                        re.I):
                return sql
    raise AssertionError(f"no CREATE statement for {name} in the migrations")


# ---------------------------------------------------------------- dates, on every SQLite

IMPOSSIBLE_DATES = ("2026-02-30", "2026-04-31", "2025-02-29")
REAL_DATES = ("2024-02-29", "2026-01-31", "2026-12-31", "2026-02-28", "2026-04-30")


def date_checks() -> list[tuple[str, str, str]]:
    """(file, column, expression) for every `CHECK (col IS date(col) ...)` in migrations/."""
    out = []
    for path in sorted(Path(a.MIGRATIONS).glob("*.sql")):
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
    """`x IS date(x)` alone lets SQLite < 3.45 store 2026-02-30; each date CHECK also compares the day with
    the month's last day. The guard is evaluated on its own too, so this holds on a SQLite whose date() is
    lenient. This mirrors modules/hr's test of the same rule."""
    checks = date_checks()
    assert sorted(c for _, c, _ in checks) == sorted(["entry_date", "issue_date", "due_date", "payment_date",
                                                      "issue_date", "due_date", "payment_date"]), checks
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


def test_every_money_column_is_an_integer_with_a_cap():
    """No amount is a REAL, and every one is capped, so a sum can't overflow a 64-bit integer."""
    conn, _ = fresh("money_columns")
    a.init_db(conn)
    money = []
    for (table,) in conn.execute("""SELECT name FROM sqlite_master WHERE type = 'table'
                                     AND name LIKE 'acct\\_%' ESCAPE '\\'"""):
        for row in conn.execute(f"PRAGMA table_info({table})"):
            if row["name"].endswith("_cents"):
                money.append((table, row["name"], row["type"]))
    assert money, "no money columns found"
    assert [c for c in money if c[2] != "INTEGER"] == []
    schema = "\n".join(path.read_text() for _, _, path in m.migrations(a.MIGRATIONS))
    for table, column, _ in money:
        # Either the column carries the cap itself, or it is held under another column that does
        # (amount_paid_cents can never exceed total_cents, which is capped).
        direct = re.search(rf"{column}\s+INTEGER[^,]*?<= {a.MAX_CENTS}", schema)
        indirect = re.search(rf"CHECK \({column} <= total_cents\)", schema)
        assert direct or indirect, f"{table}.{column} is not capped"


if __name__ == "__main__":
    print(f"Python {sys.version.split()[0]}, SQLite {sqlite3.sqlite_version}")
    tests = [(n, f) for n, f in globals().items() if n.startswith("test_") and callable(f)]
    for name, fn in tests:
        fn()
        print(f"PASS {name}")
    print(f"\n{len(tests)} tests passed")
