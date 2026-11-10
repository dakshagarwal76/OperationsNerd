"""
Accounting module: the FastAPI router (api.py) over a throwaway database, the
read-only UI page, and the additive hook in operations_nerd/main.py that mounts
both only when ACCOUNTING_MODULE_ENABLED=1. Run from the repo root with the venv
(pytest also collects these):

    python -B modules/accounting/tests/test_api.py
"""

import json
import os
import re
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

accounting = api.accounting
TMP = tempfile.TemporaryDirectory(prefix="accounting_api_", ignore_cleanup_errors=True)


def client(name: str) -> TestClient:
    os.environ["ACCOUNTING_DATABASE_PATH"] = os.path.join(TMP.name, name + ".db")
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


CHART = [("1000", "Bank", "asset", None), ("1010", "Petty cash", "asset", None),
         ("1100", "Accounts receivable", "asset", "accounts_receivable"),
         ("2100", "Accounts payable", "liability", "accounts_payable"),
         ("3000", "Capital", "equity", None), ("4000", "Sales", "income", None),
         ("5000", "Rent", "expense", None)]


def seeded(name: str) -> Calls:
    s = Calls(client(name))
    for code, label, kind, role in CHART:
        s.write("POST", "/accounting/accounts", {"code": code, "name": label, "account_type": kind,
                                                 "role": role}, key=f"account:{code}", expect=201)
    s.write("POST", "/accounting/customers", {"name": "Northgate Offices", "payment_terms_days": 14},
            key="customer", expect=201)
    s.write("POST", "/accounting/suppliers", {"name": "Dockside Roastery", "payment_terms_days": 30},
            key="supplier", expect=201)
    return s


def an_invoice(s, key="invoice", number="INV-001", cents=30_000, **extra):
    body = {"customer_id": 1, "invoice_number": number, "issue_date": "2026-03-01",
            "lines": [{"description": "Catering", "unit_price_cents": cents, "account_code": "4000"}]} | extra
    return s.write("POST", "/accounting/invoices", body, key=key, expect=201).json()


def a_bill(s, key="bill", number="DR-1", cents=27_000, **extra):
    body = {"supplier_id": 1, "bill_number": number, "issue_date": "2026-03-01",
            "lines": [{"description": "Rent", "unit_price_cents": cents, "account_code": "5000"}]} | extra
    return s.write("POST", "/accounting/bills", body, key=key, expect=201).json()


# ---------------------------------------------------------------- the chart and the journal

def test_account_endpoints():
    s = seeded("accounts")
    assert s.get("/accounting/schema") == {"module": "accounting", "version": 5}
    assert [x["code"] for x in s.get("/accounting/accounts")] == [c[0] for c in CHART]
    assert [x["code"] for x in s.get("/accounting/accounts?account_type=income")] == ["4000"]
    bank = s.get("/accounting/accounts/1")
    assert bank["code"] == "1000" and bank["balance_cents"] == 0
    assert s.get("/accounting/accounts/1/balance")["balance_cents"] == 0
    renamed = s.write("PATCH", "/accounting/accounts/1", {"changes": {"name": "Bank current account"}},
                      expect=200).json()
    assert renamed["name"] == "Bank current account"
    s.write("PATCH", "/accounting/accounts/1", {"changes": {"code": "9999"}}, expect=422)
    s.write("POST", "/accounting/accounts", {"code": "1000", "name": "Second bank", "account_type": "asset"},
            expect=409)
    s.write("POST", "/accounting/accounts", {"code": "9000", "name": "Odd", "account_type": "profit"}, expect=422)
    s.get("/accounting/accounts/99", expect=404)


