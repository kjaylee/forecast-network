import test from 'node:test';
import assert from 'node:assert/strict';
import {escapeHtml,safeExternalUrl,probability,percentage,forecastIsOpen,canChallenge,makeHistoryPath,createApi,formatEpochDate,displayForecast} from '../public/lib.mjs';

test('untrusted content is encoded across text and attribute contexts',()=>{
  assert.equal(escapeHtml(`<img src=x onerror="alert('x')">&`),'&lt;img src=x onerror=&quot;alert(&#39;x&#39;)&quot;&gt;&amp;');
  assert.equal(escapeHtml(null),'');
});

test('external links reject executable and credential-bearing addresses',()=>{
  for(const value of ['javascript:alert(1)','data:text/html,test','//evil.example','https://user:secret@example.org','invalid'])assert.equal(safeExternalUrl(value),null);
  assert.equal(safeExternalUrl('https://example.org/source?a=1&b=2'),'https://example.org/source?a=1&b=2');
});

test('missing, invalid and zero probabilities remain distinct',()=>{
  for(const value of [null,undefined,'50',NaN,Infinity,-1,101])assert.equal(probability(value),null);
  assert.equal(percentage(null),'—');
  assert.equal(percentage(0),'0%');
  assert.equal(percentage(100),'100%');
  assert.equal(percentage(64.8),'65%');
});

test('full timestamps display the timezone and preserve UTC-to-Korea day rollover',()=>{
  const timestamp=Date.parse('2026-09-09T18:00:00Z');
  const utc=formatEpochDate(timestamp,{full:true,timeZone:'UTC'});
  const korea=formatEpochDate(timestamp,{full:true,timeZone:'Asia/Seoul'});
  assert.match(utc,/Sep 9/);
  assert.match(utc,/06:00/);
  assert.match(utc,/UTC/);
  assert.match(korea,/Sep 10/);
  assert.match(korea,/03:00/);
  assert.match(korea,/GMT\+9/);
  assert.equal(formatEpochDate(null,{full:true}),'Pending');
  assert.equal(formatEpochDate(NaN,{full:true}),'Pending');
});

test('English display translations are hash-bound and preserve canonical rules and timing',()=>{
  const forecast={id:'a',specificationHash:'abc',title:'원래 제목',question:'원래 질문',closeAt:123,
    ai:{probability:75,rationale:'원래 근거'},
    specification:{canonicalQuestion:'원래 질문',closeAt:123,rules:[{clauseId:'yes-1',outcome:'YES',condition:'원래 조건'}],invalidationRules:['원래 무효 조건']}};
  const original=structuredClone(forecast);
  const translation={language:'en',sourceLanguage:'ko',specificationHash:'abc',title:'Translated title',question:'Translated question',rules:[{clauseId:'yes-1',condition:'Translated condition'}],invalidationRules:['Translated invalidation'],aiRationale:'Translated reasoning'};
  const display=displayForecast(forecast,translation);
  assert.equal(display.title,'Translated title');
  assert.equal(display.specification.rules[0].condition,'Translated condition');
  assert.equal(display.specification.rules[0].outcome,'YES');
  assert.equal(display.specification.closeAt,123);
  assert.equal(display.specificationHash,'abc');
  assert.equal(display.ai.probability,75);
  assert.equal(display.ai.rationale,'Translated reasoning');
  assert.equal(display.translationLanguage,'en');
  assert.deepEqual(forecast,original,'canonical input must stay unchanged');
  assert.equal(displayForecast(forecast,{...translation,specificationHash:'wrong'}),forecast);
  assert.equal(displayForecast(forecast,{...translation,language:'fr'}),forecast);
});

test('participation checks honor both state and exact deadline',()=>{
  const forecast={state:'OPEN',openAt:100,closeAt:200};
  assert.equal(forecastIsOpen(forecast,99),false);
  assert.equal(forecastIsOpen(forecast,100),true);
  assert.equal(forecastIsOpen(forecast,199),true);
  assert.equal(forecastIsOpen(forecast,200),false);
  assert.equal(forecastIsOpen({...forecast,state:'PAUSED'},150),false);
  assert.equal(forecastIsOpen({...forecast,closeAt:null},150),false);
});

