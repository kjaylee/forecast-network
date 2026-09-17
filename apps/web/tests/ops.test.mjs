import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {analyticsQuery,createAnalyticsSession,formatCount,formatFixed,knownCostLabel,ratioPresentation,validateReport} from '../public/ops.mjs';

const token='fixture-admin-token-never-real-0123456789';
const now=Date.UTC(2026,8,15,12),day=86400000;
const dates={start:'2026-08-15',end:'2026-09-14',cohortStart:'2026-08-15',cohortEnd:'2026-09-14'};
const url=analyticsQuery(dates,now);
function payload(){const q=new URL(url,'https://local.invalid').searchParams;return {data:{formulaVersion:'product-analytics-v1',asOfMs:now,window:{startMs:Number(q.get('start')),endMs:Number(q.get('end')),timezone:'UTC',complete:true},cohortWindow:{startMs:Number(q.get('cohortStart')),endMs:Number(q.get('cohortEnd'))},population:{kind:'fixture'},activity:{daily:[]},cohorts:[],retention:{},quality:{},disputes:{},costs:{provider:{unit:'USD_MICRO'},chain:{unit:'DEVNET_LAMPORT'}},inputHash:'a'.repeat(64)}};}
const response=(body=payload(),status=200)=>({ok:status>=200&&status<300,status,text:async()=>JSON.stringify(body)});
const deferred=()=>{let resolve;const promise=new Promise(r=>resolve=r);return{promise,resolve};};