def test_journal_endpoints():
    s = seeded("journal")
    posted = s.write("POST", "/accounting/journal", {
        "entry_date": "2026-01-01", "memo": "Opening capital",
        "lines": [{"account_code": "1000", "debit_cents": 500_000},
                  {"account_code": "3000", "credit_cents": 500_000}]}, expect=201).json()
    assert posted["total_cents"] == 500_000 and posted["source"] == "manual"
    assert s.get(f"/accounting/journal/{posted['entry_id']}") == posted
    assert [e["memo"] for e in s.get("/accounting/journal")] == ["Opening capital"]
    assert s.get("/accounting/journal?from_date=2026-02-01") == []
    # Unbalanced is a 409, not a 500 or a silently crooked entry.
    bad = s.write("POST", "/accounting/journal", {
        "entry_date": "2026-01-02", "memo": "Crooked",
        "lines": [{"account_code": "1000", "debit_cents": 100},
                  {"account_code": "3000", "credit_cents": 99}]}, expect=409)
    assert "Unbalanced" in bad.json()["detail"]
    # The document sources can't be posted by hand.
    s.write("POST", "/accounting/journal", {
        "entry_date": "2026-01-02", "memo": "Faked invoice", "source": "invoice",
        "lines": [{"account_code": "1100", "debit_cents": 100},
                  {"account_code": "4000", "credit_cents": 100}]}, expect=422)
    payroll = s.write("POST", "/accounting/journal", {
        "entry_date": "2026-01-31", "memo": "January pay run", "source": "payroll", "source_ref": "PR-1",
        "lines": [{"account_code": "5000", "debit_cents": 1_000},
                  {"account_code": "1000", "credit_cents": 1_000}]}, expect=201).json()
    assert payroll["source"] == "payroll"
    reversal = s.write("POST", f"/accounting/journal/{payroll['entry_id']}/reverse",
                       {"memo": "Wrong pay run"}, expect=201).json()
    assert reversal["reverses_id"] == payroll["entry_id"] and reversal["memo"] == "Wrong pay run"
    s.write("POST", f"/accounting/journal/{payroll['entry_id']}/reverse", {}, expect=409)
    s.write("POST", "/accounting/journal/99/reverse", {}, expect=404)
    assert s.get("/accounting/reports/trial-balance?as_of=2026-12-31")["balanced"]


# ---------------------------------------------------------------- invoices and bills

def test_invoice_endpoints_through_to_paid():
    s = seeded("invoices")
    draft = an_invoice(s, terms="14 days", notes="PO 8812")
    assert draft["status"] == "draft" and draft["due_date"] == "2026-03-15" and draft["total_cents"] == 30_000
    assert draft["terms"] == "14 days"
    s.write("POST", "/accounting/invoices/1/payments", {"amount_cents": 100, "payment_date": "2026-03-05",
                                                        "account_id": 1}, expect=409)
    sent = s.write("POST", "/accounting/invoices/1/send", {}, expect=200).json()
    assert sent["status"] == "sent" and sent["entry_id"]
    part = s.write("POST", "/accounting/invoices/1/payments", {"amount_cents": 10_000,
                                                               "payment_date": "2026-03-10", "account_id": 1,
                                                               "reference": "BACS 1"}, expect=201).json()
    assert part["status"] == "sent" and part["outstanding_cents"] == 20_000
    s.write("POST", "/accounting/invoices/1/payments", {"amount_cents": 20_001, "payment_date": "2026-03-11",
                                                        "account_id": 1}, expect=409)
    paid = s.write("POST", "/accounting/invoices/1/payments", {"amount_cents": 20_000,
                                                               "payment_date": "2026-03-11",
                                                               "account_id": 1}, expect=201).json()
    assert paid["status"] == "paid" and paid["outstanding_cents"] == 0 and len(paid["payments"]) == 2
    assert s.get("/accounting/invoices/1?as_of=2026-12-31")["effective_status"] == "paid"
    assert [i["invoice_number"] for i in s.get("/accounting/invoices?customer_id=1")] == ["INV-001"]
    assert s.get("/accounting/invoices?open_only=true") == []
    s.get("/accounting/invoices/99", expect=404)
    s.write("POST", "/accounting/invoices/1/void", {"reason": "too late"}, expect=409)


def test_overdue_comes_from_the_as_of_date():
    s = seeded("overdue")
    an_invoice(s)
    s.write("POST", "/accounting/invoices/1/send", {}, expect=200)
    assert s.get("/accounting/invoices/1?as_of=2026-03-15")["effective_status"] == "sent"
    assert s.get("/accounting/invoices/1?as_of=2026-03-16")["effective_status"] == "overdue"
    assert [i["invoice_number"] for i in s.get("/accounting/invoices?status=overdue&as_of=2026-04-01")] == \
        ["INV-001"]
    assert s.get("/accounting/invoices?status=overdue&as_of=2026-03-01") == []


