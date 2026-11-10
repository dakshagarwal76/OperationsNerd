"""
Fund accounting: per-fund balancing, grants, overspend blocking, release, migration
backfill and reports. Run from repo root:

    python -B modules/accounting/tests/test_funds.py
"""

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_ledger import chart, rejected, with_chart
import accounting as a

TMP = tempfile.TemporaryDirectory(prefix="accounting_funds_", ignore_cleanup_errors=True)
m = a._migrate
LATEST = a.latest_version()
AS_OF = "2026-12-31"


def fresh(name):
    path = os.path.join(TMP.name, name + ".db")
    return a.connect(path), path


def equity_pair(conn):
    unrestricted = a.account_by_code(conn, "3000")["account_id"]
    restricted = a.add_account(conn, "eq:res", "3100", "Donor-restricted net assets", "equity")["account_id"]
    clearing = a.add_account(conn, "eq:clear", "3200", "Interfund clearing", "liability")["account_id"]
    grant_rev = a.account_by_code(conn, "4000")["account_id"]
    expense = a.account_by_code(conn, "5000")["account_id"]
    return unrestricted, restricted, clearing, grant_rev, expense


def test_default_fund_exists_and_journal_lines_tagged():
    conn, _ = with_chart("default_fund")
    gen = a.fund(conn, a.default_fund_id(conn))
    assert gen["code"] == "GEN" and gen["restriction_type"] == "unrestricted" and gen["is_default"]
    a.post_journal_entry(conn, "e1", "2026-01-01", "Opening", [
        {"account_code": "1000", "debit_cents": 100_000},
        {"account_code": "3000", "credit_cents": 100_000},
    ])
    entry = a.journal_entry(conn, 1)
    assert all(line["fund_id"] == gen["fund_id"] for line in entry["lines"])


def test_per_fund_balance_enforced_in_python_and_database():
    conn, _ = with_chart("per_fund_bal")
    gen = a.default_fund_id(conn)
    forest = a.add_fund(conn, "f", "FRST", "Forest", "temporarily_restricted")["fund_id"]
    rejected(lambda: a.post_journal_entry(conn, "bad", "2026-01-02", "Unbalanced in fund", [
        {"account_code": "1000", "debit_cents": 1_000, "fund_id": gen},
        {"account_code": "3000", "credit_cents": 500, "fund_id": gen},
        {"account_code": "3000", "credit_cents": 500, "fund_id": forest},
    ]), "debits (1000) must equal credits (500)", a.Unbalanced)
  # Valid cross-fund transfer-style entry
    a.post_journal_entry(conn, "ok", "2026-01-02", "Cross-fund", [
        {"account_code": "1000", "debit_cents": 1_000, "fund_id": forest},
        {"account_code": "3000", "credit_cents": 1_000, "fund_id": forest},
        {"account_code": "1000", "credit_cents": 1_000, "fund_id": gen},
        {"account_code": "3000", "debit_cents": 1_000, "fund_id": gen},
    ])


def test_grant_receipt_release_and_overspend_block():
    conn, _ = with_chart("grant_flow")
    unres, restricted, clearing, grant_rev, expense = equity_pair(conn)
    forest = a.add_fund(conn, "ff", "FRST", "Forest", "temporarily_restricted")["fund_id"]
    bank = a.account_by_code(conn, "1000")["account_id"]
    a.record_grant_receipt(conn, "g1", forest, "2026-02-01", 500_000, bank, grant_rev, "Forest grant")
    fb = a.fund(conn, forest, as_of=AS_OF)
    assert fb["available_cents"] == 500_000
    a.post_journal_entry(conn, "spend", "2026-03-01", "Trail work", [
        {"account_id": expense, "debit_cents": 120_000, "fund_id": forest},
        {"account_code": "1000", "credit_cents": 120_000, "fund_id": forest},
    ])
    assert a.fund(conn, forest, as_of=AS_OF)["available_cents"] == 380_000
    rejected(lambda: a.post_journal_entry(conn, "over", "2026-03-02", "Too much", [
        {"account_id": expense, "debit_cents": 400_000, "fund_id": forest},
        {"account_code": "1000", "credit_cents": 400_000, "fund_id": forest},
    ]), "has 380000 cents available", a.InsufficientFundBalance)
    n = len(a.journal_entries(conn))
    released = a.release_from_restriction(conn, "rel", forest, 200_000, "2026-06-01", "Milestone met",
                                        restricted, unres, clearing)
    assert len(a.journal_entries(conn)) == n + 1
    assert released["source"] == "fund_release" and len(released["lines"]) == 4
    assert a.fund(conn, forest, as_of=AS_OF)["available_cents"] == 180_000


