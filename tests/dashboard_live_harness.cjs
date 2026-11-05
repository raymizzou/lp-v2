const assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs');
const {html,js,payload}=JSON.parse(fs.readFileSync(0,'utf8'));
const target={innerHTML:''},source={textContent:html.match(/id="summary-data">([\s\S]*?)<\/script>/)[1]};
const document={getElementById:id=>id==='summary-data'?source:target,querySelectorAll:()=>[],documentElement:{dataset:{}}};
const context=vm.createContext({document,structuredClone,Intl,Date,Number,URLSearchParams,AbortController,setTimeout,clearTimeout,
 location:{search:'?demo=1',hash:''},history:{replaceState(){}},setInterval(){},fetch(){throw Error('offline only');}});
vm.runInContext(js,context);
context.payload=payload;context.now=Date.parse(payload.as_of);
// Direct rendering uses the served function, preserving its actual escaping path.
vm.runInContext("render('live',payload,now)",context);
let row=target.innerHTML.match(/<tbody>([\s\S]*?)<\/tbody>/)[1].match(/<tr>([\s\S]*?)<\/tr>/)[1];
let cells=[...row.matchAll(/<td[^>]*>([\s\S]*?)<\/td>/g)].map(m=>m[1]);
assert.equal(cells.length,14);
for(const [index,value] of [[7,'10'],[8,'2'],[9,'1'],[10,'25'],[11,'0.20'],[12,'4'],[13,'15']])assert(cells[index].includes(value),[index,cells[index]]);
assert(cells[13].includes('3.00'),'net amount next to rate in existing final cell');
assert.match(target.innerHTML,/&lt;img/);assert.doesNotMatch(target.innerHTML,/<img/);
assert.doesNotMatch(target.innerHTML,/未下单|仅计算，未发单/);
context.payload.markets[0].m4={};
vm.runInContext("render('live',payload,now)",context);
row=target.innerHTML.match(/<tbody>([\s\S]*?)<\/tbody>/)[1].match(/<tr>([\s\S]*?)<\/tr>/)[1];
cells=[...row.matchAll(/<td[^>]*>([\s\S]*?)<\/td>/g)].map(m=>m[1]);
assert.equal(cells.length,14);assert(cells.slice(7).every(c=>c.includes('UNKNOWN')));
console.log('live m4, amount, fourteen columns and escaping PASS');