def test_voiding_an_invoice_through_the_api_reverses_its_entry():
    s = seeded("void")
    an_invoice(s)
    sent = s.write("POST", "/accounting/invoices/1/send", {}, expect=200).json()
    voided = s.write("POST", "/accounting/invoices/1/void", {"reason": "wrong customer"}, expect=200).json()
    assert voided["status"] == "void" and voided["void_entry_id"]
    assert s.get(f"/accounting/journal/{voided['void_entry_id']}")["reverses_id"] == sent["entry_id"]
    assert s.get("/accounting/accounts/3/balance")["balance_cents"] == 0            # receivables back to nil
    s.write("POST", "/accounting/invoices/1/void", {}, expect=422)                  # a reason is required


def test_bill_endpoints_through_to_paid():
    s = seeded("bills")
    draft = a_bill(s)
    assert draft["status"] == "draft" and draft["due_date"] == "2026-03-31"
    s.write("POST", "/accounting/bills/1/payments", {"amount_cents": 100, "payment_date": "2026-03-05",
                                                     "account_id": 1}, expect=409)
    received = s.write("POST", "/accounting/bills/1/receive", {}, expect=200).json()
    assert received["status"] == "sent" and received["outstanding_cents"] == 27_000
    paid = s.write("POST", "/accounting/bills/1/payments", {"amount_cents": 27_000,
                                                            "payment_date": "2026-03-10",
                                                            "account_id": 1}, expect=201).json()
    assert paid["status"] == "paid"
    assert [b["bill_number"] for b in s.get("/accounting/bills?supplier_id=1")] == ["DR-1"]
    # A bill line can't be a sale, and a receipt can't land in an expense account.
    s.write("POST", "/accounting/bills", {"supplier_id": 1, "bill_number": "DR-2", "issue_date": "2026-03-01",
                                          "lines": [{"description": "Odd", "unit_price_cents": 100,
                                                     "account_code": "4000"}]}, expect=409)
    s.get("/accounting/bills/99", expect=404)


def test_report_endpoints():
    s = seeded("reports")
    s.write("POST", "/accounting/journal", {
        "entry_date": "2026-01-01", "memo": "Opening capital",
        "lines": [{"account_code": "1000", "debit_cents": 500_000},
                  {"account_code": "3000", "credit_cents": 500_000}]}, expect=201)
    an_invoice(s, cents=30_000)
    s.write("POST", "/accounting/invoices/1/send", {}, expect=200)
    a_bill(s, cents=27_000)
    s.write("POST", "/accounting/bills/1/receive", {}, expect=200)
    trial = s.get("/accounting/reports/trial-balance?as_of=2026-03-31")
    assert trial["balanced"] and trial["total_debit_cents"] == 500_000 + 30_000 + 27_000
    assert [r["code"] for r in s.get("/accounting/reports/trial-balance?as_of=2026-03-31&include_empty=true"
                                     )["rows"]] == [c[0] for c in CHART]
    pl = s.get("/accounting/reports/profit-and-loss?from_date=2026-01-01&to_date=2026-12-31")
    assert (pl["total_income_cents"], pl["total_expense_cents"], pl["net_income_cents"]) == (30_000, 27_000,
                                                                                             3_000)
    sheet = s.get("/accounting/reports/balance-sheet?as_of=2026-03-31")
    assert sheet["balanced"] and sheet["retained_earnings_cents"] == 3_000
    ar = s.get("/accounting/reports/ar-aging?as_of=2026-04-01")
    assert ar["totals"]["total_cents"] == 30_000 and ar["documents"][0]["bucket"] == "1_30"
    ap = s.get("/accounting/reports/ap-aging?as_of=2026-04-01")
    assert ap["totals"]["total_cents"] == 27_000
    assert [row["supplier"] for row in ap["by_supplier"]] == ["Dockside Roastery"]
    s.get("/accounting/reports/profit-and-loss?from_date=2026-01-01", expect=422)   # to_date is required
    s.get("/accounting/reports/trial-balance?as_of=2026-02-30", expect=422)


