
'use strict';
const base=JSON.parse(document.getElementById('summary-data').textContent);
const U='<span class="unknown">UNKNOWN</span>';
const esc=v=>String(v).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const known=v=>v!==null&&v!==undefined&&v!==''&&v!=='UNKNOWN';
const val=v=>known(v)?esc(v):U;
const money=v=>known(v)&&Number.isFinite(Number(v))?Number(v).toFixed(2):'UNKNOWN';
const time=(v,full=false)=>{if(!known(v)||Number.isNaN(Date.parse(v)))return U;return new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',...(full?{year:'numeric',month:'2-digit',day:'2-digit'}:{}),hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}).format(new Date(v));};
const badge=(text,kind='neutral')=>`<span class="badge ${kind}">${text}</span>`;
const cols=['标的','估分','状态（拟挂 / 不挂 + 原因）','拟挂 Yes（价 × 量）','拟挂 No（价 × 量）','两边合计成本 / 安全垫','拟占用资金','LP 收益率','今日奖励','今日返佣','份额占比','已配对利润','未配对库存','净收益率 %/天'];
const fields=['lp_yield_pct_per_day','reward_today','rebate_today','reward_share','paired_profit','unpaired_inventory','net_yield_pct_per_day'];
const parts=[['库存成本','inventory_cost'],['活动买挂','open_buys'],['在途·UNKNOWN','pending_requests'],['补腿预留','hedge_reserve'],['费用预留','fee_reserve']];
const demo=new URLSearchParams(location.search).get('demo')==='1';
let activeState='normal';
function render(state,payload=base,nowMs=Date.now()){
 activeState=state;
 const unavailable=state==='unreachable';
 const d=structuredClone(payload||{});d.run=d.run||{};d.capital=d.capital||{};d.safety=d.safety||{};d.wallet=d.wallet||{};d.freshness=d.freshness||{};d.markets=Array.isArray(d.markets)?d.markets:[];if(state==='wallet'){d.wallet.available_usdc='UNKNOWN';d.freshness.wallet_as_of='UNKNOWN';}
 const liveMode=d.mode==='live';
 const rotation=unavailable?{}:d.rotation||{},scan=rotation.scan||{};
 const blocker=unavailable?null:([rotation.blocker,d.blocker].find(b=>b&&typeof b==='object')||null);
 const waitHtml=blocker?`<div role="status"><h2>等待诊断 · ${blocker.escalated===true?'已升级':'等待中'}</h2><p>${val(blocker.code)} · since ${val(blocker.since)} · deadline ${val(blocker.deadline)}</p><p>${esc(JSON.stringify(blocker.numbers??'UNKNOWN'))}</p><p>${val(blocker.next_step)} · 停机安全 ${blocker.safe_to_stop===true?'是':blocker.safe_to_stop===false?'否':'UNKNOWN'}</p></div>`:'';
 const coverage=scan.reached_end===true&&!(scan.errors||[]).length?'政治类完整扫描第一':'已扫范围内第一';
 const r=unavailable?{}:d.run,c=unavailable?{}:d.capital,s=unavailable?{}:d.safety,f=unavailable?{}:d.freshness;
 const now=demo?new Date(d.as_of).getTime()+(state==='stale'?1800000:5000):nowMs;
 const age=v=>known(v)?(now-Date.parse(v))/1000:NaN;
 const stale=!unavailable&&age(d.as_of)>15;
 const oldDecision=!unavailable&&age(f.decision_as_of)>60,oldScore=!unavailable&&age(f.scores_as_of)>1500;
 const amount=(v,unit=true)=>`${val(money(v))}${unit?'<span class="unit">USDC</span>':''}`;
 const cap=Number(r.capital_usdc),total=Number(c.total),reserve=Number(c.proposed_reserve);
 const pct=v=>Number.isFinite(v)&&cap>0?Math.min(100,Math.max(0,v/cap*100)):0;
 const remaining=known(c.proposed_reserve)&&known(r.capital_usdc)?money(cap-reserve):'UNKNOWN';
 const banner=unavailable?['bad','lp_v2 不可达','当前快照读取失败，所有数据均显示 UNKNOWN；不会把上次的 0 当成当前结果。']:state==='wallet'?['warn','钱包余额读不到','钱包可用与余额更新时间显示 UNKNOWN；其他已知数据继续展示。']:stale?['bad','数据过期','快照已过期；保留上次数据。决策与估分按各自时间标记。']:liveMode?['','实盘执行','单市场执行；订单与资金以流水和交易所读回为准。']:['','只读试运行，未下单','拟挂只是模拟计划。真实占用与拟挂预留分开计算和展示。'];
 let rows;
 if(unavailable){rows=Array.from({length:2},()=>`<tr>${cols.map(()=>`<td>${U}</td>`).join('')}</tr>`).join('');}
 else rows=d.markets.map(m=>{
 const missingScore=!known(m.score),p=missingScore?null:m.proposed;
 const income=key=>`${val(m.m4?.[key])}${known(m.m4?.[key])&&['lp_yield_pct_per_day','reward_share','net_yield_pct_per_day'].includes(key)?'%':''}${key==='net_yield_pct_per_day'?`<span class="cell-sub">${amount(m.m4?.net_profit)}</span>`:''}`;
 const quote=side=>p?`<span class="quote">${val(p[side]?.price)} × ${val(p[side]?.quantity)}</span><span class="cell-sub">${oldDecision?'历史拟挂 · 决策停滞':'USDC / 份 × 份'}</span>`:'<span class="dash" aria-label="不适用">—</span>';
 return `<tr><td><div class="market-title">${val(m.title)}</div><span class="market-id">${val(m.condition_id)}</span></td><td class="${oldScore?'stale-cell':''}"><span class="score">${val(m.score)}</span><span class="cell-sub">${oldScore?'估分过期':'每轮扫描'}</span><span class="cell-sub">订单簿 ${time(m.books_as_of)}</span></td><td>${badge(missingScore?'不挂':m.eligible?(liveMode?'执行':'拟挂'):'不挂',m.eligible&&!missingScore?'good':'neutral')}${missingScore||m.skip_reason?`<p class="reason">${esc(m.skip_reason||(missingScore?'估分缺失':''))}</p>`:`<span class="cell-sub">${oldDecision?'决策停滞':liveMode?'按交易所读回':'仅计算，未发单'}</span>`}</td><td class="${oldDecision?'stale-cell':''}">${quote('yes')}</td><td class="${oldDecision?'stale-cell':''}">${quote('no')}</td><td>${p?`<span class="quote">${val(p.pair_cost)} / ${val(p.safety_margin)}</span><span class="cell-sub">每对份额 · USDC</span>`:'<span class="dash">—</span>'}</td><td>${p?`<strong>${val(money(p.reserved_usdc))}</strong><span class="cell-sub">USDC · 只读模拟</span>`:'<span class="dash">—</span>'}</td>${fields.map(key=>`<td>${income(key)}</td>`).join('')}</tr>`;
 }).join('');
 document.getElementById('dashboard').innerHTML=`
 <div class="card-head"><div class="run-title"><span class="section-num">01 / OVERVIEW</span><h2>${liveMode?'实盘执行':'只读试运行'}</h2>${badge(unavailable?'状态 UNKNOWN':liveMode?'运行中':'未下单',unavailable?'neutral':'good')}</div><div class="updated">${badge(unavailable?'接口不可达':stale?'数据过期':'本地快照',unavailable||stale?'bad':'neutral')}<span>更新于 ${time(unavailable?null:d.as_of)}（北京时间）</span></div></div>
 ${Number(d.schema_version)>1&&!unavailable?'<div class="banner warn">接口版本较新，部分字段未显示</div>':''}
 <div class="banner ${banner[0]}" role="status" aria-live="polite"><strong>${banner[1]}</strong> · ${banner[2]}</div>
 <div class="stats"><div class="stat"><div class="stat-label">资金上限</div><div class="stat-value">${amount(r.capital_usdc)}</div><div class="stat-hint">启动参数</div></div><div class="stat"><div class="stat-label">钱包可用</div><div class="stat-value">${amount(unavailable?null:d.wallet?.available_usdc)}</div><div class="stat-hint">只读余额</div></div><div class="stat"><div class="stat-label">地区检查</div><div class="stat-value" style="font-size:24px">${unavailable?U:r.region_check==='ok'?'通过':val(r.region_check)}</div><div class="stat-hint">${unavailable?'未取得状态':liveMode?'实盘运行环境':'只读运行环境'}</div></div><div class="stat"><div class="stat-label">已下单</div><div class="stat-value">${val(s.orders_sent)}<span class="unit">次</span></div><div class="stat-hint">自启动累计</div></div><div class="stat"><div class="stat-label">已撤单</div><div class="stat-value">${val(s.cancels_sent)}<span class="unit">次</span></div><div class="stat-hint">自启动累计</div></div></div>
 <div class="meta"><span>启动时间<strong>${time(r.started_at,true)}（北京时间）</strong></span><span>代码 SHA<strong class="mono">${val(r.code_sha)}</strong></span><span>钱包尾号<strong class="mono">${val(r.wallet_tail)}</strong></span><span>运行模式<strong>${unavailable?U:liveMode?'实盘':'只读'}</strong></span></div>
 <div class="capital"><div class="capital-heading"><h2><span class="section-num">02</span>资金占用</h2><span class="muted" style="font-size:11px">条形总长 = 资金上限</span></div><div class="capacity-grid"><div><div class="bar-label"><span>真实占用</span><strong>${amount(c.total)}</strong></div><div class="track ${unavailable?'empty-track':''}" aria-label="真实占用 ${unavailable?'UNKNOWN':money(c.total)+' USDC'}"><div class="actual-fill" style="width:${pct(total)}%"></div></div><div class="capital-parts">${parts.map(([label,key])=>`<span>${label}<b>${val(c[key])}</b></span>`).join('')}</div></div><div><div class="bar-label"><span>拟挂预留 <span class="muted">（只读模拟，非真实占用）</span></span><strong>${amount(c.proposed_reserve)}</strong></div><div class="track proposed-track ${unavailable?'empty-track':''}" aria-label="拟挂预留 ${unavailable?'UNKNOWN':money(c.proposed_reserve)+' USDC'}"><div class="proposed-fill" style="width:${pct(reserve)}%"></div></div><div class="capital-hint"><span>虚线 / 斜纹 · 份数 × 1 USDC</span><span>剩余可开额度 <strong>${val(remaining)}</strong> USDC</span></div></div></div></div>
 <div class="freshness"><span>上一轮决策<strong>${time(f.decision_as_of)}</strong>${oldDecision?' '+badge('决策停滞','warn'):''}</span><span>估分更新于<strong>${time(f.scores_as_of)}</strong>${oldScore?' '+badge('估分过期','warn'):''}</span><span>余额更新于<strong>${time(f.wallet_as_of)}</strong></span><span class="cadence">${demo?'离线演示 · 固定假快照，不发请求':'页面拉取 5 秒 · 实时状态快照'}</span></div>
 ${waitHtml}<div class="rotation" role="region" aria-label="市场轮换"><h2>市场轮换</h2><p>当前 ${val(rotation.current)} · ${esc(coverage)} ${val(rotation.candidate)} · ${val(rotation.phase)} · ${val(rotation.reason)}</p><p>切换 ${val(rotation.switch_count)} · 撤单 ${val(rotation.cancel_count)} · 扫描 ${time(scan.started_at,true)} → ${time(scan.finished_at,true)} · 耗时 ${val(scan.duration_s)} 秒（目录 ${val(scan.directory_duration_s)} / 市场 ${val(scan.market_duration_s)}）</p><p>${val(scan.pages)} 页 · ${val(scan.rows)} 条 · ${val(scan.requests)} 请求 · 初筛 ${val(scan.prescreened)} · ${scan.reached_end===true?'已到底':'不完整'} · ${esc(JSON.stringify(scan.errors||[]))}</p></div>
 <div class="market-head"><div><h2><span class="section-num">03</span>市场计划</h2><p>价格仅为拟挂参考 · M4 起展示实际收益和库存 · 窄屏可横向滚动表格</p></div><span class="market-count">${unavailable?U:d.markets.length+' 个市场 · '+d.markets.filter(m=>m.proposed).length+' 拟挂'}</span></div>
 <div class="table-wrap" tabindex="0" role="region" aria-label="市场表，14 列，可横向滚动"><table class="markets"><colgroup><col style="width:165px"><col style="width:65px"><col style="width:125px"><col style="width:104px"><col style="width:104px"><col style="width:117px"><col style="width:105px">${fields.map(()=>'<col style="width:94px">').join('')}</colgroup><thead><tr>${cols.map((name,i)=>`<th scope="col"><span class="col-no">${String(i+1).padStart(2,'0')}</span>${name}${i>=7?'<span class="m4">M4 起</span>':''}</th>`).join('')}</tr></thead><tbody>${rows}</tbody></table></div>
 <div class="table-note"><span>${unavailable?'接口不可达时不保留任何数值；市场名也标 UNKNOWN。':'“—” 表示不挂时无拟挂方案；UNKNOWN 表示该项没有数据。'}</span><span>旧钱包挂单不计为 lp_v2 占用</span></div>`;
 document.querySelectorAll('[data-state]').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.state===state)));
 document.documentElement.dataset.state=state;
}
let pending=false,latestPayload=null,unavailable=true;
function repaint(){render(unavailable?'unreachable':'live',latestPayload||{});}
async function poll(){
 if(pending)return;
 pending=true;
 const controller=new AbortController();
 let timeout;
 try {
  const payload=await Promise.race([
   (async()=>{
    const response=await fetch('/summary',{cache:'no-store',credentials:'omit',signal:controller.signal});
    if(!response.ok)throw Error('unavailable');
    return response.json();
   })(),
   new Promise((_,reject)=>{timeout=setTimeout(()=>{controller.abort();reject(Error('timeout'));},10000);})
  ]);
  latestPayload=payload;unavailable=false;repaint();
 } catch (_) {latestPayload=null;unavailable=true;repaint();}
 finally {clearTimeout(timeout);pending=false;}
}
if(demo){
 document.getElementById('demo-controls').hidden=false;
 document.getElementById('source-label').textContent='离线演示 / 固定假数据';
 document.querySelectorAll('[data-state]').forEach(b=>b.addEventListener('click',()=>{render(b.dataset.state);history.replaceState(null,'','#'+b.dataset.state);}));
 render(['normal','wallet','unreachable','stale'].includes(location.hash.slice(1))?location.hash.slice(1):'normal');
}else{
 repaint();poll();setInterval(poll,5000);setInterval(repaint,1000);
}
