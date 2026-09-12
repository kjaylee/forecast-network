import test from 'node:test';
import assert from 'node:assert/strict';
import {createMarketClient,validateMarketQuote,validateMarket,claimsInPoints} from '../public/market-client.mjs';
const market=()=>({forecastId:'f1',mode:'shadow',status:'open',yesProbabilityBps:5000,revision:0,atomicScale:1000000,maxSpendPoints:100,specificationHash:'spec1',policyHash:'policy1',liveEnabled:false});
const quote=(overrides={})=>({quoteId:'q1',forecastId:'f1',userId:'u1',mode:'shadow',side:'YES',spendPoints:50,claimsAtomic:'98000000',priceBeforeBps:5000,priceAfterBps:5100,expiresAt:2000,revision:0,specificationHash:'spec1',policyHash:'policy1',...overrides});
const deferred=()=>{let resolve;const promise=new Promise(r=>resolve=r);return {promise,resolve};};
const receipt=q=>({...q,id:'fill1',status:'accepted',acceptedAt:1001});
test('quote binds account, source, policy, side, amount and atomic output',()=>{
 const context={market:market(),userId:'u1',side:'YES',spendPoints:50};
 assert.equal(validateMarketQuote(quote(),context,1000).claimsAtomic,'98000000');
 for(const change of [{userId:'u2'},{mode:'active'},{forecastId:'f2'},{side:'NO'},{spendPoints:51},{specificationHash:'other'},{policyHash:'other'},{claimsAtomic:98},{claimsAtomic:'1e7'},{claimsAtomic:'0'},{priceAfterBps:10001},{expiresAt:1000}])assert.throws(()=>validateMarketQuote(quote(change),context,1000));
 assert.equal(claimsInPoints('123456789'),123.456789);assert.throws(()=>validateMarket({...market(),atomicScale:1},'f1'));
});
test('only eligibility review permits an unavailable price with no claimed probability revision',()=>{
 const reviewed={...market(),probabilityStatus:'eligibility_review',yesProbabilityBps:null,probabilityRevision:null};
 assert.equal(validateMarket(reviewed,'f1'),reviewed);
 const frozen={...market(),revision:5,probabilityStatus:'frozen_before_evidence',probabilityRevision:3};
 assert.equal(validateMarket(frozen,'f1'),frozen);
 for(const value of [
  {...market(),yesProbabilityBps:null},
  {...reviewed,probabilityStatus:'current'},
  {...reviewed,yesProbabilityBps:0},
  {...reviewed,probabilityRevision:1},
  {...reviewed,probabilityRevision:undefined},
  {...frozen,yesProbabilityBps:null},
  {...frozen,probabilityRevision:6},
  {...frozen,probabilityRevision:-1},
  {...frozen,probabilityRevision:'3'},
  {...frozen,probabilityRevision:undefined},
  {...market(),probabilityStatus:'invented'},
 ])assert.throws(()=>validateMarket(value,'f1'));
});
for(const probabilityStatus of ['eligibility_review','frozen_before_evidence']){
 const stopped=()=>({...market(),probabilityStatus,probabilityRevision:probabilityStatus==='eligibility_review'?null:0,yesProbabilityBps:probabilityStatus==='eligibility_review'?null:5000});
 test(`${probabilityStatus}: direct quote calls cannot bypass the display gate`,async()=>{
  let calls=0;
  const client=createMarketClient({now:()=>1000,api:async()=>{calls++;return quote();}});
  client.attach(stopped(),'u1');await client.quote();await client.fill();
  assert.equal(calls,0);assert.equal(client.get().quote,null);
 });
 test(`${probabilityStatus}: a fresh hold blocks an already quoted fill and ignores late quotes`,async()=>{
  let calls=0;
  const client=createMarketClient({now:()=>1000,api:async()=>{calls++;return quote();}});
  client.attach(market(),'u1');await client.quote();client.get().market=stopped();await client.fill();assert.equal(calls,1);
  const waiting=deferred();
  const late=createMarketClient({now:()=>1000,api:async()=>waiting.promise});
  late.attach(market(),'u1');const pending=late.quote();late.get().market=stopped();waiting.resolve(quote());await pending;
  assert.equal(late.get().quote,null);
 });
 test(`${probabilityStatus}: lost acknowledgments remain unresolved without resubmitting a stopped market`,async()=>{
  let calls=0;
  const client=createMarketClient({now:()=>1000,api:async(path)=>{if(path.endsWith('/quote'))return quote();calls++;throw {status:0,code:'timeout'};}});
  client.attach(market(),'u1');await client.quote();await client.fill();assert.equal(client.get().phase,'uncertain');
  client.detach();client.attach(stopped(),'u1');await client.fill();assert.equal(calls,1);assert.equal(client.get().phase,'uncertain');
 });
}
test('anonymous preview cannot accept or impersonate a funded quote',async()=>{
 let fills=0;const client=createMarketClient({now:()=>1000,api:async(path)=>{if(path.endsWith('/fill'))fills++;return quote({quoteId:null,userId:'preview',mode:'preview',nonbinding:true});}});
 client.attach(market(),null);await client.quote();assert.equal(client.get().phase,'quoted');await client.fill();assert.equal(fills,0);
});
test('double confirmation sends one exact request with minimum return',async()=>{
 const waiting=deferred(),calls=[];const client=createMarketClient({now:()=>1000,randomId:()=>'stable-key',api:async(path,options)=>{calls.push(options);return path.endsWith('/quote')?quote():waiting.promise;}});
 client.attach(market(),'u1');await client.quote();const first=client.fill();await client.fill();assert.equal(calls.length,2);assert.deepEqual(calls[1].body,{quoteId:'q1',minClaimsAtomic:'98000000',idempotencyKey:'stable-key',expectedUserId:'u1'});waiting.resolve(receipt(quote()));await first;assert.equal(client.get().phase,'filled');
});
test('uncertain confirmation retries exact identity after leaving and returning',async()=>{
 const bodies=[];let calls=0;const client=createMarketClient({now:()=>1000,randomId:()=>'stable-key',api:async(path,{body})=>{if(path.endsWith('/quote'))return quote();bodies.push(body);if(!calls++)throw {status:0,code:'timeout'};return receipt(quote());}});
 client.attach(market(),'u1');await client.quote();await client.fill();assert.equal(client.get().phase,'uncertain');client.detach();client.attach(market(),'u2');assert.equal(client.get().phase,'idle');client.attach(market(),'u1');assert.equal(client.get().phase,'uncertain');client.input('NO','20');assert.equal(client.get().side,'YES');await client.fill();assert.deepEqual(bodies[0],bodies[1]);assert.equal(client.get().phase,'filled');
});
test('stale owner or route results cannot paint or accept a position',async()=>{
 const waiting=deferred();const client=createMarketClient({now:()=>1000,api:async()=>waiting.promise});client.attach(market(),'u1');const pending=client.quote();client.detach();client.attach(market(),'u2');waiting.resolve(quote());await pending;assert.equal(client.get().quote,null);assert.equal(client.get().userId,'u2');
});
test('input changes invalidate pending quotes and expired quotes require an explicit refresh',async()=>{
 let now=1000;const waiting=deferred();const client=createMarketClient({now:()=>now,api:async()=>waiting.promise});client.attach(market(),'u1');const pending=client.quote();client.input('NO','20');waiting.resolve(quote());await pending;assert.equal(client.get().quote,null);
 const second=createMarketClient({now:()=>now,api:async()=>quote()});second.attach(market(),'u1');await second.quote();now=3000;await second.fill();assert.equal(second.get().phase,'expired');assert.equal(second.get().error.code,'market_quote_expired');
});
test('disabled live mode never spends participation points',async()=>{
 let fills=0;const client=createMarketClient({now:()=>1000,api:async(path)=>{if(path.endsWith('/fill'))fills++;return quote({mode:'active'});}});client.attach({...market(),mode:'active'},'u1');await client.quote();await client.fill();assert.equal(fills,0);
});
test('a mismatched receipt remains uncertain and does not claim acceptance',async()=>{
 const client=createMarketClient({now:()=>1000,api:async(path)=>path.endsWith('/quote')?quote():receipt(quote({userId:'u2'}))});client.attach(market(),'u1');await client.quote();await client.fill();assert.equal(client.get().phase,'uncertain');assert.equal(client.get().receipt,null);
});
async function uncertainClient(lookup,onChange=()=>{}){
 const calls=[];
 const client=createMarketClient({now:()=>1000,randomId:()=>'request/key+one',onChange,api:async(path,options)=>{
  calls.push({path,options});
  if(path.endsWith('/quote'))return quote();
  if(path.endsWith('/fill'))throw {status:0,code:'timeout'};
  return lookup(path,options);
 }});
 client.attach(market(),'u1');await client.quote();await client.fill();
 client.get().market={...market(),probabilityStatus:'frozen_before_evidence',probabilityRevision:0};
 return {client,calls};
}
const reconciled=(status='accepted',value=receipt(quote()))=>({forecastId:'f1',userId:'u1',quoteId:'q1',status,receipt:value});
test('reconciliation sends only an authenticated GET with the exact original request identity',async()=>{
 const {client,calls}=await uncertainClient(async()=>reconciled());
 await client.reconcile();await client.reconcile();await client.fill();
 assert.equal(calls.length,3);
 assert.equal(calls[2].options.method,'GET');assert.equal(calls[2].options.body,undefined);
 const parsed=new URL(calls[2].path,'https://forecast.example');
 assert.equal(parsed.pathname,'/api/forecasts/f1/market/receipt');
 assert.equal(parsed.searchParams.get('quoteId'),'q1');
 assert.equal(parsed.searchParams.get('idempotencyKey'),'request/key+one');
 assert.equal(client.get().phase,'filled');assert.equal(client.get().receipt.id,'fill1');assert.equal(client.get().request,null);
 client.detach();client.attach(market(),'u1');assert.equal(client.get().phase,'idle');
});
test('a void receipt confirms only the original point principal return',async()=>{
 const original={...receipt(quote()),status:'void',refundedPoints:50,voidedAt:1002,eligibilityDecisionId:'decision-1'};
 const {client,calls}=await uncertainClient(async()=>reconciled('void',original));
 await client.reconcile();await client.fill();
 assert.equal(client.get().phase,'void');assert.equal(client.get().receipt.refundedPoints,50);assert.equal(client.get().request,null);
 assert.equal(calls.filter(call=>call.options.method==='POST'&&call.path.endsWith('/fill')).length,1);
});
test('a definitive not_accepted response clears only that unresolved request',async()=>{
 const {client}=await uncertainClient(async()=>reconciled('not_accepted',null));
 await client.reconcile();assert.equal(client.get().phase,'not_accepted');assert.equal(client.get().quote,null);assert.equal(client.get().receipt,null);assert.equal(client.get().request,null);
 client.detach();client.attach(market(),'u1');assert.equal(client.get().phase,'idle');
});
test('pending, missing responses and outages preserve uncertainty without a busy retry loop',async()=>{
 for(const lookup of [async()=>reconciled('pending',null),async()=>{throw {status:404,code:'not_found'};},async()=>{throw {status:503,code:'unavailable'};}]){
  const {client,calls}=await uncertainClient(lookup);
  await client.reconcile();assert.equal(client.get().phase,'uncertain');assert.ok(client.get().request);assert.equal(calls.length,3);
  client.detach();client.attach(market(),'u1');assert.equal(client.get().phase,'uncertain');
 }
});
test('duplicate reconciliation calls share one in-flight lookup and never race a write',async()=>{
 const waiting=deferred();const {client,calls}=await uncertainClient(async()=>waiting.promise);
 const first=client.reconcile();await client.reconcile();client.get().market=market();await client.fill();
 assert.equal(calls.length,3);assert.equal(client.get().reconciling,true);
 waiting.resolve(reconciled('pending',null));await first;
 assert.equal(client.get().reconciling,false);assert.equal(client.get().phase,'uncertain');
});
test('a stale GET after account or route changes cannot clear or display the old private request',async()=>{
 for(const change of ['account','route']){
  const waiting=deferred(),paints=[];const {client}=await uncertainClient(async()=>waiting.promise,entry=>paints.push(entry?.userId));
  const lookup=client.reconcile();client.detach();client.attach(change==='account'?market():{...market(),forecastId:'f2'},change==='account'?'u2':'u1');
  const before=paints.length;waiting.resolve(reconciled());await lookup;
  assert.equal(paints.length,before);assert.equal(client.get().phase,'idle');assert.equal(client.get().receipt,null);
  client.detach();client.attach(market(),'u1');assert.equal(client.get().phase,'uncertain');assert.equal(client.get().reconciling,false);
 }
});
test('forged status envelopes and receipts cannot settle an unresolved request',async()=>{
 const good=reconciled();
 const changes=[{forecastId:'f2'},{userId:'u2'},{quoteId:'q2'},{status:'invented'},{status:'pending'},
  ...['forecastId','userId','quoteId','mode','side','spendPoints','claimsAtomic','specificationHash','policyHash','revision','priceBeforeBps','priceAfterBps','expiresAt'].map(field=>({receipt:{...good.receipt,[field]:'forged'}})),
  {receipt:{...good.receipt,status:'void'}},{receipt:{...good.receipt,id:''}},{receipt:{...good.receipt,acceptedAt:null}},
  {status:'void',receipt:{...good.receipt,status:'void',refundedPoints:51,voidedAt:1002,eligibilityDecisionId:'d1'}},
  {status:'void',receipt:{...good.receipt,status:'void',refundedPoints:50,voidedAt:1000,eligibilityDecisionId:'d1'}},
 ];
 for(const change of changes){
  const {client}=await uncertainClient(async()=>({...good,...change}));await client.reconcile();
  assert.equal(client.get().phase,'uncertain');assert.equal(client.get().receipt,null);assert.ok(client.get().request);assert.equal(client.get().error.code,'market_response_invalid');
 }
});
test('final outcomes do not retain an early-review pending notice',async()=>{
 const {readFileSync}=await import('node:fs');
 const source=readFileSync(new URL('../public/app.js',import.meta.url),'utf8');
 const start=source.indexOf('function earlyResolutionMarkup('),end=source.indexOf('\nfunction marketMarkup',start);
 const render=new Function(`${source.slice(start,end)};return earlyResolutionMarkup;`)();
 for(const finalizedOutcome of ['YES','NO','INVALID'])assert.equal(render({earlyResolution:{triggerHash:'retained'},finalizedOutcome}), '');
 assert.equal(render({earlyResolution:null}), '');
});