# ---------------------------------------------------------------- the rules of the surface

def test_writes_need_an_idempotency_key_and_replay_by_it():
    s = seeded("idempotency")
    body = {"name": "Southbank Studios", "payment_terms_days": 7}
    assert s.c.post("/accounting/customers", json=body).status_code == 422          # no key
    first = s.write("POST", "/accounting/customers", body, key="add-studios", expect=201).json()
    again = s.write("POST", "/accounting/customers", body, key="add-studios", expect=201).json()
    assert again == first and len(s.get("/accounting/customers")) == 2
    clash = s.write("POST", "/accounting/customers", dict(body, payment_terms_days=14), key="add-studios",
                    expect=409)
    assert "IdempotencyConflict" in clash.json()["detail"]


def test_bad_input_maps_to_422_and_unknown_fields_are_refused():
    s = seeded("bad_input")
    s.write("POST", "/accounting/invoices", {"customer_id": 1, "invoice_number": "INV-9",
                                             "issue_date": "2026-02-30",
                                             "lines": [{"description": "X", "unit_price_cents": 1,
                                                        "account_code": "4000"}]}, expect=422)
    s.write("POST", "/accounting/invoices", {"customer_id": 1, "invoice_number": "INV-9",
                                             "issue_date": "2026-03-01", "vat_rate": 20,
                                             "lines": [{"description": "X", "unit_price_cents": 1,
                                                        "account_code": "4000"}]}, expect=422)
    s.write("POST", "/accounting/invoices", {"customer_id": 1, "invoice_number": "INV-9",
                                             "issue_date": "2026-03-01", "lines": []}, expect=422)
    s.write("POST", "/accounting/customers", {"name": "Bad", "email": "nope"}, expect=422)
    s.write("POST", "/accounting/invoices", {"customer_id": 99, "invoice_number": "INV-9",
                                             "issue_date": "2026-03-01",
                                             "lines": [{"description": "X", "unit_price_cents": 1,
                                                        "account_code": "4000"}]}, expect=404)
    # A line has to name its account one way or the other, not both.
    s.write("POST", "/accounting/invoices", {"customer_id": 1, "invoice_number": "INV-9",
                                             "issue_date": "2026-03-01",
                                             "lines": [{"description": "X", "unit_price_cents": 1}]},
            expect=422)
    s.write("POST", "/accounting/invoices", {"customer_id": 1, "invoice_number": "INV-9",
                                             "issue_date": "2026-03-01",
                                             "lines": [{"description": "X", "unit_price_cents": 1,
                                                        "account_id": 6, "account_code": "4000"}]}, expect=422)


def test_wrong_json_types_are_refused_not_coerced():
    """pydantic's lax mode would turn {"unit_price_cents": "100"} into 100 and {"customer_id": true} into 1."""
    s = seeded("strict_types")
    line = {"description": "Catering", "unit_price_cents": 100, "account_code": "4000"}
    base = {"customer_id": 1, "invoice_number": "INV-9", "issue_date": "2026-03-01", "lines": [line]}
    for bad in ({"customer_id": True}, {"customer_id": "1"}, {"customer_id": 1.0},
                {"lines": [line | {"unit_price_cents": "100"}]}, {"lines": [line | {"quantity": 1.0}]},
                {"lines": [line | {"unit_price_cents": 10.5}]}):
        s.write("POST", "/accounting/invoices", base | bad, expect=422)
    for bad in ({"amount_cents": "100"}, {"amount_cents": 10.5}, {"account_id": True}):
        s.write("POST", "/accounting/invoices/1/payments",
                {"amount_cents": 100, "payment_date": "2026-03-10", "account_id": 1} | bad, expect=422)
    s.write("POST", "/accounting/journal", {"entry_date": "2026-01-01", "memo": "Odd",
                                            "lines": [{"account_code": "1000", "debit_cents": "100"},
                                                      {"account_code": "3000", "credit_cents": 100}]},
            expect=422)
    assert s.get("/accounting/invoices") == [] and s.get("/accounting/journal") == []


