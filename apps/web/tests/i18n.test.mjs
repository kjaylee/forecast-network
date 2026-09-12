import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {execFileSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';
import {coreMessages} from '../public/locales/core.mjs';
import {uiMessages} from '../public/locales/ui.mjs';
import {cardMessages} from '../public/locales/cards.mjs';
import {SUPPORTED_LOCALES,LOCALE_STORAGE_KEY,messages,t,getLocale,setLocale,initializeLocale,normalizeLocale,intlLocale,formatNumber,errorText,fieldValidationMessage} from '../public/i18n.mjs';
import {formatEpochDate,createApi,escapeHtml,forecastSubmission,displayForecast} from '../public/lib.mjs';
import {signOwnershipChallenge} from '../public/wallet.mjs';

const placeholders=value=>[...value.matchAll(/\{([A-Za-z][A-Za-z0-9_]*)\}/g)].map(match=>match[1]).sort();
test('native field constraints use the selected language and preserve numeric bounds',()=>{
  for(const {code} of SUPPORTED_LOCALES){
    assert.equal(fieldValidationMessage({validity:{valueMissing:true}},code),t('error.field_required',{},code));
    assert.equal(fieldValidationMessage({validity:{rangeOverflow:true},max:'1000'},code),t('error.field_max',{max:formatNumber(1000,{},code)},code));
    assert.equal(fieldValidationMessage({validity:{tooShort:true},minLength:32},code),t('error.field_short',{min:formatNumber(32,{},code)},code));
  }
});
test('all packs have identical nonempty keys and interpolation contracts',()=>{
  assert.deepEqual(SUPPORTED_LOCALES.map(item=>item.code),['en','ko','ja','zh-Hant']);
  const seen=new Set();
  for(const pack of [coreMessages,uiMessages,cardMessages]){
    const keys=Object.keys(pack.en).sort();assert.ok(keys.length>10);
    for(const key of keys){assert.ok(!seen.has(key),'duplicate namespace key: '+key);seen.add(key);}
    for(const {code} of SUPPORTED_LOCALES){
      assert.deepEqual(Object.keys(pack[code]).sort(),keys);
      for(const key of keys){
        assert.equal(typeof pack[code][key],'string');assert.ok(pack[code][key].trim(),code+': '+key);
        assert.deepEqual(placeholders(pack[code][key]),placeholders(pack.en[key]),code+': '+key);
        assert.doesNotMatch(pack[code][key],/<\/?[a-z][^>]*>/i,'catalogs are text, never executable markup');
      }
    }
  }
  assert.equal(Object.keys(messages.en).length,seen.size);
});

test('first visit stays English and supported regional aliases resolve safely',()=>{
  assert.equal(initializeLocale({storage:null,document:null}),'en');
  const cases={'ko-KR':'ko','ja_JP':'ja','zh-Hant':'zh-Hant','zh-Hant-HK':'zh-Hant','zh-TW':'zh-Hant','zh-HK':'zh-Hant','en-GB':'en','zh-CN':'en','zh-Hans':'en','fr':'en','<script>':'en'};
  for(const [value,expected] of Object.entries(cases))assert.equal(normalizeLocale(value),expected);
  assert.equal(normalizeLocale(null),'en');assert.equal(normalizeLocale({code:'ko'}),'en');
});

test('explicit preference survives navigation and unavailable storage does not break switching',()=>{
  const data=new Map();const storage={getItem:key=>data.get(key),setItem:(key,value)=>data.set(key,value)};
  const document={documentElement:{lang:'en',dir:'ltr'}};
  assert.equal(setLocale('zh-HK',{storage,document}),'zh-Hant');
  assert.equal(data.get(LOCALE_STORAGE_KEY),'zh-Hant');assert.equal(document.documentElement.lang,'zh-Hant');
  setLocale('en',{storage:null,document:null});assert.equal(initializeLocale({storage,document}),'zh-Hant');
  const blocked={getItem(){throw new Error('blocked');},setItem(){throw new Error('blocked');}};
  assert.doesNotThrow(()=>initializeLocale({storage:blocked,document}));assert.equal(getLocale(),'en');
  assert.equal(setLocale('ja',{storage:blocked,document}),'ja');assert.equal(document.documentElement.lang,'ja');
  assert.deepEqual([...data.keys()],[LOCALE_STORAGE_KEY],'only language preference is stored');
  setLocale('en',{storage:null,document:null});
});

test('localized interpolation is plaintext and cannot alter domain values',()=>{
  const payload={forecast:{revision:3},outcome:'NO',confidence:24,stakePoints:0,expectedUserId:'user-a'};
  const expected=forecastSubmission(payload);
  const canonical={id:'forecast-a',title:'Original question?',specificationHash:'same',closeAt:123};
  const serialized=JSON.stringify(canonical);
  for(const {code} of SUPPORTED_LOCALES){
    setLocale(code,{storage:null,document:null});assert.deepEqual(forecastSubmission(payload),expected);
    assert.equal(displayForecast(canonical),canonical);assert.equal(JSON.stringify(canonical),serialized);
    const text=t('error.stake_limit_amount',{limit:'<img onerror="boom">'},code);
    assert.ok(text.includes('<img'));assert.ok(!escapeHtml(text).includes('<img'));
  }
  setLocale('en',{storage:null,document:null});
});

test('date and number formatting follows the selected locale without changing instants',()=>{
  const timestamp=Date.parse('2026-09-09T18:00:00Z');
  for(const {code} of SUPPORTED_LOCALES){
    assert.equal(formatNumber(12345.6,{},code),new Intl.NumberFormat(intlLocale(code)).format(12345.6));
    const output=formatEpochDate(timestamp,{full:true,timeZone:'Asia/Seoul',locale:code});
    assert.equal(output,new Intl.DateTimeFormat(intlLocale(code),{month:'short',day:'numeric',year:'numeric',hour:'2-digit',minute:'2-digit',timeZoneName:'short',timeZone:'Asia/Seoul'}).format(timestamp));
    assert.equal(formatEpochDate(null,{locale:code}),t('common.pending',{},code));
  }
});

test('localized server errors retain status/code and never display unknown raw errors',async()=>{
  try{
    for(const {code} of SUPPORTED_LOCALES){
      setLocale(code,{storage:null,document:null});
      const api=createApi(async()=>Response.json({error:{code:'insufficient_points',message:'Not enough available points.'}},{status:409}));
      await assert.rejects(api('/api/points'),error=>error.status===409&&error.code==='insufficient_points'&&
        error.message===(code==='en'?'Not enough available points.':t('error.insufficient_points')));
      assert.equal(errorText({code:'future_error',message:'Raw internal text'}),t('error.request_failed'));
    }
  }finally{setLocale('en',{storage:null,document:null});}
});

test('wallet ownership message bytes are identical in every interface language',async()=>{
  const account={address:'public-address',chains:['solana:devnet'],features:['solana:signMessage']};
  const message='forecast.eastsea.xyz asks you to verify your wallet.\nChain: solana:devnet\nNonce: test-only';
  const challenge={address:account.address,chain:'solana:devnet',message,expiresAt:2000,challengeId:'challenge-1'};
  const observed=[];
  const wallet={name:'Test wallet',chains:['solana:devnet'],features:{'standard:connect':{connect:async()=>({accounts:[account]})},'solana:signMessage':{signMessage:async input=>{observed.push(input.message);return [{signedMessage:input.message,signature:new Uint8Array(64)}];}}}};
  try{
    for(const {code} of SUPPORTED_LOCALES){setLocale(code,{storage:null,document:null});const proof=await signOwnershipChallenge(wallet,account,challenge,{now:()=>1000});assert.equal(proof.challengeId,challenge.challengeId);}
    for(const bytes of observed)assert.deepEqual(bytes,new TextEncoder().encode(message));
    assert.equal(challenge.message,message);
  }finally{setLocale('en',{storage:null,document:null});}
});

test('all literal public server error codes have translations',()=>{
  const root=fileURLToPath(new URL('../../../',import.meta.url));
  const script=`import ast,json,pathlib\nroot=pathlib.Path(${JSON.stringify(root)})\ncodes=set()\nfor p in [*root.joinpath('packages/application/src').rglob('*.py'),root/'apps/web/src/entry.py']:\n tree=ast.parse(p.read_text())\n for n in ast.walk(tree):\n  if isinstance(n,ast.Call) and isinstance(n.func,ast.Name) and n.func.id=='AppError' and len(n.args)>1 and isinstance(n.args[1],ast.Constant): codes.add(n.args[1].value)\nprint(json.dumps(sorted(codes)))`;
  const codes=JSON.parse(execFileSync('python3',['-c',script],{encoding:'utf8',env:{...process.env,PYTHONDONTWRITEBYTECODE:'1',TMPDIR:root+'tmp'}}));
  for(const code of [...codes,'invalid_request','service_unavailable'])assert.ok(coreMessages.en['error.'+code],code);
});

test('all literal translation references exist in the complete catalogs',()=>{
  for(const name of ['app.js','lib.mjs','wallet.mjs','share-card.mjs','profile-card.mjs','document-i18n.mjs']){
    const source=readFileSync(new URL('../public/'+name,import.meta.url),'utf8');
    for(const match of source.matchAll(/['"]((?:ui|card|common|error)\.[A-Za-z0-9_.-]+)['"]/g)){
      if(match[1].endsWith('.'))continue;
      assert.ok(Object.hasOwn(messages.en,match[1]),name+': '+match[1]);
    }
  }
});
