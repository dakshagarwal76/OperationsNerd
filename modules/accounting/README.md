# Accounting module: a double-entry ledger, invoices and bills

A chart of accounts, an append-only double-entry journal, customers and
suppliers, invoices and bills with line items, the payments that settle them,
and five reports (trial balance, profit and loss, balance sheet, AR aging, AP
aging). Built in the same way as the HR module (`modules/hr/`, on
`daksh/hr-module`) and the logistics stock engine (`modules/logistics/`, on
`jasmeet/logistics-stock`). It uses stdlib `sqlite3` only, imports nothing from
`operations_nerd/` and makes no LLM calls.

No minimum SQLite version is needed. `date()` before SQLite 3.45 lets
impossible days such as 2026-02-30 through, so each date CHECK also compares the
day with the last day of its month; the rules are the same on every version.

```
modules/accounting/
  accounting.py          commands, queries and reports (the API other code calls)
  migrate.py             versioned migrations + automatic backup (usable by any module)
  migrations/0001_ledger.sql
  migrations/0002_sales_and_purchases.sql
  migrations/0003_document_entries.sql
  migrations/0004_fund_accounting.sql
  api.py                 FastAPI router, /accounting/... (local dev only, see below)
  frontend/index.html    read-only dashboard at /accounting/ui
  demo_cafe.py           walkthrough for a small cafe
  tests/                 plain scripts, also collected by pytest
```

Each business has its own private database, so nothing takes a business id: a
connection is one business's books. Every table is prefixed `acct_`.

## Design

**The journal is the only place a figure lives.** `acct_journal_entries` and
`acct_journal_lines` are append-only: an entry is written once, with its lines,
and never changes. Debits equal credits for every entry, and every entry has at
least two lines, checked by the database itself. The lines are inserted *before*
the entry row, so a `BEFORE INSERT` trigger on the entry can add them up and
refuse the whole thing; there is no code path, not even raw SQL, that leaves the
books unbalanced. A mistake is corrected by a **reversing entry** (the same
lines with debits and credits swapped), never by an edit, and an entry is
reversed at most once — correct a reversal by reversing the reversal.

**Invoices and bills are documents, not balances.** What an invoice does to the
books is the journal entry it points at (`entry_id`); its own row carries who,
when, how much and what state it is in, so a person can read it and so the aging
reports know when the money is due. The posting rules:

| what | debit | credit |
|---|---|---|
| send an invoice | accounts receivable | the income account on each line |
| receive a bill | the expense account on each line | accounts payable |
| an invoice payment | the account the money arrived in | accounts receivable |
| a bill payment | accounts payable | the account the money left from |

Receivables and payables are found by **role**, not by account number: one
account in the chart carries `role='accounts_receivable'` and one
`role='accounts_payable'` (unique, enforced by a partial index), so a business
can number its chart however it likes. Every figure the reports show is read
back out of the journal, never out of a document row.

**Money is whole cents in INTEGER columns**, everywhere, in and out: no amount
is ever a float, so no sum ever drifts. A line's `amount_cents` is
`quantity * unit_price_cents` with an integer quantity, so nothing rounds.
Amounts are capped at `MAX_CENTS` (100 billion in whole currency), far above any
small business's numbers, so every sum stays inside SQLite's 64-bit integers.

**Statuses are `draft`, `sent`, `paid` and `void`** and only move forward
(`draft → sent → paid`, or `→ void` from either of the first two).
`overdue` is **not** a stored status: whether a document is overdue depends on
what day it is, so storing it would mean a row that goes stale on its own.
Queries derive it from the due date and an `as_of` date and return it as
`effective_status`.

| status | what it means | in the books? |
|---|---|---|
| `draft` | written down, not yet issued; still editable by voiding it | no entry, nothing in the reports |
| `sent` | issued (an invoice sent, a bill received) | its entry is posted |
| `paid` | payments add up to the total exactly | its entry plus a payment entry each |
| `void` | cancelled; `reason` required | a draft had no entry; a sent one's entry is reversed |
| `overdue` | derived: `sent` (or part-paid) and `due_date < as_of` | — |

Voiding is the only way back: a document with any money against it can never be
voided (there is no refund in v1, and a payment entry is not reversed on its own),
and a void never deletes anything,
so the original entry and its reversal both stay on file.

