-- Explicit capital classification, never inferred from a code or account name.
INSERT INTO acct_command_names(name) VALUES ('designate_capital_account');
CREATE TABLE acct_fund_capital_accounts (
    account_id INTEGER PRIMARY KEY REFERENCES acct_accounts(id)
);
CREATE TRIGGER acct_fund_capital_accounts_asset BEFORE INSERT ON acct_fund_capital_accounts
WHEN NOT EXISTS(SELECT 1 FROM acct_accounts WHERE id=NEW.account_id AND account_type='asset')
  OR EXISTS(SELECT 1 FROM acct_journal_lines WHERE account_id=NEW.account_id)
BEGIN SELECT RAISE(ABORT, 'capital designation requires an unposted asset account'); END;
CREATE TRIGGER acct_fund_capital_accounts_no_update BEFORE UPDATE ON acct_fund_capital_accounts
BEGIN SELECT RAISE(ABORT, 'capital designation is immutable'); END;
CREATE TRIGGER acct_fund_capital_accounts_no_delete BEFORE DELETE ON acct_fund_capital_accounts
BEGIN SELECT RAISE(ABORT, 'capital designation is immutable'); END;