def test_numbers_too_big_for_sqlite_are_a_422_or_404_not_a_500():
    s = seeded("too_big")
    huge = 2 ** 70
    s.get(f"/accounting/accounts/{huge}", expect=404)
    s.get(f"/accounting/invoices/{huge}", expect=404)
    s.get(f"/accounting/bills/{huge}", expect=404)
    s.get(f"/accounting/journal/{huge}", expect=404)
    assert s.get(f"/accounting/invoices?customer_id={huge}") == []
    s.write("POST", "/accounting/journal", {"entry_date": "2026-01-01", "memo": "Huge",
                                            "lines": [{"account_code": "1000", "debit_cents": huge},
                                                      {"account_code": "3000", "credit_cents": huge}]},
            expect=422)
    s.write("POST", "/accounting/invoices", {"customer_id": 1, "invoice_number": "INV-9",
                                             "issue_date": "2026-03-01",
                                             "lines": [{"description": "X", "unit_price_cents": huge,
                                                        "account_code": "4000"}]}, expect=422)
    s.write("POST", f"/accounting/invoices/{huge}/send", {}, expect=422)
    assert s.get("/accounting/journal") == []


def test_send_receive_and_reverse_take_an_empty_body():
    s = seeded("empty_body")
    an_invoice(s)
    r = s.c.post("/accounting/invoices/1/send", headers={"Idempotency-Key": "nobody"})
    assert r.status_code == 200, r.text                                            # no body at all
    a_bill(s)
    r = s.c.post("/accounting/bills/1/receive", content=b"",
                 headers={"Idempotency-Key": "nobody:bill", "Content-Type": "application/json"})
    assert r.status_code == 200, r.text                                            # an empty JSON body
    s.write("POST", "/accounting/invoices/1/payments", {"amount_cents": 1, "payment_date": "2026-03-10",
                                                        "account_id": 1}, expect=201)
    # Voiding always needs its reason, so there is a record of why.
    assert s.c.post("/accounting/bills/1/void", headers={"Idempotency-Key": "noreason"}).status_code == 422


def test_every_string_and_the_key_have_a_length_limit():
    s = seeded("limits")
    long, longer = "x" * (accounting.MAX_TEXT + 1), "x" * (accounting.MAX_LONG_TEXT + 1)
    line = {"description": "Catering", "unit_price_cents": 100, "account_code": "4000"}
    base = {"customer_id": 1, "invoice_number": "INV-9", "issue_date": "2026-03-01", "lines": [line]}
    for field in ("invoice_number",):
        r = s.write("POST", "/accounting/invoices", base | {field: long}, expect=422)
        assert "at most 200" in r.text, r.text
    s.write("POST", "/accounting/invoices", base | {"terms": longer}, expect=422)
    s.write("POST", "/accounting/invoices", base | {"notes": longer}, expect=422)
    s.write("POST", "/accounting/invoices", base | {"lines": [line | {"description": long}]}, expect=422)
    s.write("POST", "/accounting/invoices", base | {"issue_date": "2026-03-01" * 2}, expect=422)
    s.write("POST", "/accounting/invoices", base, key="k" * (accounting.MAX_TEXT + 1), expect=422)
    s.write("POST", "/accounting/customers", {"name": long}, expect=422)
    s.write("POST", "/accounting/accounts", {"code": long, "name": "Long", "account_type": "asset"}, expect=422)
    s.write("POST", "/accounting/journal", {"entry_date": "2026-01-01", "memo": longer,
                                            "lines": [{"account_code": "1000", "debit_cents": 1},
                                                      {"account_code": "3000", "credit_cents": 1}]}, expect=422)
    s.get(f"/accounting/accounts/1/balance?as_of={'2026-01-01' * 2}", expect=422)
    s.get(f"/accounting/journal?source={long}", expect=422)
    # An entry or a document can't be made huge either.
    many = [{"account_code": "1000", "debit_cents": 1}] * (accounting.MAX_LINES + 1)
    s.write("POST", "/accounting/journal", {"entry_date": "2026-01-01", "memo": "Many", "lines": many},
            expect=422)
    s.write("POST", "/accounting/invoices", base | {"lines": [line] * (accounting.MAX_LINES + 1)}, expect=422)
    # Right at the limits is accepted.
    s.write("POST", "/accounting/invoices", base | {"terms": "t" * accounting.MAX_LONG_TEXT},
            key="k" * accounting.MAX_TEXT, expect=201)