test('dispute controls close at deadline even before a server sweep',()=>{
  assert.equal(canChallenge({state:'CHALLENGE',challengeUntil:200},199),true);
  assert.equal(canChallenge({state:'DISPUTED',challengeUntil:200},199),true);
  assert.equal(canChallenge({state:'CHALLENGE',challengeUntil:200},200),false);
  assert.equal(canChallenge({state:'FINALIZED',challengeUntil:200},199),false);
});

test('chart plots actual observations, preserves zero, and handles one observation',()=>{
  assert.equal(makeHistoryPath([{at:1,probability:null}]),null);
  const one=makeHistoryPath([{at:1,probability:0}]);
  assert.equal(one.path,'M250.00,150.00');
  const history=[{at:30,probability:100},{at:10,probability:0},{at:20,probability:null}];
  assert.equal(makeHistoryPath(history).path,'M20.00,150.00 L480.00,20.00');
  assert.equal(history[0].at,30,'caller history is unchanged');
});

test('uncertain write retries reuse identity and successful later actions receive new keys',async()=>{
  const calls=[];
  const client=createApi(async(path,request)=>{
    calls.push({path,request});
    if(calls.length===1)throw new TypeError('connection closed');
    return Response.json({data:{ok:true}});
  });
  const options={method:'POST',body:{outcome:'YES',confidence:70,revision:2},idempotent:true};
  await assert.rejects(client('/api/forecasts/a/forecast',options),{code:'network_error'});
  await client('/api/forecasts/a/forecast',options);
  await client('/api/forecasts/a/forecast',options);
  assert.equal(calls[0].request.headers['Idempotency-Key'],calls[1].request.headers['Idempotency-Key']);
  assert.notEqual(calls[1].request.headers['Idempotency-Key'],calls[2].request.headers['Idempotency-Key']);
  assert.equal(calls[0].request.credentials,'same-origin');
  assert.equal(calls[0].request.headers['X-Forecast-Client'],'web');
  assert.equal(JSON.parse(calls[0].request.body).idempotencyKey,calls[0].request.headers['Idempotency-Key']);
  assert.equal(options.body.idempotencyKey,undefined,'caller payload is not mutated');
});

test('changed revision has a distinct retry identity and stale server state is surfaced',async()=>{
  const keys=[];
  const client=createApi(async(path,request)=>{
    keys.push(request.headers['Idempotency-Key']);
    return keys.length===1?Response.json({error:{code:'stale_revision',message:'refresh'}},{status:409}):Response.json({data:{ok:true}});
  });
  await assert.rejects(client('/api/forecasts/a/forecast',{method:'POST',body:{revision:2},idempotent:true}),{status:409,code:'stale_revision'});
  await client('/api/forecasts/a/forecast',{method:'POST',body:{revision:3},idempotent:true});
  assert.notEqual(keys[0],keys[1]);
});

test('requests reject cross-origin destinations and invalid JSON replies',async()=>{
  const client=createApi(async()=>new Response('<html>unavailable</html>',{status:502}));
  await assert.rejects(client('https://example.org/api/me'),/same-origin/);
  await assert.rejects(client('//example.org/api/me'),/same-origin/);
  await assert.rejects(client('/api/me'),{status:502,code:'invalid_response'});
});

test('timeout aborts the request and retains the original write key',async()=>{
  const keys=[];
  const client=createApi(async(path,request)=>{
    keys.push(request.headers['Idempotency-Key']);
    if(keys.length>1)return Response.json({data:{ok:true}});
    return new Promise((resolve,reject)=>request.signal.addEventListener('abort',()=>reject(new DOMException('Aborted','AbortError'))));
  });
  const options={method:'POST',body:{draftId:'draft-a'},idempotent:true,timeout:5};
  await assert.rejects(client('/api/forecasts',options),{code:'timeout'});
  await client('/api/forecasts',options);
  assert.equal(keys[0],keys[1]);
});
