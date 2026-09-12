import test from 'node:test';
import assert from 'node:assert/strict';
import {webcrypto} from 'node:crypto';
import {readFileSync} from 'node:fs';
import {canonicalTranslationJson,verifyTranslationResponse,createForecastTranslations} from '../public/forecast-translation.mjs';
import {uiMessages} from '../public/locales/ui.mjs';
if(!globalThis.crypto)globalThis.crypto=webcrypto;
const clone=value=>JSON.parse(JSON.stringify(value));
const hash=async(prefix,value)=>Buffer.from(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(prefix+value))).toString('hex');
const sourcePrefix='forecast-network:sha256:display-source:v1\n';
const prefix='forecast-network:sha256:display-translation:v1\n';
function forecast(){return {id:'f_test',specificationHash:'a'.repeat(64),title:'A launch in 2028?',question:'Will the launch happen before 2028-01-01 00:00 UTC?',openAt:1000,closeAt:2000,ai:{rationale:'An estimate, not a resolved result.',probability:62},specification:{rules:[{clauseId:'yes-1',outcome:'YES',condition:'Official confirmation before the deadline.'},{clauseId:'no-1',outcome:'NO',condition:'No official confirmation by the deadline.'}],invalidationRules:['The official source is unavailable.']}};}
async function response(language='ko',status='ready'){
  const f=forecast();
  const source={schemaVersion:1,forecastId:f.id,specificationHash:f.specificationHash,language:'en',title:f.title,question:f.question,rules:clone(f.specification.rules),invalidationRules:clone(f.specification.invalidationRules),aiRationale:f.ai.rationale,openAt:f.openAt,closeAt:f.closeAt};
  const sourceHash=await hash(sourcePrefix,canonicalTranslationJson(source));
  const data={forecastId:f.id,specificationHash:f.specificationHash,sourceHash,language,sourceLanguage:'en',title:'2028년 출시?',question:'2028-01-01 00:00 UTC 전에 출시될까요?',rules:source.rules.map(rule=>({...rule,condition:`번역: ${rule.condition}`})),invalidationRules:['공식 출처를 이용할 수 없습니다.'],aiRationale:'확정 결과가 아닌 추정치입니다.',attribution:'AI translation',translatedAt:1234};
  const canonicalJson=canonicalTranslationJson(data);
  return {status,source,sourceHash,translation:status==='missing'?null:{...data,translationHash:await hash(prefix,canonicalJson),canonicalJson,commitmentProfile:{algorithm:'SHA-256',prefix}}};
}
async function changeEnvelope(value,change){const parsed=JSON.parse(value.translation.canonicalJson);change(parsed);value.translation.canonicalJson=canonicalTranslationJson(parsed);value.translation.translationHash=await hash(prefix,value.translation.canonicalJson);return value;}
function deferred(){let resolve;const promise=new Promise(done=>{resolve=done;});return {promise,resolve};}