**Commands** each run in one `BEGIN IMMEDIATE` transaction with an idempotency
key unique across all commands (`acct_commands`). Replaying a key with the same
command and payload returns the stored result and writes nothing; the same key
with anything else raises `IdempotencyConflict`; a failed command records
nothing, so its key stays free. Dates come from the caller on every command;
read-only queries and reports take an `as_of` (or a range) and fall back to the
machine's date only when it isn't given — nothing stores it.

**Text is normalised before it's stored.** Every name, code, number and
description is NFC-normalised, trimmed, and inner runs of whitespace become one
space; email is also lowercased. So `" 1000 "` and `"1000"` are the same account
code, and `Accounts@Northgate.test` clashes with `accounts@northgate.test`.
Text inputs are capped at `accounting.MAX_TEXT` (200) characters, notes, terms
and memos at `MAX_LONG_TEXT` (2000), and one entry or document at `MAX_LINES`
(500) lines; longer is a `ValueError` (422 in the API).

### Tables

| table | what | rules (enforced by the database) |
|---|---|---|
| `acct_command_names` | allowed command names | each migration adds its own |
| `acct_commands` | idempotency log | immutable |
| `acct_entry_sources` | allowed entry sources | `manual`, `payroll`, `invoice`, `bill`, `invoice_payment`, `bill_payment` |
| `acct_accounts` | the chart of accounts | `code` unique (case-insensitively); `account_type` one of asset, liability, equity, income, expense; type and code never change; at most one account per `role`; a role needs the matching type; never deleted; an account with entries can't be deactivated |
| `acct_journal_entries` | one entry per event | immutable; debits = credits = `total_cents`; at least two lines; `reverses_id` points at an entry that isn't itself already reversed, and its lines must mirror that entry exactly; `source` from `acct_entry_sources`; never deleted |
| `acct_journal_lines` | the debits and credits | immutable; exactly one of `debit_cents`/`credit_cents` is positive and the other 0; the account must be active; `line_no` unique per entry; written before their entry (so the entry's trigger can check them) and refused once the entry exists |
| `acct_customers`, `acct_suppliers` | who we invoice and who bills us | `name` unique (case-insensitively); `payment_terms_days` 0..3650; never deleted (deactivated instead) |
| `acct_invoices` | invoices | `invoice_number` unique per customer; `due_date` on or after `issue_date`; born `draft` with no entry and nothing paid; status moves forward only; `total_cents` equals the sum of its lines; a non-draft, non-void invoice has an entry whose receivable debit equals its total; `paid` exactly when payments equal the total; `void` needs a `void_entry_id` reversing its entry (unless it never had one) and no money paid; never deleted |
| `acct_invoice_lines` | invoice line items | immutable; `amount_cents = quantity * unit_price_cents`; `quantity` 1..`MAX_QUANTITY`; the account must be an income account; `line_no` unique per invoice; insertable only while the invoice is a draft |
| `acct_invoice_payments` | money received | immutable; the invoice must be `sent` (not draft, void or already settled); the total paid can't exceed the invoice; `payment_date` on or after the invoice's entry date; the deposit account must be an asset account that isn't the receivables control account; its entry's credit equals the amount |
| `acct_bills`, `acct_bill_lines`, `acct_bill_payments` | the same, the other way round | `bill_number` unique per supplier; bill lines must be expense accounts and never the payables control account; a payment's source account is an asset account that isn't payables |
| `module_migrations` | applied schema versions per module | shared by all modules (see Migrations) |

### Reports

All five read the journal, so they agree with it by construction.

| report | what | how |
|---|---|---|
| `trial_balance(as_of, include_empty=False)` | every account's debits and credits up to `as_of`, and whether they're equal | sums `acct_journal_lines` by account |
| `profit_and_loss(from_date, to_date)` | income, expenses and the net for a period | income and expense accounts, entries dated in the range |
| `balance_sheet(as_of)` | assets = liabilities + equity + profit so far | balance-sheet accounts, plus `retained_earnings_cents` (income − expenses up to `as_of`), which is what makes it balance |
| `ar_aging(as_of)` | who owes us what, by how late | per invoice and per customer, in buckets `current`, `1_30`, `31_60`, `61_90`, `over_90` |
| `ap_aging(as_of)` | what we owe, by how late | the same, per bill and per supplier |
| `fund_balances(as_of)` | net assets per fund | sums journal lines by fund and account type |
| `statement_of_activities(from_date, to_date)` | income and expenses split by donor restriction | unrestricted funds vs restricted funds in separate columns |

`trial_balance`, `profit_and_loss` and `balance_sheet` accept an optional
`fund_id`: totals with no filter match the whole-business reports (every line
tags to the default **General** unrestricted fund after migration, so existing
databases behave the same until restricted funds are used).

The agings are **as the books stood on `as_of`**: a document counts if its own
entry is dated on or before `as_of` and its void (if any) came later, and its
outstanding amount is the total less the payments dated on or before `as_of`. So
each aging total equals the balance of its control account on that date — a test
asserts it at five different dates. Drafts never appear (they aren't in the
books), and the day a document falls due it is still `current`.

### Fund accounting

Non-profits track **funds**: buckets that show whether money is unrestricted or
donor-restricted to a purpose. Migration `0004_fund_accounting.sql` adds
`acct_funds` and a **`fund_id` on every journal line**. Each entry must balance
overall and **within each fund** (the database checks both). Every business
gets a default **General** unrestricted fund (`code` `GEN`); upgrades backfill
existing lines to it, so behaviour without restricted funds is unchanged.

| restriction type | meaning |
|---|---|
| `unrestricted` | operating grants and spending with no donor limit |
| `temporarily_restricted` | donor earmarked until a time or purpose is met |
| `permanently_restricted` | donor earmarked without a release date |

**Grant receipt** (`record_grant_receipt`) debits cash and credits grant income,
both tagged to the fund, which increases its **available** balance. **Spending**
against a restricted fund (expense debits on manual entries or bills) is
**blocked** when it would exceed available balance
(`InsufficientFundBalance`). **Release from restriction**
(`release_from_restriction`) posts a four-line interfund entry that moves net
assets from the restricted fund to unrestricted when the purpose is met. Invoices
and bills accept an optional `fund_id`; otherwise they post to the default fund.

Demo: `python -B modules/accounting/demo_nonprofit.py`.

### Hook for HR payroll

`accounting.post_payroll_entry(conn, key, entry_date, memo, lines, source_ref=None)`
is `post_journal_entry` with `source='payroll'`. It is **documented and
deliberately unwired**: nothing in this module, and nothing in `modules/hr/`,
calls it, and a test walks the AST of `accounting.py` and `api.py` to prove no
call site exists. `payroll` is a valid source from migration 0001, so the hook
needs no schema change later.

When payroll is built it should work out the gross, the deductions and the net
itself and hand the finished, balanced lines over, one entry per pay run, with
the pay run's own id as the idempotency key, so a retried pay run posts once:

```python
accounting.post_payroll_entry(conn, f"payrun:{run_id}", "2026-01-31", "January pay run", [
    {"account_code": "6000", "debit_cents":  gross_cents},        # wages expense
    {"account_code": "2200", "credit_cents": tax_cents},          # PAYE / tax payable
    {"account_code": "2210", "credit_cents": pension_cents},      # pension payable
    {"account_code": "2220", "credit_cents": net_cents},          # net wages payable
], source_ref=f"payrun:{run_id}")
```

Paying the staff is a second entry (debit net wages payable, credit the bank
account). This module holds **no pay figure of its own** — no salary, rate, bank
or tax field in any table or request body, and a test fails if one appears. The
reasons are HR's (`modules/hr/README.md`, "No payroll or sensitive fields"): the
databases are plain SQLite files with plain backup copies, with no encryption at
rest and no access control inside a business. Which accounts a pay run posts to,
and who may run one, are open questions below.

### Agreements

Out of scope for v1 except the text: `acct_customers`, `acct_suppliers`,
`acct_invoices` and `acct_bills` each carry a `terms` field (up to 2000
characters) and a `notes` field. A customer's `terms` is the standing agreement,
a document's `terms` is what was agreed for that one job, and
`payment_terms_days` is the only part of an agreement the module acts on: it
sets a document's default `due_date`. Nothing parses the text.

### Trust boundary

The rules live in the database's triggers and CHECKs, so they protect against
any code that writes through SQLite, but not against someone who can change the
schema: anyone with write access to the file can drop a trigger. That access is
the trust boundary; the module doesn't try to defend against it. What it does is
notice: `init_db` (and the API, on every request) runs `verify_schema`, which
compares every `acct_` trigger, index and table with what the migration files
create and raises `SchemaTampered` (503 in the API) on anything missing, changed
or extra. Restoring the definition (or a backup) clears it. It can't detect rows
that were changed while a rule was missing.

## Commands and queries (`accounting.py`)

```python
import accounting
conn = accounting.connect("business.db")
accounting.init_db(conn)               # migrates to the latest version, backing up first

accounting.add_account(conn, key, code, name, account_type, role=None, notes=None)
accounting.update_account(conn, account_id, {"name": ...}, key)        # name, notes, is_active

accounting.post_journal_entry(conn, key, entry_date, memo, lines, source="manual", source_ref=None)
accounting.post_payroll_entry(conn, key, entry_date, memo, lines, source_ref=None)   # the HR hook
accounting.reverse_journal_entry(conn, entry_id, key, entry_date=None, memo=None)

accounting.add_customer(conn, key, name, **fields)     # email, phone, billing_address,
accounting.add_supplier(conn, key, name, **fields)     # payment_terms_days, terms, notes
accounting.update_customer(conn, customer_id, changes, key)
accounting.update_supplier(conn, supplier_id, changes, key)

accounting.create_invoice(conn, key, customer_id, invoice_number, issue_date, lines,
                          due_date=None, terms=None, notes=None)
accounting.send_invoice(conn, invoice_id, key, entry_date=None)
accounting.record_invoice_payment(conn, invoice_id, amount_cents, payment_date, deposit_account_id, key,
                                  reference=None)
accounting.void_invoice(conn, invoice_id, reason, key, entry_date=None)
accounting.create_bill / receive_bill / record_bill_payment / void_bill       # the same, for bills

accounting.account / account_by_code / accounts / account_balance
accounting.journal_entry / journal_entries
accounting.customer / customers / supplier / suppliers
accounting.invoice / invoices / bill / bills             # as_of= derives effective_status
accounting.add_fund / update_fund / fund / funds / default_fund_id
accounting.record_grant_receipt / release_from_restriction
accounting.trial_balance / profit_and_loss / balance_sheet / ar_aging / ap_aging
accounting.fund_balances / statement_of_activities   # optional fund_id on TB, P&L, balance sheet
accounting.verify_schema(conn)         # raises SchemaTampered (init_db calls it)
```

A journal line names its account by `account_id` **or** `account_code` (not
both) and carries exactly one of `debit_cents` or `credit_cents`, plus an
optional `memo`. A document line is
`{"description": ..., "unit_price_cents": ..., "quantity": 1, "account_code": ...}`.

Errors: `ValueError` (bad input), `NotFound`, `InvalidState`, `Unbalanced`,
`IdempotencyConflict`, `SchemaTampered` (all but `ValueError` subclass
`AccountingError`); a raw write that breaks a database rule raises
`sqlite3.IntegrityError`.

- **Due dates** default to `issue_date` plus the party's `payment_terms_days`
  (0 means due on issue).
- **Part payments** are allowed and leave the document `sent`; the payment that
  brings the total to exactly the invoice amount makes it `paid`. A payment that
  would overpay is an `InvalidState`, and two concurrent payments can't both
  spend the same room (the database's own check settles the race; tested).
- **Entry dates:** sending or receiving dates the entry on the document's issue
  date unless `entry_date` says otherwise; a payment's entry carries the payment
  date; a reversal carries the original entry's date, so a void doesn't move
  profit into another period.
- **Amounts** are capped at `MAX_CENTS` and ids at SQLite's 2^63 − 1, so a typo
  is a `ValueError`, not a crash. A read by an id no row can have (0, negative,
  past 2^63 − 1) is `NotFound`; a non-integer id is a `ValueError`. Booleans are
  not integers here: `{"quantity": True}` is refused.

## HTTP API (`api.py`)

| method | path | |
|---|---|---|
| GET, POST | `/accounting/accounts` | list (`?account_type=`, `?active_only=`), add |
| GET, PATCH | `/accounting/accounts/{id}` | read, update (`changes`) |
| GET | `/accounting/accounts/{id}/balance` | `?as_of=` |
| GET, POST | `/accounting/journal` | list (`?from_date=`, `?to_date=`, `?source=`, `?limit=`), post |
| GET | `/accounting/journal/{id}` | the entry and its lines |
| POST | `/accounting/journal/{id}/reverse` | optional `entry_date`, `memo` |
| GET, POST | `/accounting/customers`, `/accounting/suppliers` | list, add |
| GET, PATCH | `/accounting/customers/{id}`, `/accounting/suppliers/{id}` | read, update |
| GET, POST | `/accounting/invoices` | list (`?customer_id=`, `?status=`, `?as_of=`, `?open_only=`), create a draft |
| GET | `/accounting/invoices/{id}` | `?as_of=` for `effective_status` |
| POST | `/accounting/invoices/{id}/send` | the body is optional |
| POST | `/accounting/invoices/{id}/payments` | `amount_cents`, `payment_date`, `account_id`, `reference` |
| POST | `/accounting/invoices/{id}/void` | `reason` required |
| GET, POST, … | `/accounting/bills…` | the same five, with `/receive` instead of `/send` |
| GET | `/accounting/reports/trial-balance` | `?as_of=`, `?include_empty=` |
| GET | `/accounting/reports/profit-and-loss` | `?from_date=`, `?to_date=` (both required) |
| GET | `/accounting/reports/balance-sheet`, `/ar-aging`, `/ap-aging` | `?as_of=` |
| GET | `/accounting/schema` | schema version |

Every POST and PATCH needs an `Idempotency-Key` header (it becomes the command's
key, so clients can retry). Status codes: 422 bad input (unknown fields are
refused, and JSON types are strict — `"100"` is not 100), 404 not found, 409
conflicts (`InvalidState`, `Unbalanced`, `IdempotencyConflict`, a database
rule), 503 if `ACCOUNTING_DATABASE_PATH` isn't set, if a database this process
already set up has disappeared (it is not quietly recreated empty; putting the
file back recovers), if the database can't be read, or on `SchemaTampered`. A
database behind the code's schema version is migrated on its next request.
Every string field, query parameter and the `Idempotency-Key` header is
length-capped (as above).

`frontend/index.html` is a **read-only** dashboard mounted at
`/accounting/ui`: the five reports, the invoice and bill lists and the chart of
accounts, with one `as of` date box. It writes nothing, so it needs no
idempotency key, and it renders every value with `createElement` and
`textContent` and never `innerHTML`, so a customer called
`<img src=x onerror=...>` is drawn as that text — the same rule as
`operations_nerd/frontend/index.html`, with a test of its own.

There's no login or per-business database routing on `main` yet, so the router
is a **local development surface only**: `main.py` mounts the router and the UI
when `ACCOUNTING_MODULE_ENABLED=1`, never when `APP_ENV=production`, on the
SQLite file in `ACCOUNTING_DATABASE_PATH`.

```
cd operations_nerd
LLM_PROVIDER=stub ACCOUNTING_MODULE_ENABLED=1 ACCOUNTING_DATABASE_PATH=/tmp/acct_dev.db uvicorn main:app
```

PowerShell: `$env:ACCOUNTING_MODULE_ENABLED="1"; $env:ACCOUNTING_DATABASE_PATH="$env:TEMP\acct_dev.db"; uvicorn main:app`.

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
  `.gitignore` ignores `*.bak`.
- A run takes one `BEGIN IMMEDIATE` lock, re-reads the version inside it and
  only then backs up (through a second, read-only connection), so two processes
  migrating at once take one backup and apply each version once. Each version
  runs in its own savepoint with its `module_migrations` row: a failing version
  rolls back completely, while the versions before it in the same run are kept.
  It refuses to go backwards or to open a database newer than the code.
- 0001 is a usable ledger on its own (accounts and the journal); 0002 adds
  customers, suppliers, invoices, bills and payments; 0003 keeps a payment or a
  void from being dated before the document's entry, refuses a reversal of a
  payment entry, and gives each document its own journal entry. All three are
  additive, so an upgrade rewrites nothing.

`accounting.init_db(conn, backup_dir=None)` is
`migrate(conn, "accounting", accounting.MIGRATIONS)` followed by
`verify_schema`. Whoever builds the per-business database opener should call
each module's `init_db` when opening a business's file (and pick the backup
directory and retention, which nothing here prunes yet).

`migrate.py` is byte-for-byte HR's copy, loaded under the module name
`accounting_migrate` so the two modules can't shadow each other (see the notes
at the end).

## Tests

From the repo root (the core runner includes module tests):

```
python scripts/run_tests.py modules/accounting      # just accounting
python -B modules/accounting/tests/test_sales.py    # one script
python -m pytest modules/accounting/tests           # if pytest is installed
```

| script | tests | covers |
|---|---|---|
| `test_ledger.py` | 30 | the chart of accounts (codes, types, roles, deactivation), balanced entries and every way to unbalance one, reversals and mirrored lines, append-only-ness, idempotent replay/conflict/failed key, 8 concurrent posters, input limits, raw SQL against every account, entry and line rule, and that the payroll hook has no call site (by AST) |
| `test_sales.py` | 30 | customers, draft invoices and their lines, sending, part and full payments, overpaying, voiding a draft and a sent invoice, overdue derived from `as_of`, listing and filters, replay, a racing overpay, raw SQL against every invoice rule |
| `test_purchases.py` | 16 | suppliers, bill numbers unique per supplier, receiving, payments, voiding, expense-account and control-account rules, replay, raw SQL against every bill rule |
| `test_reports.py` | 18 | a hand-computed year: trial balance, P&L (accrual, not cash), the balance sheet balancing on every 7th day of the year, retained earnings, aging buckets and their boundaries, each aging equalling its control account at five dates, voids/drafts/part payments in the agings, and `as_of` defaulting to today without being stored |
| `test_migrations.py` | 17 | fresh, rerun, stepwise upgrade with a backup holding the old data, v1 usable alone, failing migration rolls back (keeping earlier versions of the same run), numbering rules, a newer database refused, sharing a database with HR's and logistics' tables, a 4-thread migration race, concurrent migrators taking one backup, in-memory, schema-tamper detection, every date CHECK refusing impossible days on any SQLite version, every money column INTEGER and capped |
| `test_api.py` | 20 | every endpoint, 404/409/422/503 mapping, `Idempotency-Key` required and replayed, strict JSON types (no `"100"`→100 or `true`→1), empty bodies on send/receive, too-big numbers, length limits on bodies, header, query and path, a vanished database, a tampered schema, `main.py` mounting the router and the UI only when enabled and never in production, the UI never building HTML from a string, and every command being reachable over HTTP |
| `test_cafe_walkthrough.py` | 3 | the demo end to end against figures worked out by hand, and replaying it writes nothing |
| `test_funds.py` | 7 | per-fund balancing, grants, overspend blocking, release, migration backfill, reports |

"Raw" tests write SQL directly, bypassing `accounting.py`, so anything they see
rejected is rejected by the database itself.

## Walkthrough

`python -B modules/accounting/demo_cafe.py` (a temp database, deleted
afterwards; `--db cafe.db` keeps it): a cafe opens its books with 5,000.00 of
capital, invoices Northgate Offices 585.00 on 14-day terms and banks 400.00 of
it, enters the roastery's bean bill and the month's rent, voids a bill it
entered twice, and reads all five reports back out of the journal.

## Known limits (v1)

- **No tax.** No VAT/GST or sales-tax lines, rates or returns. A tax figure can
  be a line of its own on a manual entry, but nothing works it out.
- **One currency**, unnamed: amounts are cents of whatever the business uses. No
  foreign-currency documents or revaluation.
- **No fractional quantities.** `quantity` is a whole number, so 1.5 hours is
  priced as 90 units of a minute (or a line of its own). This keeps
  `amount_cents` exact, which matters more in v1 than fractional units.
- **No period close.** Nothing stops a backdated entry in a period already
  reported on; the reports simply change. There is no year-end closing entry
  either: the balance sheet derives `retained_earnings_cents` instead.
- **No credit notes or refunds** as their own documents: void the document, or
  post a manual entry.
- **No editing a draft.** A wrong draft is voided and re-entered (the lines are
  immutable, which is what keeps the totals honest).
- **No bank reconciliation, no statement import, no attachments.**
- **No users or roles**, so nothing records who posted an entry beyond the
  memo, and no approval is needed to post one.
- Backups aren't pruned.
- Someone with write access to the database file can still change it; the schema
  check notices changed rules, not changed data (see Trust boundary).
- The agings use one calendar date; there's no per-business time zone yet.

## Notes for Amaan and the team

- **No conflicts.** Accounting touches only `modules/accounting/` plus small
  additive edits to `operations_nerd/main.py`, `scripts/run_tests.py`,
  `documentation/CONTRIBUTING.md` and `.gitignore` — the same four files the HR
  branch edits, in the same places, so expect a trivial conflict in each if HR
  merges first (both add a block guarded by their own env var).
- **`migrate.py` is duplicated.** This folder's copy is byte-for-byte
  `modules/hr/migrate.py`. Both modules load it from their own folder, so one
  shared copy (say `modules/_shared/migrate.py` or a small package) should
  replace the two when both land. Until then, this module loads it by path as
  `accounting_migrate` and `api.py` loads `accounting.py` by path too, so
  neither module puts its folder on `sys.path` and HR's plain `import migrate`
  always gets HR's copy. Verified with both routers mounted.
- **Payroll is the one real seam with HR.** `post_payroll_entry` is ready and
  unwired; see the questions below for what it needs decided.
- **Per-business database:** `api.py` reads one file from
  `ACCOUNTING_DATABASE_PATH`. When login and per-business routing land, replace
  `get_conn` with the signed-in business's connection and drop the production
  guard.

### Open scope questions

1. **A default chart of accounts.** Should a new business start with a
   ready-made chart (and which one — by country?), or build its own? Right now
   it starts empty, which means a first-run wizard somewhere.
2. **Tax.** Which market first, and is per-line tax with a rate table in scope
   for v2, or do we stay tax-free and let the accountant adjust?
3. **Payroll's accounts.** When HR payroll lands, who decides which accounts a
   pay run posts to (wages expense, tax payable, pension payable, net wages
   payable) — a settings page, or fixed codes? And may the owner see the module
   at all, given pay figures become visible in the journal?
4. **Credit notes and refunds.** Needed for v1 in the field, or is voiding
   enough?
5. **Who may post.** Once there are users, should posting a manual journal entry
   or voiding a sent document need a second person, and should the journal
   record the user?
6. **Period close.** Do we need to lock a period (and a "books closed through"
   date) before this is in front of an accountant?
7. **Multi-currency**, and whether the business's currency belongs in this
   module's settings or the app's.

### Restricted-fund spendable balance correction (migration 0005)

Spendable balance is net income plus net equity, less net expenses and net
capital acquisition debits. Use `designate_capital_account(conn, key, account_id)`
on an asset account **before its first posting** to classify capital explicitly.
The designation is immutable. Cash accounts are not capital accounts. Account
names and codes are never used to guess this distinction.

A cash-to-equipment purchase reduces spendable balance once. A cash-to-cash or
capital-to-capital transfer does not reduce it again. Depreciation debits expense
and credits designated capital, so its net spendable effect is zero. A capital
refund or journal reversal restores only the corresponding net amount. A bill
for designated capital consumes capacity when received, not again when paid.
These are spendable-capacity rules, not a change to the normal balance sheet.

**Migration limit:** existing asset postings are not automatically classified.
An operator must review existing asset histories and agree an opening spendable
adjustment/mapping before relying on this guard for those balances. The helper
refuses retroactive classification. HTTP chart setup uses
`POST /accounting/accounts/{account_id}/designate-capital` with an Idempotency-Key. Unclassified
asset purchases cannot be distinguished from cash transfers by this schema.

Temporary releases debit restricted net assets and credit unrestricted net
assets using per-fund balanced liability-clearing lines. Equity clearing is
refused because its credit would cancel the restricted equity debit in capacity.
Permanent funds always remain restricted by default. The release command refuses
them instead of borrowing temporary-purpose rules. No application-level override
or permanent-release exception is provided. A deliberate programmer change is
separate from the normal release workflow.
