"""Duplicate designation is a domain error, not a leaked SQLite constraint.
Disposable SQLite only; no provider requests.
"""
import os
from pathlib import Path
import sys
import unittest
ROOT = Path(os.environ.get('ACCOUNTING_SOURCE_ROOT', Path(__file__).resolve().parents[3]))
sys.path[:0] = [str(ROOT/'modules/accounting'),str(ROOT/'modules/accounting/tests')]
import accounting as a
from test_fund_spendable_regressions import setup
class DesignationErrors(unittest.TestCase):
 def setUp(self): self.c,self.f,self.b,self.k,self.u,self.r,self.l,self.e=setup()
 def tearDown(self): self.c.close()
 def check_duplicate(self, key):
  before=self.c.execute('SELECT COUNT(*) FROM acct_commands').fetchone()[0]
  with self.assertRaisesRegex(a.InvalidState,'already designated as capital'):
   a.designate_capital_account(self.c,key,self.k)
  self.assertEqual(self.c.execute('SELECT COUNT(*) FROM acct_commands').fetchone()[0],before)
  self.assertEqual(self.c.execute('SELECT COUNT(*) FROM acct_fund_capital_accounts WHERE account_id=?',(self.k,)).fetchone()[0],1)
 def test_duplicate_new_key(self): self.check_duplicate('second')
 def test_duplicate_after_posting(self):
  a.post_journal_entry(self.c,'spend','2026-02-01','Asset',[{'account_id':self.k,'debit_cents':100,'fund_id':self.f},{'account_id':self.b,'credit_cents':100,'fund_id':self.f}]);self.check_duplicate('second')
 def test_rejected_key_reusable(self):
  self.check_duplicate('second');new=a.add_account(self.c,'new','1520','Other asset','asset')['account_id'];a.designate_capital_account(self.c,'second',new)
 def test_original_key_replays(self): self.assertEqual(a.designate_capital_account(self.c,'capital',self.k),{'account_id':self.k,'capital':True})
 def test_replay_after_posting(self):
  a.post_journal_entry(self.c,'spend','2026-02-01','Asset',[{'account_id':self.k,'debit_cents':100,'fund_id':self.f},{'account_id':self.b,'credit_cents':100,'fund_id':self.f}]);self.assertTrue(a.designate_capital_account(self.c,'capital',self.k)['capital'])
 def test_original_key_other_payload_conflict(self):
  with self.assertRaises(a.IdempotencyConflict):a.designate_capital_account(self.c,'capital',self.b)
 def test_nonasset_still_rejected(self):
  with self.assertRaisesRegex(a.InvalidState,'asset account'):a.designate_capital_account(self.c,'bad',self.e)
 def test_posted_undesignated_still_rejected(self):
  with self.assertRaisesRegex(a.InvalidState,'first posting'):a.designate_capital_account(self.c,'bank',self.b)
 def test_duplicate_does_not_change_capacity(self):
  self.check_duplicate('other');self.assertEqual(a.fund(self.c,self.f,as_of='2026-12-31')['available_cents'],10000)
 def test_duplicate_twice_still_domain_error(self):self.check_duplicate('other');self.check_duplicate('other')
 def test_new_asset_designation(self):
  new=a.add_account(self.c,'new','1520','Other','asset')['account_id'];self.assertEqual(a.designate_capital_account(self.c,'newcap',new),{'account_id':new,'capital':True})
 def test_other_fund_does_not_reclassify_chart(self):
  a.add_fund(self.c,'other','OTHER','Other','temporarily_restricted');self.check_duplicate('second')
if __name__=='__main__': unittest.main()
