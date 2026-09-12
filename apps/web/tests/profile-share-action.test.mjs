import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {webcrypto} from 'node:crypto';
import {profileCardData,profileAccuracyLabel,profileScoreLabel} from '../public/profile-card.mjs';
import {t,getLocale,setLocale,formatNumber} from '../public/i18n.mjs';
import {formatEpochDate,escapeHtml} from '../public/lib.mjs';
import {shareCardData} from '../public/share-card.mjs';

const source=await readFile(new URL('../public/app.js',import.meta.url),'utf8');
function section(start,end){const from=source.indexOf(start),to=source.indexOf(end,from);assert.ok(from>=0&&to>from);return source.slice(from,to);}
const currentSource=section('function profileShareCurrent(','function profileCaption(');
const captionSource=section('function profileCaption(','function profileCardControls(');
const verifySource=section('async function verifyProfileRecord(','async function shareProfile(');
const createSource=section('async function shareProfile(','async function renderProfileShare(');
const renderSource=section('async function renderProfileShare(','async function updateProfileCard(');
const publishedSource=section('function publishedRecordMarkup(','async function renderCreator(');
const forecastRenderSource=section('async function renderForecastShare(','function recordSuccessfulShare(');
const caption=new Function('profileAccuracyLabel','profileScoreLabel','t','getLocale','formatNumber',`${captionSource};return profileCaption;`)(profileAccuracyLabel,profileScoreLabel,t,getLocale,formatNumber);
const verify=new Function('profileCardData','crypto','location','t','getLocale',`${verifySource};return verifyProfileRecord;`)(profileCardData,webcrypto,{origin:'https://forecast.example'},t,getLocale);
function deferred(){let resolve;const promise=new Promise(yes=>{resolve=yes;});return {promise,resolve};}
function payload(){return {schemaVersion:1,asOf:2000,user:{id:'u_example',displayName:'Example',handle:'example',createdAt:1000},metrics:{totalForecasts:1,resolvedForecasts:1,correctForecasts:1,invalidForecasts:0,accuracy:100,brierScore:0,calibrationScore:1},sampleStatus:'provisional',history:[],historyTruncated:true,highlight:null,methodology:{version:'profile-card-v1'},commitmentProfile:{algorithm:'SHA-256',encoding:'UTF-8',prefix:'forecast-network:sha256:profile-card-json:v1\n'}};}
function sorted(value){if(Array.isArray(value))return value.map(sorted);if(value&&typeof value==='object')return Object.fromEntries(Object.keys(value).sort().map(key=>[key,sorted(value[key])]));return value;}
async function envelope(value=payload()){
  const canonicalJson=JSON.stringify(sorted(value));
  const bytes=new TextEncoder().encode(value.commitmentProfile.prefix+canonicalJson);
  const snapshotHash=Buffer.from(await webcrypto.subtle.digest('SHA-256',bytes)).toString('hex');
  return {...value,canonicalJson,snapshotHash};
}

test('captions distinguish unscored, missing and true zero scores without claiming rounded perfection',()=>{
  assert.doesNotMatch(caption({metrics:{totalForecasts:0,resolvedForecasts:0}}),/accuracy|100%|0%/);
  assert.match(caption({metrics:{totalForecasts:12,resolvedForecasts:0}}),/12 forecasts on record/);
  const base={sampleStatus:'established',metrics:{resolvedForecasts:10,correctForecasts:0,accuracy:0,brierScore:1}};
  assert.match(caption(base),/^0% accuracy/);
  assert.match(caption({...base,metrics:{...base.metrics,accuracy:null,brierScore:null}}),/Accuracy unavailable/);
  assert.doesNotMatch(caption({...base,metrics:{...base.metrics,accuracy:99.999}}),/^100%/);
  assert.match(caption({...base,sampleStatus:'provisional'}),/small sample/);
});

test('public-record verification uses retained bytes rather than altered duplicate response fields',async()=>{
  const snapshot=await envelope();
  snapshot.user={...snapshot.user,displayName:'Imposter'};
  snapshot.metrics={...snapshot.metrics,correctForecasts:999};
  const data=await verify(snapshot);
  assert.equal(data.user.displayName,'Example');
  assert.equal(data.metrics.correctForecasts,1);
  assert.equal(new URL(data.url).searchParams.get('record'),snapshot.snapshotHash);
});

test('modified retained bytes, wrong commitment prefixes and oversized records are rejected',async()=>{
  const snapshot=await envelope();
  await assert.rejects(verify({...snapshot,canonicalJson:snapshot.canonicalJson.replace('Example','Altered')}),/could not be verified/);
  await assert.rejects(verify({...snapshot,commitmentProfile:{...snapshot.commitmentProfile,prefix:'other\n'}}),{message:t('ui.recordVerificationMissing')});
  await assert.rejects(verify({...snapshot,canonicalJson:'x'.repeat(131073)}),{message:t('ui.recordVerificationMissing')});
});

