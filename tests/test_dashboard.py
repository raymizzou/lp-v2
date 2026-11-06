import json
from pathlib import Path
import subprocess
from decimal import Decimal
from lp_v2.cli import RunConfig
from tests.test_status_server import request, AT
from tests.test_readonly_runtime import ScriptedReads


def test_standalone_dashboard_contract():
    from lp_v2.status_server import make_handler
    from tests.test_readonly_sdk import scanned_runner as ReadOnlyRunner
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
    from tests.test_readonly_sdk import scanned_runner as ReadOnlyRunner
    runner=ReadOnlyRunner(RunConfig(Decimal('100'),AT),ScriptedReads())
    handler=make_handler(runner.config,runner)
    html=request(handler,path='/dashboard')[2].decode()
    js=request(handler,path='/dashboard.js')[2].decode()
    result=subprocess.run(['node',str(Path(__file__).with_name('dashboard_pending_harness.cjs'))],input=json.dumps(dict(html=html,js=js)),text=True,capture_output=True,timeout=15)
    assert result.returncode==0,result.stdout+result.stderr


def test_served_dashboard_csp_allows_same_origin_assets():
    from html.parser import HTMLParser
    from lp_v2.status_server import make_handler
    from tests.test_readonly_sdk import scanned_runner as ReadOnlyRunner
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
    from tests.test_readonly_sdk import scanned_runner as ReadOnlyRunner
    runner=ReadOnlyRunner(RunConfig(Decimal('100'),AT),ScriptedReads())
    handler=make_handler(runner.config,runner)
    html=request(handler,path='/dashboard')[2].decode()
    js=request(handler,path='/dashboard.js')[2].decode()
    from tests.test_readonly_sdk import step_at
    payload=step_at(runner,AT)
    payload['mode']='live';payload['safety']['orders_sent']=2
    payload['markets'][0]['title']='<img src=x onerror=alert(1)>'
    payload['markets'][0]['m4']=dict(lp_yield_pct_per_day='10',reward_today='2',rebate_today='1',reward_share='25',
        paired_profit='0.20',unpaired_inventory='4',net_yield_pct_per_day='15',net_profit='3.00')
    result=subprocess.run(['node',str(Path(__file__).with_name('dashboard_live_harness.cjs'))],
        input=json.dumps(dict(html=html,js=js,payload=payload)),text=True,capture_output=True,timeout=15)
    assert result.returncode==0,result.stdout+result.stderr


def test_rotation_coverage_and_waiting():
    js=Path('src/lp_v2/dashboard/dashboard.js').read_text()
    html=Path('src/lp_v2/dashboard/index.html').read_text()
    program=r'''
const vm=require('node:vm'),fs=require('node:fs'),assert=require('node:assert/strict');
const input=JSON.parse(fs.readFileSync(0,'utf8'));
const base=JSON.parse(input.html.match(/id="summary-data">([\s\S]*?)<\/script>/)[1]);
const target={innerHTML:''};
const document={getElementById:id=>id==='summary-data'?{textContent:JSON.stringify(base)}:target,querySelectorAll:()=>[],documentElement:{dataset:{}}};
const context=vm.createContext({document,structuredClone,Intl,Date,Number,URLSearchParams,location:{search:'?demo=1',hash:''},history:{replaceState(){}}});
vm.runInContext(input.js,context);
context.payload={...base,rotation:{current:'market-A',candidate:'<market-B>',phase:'exiting',reason:'waiting_inventory',switch_count:3,cancel_count:6,scan:{started_at:base.as_of,finished_at:base.as_of,duration_s:115,directory_duration_s:100,market_duration_s:15,pages:115,rows:11411,requests:130,prescreened:5,reached_end:false,errors:[{page:116,reason:'cursor_invalid'}]}}};
vm.runInContext("render('normal',payload)",context);
for(const expected of ['已扫范围内第一','market-A','&lt;market-B&gt;','waiting_inventory','115','11411','130','100','15','切换 3','撤单 6'])assert.ok(target.innerHTML.includes(expected),expected);
assert.ok(!target.innerHTML.includes('政治类完整扫描第一'));
context.payload.rotation.scan.reached_end=true;
vm.runInContext("render('normal',payload)",context);
assert.ok(target.innerHTML.includes('已扫范围内第一'));
assert.ok(!target.innerHTML.includes('政治类完整扫描第一'));
context.payload.rotation.scan.errors=[];
vm.runInContext("render('normal',payload)",context);
assert.ok(target.innerHTML.includes('政治类完整扫描第一'));
'''
    result=subprocess.run(['node','-e',program],input=json.dumps(dict(js=js,html=html)),text=True,capture_output=True,timeout=10)
    assert result.returncode==0,result.stdout+result.stderr


def test_wait_blocker_visibility():
    from lp_v2.status_server import make_handler
    from tests.test_readonly_sdk import scanned_runner as ReadOnlyRunner
    runner = ReadOnlyRunner(RunConfig(Decimal('100'), AT), ScriptedReads())
    handler = make_handler(runner.config, runner)
    payload = dict(html=request(handler, path='/dashboard')[2].decode(),
                   js=request(handler, path='/dashboard.js')[2].decode())
    script = r'''
const vm=require('node:vm'),assert=require('node:assert/strict');
const input=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
const base=JSON.parse(input.html.match(/id="summary-data">([\s\S]*?)<\/script>/)[1]);
const target={innerHTML:''};
const context=vm.createContext({document:{getElementById:id=>id==='summary-data'?{textContent:JSON.stringify(base)}:target,
 querySelectorAll:()=>[],documentElement:{dataset:{}}},structuredClone,Intl,Date,Number,URLSearchParams,
 location:{search:'?demo=1',hash:''},history:{replaceState(){}}});
vm.runInContext(input.js,context);
context.payload=structuredClone(base);
context.payload.rotation={blocker:{code:'unexpected_error:Novel@balance',since:'2026-11-06T00:00:00Z',
 deadline:'2026-11-06T00:10:00Z',escalated:true,numbers:{difference:'0.125'},next_step:'check <secret>',safe_to_stop:'UNKNOWN'}};
vm.runInContext("render('normal',payload)",context);
let html=target.innerHTML;
for(const text of ['unexpected_error:Novel@balance','2026-11-06T00:10:00Z','0.125','check &lt;secret&gt;','已升级','停机安全 UNKNOWN'])assert.ok(html.includes(text),text);
assert.ok(!html.includes('<secret>'));
context.payload.rotation.blocker.safe_to_stop=false;
vm.runInContext("render('normal',payload)",context);assert.ok(target.innerHTML.includes('停机安全 否'));
context.payload.blocker=context.payload.rotation.blocker;context.payload.rotation={};
vm.runInContext("render('normal',payload)",context);assert.ok(target.innerHTML.includes('unexpected_error:Novel@balance'));
'''
    result = subprocess.run(['node', '-e', script], input=json.dumps(payload), text=True, capture_output=True)
    assert result.returncode == 0, result.stdout + result.stderr
