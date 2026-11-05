import json
from pathlib import Path
import subprocess
from decimal import Decimal
from lp_v2.cli import RunConfig
from tests.test_status_server import request, AT
from tests.test_readonly_runtime import ScriptedReads


def test_standalone_dashboard_contract():
    from lp_v2.status_server import make_handler
    from lp_v2.readonly_runtime import ReadOnlyRunner
    runner=ReadOnlyRunner(RunConfig(Decimal('100'),AT),ScriptedReads())
    handler=make_handler(runner.config,runner)
    code,headers,html=request(handler,path='/dashboard')
    assert code==200 and headers.get_content_type()=='text/html'
    code,headers,js=request(handler,path='/dashboard.js')
    assert code==200
    for path in ('/../cli.py','/dashboard/../../cli.py','/dashboard.js?x=1'):
        assert request(handler,path=path)[0]==404
    result=subprocess.run(['node',str(Path(__file__).with_name('dashboard_harness.cjs'))],input=json.dumps(dict(html=html.decode(),js=js.decode())),text=True,capture_output=True)
    assert result.returncode==0,result.stdout+result.stderr


def test_poll_pending_and_out_of_order_remain_truthful():
    from lp_v2.status_server import make_handler
    from lp_v2.readonly_runtime import ReadOnlyRunner
    runner=ReadOnlyRunner(RunConfig(Decimal('100'),AT),ScriptedReads())
    handler=make_handler(runner.config,runner)
    html=request(handler,path='/dashboard')[2].decode()
    js=request(handler,path='/dashboard.js')[2].decode()
    result=subprocess.run(['node',str(Path(__file__).with_name('dashboard_pending_harness.cjs'))],input=json.dumps(dict(html=html,js=js)),text=True,capture_output=True,timeout=15)
    assert result.returncode==0,result.stdout+result.stderr


def test_served_dashboard_csp_allows_same_origin_assets():
    from html.parser import HTMLParser
    from lp_v2.status_server import make_handler
    from lp_v2.readonly_runtime import ReadOnlyRunner
    runner=ReadOnlyRunner(RunConfig(Decimal('100'),AT),ScriptedReads())
    handler=make_handler(runner.config,runner)
    status,headers,body=request(handler,path='/dashboard?demo=1')
    assert status==200
    policies=[]
    class Tags(HTMLParser):
        def handle_starttag(self,tag,attrs):
            fields=dict(attrs)
            if tag=='meta' and fields.get('http-equiv','').lower()=='content-security-policy':
                policies.append(fields['content'])
    Tags().feed(body.decode())
    policies.append(headers['Content-Security-Policy'])
    assert 'src="/dashboard.js"' in body.decode()
    for policy in policies:
        directives={part.split()[0]:part.split()[1:] for part in policy.split(';') if part.split()}
        for name in ('script-src','connect-src'):
            allowed=directives.get(name,directives.get('default-src',[]))
            assert "'self'" in allowed,(name,policy)
            assert "'unsafe-inline'" not in allowed and "'unsafe-eval'" not in allowed
            assert not any(source=='*' or ':' in source for source in allowed),policy
        assert "'unsafe-inline'" in directives['style-src']
        for name in ('object-src','base-uri','form-action'):
            assert directives.get(name)==["'none'"],(name,policy)
    assert request(handler,path='/dashboard.js')[0]==200


def test_live_income_fourteen_columns():
    from lp_v2.status_server import make_handler
    from lp_v2.readonly_runtime import ReadOnlyRunner
    runner=ReadOnlyRunner(RunConfig(Decimal('100'),AT),ScriptedReads())
    handler=make_handler(runner.config,runner)
    html=request(handler,path='/dashboard')[2].decode()
    js=request(handler,path='/dashboard.js')[2].decode()
    payload=runner.step(AT)
    payload['mode']='live';payload['safety']['orders_sent']=2
    payload['markets'][0]['title']='<img src=x onerror=alert(1)>'
    payload['markets'][0]['m4']=dict(lp_yield_pct_per_day='10',reward_today='2',rebate_today='1',reward_share='25',
        paired_profit='0.20',unpaired_inventory='4',net_yield_pct_per_day='15',net_profit='3.00')
    result=subprocess.run(['node',str(Path(__file__).with_name('dashboard_live_harness.cjs'))],
        input=json.dumps(dict(html=html,js=js,payload=payload)),text=True,capture_output=True,timeout=15)
    assert result.returncode==0,result.stdout+result.stderr