test('a profile publication response after an account switch cannot display the old record',async()=>{
  const response=deferred(),entered=deferred();let verifications=0,renders=0;
  const state={user:{id:'u_example'}};
  const dialog={open:false,innerHTML:'',classList:{add(){}},showModal(){this.open=true;}};
  const build=new Function('state','shareDialog','api','ensureAuth','shareHeader','profileCardActions','verifyProfileRecord','renderProfileShare','showError','location','t','getLocale','esc',`let shareSession=null;${currentSource}${createSource};return shareProfile;`);
  const share=build(state,dialog,async(path,options)=>{assert.equal(path,'/api/me/share-card');assert.deepEqual(options.body,{expectedUserId:'u_example'});entered.resolve();return response.promise;},async()=>true,()=>'',()=>'',async()=>{verifications+=1;},async()=>{renders+=1;},()=>{}, {origin:'https://forecast.example'},t,getLocale,value=>value);
  const pending=share();await entered.promise;state.user={id:'u_other'};const html=dialog.innerHTML;
  response.resolve(await envelope());await pending;
  assert.equal(verifications,0);assert.equal(renders,0);assert.equal(dialog.innerHTML,html);
});

for(const scenario of ['owner change','newer format'])test(`a late PNG cannot overwrite the preview after ${scenario}`,async()=>{
  const blob=deferred(),entered=deferred();let queries=0;
  const state={user:{id:'u_example'}};
  const session={kind:'profile',owner:'u_example',renderVersion:0,format:'landscape',theme:'paper',snapshot:{user:{displayName:'Example',handle:'example'},asOf:2000},caption:'Record'};
  const dialog={open:true,innerHTML:'',querySelector(){queries+=1;throw new Error('Stale preview wrote to the DOM');}};
  const document={fonts:{ready:Promise.resolve()},createElement:()=>({setAttribute(){}})};
  const build=new Function('state','shareSession','shareDialog','document','loadCardFonts','shareHeader','profileCardControls','profileCardActions','date','esc','renderProfileCard','canvasPng','showError','t','getLocale','formatEpochDate',`${currentSource}${renderSource};return renderProfileShare;`);
  const render=build(state,session,dialog,document,async()=>true,()=>'',()=>'',()=>'',()=>'',value=>value,()=>{},async()=>{entered.resolve();return blob.promise;},()=>{},t,getLocale,formatEpochDate);
  const pending=render(session);await entered.promise;
  const newerFile={name:'newer.png'};
  if(scenario==='owner change')state.user={id:'u_other'};else session.renderVersion+=1;
  session.file=newerFile;blob.resolve(new Blob(['png']));await pending;
  assert.equal(session.file,newerFile);assert.equal(queries,0);
});

test('profile publication retains its selected language while awaiting the retained record',async()=>{
  const previous=getLocale(),response=deferred(),entered=deferred();let rendered;
  const state={user:{id:'u_example'}};
  const dialog={open:false,innerHTML:'',classList:{add(){}},showModal(){this.open=true;}};
  const build=new Function('deps',`const {state,shareDialog,api,ensureAuth,shareHeader,profileCardActions,verifyProfileRecord,renderProfileShare,showError,location,t,getLocale,esc,profileCaption}=deps;let shareSession=null;${currentSource}${createSource};return shareProfile;`);
  const share=build({state,shareDialog:dialog,api:async(path,options)=>{assert.equal(path,'/api/me/share-card');assert.deepEqual(options.body,{expectedUserId:'u_example'});entered.resolve();return response.promise;},ensureAuth:async()=>true,shareHeader:()=>'',profileCardActions:()=>'',verifyProfileRecord:verify,renderProfileShare:async session=>{rendered=session;},showError:error=>{throw error;},location:{origin:'https://forecast.example'},t,getLocale,esc:escapeHtml,profileCaption:caption});
  try{
    setLocale('ja');const pending=share();await entered.promise;
    setLocale('ko');response.resolve(await envelope());await pending;
    assert.equal(rendered.locale,'ja');
    assert.equal(rendered.caption,caption(rendered.snapshot,'ja'));
    assert.notEqual(rendered.caption,caption(rendered.snapshot,'ko'));
    assert.equal(rendered.snapshot.user.id,'u_example');
  }finally{setLocale(previous);}
});

