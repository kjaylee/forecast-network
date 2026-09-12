import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {escapeHtml as esc,safeExternalUrl,displayForecast} from '../public/lib.mjs';
import {t as translate,formatNumber} from '../public/i18n.mjs';

const source=readFileSync(new URL('../public/app.js',import.meta.url),'utf8');
const start=source.indexOf('function eligibilityPersonalText(');
const end=source.indexOf('\nfunction marketNumber(',start);
assert.ok(start>=0&&end>start);
const make=new Function('t','esc','safeExternalUrl','date','pointCount','externalLink','displayForecast','stateLabel','formatNumber',`${source.slice(start,end)};return {eligibilityMarkup,myForecastMarkup};`);
const cutoff=Date.UTC(2026,8,10,12);
const base={status:'complete',cutoffAt:cutoff,timeBasis:'published_instant',publishedEvidenceUrl:'https://example.org/evidence?a=1&b=2',policyVersion:'evidence-cutoff-v1',personal:null};
function renderers(locale='en'){
  const t=(key,params)=>translate(key,params,locale);
  const link=(url,label)=>`<a href="${esc(safeExternalUrl(url))}" target="_blank" rel="noopener noreferrer">${esc(label)}</a>`;
  return {...make(t,esc,safeExternalUrl,value=>new Date(value).toISOString(),value=>formatNumber(value,{},locale),link,displayForecast,value=>value,value=>formatNumber(value,{},locale)),t};
}

for(const locale of ['en','ko','ja','zh-Hant']){
  test(`${locale}: evidence cutoff and personal outcomes use complete localized copy`,()=>{
    const {eligibilityMarkup:render,t}=renderers(locale);
    const html=render(base);
    assert.ok(html.includes(esc(t('ui.eligibilityCutoff',{date:new Date(cutoff).toISOString()}))));
    assert.ok(html.includes(esc(t('ui.eligibilityPolicy'))));
    assert.match(html,/rel="noopener noreferrer"/);
    assert.match(html,/a=1&amp;b=2/);
    for(const [status,key] of Object.entries({eligible:'eligibilityEligible',void:'eligibilityVoid',review:'eligibilityPersonalReview',restored:'eligibilityRestored'})){
      const result=render({...base,personal:{status,refundedPoints:0,adjustmentPending:false}});
      assert.ok(result.includes(esc(t(`ui.${key}`))),status);
      assert.doesNotMatch(result,/ui\.eligibility|undefined|NaN/);
    }
    assert.notEqual(t('ui.evidenceRefund'),'ui.evidenceRefund');
    assert.notEqual(t('ui.evidenceRestore'),'ui.evidenceRestore');
  });
}

test('older detail responses and absent personal participation do not invent a void receipt',()=>{
  const {eligibilityMarkup:render,t}=renderers();
  for(const empty of [undefined,null,{status:'none'},{status:'unrecognized'}])assert.equal(render(empty),'');
  for(const personal of [null,{status:'none'}]){
    const html=render({...base,personal});
    assert.ok(html.includes(t('ui.eligibilityTitle')));
    assert.ok(!html.includes(t('ui.eligibilityVoid')));
  }
  assert.match(source,/eligibilityMarkup\(data\.eligibility\)/);
});

test('observation time never becomes a fabricated publication cutoff',()=>{
  const {eligibilityMarkup:render,t}=renderers();
  for(const caseData of [{timeBasis:'observed_upper_bound'},{cutoffAt:null},{cutoffAt:'2026-09-10'},{cutoffAt:NaN}]){
    const html=render({...base,status:'review',...caseData,personal:{status:'review'}});
    assert.ok(html.includes(esc(t('ui.eligibilityUnknownTime'))));
    assert.ok(html.includes(esc(t('ui.eligibilityPersonalReview'))));
    assert.ok(!html.includes(new Date(cutoff).toISOString()));
  }
});