test('verified envelope localizes all display fields without mutating forecast or trusting outer fields',async()=>{
  const input=forecast(),before=clone(input),data=await response();data.translation.question='unverified outer text';
  const result=await verifyTranslationResponse(data,input,'ko');
  assert.equal(result.translation.question,'2028-01-01 00:00 UTC 전에 출시될까요?');
  assert.equal(result.translation.rules.length,2);assert.equal(result.translation.invalidationRules.length,1);
  assert.deepEqual(input,before);
});
test('sorted UTF-8 canonical source digest accepts Korean, Japanese, and Traditional Chinese targets',async()=>{
  for(const language of ['ko','ja','zh-Hant'])assert.equal((await verifyTranslationResponse(await response(language),forecast(),language)).translation.language,language);
});
test('feed projection may omit rules but still binds visible English identity',async()=>{
  const input=forecast();delete input.specification;
  assert.equal((await verifyTranslationResponse(await response(),input,'ko')).translation.forecastId,input.id);
});
for(const field of ['id','specificationHash','title','question'])test(`rejects translation for a different visible ${field}`,async()=>{
  const input=forecast();input[field]='different';await assert.rejects(verifyTranslationResponse(await response(),input,'ko'));
});
test('rejects changed editorial rationale and detail criteria even with the same specification hash',async()=>{
  for(const edit of [input=>input.ai.rationale='Changed estimate',input=>input.specification.rules[0].condition='Changed rule',input=>input.specification.invalidationRules.push('Extra')]){const input=forecast();edit(input);await assert.rejects(verifyTranslationResponse(await response(),input,'ko'));}
});
test('rejects corrupted source, source digest, or translation digest',async()=>{
  for(const edit of [data=>data.source.title='tampered',data=>data.sourceHash='b'.repeat(64),data=>data.translation.translationHash='c'.repeat(64)]){const data=await response();edit(data);await assert.rejects(verifyTranslationResponse(data,forecast(),'ko'));}
});
test('rejects wrong language/source binding and moved or replaced rule identities despite a matching translation hash',async()=>{
  for(const edit of [data=>data.language='ja',data=>data.sourceHash='d'.repeat(64),data=>data.rules.reverse(),data=>data.rules[0].outcome='NO',data=>data.rules[0].clauseId='changed',data=>data.invalidationRules=[],data=>data.aiRationale=null])await assert.rejects(verifyTranslationResponse(await changeEnvelope(await response(),edit),forecast(),'ko'));
});
test('rejects noncanonical JSON even when its own digest matches',async()=>{
  const data=await response();data.translation.canonicalJson=JSON.stringify(JSON.parse(data.translation.canonicalJson),null,2);data.translation.translationHash=await hash(prefix,data.translation.canonicalJson);
  await assert.rejects(verifyTranslationResponse(data,forecast(),'ko'));
});
test('valid missing-cache response carries a verified source and no invented translation',async()=>{
  const data=await response('ko','missing');assert.equal((await verifyTranslationResponse(data,forecast(),'ko')).translation,null);
  data.translation={};await assert.rejects(verifyTranslationResponse(data,forecast(),'ko'));
});
test('cache is checked before generation and POST sends only the hash-bound translation request',async()=>{
  const calls=[],missing=await response('ko','missing'),ready=await response();
  const controller=createForecastTranslations({api:async(path,options)=>{calls.push({path,options});return options?ready:missing;}});controller.reset('ko');controller.register(forecast());
  await controller.translate('f_test');
  assert.equal(calls.length,2);assert.match(calls[0].path,/translation\?language=ko$/);
  assert.deepEqual(calls[1].options,{method:'POST',timeout:130000,body:{language:'ko',specificationHash:'a'.repeat(64),sourceHash:missing.sourceHash}});
  assert.equal(controller.get('f_test').status,'translated');
  controller.original('f_test');assert.equal(controller.get('f_test').translation,null);
  await controller.translate('f_test');assert.equal(calls.length,2);assert.equal(controller.get('f_test').status,'translated');
});
test('English UI does not translate an English source or offer extra language choices',async()=>{
  const calls=[],controller=createForecastTranslations({api:async(path)=>{calls.push(path);return response('ja');}});controller.reset('en');controller.register(forecast());
  await controller.translate('f_test');assert.equal(controller.get('f_test').choices,false);assert.equal(calls.length,0);
  await controller.translate('f_test','ja');assert.equal(controller.get('f_test').translation,null);assert.equal(calls.length,0);
});
test('double click shares pending work without generating twice',async()=>{
  const waiting=deferred(),ready=await response();let count=0;
  const controller=createForecastTranslations({api:async()=>{count++;return waiting.promise;}});controller.reset('ko');controller.register(forecast());
  const pending=controller.translate('f_test');await controller.translate('f_test');assert.equal(count,1);assert.equal(controller.get('f_test').status,'pending');
  waiting.resolve(ready);await pending;assert.equal(controller.get('f_test').status,'translated');
});
for(const cancellation of ['locale','route','original','identity'])test(`late cache response cannot generate or display after ${cancellation} changes`,async()=>{
  const waiting=deferred(),missing=await response('ko','missing');let calls=0;
  const controller=createForecastTranslations({api:async()=>{calls++;return waiting.promise;}});controller.reset('ko');controller.register(forecast());const pending=controller.translate('f_test');
  if(cancellation==='original')controller.original('f_test');
  else if(cancellation==='identity'){const input=forecast();input.title='A newer title';controller.register(input);}
  else {controller.reset(cancellation==='locale'?'ja':'ko');controller.register(forecast());}
  waiting.resolve(missing);await pending;assert.equal(calls,1);assert.equal(controller.get('f_test').status,'original');assert.equal(controller.get('f_test').translation,null);
});
test('late generation response cannot overwrite a new route or locale',async()=>{
  const waiting=deferred(),missing=await response('ko','missing'),ready=await response();let generated;
  const started=new Promise(resolve=>{generated=resolve;});
  const controller=createForecastTranslations({api:async(path,options)=>{if(!options)return missing;generated();return waiting.promise;}});controller.reset('ko');controller.register(forecast());const pending=controller.translate('f_test');await started;
  controller.reset('ja');controller.register(forecast());waiting.resolve(ready);await pending;
  assert.equal(controller.get('f_test').status,'original');assert.equal(controller.get('f_test').translation,null);
});
test('a source changing between cache lookup and generation cannot be displayed',async()=>{
  const missing=await response('ko','missing'),ready=await response();ready.sourceHash='f'.repeat(64);
  const controller=createForecastTranslations({api:async(path,options)=>options?ready:missing});controller.reset('ko');controller.register(forecast());await controller.translate('f_test');
  assert.equal(controller.get('f_test').status,'error');assert.equal(controller.get('f_test').translation,null);
});
for(const [failure,expected] of [[{code:'translation_in_progress',status:409},'busy'],[{status:429},'limited'],[{status:503},'failed']])test(`${expected} response keeps original available and allows explicit retry`,async()=>{
  let calls=0;const ready=await response();const controller=createForecastTranslations({api:async()=>{if(calls++===0)throw failure;return ready;}});controller.reset('ko');controller.register(forecast());await controller.translate('f_test');
  assert.equal(controller.get('f_test').status,'error');assert.equal(controller.get('f_test').error,expected);assert.equal(controller.get('f_test').translation,null);
  await controller.translate('f_test');assert.equal(controller.get('f_test').status,'translated');
});
test('all translation affordances have complete four-language messages',()=>{
  const keys=['translationTranslate','translationOriginal','translationRetry','translationPending','translationChoose','translationAi','translationAuthority','translationFailed','translationBusy','translationLimited'];
  for(const language of ['en','ko','ja','zh-Hant'])for(const key of keys)assert.ok(uiMessages[language][`ui.${key}`]);
});
test('translation repaint updates only display text and its controls, preserving actual input nodes',()=>{
  const source=readFileSync(new URL('../public/app.js',import.meta.url),'utf8');
  const start=source.indexOf('function paintForecastTranslations()'),end=source.indexOf('\nfunction forecastCard(',start);
  const implementation=source.slice(start,end);
  const input={value:'125',focused:true},comment={value:'A typed comment'};
  const nodes=[{dataset:{translationForecast:'f_test',translationField:'question'},textContent:forecast().question,lang:'en'}, {dataset:{translationForecast:'f_test',translationField:'rule:0'},textContent:forecast().specification.rules[0].condition,lang:'en'}];
  const document={querySelectorAll(selector){if(selector==='[data-translation-forecast]')return nodes;if(selector==='[data-translation-control]')return [];throw new Error(`Unexpected DOM selection: ${selector}`);}};
  const entry={forecast:forecast(),status:'translated',translation:{language:'ja',question:'翻訳された質問',rules:[{condition:'翻訳された条件'}]}};
  const paint=new Function('document','forecastTranslations',`${implementation};return paintForecastTranslations;`)(document,{get:()=>entry});
  paint();assert.equal(nodes[0].textContent,'翻訳された質問');assert.equal(nodes[1].textContent,'翻訳された条件');assert.equal(nodes[0].lang,'ja');
  entry.status='original';paint();assert.equal(nodes[0].textContent,forecast().question);assert.equal(nodes[0].lang,'en');assert.equal(input.value,'125');assert.ok(input.focused);assert.equal(comment.value,'A typed comment');
});