def test_no_database_configured_is_a_503():
    c = client("unused")
    os.environ.pop("ACCOUNTING_DATABASE_PATH")
    assert c.get("/accounting/accounts").status_code == 503


def test_a_database_that_disappears_is_a_503_and_recovers_when_put_back():
    s = seeded("vanishing")
    path = os.environ["ACCOUNTING_DATABASE_PATH"]
    for suffix in ("-wal", "-shm"):
        if os.path.exists(path + suffix):
            os.remove(path + suffix)
    os.rename(path, path + ".away")
    try:
        r = s.c.get("/accounting/accounts")
        assert r.status_code == 503 and "can't be opened" in r.text, r.text
        s.write("POST", "/accounting/customers", {"name": "Later"}, expect=503)
        assert not os.path.exists(path)                                             # not recreated empty
    finally:
        os.rename(path + ".away", path)
    assert [x["code"] for x in s.get("/accounting/accounts")] == [c[0] for c in CHART]


def test_a_tampered_schema_is_a_503_and_clears_when_the_rule_is_put_back():
    s = seeded("tampered")
    path = os.environ["ACCOUNTING_DATABASE_PATH"]
    raw = sqlite3.connect(path)
    raw.execute("DROP TRIGGER acct_journal_entries_balanced")
    raw.commit()
    r = s.c.get("/accounting/accounts")
    assert r.status_code == 503 and "SchemaTampered" in r.text, r.text
    assert "acct_journal_entries_balanced is missing" in r.text
    sql = next(q for q in accounting._migrate._statements((accounting.MIGRATIONS / "0001_ledger.sql").read_text())
               if "CREATE TRIGGER acct_journal_entries_balanced" in q)
    raw.execute(sql)
    raw.commit()
    raw.close()
    assert len(s.get("/accounting/accounts")) == len(CHART)


def test_no_api_body_carries_a_field_this_module_has_no_business_holding():
    """The module keeps no pay figure, bank detail or tax number of its own: payroll posts finished journal
    lines through the hook (README), and the business's own bank account is a chart account, not a sort code."""
    banned = {"salary", "wage", "wages", "pay", "payroll", "iban", "sortcode", "ssn", "sin", "national",
              "passport", "dob", "birth", "medical", "health", "card", "cvv"}
    models = [m for m in vars(api).values()
              if isinstance(m, type) and issubclass(m, api._Body) and m is not api._Body]
    assert len(models) == 17
    for model in models:
        for name in model.model_fields:
            assert not banned & set(name.split("_")), (model.__name__, name)


# ---------------------------------------------------------------- main.py and the UI

