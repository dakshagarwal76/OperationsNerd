"""Disposable SQLite regressions, no model/network. Plain script or pytest."""
import sys,uuid,sqlite3
from pathlib import Path
sys.path[:0]=[str(Path(__file__).resolve().parents[1]),str(Path(__file__).resolve().parent)]
import accounting as a
from test_funds import with_chart,equity_pair,AS_OF

def setup(kind='temporarily_restricted'):
 c,_=with_chart('reg_'+uuid.uuid4().hex);u,r,l,v,e=equity_pair(c)
 f=a.add_fund(c,'f','FUND','Grant',kind)['fund_id'];bank=a.account_by_code(c,'1000')['account_id']
 asset=a.add_account(c,'asset','1500','Equipment','asset')['account_id']
 a.designate_capital_account(c,'capital',asset)
 a.record_grant_receipt(c,'grant',f,'2026-01-01',10000,bank,v,'Grant')
 return c,f,bank,asset,u,r,l,e

def available(c,f):return a.fund(c,f,as_of=AS_OF)['available_cents']
def post(c,f,debit,credit,amount,key='p'):
 return a.post_journal_entry(c,key,'2026-02-01','Test',[{'account_id':debit,'debit_cents':amount,'fund_id':f},{'account_id':credit,'credit_cents':amount,'fund_id':f}])
def release(c,f,u,r,l,n=4000,key='rel'):return a.release_from_restriction(c,key,f,n,'2026-03-01','Purpose met',r,u,l)
def rejects(fn,exc=a.AccountingError):
 try:fn()
 except exc:return
 raise AssertionError('accepted invalid operation')

def test_capital_reduces_available():
 c,f,b,k,*_=setup();post(c,f,k,b,8000);assert available(c,f)==2000

def test_second_purchase_cannot_reuse_grant():
 c,f,b,k,*_=setup();post(c,f,k,b,8000);before=len(a.journal_entries(c));rejects(lambda:post(c,f,k,b,8000,'second'));assert len(a.journal_entries(c))==before

def test_capital_exact_balance():
 c,f,b,k,*_=setup();post(c,f,k,b,10000);assert available(c,f)==0

def test_capital_over_balance():
 c,f,b,k,*_=setup();rejects(lambda:post(c,f,k,b,10001));assert available(c,f)==10000

def test_expense_after_capital_blocked():
 c,f,b,k,u,r,l,e=setup();post(c,f,k,b,8000);rejects(lambda:post(c,f,e,b,2001,'expense'))

def test_capital_reversal_restores_once():
 c,f,b,k,*_=setup();p=post(c,f,k,b,8000);a.reverse_journal_entry(c,p['entry_id'],'rev');assert available(c,f)==10000

def test_capital_refund_restores_capacity():
 c,f,b,k,*_=setup();post(c,f,k,b,8000);post(c,f,b,k,3000,'refund');assert available(c,f)==5000

def test_bank_transfer_not_capital_spend():
 c,f,b,k,*_=setup();cash=a.account_by_code(c,'1010')['account_id'];post(c,f,cash,b,8000);assert available(c,f)==10000

def test_capital_transfer_not_double_counted():
 c,f,b,k,*_=setup();k2=a.add_account(c,'a2','1510','Equipment 2','asset')['account_id'];a.designate_capital_account(c,'cap2',k2);post(c,f,k,b,8000);post(c,f,k2,k,8000,'transfer');assert available(c,f)==2000

def test_depreciation_not_double_counted():
 c,f,b,k,u,r,l,e=setup();post(c,f,k,b,10000);post(c,f,e,k,2000,'depreciation');assert available(c,f)==0

def test_release_reduces_source_capacity():
 c,f,b,k,u,r,l,e=setup();release(c,f,u,r,l);assert available(c,f)==6000

def test_release_has_correct_equity_sides():
 c,f,b,k,u,r,l,e=setup();p=release(c,f,u,r,l);lines=p['lines'];assert any(x['account_id']==r and x['debit_cents']==4000 for x in lines);assert any(x['account_id']==u and x['credit_cents']==4000 for x in lines)

def test_release_replay_no_second_reduction():
 c,f,b,k,u,r,l,e=setup();release(c,f,u,r,l);release(c,f,u,r,l);assert available(c,f)==6000

def test_release_after_capital_cannot_exceed_available():
 c,f,b,k,u,r,l,e=setup();post(c,f,k,b,8000);rejects(lambda:release(c,f,u,r,l,3000));assert available(c,f)==2000

def test_permanent_release_remains_restricted_by_default():
 c,f,b,k,u,r,l,e=setup('permanently_restricted');rejects(lambda:release(c,f,u,r,l));assert available(c,f)==10000

def test_designation_cannot_reclassify_posted_asset():
 c,f,b,k,*_=setup();rejects(lambda:a.designate_capital_account(c,'bankcap',b))

def test_designation_requires_asset():
 c,f,b,k,u,r,l,e=setup();rejects(lambda:a.designate_capital_account(c,'expcap',e))

def test_designation_immutable_database():
 c,f,b,k,*_=setup();rejects(lambda:c.execute('DELETE FROM acct_fund_capital_accounts WHERE account_id=?',(k,)),sqlite3.IntegrityError)

def test_unrestricted_spending_unchanged():
 c,f,b,k,*_=setup();g=a.default_fund_id(c);post(c,g,k,b,50000);assert available(c,g)==a.MAX_CENTS

def test_as_of_excludes_future_capital():
 c,f,b,k,*_=setup();post(c,f,k,b,8000);assert a.fund(c,f,as_of='2026-01-15')['available_cents']==10000

def test_equity_clearing_cannot_cancel_release():
 c,f,b,k,u,r,l,e=setup();rejects(lambda:release(c,f,u,r,u));assert available(c,f)==10000

def test_capital_funds_isolated():
 c,f,b,k,u,r,l,e=setup();other=a.add_fund(c,'f2','FUND2','Other','temporarily_restricted')['fund_id'];post(c,f,k,b,8000);assert available(c,other)==0;rejects(lambda:post(c,other,k,b,1,'other'))

def capital_bill(c,f,k,amount=8000,key='bill'):
 supplier=a.add_supplier(c,'supplier:'+key,'Supplier '+key)['id']
 draft=a.create_bill(c,key,supplier,key,'2026-02-01',[{'description':'Equipment','unit_price_cents':amount,'account_id':k}],fund_id=f)
 return a.receive_bill(c,draft['bill_id'],'receive:'+key)

def test_capital_bill_payment_not_second_spend():
 c,f,b,k,*_=setup();bill=capital_bill(c,f,k);assert available(c,f)==2000;a.record_bill_payment(c,bill['bill_id'],8000,'2026-02-02',b,'pay');assert available(c,f)==2000

def test_capital_bill_overspend_refused():
 c,f,b,k,*_=setup();rejects(lambda:capital_bill(c,f,k,10001));assert available(c,f)==10000

def test_capital_bill_void_restores_capacity():
 c,f,b,k,*_=setup();bill=capital_bill(c,f,k);a.void_bill(c,bill['bill_id'],'Wrong bill','void');assert available(c,f)==10000

if __name__=='__main__':
 tests=[(n,v) for n,v in sorted(globals().items()) if n.startswith('test_') and callable(v)]
 for n,fn in tests:fn();print('PASS',n)
 print(len(tests),'regressions passed')