def test_bill_posts_to_restricted_fund():
    conn, _ = with_chart("bill_fund")
    _, _, _, grant_rev, expense = equity_pair(conn)
    forest = a.add_fund(conn, "bf", "FRST", "Forest", "temporarily_restricted")["fund_id"]
    bank = a.account_by_code(conn, "1000")["account_id"]
    a.record_grant_receipt(conn, "g", forest, "2026-01-10", 300_000, bank, grant_rev, "Grant")
    supplier = a.add_supplier(conn, "s", "Vendor")
    bill = a.create_bill(conn, "b", supplier["id"], "B-1", "2026-02-01", [
        {"description": "Work", "unit_price_cents": 50_000, "account_id": expense},
    ], fund_id=forest)
    sent = a.receive_bill(conn, bill["bill_id"], "recv")
    lines = a.journal_entry(conn, sent["entry_id"])["lines"]
    assert all(line["fund_id"] == forest for line in lines)


def test_migration_backfills_journal_lines():
    conn, path = fresh("migrate_backfill")
    assert m.migrate(conn, "accounting", a.MIGRATIONS, target=3)["to_version"] == 3
    chart(conn)
    a.post_journal_entry(conn, "old", "2026-01-01", "Pre-fund entry", [
        {"account_code": "1000", "debit_cents": 10_000},
        {"account_code": "3000", "credit_cents": 10_000},
    ])
    a.init_db(conn)
    assert a.schema_version(conn) == LATEST
    fids = [r[0] for r in conn.execute("SELECT DISTINCT fund_id FROM acct_journal_lines")]
    assert fids == [1]
    assert a.trial_balance(conn, as_of="2026-01-01") == a.trial_balance(conn, as_of="2026-01-01", fund_id=None)


def test_reports_without_fund_filter_unchanged_and_statement_of_activities():
    conn, _ = with_chart("reports")
    equity_pair(conn)
    gen = a.default_fund_id(conn)
    forest = a.add_fund(conn, "rf", "FRST", "Forest", "temporarily_restricted")["fund_id"]
    bank = a.account_by_code(conn, "1000")["account_id"]
    rev = a.account_by_code(conn, "4000")["account_id"]
    a.record_grant_receipt(conn, "g-gen", gen, "2026-01-05", 100_000, bank, rev, "Unrestricted grant")
    a.record_grant_receipt(conn, "g-fr", forest, "2026-01-06", 200_000, bank, rev, "Forest grant")
    tb_all = a.trial_balance(conn, as_of="2026-01-31")
    tb_gen = a.trial_balance(conn, as_of="2026-01-31", fund_id=gen)
    assert tb_all["total_debit_cents"] == 300_000
    assert tb_gen["total_debit_cents"] == 100_000
    soa = a.statement_of_activities(conn, "2026-01-01", "2026-01-31")
    assert soa["total_income_without_cents"] == 100_000
    assert soa["total_income_with_cents"] == 200_000
    fbs = a.fund_balances(conn, as_of="2026-01-31")
    assert len(fbs["funds"]) >= 2


def test_reversal_keeps_fund_ids():
    conn, _ = with_chart("rev_fund")
    gen = a.default_fund_id(conn)
    entry = a.post_journal_entry(conn, "e", "2026-01-01", "Test", [
        {"account_code": "1000", "debit_cents": 5_000, "fund_id": gen},
        {"account_code": "3000", "credit_cents": 5_000, "fund_id": gen},
    ])
    rev = a.reverse_journal_entry(conn, entry["entry_id"], "rev")
    assert all(line["fund_id"] == gen for line in rev["lines"])


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(tests)} fund tests passed")


if __name__ == "__main__":
    main()