def _routes_of_main(enabled: str) -> dict:
    env = dict(os.environ, LLM_PROVIDER="stub", ACCOUNTING_MODULE_ENABLED=enabled, APP_ENV="development",
               DATABASE_PATH=os.path.join(TMP.name, f"core_{enabled}.db"),
               ACCOUNTING_DATABASE_PATH=os.path.join(TMP.name, f"main_{enabled}.db"))
    code = ("import json, main\n"
            "from fastapi.testclient import TestClient\n"
            "paths = sorted(p for p in main.app.openapi()['paths'] if p.startswith('/accounting'))\n"
            "out = {'paths': paths}\n"
            "client = TestClient(main.app)\n"
            "if paths:\n"
            "    out['schema'] = client.get('/accounting/schema').json()\n"
            "out['ui'] = client.get('/accounting/ui/').status_code\n"
            "print(json.dumps(out))\n")
    done = subprocess.run([sys.executable, "-B", "-c", code], cwd=REPO / "operations_nerd", env=env,
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_main_mounts_the_router_and_the_ui_only_when_enabled():
    on = _routes_of_main("1")
    for path in ("/accounting/accounts", "/accounting/journal", "/accounting/invoices/{invoice_id}/send",
                 "/accounting/bills/{bill_id}/payments", "/accounting/reports/trial-balance"):
        assert path in on["paths"], path
    assert on["schema"] == {"module": "accounting", "version": 5} and on["ui"] == 200
    off = _routes_of_main("0")
    assert off["paths"] == [] and off["ui"] == 404


def test_the_ui_page_never_builds_html_from_a_string():
    """The same rule as operations_nerd/frontend/index.html: a customer called '<img src=x onerror=...>' is
    drawn as text, so the page uses createElement and textContent and never innerHTML."""
    page = (HERE.parent / "frontend" / "index.html").read_text()
    code = re.sub(r"<!--.*?-->", "", page, flags=re.S)             # the comments say the rule; the code keeps it
    for forbidden in ("innerHTML", "outerHTML", "document.write", "insertAdjacentHTML", "eval("):
        assert forbidden not in code, forbidden
    assert "textContent" in code and "createElement" in code
    # And it only ever reads: no request in it carries a method or an idempotency key.
    assert not re.search(r"method:\s*['\"](POST|PATCH|PUT|DELETE)", code)
    assert "Idempotency-Key" not in code


def test_capital_designation_endpoint_is_reachable_and_idempotent():
    c = client("capital_designation")
    calls = Calls(c)
    account = calls.write("POST", "/accounting/accounts", {"code": "1500", "name": "Equipment", "account_type": "asset"}, expect=201).json()
    path = f"/accounting/accounts/{account['account_id']}/designate-capital"
    first = calls.write("POST", path, None, key="capital", expect=200).json()
    assert calls.write("POST", path, None, key="capital", expect=200).json() == first
    assert first == {"account_id": account["account_id"], "capital": True}


def test_the_api_covers_every_command_the_module_has():
    """A command nobody can reach over HTTP is a gap; this catches one that is added later and not wired up."""
    paths = {route.path for route in api.router.routes}
    conn = accounting.connect(":memory:")
    accounting.init_db(conn)
    commands = {r["name"] for r in conn.execute("SELECT name FROM acct_command_names")}
    # post_payroll_entry is not a command name of its own: it posts with source 'payroll' through
    # /accounting/journal, which the Literal on NewJournalEntry.source allows.
    assert commands == {"add_account", "update_account", "post_journal_entry", "reverse_journal_entry",
                        "add_customer", "update_customer", "add_supplier", "update_supplier",
                        "create_invoice", "send_invoice", "record_invoice_payment", "void_invoice",
                        "create_bill", "receive_bill", "record_bill_payment", "void_bill",
                        "add_fund", "update_fund", "record_grant_receipt", "release_from_restriction",
                        "designate_capital_account"}
    for path in ("/accounting/accounts", "/accounting/accounts/{account_id}",
                 "/accounting/accounts/{account_id}/designate-capital", "/accounting/funds",
                 "/accounting/funds/{fund_id}", "/accounting/funds/grant-receipt",
                 "/accounting/funds/release-from-restriction", "/accounting/journal",
                 "/accounting/journal/{entry_id}/reverse", "/accounting/customers",
                 "/accounting/customers/{customer_id}", "/accounting/suppliers",
                 "/accounting/suppliers/{supplier_id}", "/accounting/invoices",
                 "/accounting/invoices/{invoice_id}/send", "/accounting/invoices/{invoice_id}/payments",
                 "/accounting/invoices/{invoice_id}/void", "/accounting/bills",
                 "/accounting/bills/{bill_id}/receive", "/accounting/bills/{bill_id}/payments",
                 "/accounting/bills/{bill_id}/void"):
        assert path in paths, path
    assert "payroll" in api.NewJournalEntry.model_fields["source"].annotation.__args__


if __name__ == "__main__":
    print(f"Python {sys.version.split()[0]}, SQLite {sqlite3.sqlite_version}")
    tests = [(n, f) for n, f in globals().items() if n.startswith("test_") and callable(f)]
    for name, fn in tests:
        fn()
        print(f"PASS {name}")
    print(f"\n{len(tests)} tests passed")