test('zero, unknown, immature, denominator and scaled overflow remain distinct',()=>{
 assert.equal(formatCount(0),'0');assert.equal(formatCount(null),'자료 없음');
 const ratio={numerator:0,denominator:5,status:'available',unit:'share',scale:10000,valueBp:0,valueScaled:0};
 assert.equal(ratioPresentation(ratio).value,'0.00%');
 assert.equal(ratioPresentation({...ratio,status:'immature',valueBp:null}).value,'아직 미성숙');
 assert.equal(ratioPresentation({...ratio,status:'no_denominator',denominator:0,valueBp:null}).value,'분모 없음');
 assert.equal(ratioPresentation({...ratio,status:'out_of_range',numerator:Number.MAX_SAFE_INTEGER,valueScaled:null,valueBp:null}).value,'표시 범위 초과');
 assert.equal(ratioPresentation(null).value,'자료 없음');
 assert.equal(ratioPresentation({...ratio,valueBp:1234,numerator:999}).value,'12.34%','must display server value, not recalculate numerator / denominator');
});
test('exact unit formatting preserves safe integers and never combines currencies',()=>{
 assert.equal(formatFixed(Number.MAX_SAFE_INTEGER,6),'9,007,199,254.740991');
 assert.equal(formatFixed(5000,9,{trim:true}),'0.000005');
 assert.equal(formatFixed(0,6,{trim:true}),'0');
 assert.equal(formatFixed(Number.MAX_SAFE_INTEGER+1,6),'자료 없음');
 const base={numerator:5000,denominator:1,status:'available',scale:10000,valueScaled:50000000};
 assert.equal(ratioPresentation({...base,unit:'DEVNET_LAMPORT_per_predictor'}).value,'0.000005 Devnet SOL / 활성 예측자');
 assert.equal(ratioPresentation({...base,unit:'USD_MICRO_per_predictor'}).value,'0.005 USD / 활성 예측자');
});
test('no known receipts is distinct from a confirmed zero-cost receipt',()=>{
 assert.equal(knownCostLabel({knownOperations:0,knownRecordedSubtotalAtomic:0,unit:'USD_MICRO'}),'확인된 영수증 없음');
 assert.equal(knownCostLabel({knownOperations:1,knownRecordedSubtotalAtomic:0,unit:'USD_MICRO'}),'0 USD');
 assert.equal(knownCostLabel({knownOperations:1,knownRecordedSubtotalAtomic:5000,unit:'DEVNET_LAMPORT'}),'0.000005 Devnet SOL');
});
test('only complete UTC windows become a fixed API query',()=>{
 assert.ok(url.startsWith('/api/admin/analytics?'));assert.ok(!url.includes(token));
 for(const change of [{start:'2026-02-30'},{end:'2026-09-16'},{start:'2020-01-01'},{end:dates.start},{start:'https://evil.invalid'}])assert.throws(()=>analyticsQuery({...dates,...change},now));
 assert.equal(new URL(url,'https://local.invalid').searchParams.get('end'),String(Date.UTC(2026,8,14)));
});
test('no token causes no network request and tokens never enter exposed state',async()=>{
 const calls=[],states=[];const client=createAnalyticsSession({fetchImpl:async(...args)=>{calls.push(args);return response();},now:()=>now,onChange:s=>states.push(s)});
 assert.equal(await client.refresh(dates),false);assert.equal(calls.length,0);
 assert.equal(await client.connect('too-short',dates),false);assert.equal(calls.length,0);
 assert.equal(await client.connect(token,dates),true);assert.equal(calls.length,1);
 assert.equal(calls[0][0],url);assert.equal(calls[0][1].method,'GET');assert.equal(calls[0][1].headers.Authorization,'Bearer '+token);
 for(const [key,value]of Object.entries({mode:'same-origin',credentials:'omit',cache:'no-store',redirect:'error',referrerPolicy:'no-referrer'}))assert.equal(calls[0][1][key],value);
 assert.ok(!JSON.stringify(states).includes(token));client.clear();assert.equal(client.hasCredential(),false);assert.equal(states.at(-1).report,null);
 assert.equal(await client.refresh(dates),false);assert.equal(calls.length,1);
});
test('disconnect erases data and ignores a late response even if transport ignores abort',async()=>{
 const pending=deferred(),states=[];const client=createAnalyticsSession({fetchImpl:()=>pending.promise,now:()=>now,onChange:s=>states.push(s)});
 const request=client.connect(token,dates);client.clear();pending.resolve(response());await request;
 assert.equal(client.hasCredential(),false);assert.equal(states.at(-1).phase,'locked');assert.equal(states.at(-1).report,null);
});
test('an invalid new period also fences a pending response',async()=>{
 const pending=deferred(),states=[];const client=createAnalyticsSession({fetchImpl:()=>pending.promise,now:()=>now,onChange:s=>states.push(s)});
 const request=client.connect(token,dates);await client.refresh({...dates,end:dates.start});pending.resolve(response());await request;
 assert.equal(states.at(-1).phase,'error');assert.equal(states.at(-1).report,null);assert.match(states.at(-1).error,/UTC/);client.clear();
});
test('authentication failure clears credentials without reading or reflecting response secrets',async()=>{
 let read=false;const states=[];const client=createAnalyticsSession({fetchImpl:async()=>({ok:false,status:403,text:async()=>{read=true;return token;}}),now:()=>now,onChange:s=>states.push(s)});
 await client.connect(token,dates);assert.equal(read,false);assert.equal(client.hasCredential(),false);assert.equal(states.at(-1).report,null);assert.match(states.at(-1).error,/인증/);assert.ok(!JSON.stringify(states).includes(token));
});
test('server and malformed responses clear data and produce fixed safe messages',async()=>{
 for(const result of [response(null,503),response({data:{}}),{ok:true,status:200,text:async()=>token}]){
  const states=[];const client=createAnalyticsSession({fetchImpl:async()=>result,now:()=>now,onChange:s=>states.push(s)});await client.connect(token,dates);assert.equal(states.at(-1).report,null);assert.equal(states.at(-1).phase,'error');assert.ok(!JSON.stringify(states).includes(token));client.clear();
 }
});
test('wrong report period and wrong currency fail closed',()=>{
 const wrong=payload();wrong.data.window.endMs+=day;assert.throws(()=>validateReport(wrong,url));
 const currency=payload();currency.data.costs.chain.unit='USD_MICRO';assert.throws(()=>validateReport(currency,url));
});
test('timeout cannot publish a late successful report',async()=>{
 const states=[];const client=createAnalyticsSession({fetchImpl:async()=>{await new Promise(r=>setTimeout(r,12));return response();},now:()=>now,onChange:s=>states.push(s),timeoutMs:2});
 await client.connect(token,dates);assert.equal(states.at(-1).report,null);assert.match(states.at(-1).error,/시간/);client.clear();
});
test('source excludes persistence, telemetry, token URLs and unsafe HTML rendering',()=>{
 const source=readFileSync(new URL('../public/ops.mjs',import.meta.url),'utf8'),html=readFileSync(new URL('../public/ops.html',import.meta.url),'utf8');
 assert.doesNotMatch(source,/localStorage|sessionStorage|indexedDB|console\.|sendBeacon|innerHTML|insertAdjacentHTML/);
 assert.match(source,/mode:'same-origin'/);assert.match(source,/pagehide/);assert.match(html,/type="password"/);
 assert.doesNotMatch(html,/name="(?:token|admin-token)"/);assert.match(html,/form-action 'none'/);
});