test('pending correction cannot be presented as a completed refund',()=>{
  const {eligibilityMarkup:render,t}=renderers();
  const personal={status:'void',refundedPoints:100,adjustmentPending:true};
  const pending=render({...base,status:'pending',personal});
  assert.ok(pending.includes(esc(t('ui.eligibilityPending'))));
  assert.ok(pending.includes(esc(t('ui.eligibilityAdjustmentPending'))));
  assert.ok(!pending.includes(t('ui.eligibilityRefunded',{amount:'100'})));
  const complete=render({...base,personal:{...personal,adjustmentPending:false}});
  assert.ok(complete.includes(esc(t('ui.eligibilityRefunded',{amount:'100'}))));
  assert.ok(!complete.includes(t('ui.eligibilityAdjustmentPending')));
  for(const refundedPoints of [0,-1,1.5,'100',Infinity])assert.ok(!render({...base,personal:{status:'void',refundedPoints}}).includes('committed points returned'));
});

test('unsafe evidence URLs are omitted and hostile values never become HTML',()=>{
  const {eligibilityMarkup:render,myForecastMarkup}=renderers();
  for(const publishedEvidenceUrl of ['javascript:alert(1)','data:text/html,<script>alert(1)</script>','https://user:password@example.org/']){
    assert.doesNotMatch(render({...base,publishedEvidenceUrl}),/href=/);
  }
  const poisoned=render({...base,cutoffAt:'<script>alert(1)</script>',personal:{status:'<script>',refundedPoints:'<img src=x onerror=alert(1)>'}});
  assert.doesNotMatch(poisoned,/<script|<img/);
  const row=myForecastMarkup({id:'"><svg/onload=alert(1)>',title:'<img src=x>',state:'<script>',eligibility:base});
  assert.doesNotMatch(row,/<script|<img|<svg/);
  assert.match(row,/&lt;img/);
});

test('profile preserves void/review history without presenting an excluded choice as eligible',()=>{
  const {myForecastMarkup:render,t}=renderers();
  const item={id:'f_1',title:'A future event?',state:'CHALLENGE',myForecast:{outcome:'NO',confidence:90,submittedAt:cutoff+1},stake:{amount:100,status:'settled',returned:200}};
  for(const status of ['void','review']){
    const html=render({...item,eligibility:{...base,personal:{status,refundedPoints:100}}});
    assert.match(html,/href="\/forecasts\/f_1"/);
    assert.match(html,/A future event\?/);
    assert.ok(html.includes(esc(t(status==='void'?'ui.eligibilityVoid':'ui.eligibilityPersonalReview'))));
    assert.match(html,/<span class="mine-choice"><\/span>/);
    assert.doesNotMatch(html,/200|90/);
  }
  assert.match(source,/\(data\.myForecasts\|\|\[\]\)\.map\(myForecastMarkup\)/);
});

test('restored profile choice uses the earlier authoritative receipt and shows correction state',()=>{
  const {myForecastMarkup:render,t}=renderers();
  const html=render({id:'f_1',title:'A future event?',state:'CHALLENGE',myForecast:{outcome:'YES',confidence:60,submittedAt:cutoff-1},stake:{amount:50,status:'committed'},eligibility:{...base,personal:{status:'restored',effectiveRevision:1,voidedRevisions:[2,3],refundedPoints:50,adjustmentPending:true}}});
  assert.match(html,/<span class="mine-choice">YES/);
  assert.ok(html.includes(esc(t('ui.eligibilityRestored'))));
  assert.ok(html.includes(esc(t('ui.eligibilityAdjustmentPending'))));
  assert.ok(html.includes(esc(t('ui.pointsValue',{amount:'50'}))));
  assert.ok(!html.includes(t('ui.eligibilityRefunded',{amount:'50'})));
});

test('point ledger has distinct labels for refund and reinstated commitment',()=>{
  assert.match(source,/evidence_refund:t\('ui\.evidenceRefund'\)/);
  assert.match(source,/evidence_restore:t\('ui\.evidenceRestore'\)/);
  assert.match(source,/market_void_refund:t\('ui\.marketVoidRefund'\)/);
});