test('profile card rendering passes the session locale through font loading and PNG creation',async()=>{
  const previous=getLocale(),fonts=deferred(),png=deferred(),entered=deferred();let renderLocale,pngLocale;
  const state={user:{id:'u_example'}};
  const session={kind:'profile',owner:'u_example',locale:'zh-Hant',renderVersion:0,format:'portrait',theme:'paper',snapshot:{user:{displayName:'Example',handle:'example'},asOf:2000},caption:'Record'};
  const dialog={open:true,innerHTML:'',querySelector(){throw new Error('Stale render wrote to DOM');}};
  const document={createElement:()=>({setAttribute(){}})};
  const build=new Function('deps',`const {state,shareSession,shareDialog,document,loadCardFonts,shareHeader,profileCardControls,profileCardActions,date,esc,renderProfileCard,canvasPng,showError,t,getLocale,formatEpochDate}=deps;${currentSource}${renderSource};return renderProfileShare;`);
  const render=build({state,shareSession:session,shareDialog:dialog,document,loadCardFonts:()=>fonts.promise,shareHeader:()=>'',profileCardControls:()=>'',profileCardActions:()=>'',date:()=>'',esc:escapeHtml,renderProfileCard:(_canvas,_data,options)=>{renderLocale=options.locale;},canvasPng:async(_canvas,options)=>{pngLocale=options.locale;entered.resolve();return png.promise;},showError:error=>{throw error;},t,getLocale,formatEpochDate});
  try{
    setLocale('en');const pending=render(session);setLocale('ja');fonts.resolve();await entered.promise;
    assert.equal(renderLocale,'zh-Hant');assert.equal(pngLocale,'zh-Hant');
    state.user={id:'other'};png.resolve(new Blob(['png']));await pending;
  }finally{setLocale(previous);}
});

test('localized public evidence labels exclude invalid outcomes from misses and escape actual content',()=>{
  const build=new Function('t','getLocale','formatNumber','esc','date','icon','profileCaption',`${publishedSource};return publishedRecordMarkup;`);
  const markup=build(t,getLocale,formatNumber,escapeHtml,()=>'',()=>'',caption);
  const data={...payload(),snapshotHash:'a'.repeat(64),metrics:{...payload().metrics,totalForecasts:2,invalidForecasts:1},history:[{forecastId:'invalid',title:'<img src=x onerror=alert(1)>',outcome:'YES',confidence:40,finalizedAt:1000,correct:null,resolvedOutcome:'INVALID'}]};
  const previous=getLocale();
  try{
    for(const locale of ['en','ko','ja','zh-Hant']){
      setLocale(locale);const html=markup(data);
      assert.ok(html.includes(t('card.invalid',{},locale)));
      assert.ok(!html.includes('is-incorrect'));
      assert.ok(html.includes('&lt;img src=x onerror=alert(1)&gt;'));
      assert.ok(!html.includes('<img src=x'));
    }
  }finally{setLocale(previous);}
});

test('forecast card language changes reuse one cached snapshot and reject the older PNG',async()=>{
  const first=deferred(),second=deferred(),firstEntered=deferred(),secondEntered=deferred();let writes=0;
  const session={kind:'forecast',id:'f_one',locale:'en',renderVersion:0,asOf:2000,detail:{forecast:{id:'f_one',question:'Immutable source question?',category:'science',crowd:{probability:0}}},file:null,canShareFile:false};
  const before=structuredClone(session.detail),drawn=[];
  const dialog={open:true,innerHTML:'',querySelector(){return {append(){writes+=1;}};}};
  const document={fonts:{ready:Promise.resolve()},createElement:()=>({setAttribute(){}})};
  const build=new Function('deps',`const {shareSession,shareDialog,document,loadCardFonts,t,getLocale,esc,shareHeader,shareActions,shareCardData,location,formatNumber,renderShareCard,categoryLabel,canvasPng,File,navigator,showError,api}=deps;${forecastRenderSource};return renderForecastShare;`);
  const render=build({shareSession:session,shareDialog:dialog,document,loadCardFonts:async()=>true,t,getLocale,esc:escapeHtml,shareHeader:()=>'',shareActions:()=>'',shareCardData,location:{origin:'https://forecast.example'},formatNumber,renderShareCard:(_canvas,data)=>{drawn.push(data);},categoryLabel:()=>'',canvasPng:async(_canvas,{locale})=>{if(locale==='en'){firstEntered.resolve();return first.promise;}secondEntered.resolve();return second.promise;},File,navigator:{canShare:()=>false},showError:error=>{throw error;},api:()=>{throw new Error('A language change must not fetch or publish again');}});
  const oldRender=render(session);await firstEntered.promise;
  session.locale='ko';const newRender=render(session);await secondEntered.promise;
  second.resolve(new Blob(['new PNG']));await newRender;
  const currentFile=session.file;
  first.resolve(new Blob(['old PNG']));await oldRender;
  assert.equal(session.file,currentFile);assert.equal(writes,1);
  assert.deepEqual(drawn.map(data=>({locale:data.locale,at:data.at,question:data.question})),[{locale:'en',at:2000,question:'Immutable source question?'},{locale:'ko',at:2000,question:'Immutable source question?'}]);
  assert.deepEqual(session.detail,before);
});