test('a frozen market hides stale quotes and fixed-return receipts while preserving confirmed refund history',()=>{
  const begin=source.indexOf('function marketPositionMarkup('),end=source.indexOf('\nfunction paintMarket(',begin);
  const t=(key,params)=>translate(key,params,'en');
  const make=new Function('t','esc','formatNumber','marketNumber','claimsInPoints','pointCount',`${source.slice(begin,end)};return marketPositionMarkup;`);
  const render=make(t,esc,String,String,value=>Number(BigInt(value))/1000000,String);
  const html=render({market:{mode:'active'},position:{yesClaimsAtomic:'0',noClaimsAtomic:'0',voidedFillCount:2,refundedPoints:100}});
  assert.ok(html.includes(t('ui.marketVoidRefund')));
  assert.ok(html.includes(t('ui.eligibilityRefunded',{amount:'100'})));
  assert.ok(!html.includes(t('ui.marketHeldReturns')));
  const untouched=render({market:{mode:'active'},position:{yesClaimsAtomic:'120000000',noClaimsAtomic:'0',voidedFillCount:0,refundedPoints:0}});
  assert.ok(untouched.includes('YES: 120'));
  assert.ok(!untouched.includes(t('ui.marketVoidRefund')));
  assert.match(source,/const quantity=evidenceReview\|\|phase==='void'\?null:receipt\|\|quote/);
  assert.match(source,/!evidenceReview&&receipt\?\.status==='accepted'\?/);
  assert.match(source,/!evidenceReview&&quote&&phase!=='uncertain'&&phase!=='void'\?/);
});

test('receipt lookup states distinguish pending, void and not accepted without offering a second write',()=>{
  const start=source.indexOf('function marketReconciliationMarkup('),end=source.indexOf('\nfunction marketMarkup(',start);
  const make=new Function('t','esc','pointCount','button',`${source.slice(start,end)};return marketReconciliationMarkup;`);
  for(const locale of ['en','ko','ja','zh-Hant']){
    const t=(key,params)=>translate(key,params,locale);
    const render=make(t,esc,String,(label,action,_class,extra)=>`<button data-action="${action}" ${extra}>${label}</button>`);
    const pending=render({phase:'uncertain',reconciling:true});
    assert.ok(pending.includes(esc(t('ui.marketCheckingReceipt'))));
    assert.match(pending,/data-action="market-reconcile" disabled/);
    assert.doesNotMatch(pending,/market-fill/);
    const uncertain=render({phase:'uncertain',reconciling:false});
    assert.ok(uncertain.includes(esc(t('ui.marketUncertain'))));
    assert.doesNotMatch(uncertain,/disabled/);
    const voided=render({phase:'void',receipt:{refundedPoints:50}});
    assert.ok(voided.includes(esc(t('ui.marketVoidRefund'))));
    assert.ok(voided.includes(esc(t('ui.eligibilityRefunded',{amount:'50'}))));
    assert.doesNotMatch(voided,/<button/);
    assert.ok(render({phase:'not_accepted'}).includes(esc(t('ui.marketNotAccepted'))));
    assert.equal(render({phase:'idle'}),'');
  }
  assert.match(source,/if\(entry\?\.phase==='uncertain'\)await marketClient\.reconcile\(\)/);
  assert.match(source,/action==='market-reconcile'\)\{await marketClient\.reconcile\(\)/);
});

test('an unknown market price is not displayed as a zero percent prediction',()=>{
  const start=source.indexOf('function marketPriceText('),end=source.indexOf('\nfunction marketMarkup(',start);
  const make=new Function('t','marketNumber',`${source.slice(start,end)};return {marketPriceText,marketPriceHint};`);
  for(const locale of ['en','ko','ja','zh-Hant']){
    const t=(key)=>translate(key,{},locale);
    const {marketPriceText,marketPriceHint}=make(t,value=>String(value));
    assert.equal(marketPriceText({yesProbabilityBps:0}),'YES 0%');
    assert.equal(marketPriceText({yesProbabilityBps:5400}),'YES 54%');
    for(const yesProbabilityBps of [null,undefined,NaN,-1,10001,'5000'])assert.equal(marketPriceText({yesProbabilityBps}),'YES —');
    assert.equal(marketPriceHint({probabilityStatus:'eligibility_review'}),t('ui.marketEvidenceReview'));
    assert.equal(marketPriceHint({probabilityStatus:'frozen_before_evidence'}),t('ui.marketEvidenceFrozen'));
    assert.equal(marketPriceHint({}),t('ui.marketPriceHint'));
  }
  assert.match(source,/price\.textContent=marketPriceText\(fresh\)/);
  assert.match(source,/\['eligibility_review','frozen_before_evidence'\]\.includes\(market\.probabilityStatus\)/);
});
