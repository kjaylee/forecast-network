import {t,getLocale,formatNumber,errorText,intlLocale,uiError} from '../public/i18n.mjs';
// Run the production functions with their real presentation dependencies.
function localizedFunction(...args){
  const compiled=new Function('t','getLocale','formatNumber','errorText','intlLocale','uiError',...args);
  return (...values)=>compiled(t,getLocale,formatNumber,errorText,intlLocale,uiError,...values);
}
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {pointsForUser,pointStakeLimit,pointStakeValue,validatePointStake,forecastSubmission,forecastIsOpen} from '../public/lib.mjs';
import {profileAccuracyLabel,profileScoreLabel} from '../public/profile-card.mjs';

function summary(userId='user-a',available=1000,committed=0){return {userId,available,committed,total:available+committed,
  policy:{version:'participation-points-v1',profileGrant:1000,walletGrant:500,minStake:0,maxStake:1000,winReturnMultiplier:2,invalidReturnMultiplier:1,practiceAllowed:true,purchasable:false,transferable:false,redeemable:false,reputationWeighted:false},
  onboarding:{profile:{completed:true,reward:1000},wallet:{completed:false,reward:500,eligible:true}},entries:[]};}

test('Points balances are account-bound and enforce the non-financial, unweighted contract',()=>{
  const points=summary();assert.equal(pointsForUser(points,'user-a'),points);
  assert.equal(pointsForUser(points,'user-b'),null);
  assert.equal(pointsForUser({...points,total:1001},'user-a'),null);
  assert.equal(pointsForUser({...points,policy:{...points.policy,reputationWeighted:true}},'user-a'),null);
  assert.equal(pointsForUser({...points,policy:{...points.policy,purchasable:true}},'user-a'),null);
});

test('stake values preserve zero and reject empty, fractional, NaN and out-of-range commitments',()=>{
  for(const raw of [0,'0',' 0 '])assert.equal(pointStakeValue(raw),0);
  for(const raw of ['',null,undefined,NaN,Infinity,-1,1.5,'1.5','1e3','abc'])assert.throws(()=>pointStakeValue(raw),{code:'stake_invalid'});
  assert.equal(validatePointStake(0,{userId:'user-a',points:null}),0);
  assert.throws(()=>validatePointStake(1,{userId:'user-a',points:null}),{code:'points_unavailable'});
  assert.throws(()=>validatePointStake(1001,{userId:'user-a',points:summary()}),{code:'stake_limit_exceeded'});
  assert.throws(()=>validatePointStake(101,{userId:'user-a',points:summary('user-a',100)}),{code:'insufficient_points'});
});

test('adjustment capacity includes only the same forecast’s active hold',()=>{
  const points=summary('user-a',25,200);
  const committed={amount:200,status:'committed'};
  assert.equal(pointStakeLimit(points,'user-a',committed),225);
  assert.equal(validatePointStake(225,{points,userId:'user-a',position:committed}),225);
  assert.throws(()=>validatePointStake(226,{points,userId:'user-a',position:committed}),{code:'insufficient_points'});
  assert.equal(pointStakeLimit(points,'user-a',{amount:200,status:'settled'}),25);
  assert.equal(pointStakeLimit(points,'user-b',committed),0);
  assert.equal(pointStakeLimit(summary('user-a',950), 'user-a',committed),1000);
});

test('forecast requests bind the initiating user and keep confidence unweighted',()=>{
  const request=forecastSubmission({forecast:{revision:3},outcome:'NO',confidence:24,stakePoints:1000,expectedUserId:'user-a',points:summary(),position:null});
  assert.deepEqual(request,{outcome:'NO',confidence:24,revision:3,stakePoints:1000,expectedUserId:'user-a'});
  assert.throws(()=>forecastSubmission({forecast:{revision:3},outcome:'YES',confidence:80,stakePoints:0,expectedUserId:null}),{code:'account_changed'});
  assert.throws(()=>forecastSubmission({forecast:{revision:3},outcome:'YES',confidence:80,stakePoints:1,expectedUserId:'user-b',points:summary()}),{code:'points_unavailable'});
});

const source=await readFile(new URL('../public/app.js',import.meta.url),'utf8');
const start=source.indexOf('function resetPoints(');const end=source.indexOf('function pointCount(',start);
assert.ok(start>=0&&end>start);
const code=source.slice(start,end);
const build=localizedFunction('state','pointsState','api','pointsForUser','renderPointsViews',`${code};return {resetPoints,currentPoints,acceptPoints,loadPoints};`);
function deferred(){let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return {promise,resolve,reject};}
function harness(api){
  const state={user:{id:'user-a'}};const pointsState={snapshot:null,request:0,epoch:0,loading:false,error:'',accountChanged:false};let renders=0;
  const operations=build(state,pointsState,api,pointsForUser,()=>{renders+=1;});
  return {state,pointsState,...operations,renders:()=>renders};
}

test('a delayed Points response cannot show the previous user’s balance after logout',async()=>{
  const response=deferred();const run=harness(async()=>response.promise);
  const request=run.loadPoints();run.state.user=null;run.resetPoints();run.state.user={id:'user-b'};run.acceptPoints(summary('user-b',77),'user-b');
  const expected=structuredClone(run.pointsState);const renders=run.renders();response.resolve(summary('user-a',999));await request;
  assert.deepEqual(run.pointsState,expected);assert.equal(run.renders(),renders);assert.equal(run.currentPoints().available,77);
});

test('older balance loads and errors cannot overwrite a newer request or clear its loading state',async()=>{
  const first=deferred();const second=deferred();let requests=0;
  const run=harness(async()=>++requests===1?first.promise:second.promise);
  const one=run.loadPoints();const two=run.loadPoints();first.reject(new Error('stale error'));await one;
  assert.equal(run.pointsState.loading,true);assert.equal(run.pointsState.error,'');second.resolve(summary('user-a',850));await two;
  assert.equal(run.currentPoints().available,850);assert.equal(run.pointsState.loading,false);
});

test('a shared-cookie account mismatch hides the response and requires account refresh',async()=>{
  const run=harness(async()=>summary('user-b',777));run.acceptPoints(summary('user-a',1000),'user-a');await run.loadPoints();
  assert.equal(run.currentPoints(),null);assert.equal(run.pointsState.snapshot,null);assert.equal(run.pointsState.accountChanged,true);
  assert.match(run.pointsState.error,/account changed/i);
});

test('successful mutation balances supersede an older in-flight balance read',async()=>{
  const response=deferred();const run=harness(async()=>response.promise);
  const request=run.loadPoints();run.acceptPoints(summary('user-a',950,50),'user-a');response.resolve(summary('user-a',1000));await request;
  assert.equal(run.currentPoints().available,950);assert.equal(run.currentPoints().committed,50);
});

test('profile sharing remains about accuracy and Brier, never the Points balance',()=>{
  const begin=source.indexOf('function profileCaption(');const finish=source.indexOf('function profileCardControls(',begin);
  const code=source.slice(begin,finish);
  const caption=localizedFunction('profileAccuracyLabel','profileScoreLabel',`${code};return profileCaption;`)(profileAccuracyLabel,profileScoreLabel);
  const data={metrics:{totalForecasts:10,resolvedForecasts:10,correctForecasts:7,accuracy:70,brierScore:.2},sampleStatus:'established',points:summary()};
  const before=caption(data);data.points=summary('user-a',20000,1000);const after=caption(data);
  assert.equal(before,after);assert.match(after,/70% accuracy/);assert.match(after,/Brier 0.200/);assert.doesNotMatch(after,/Points|balance|committed/);
  assert.match(caption({metrics:{totalForecasts:1,resolvedForecasts:0}}),/1 forecast on record/);
  assert.match(caption({metrics:{totalForecasts:1,resolvedForecasts:1,correctForecasts:1,accuracy:100,brierScore:0}}),/1 correct call out of 1 scored forecast\./);
});

const submitStart=source.indexOf('async function submitForecast(');const submitEnd=source.indexOf('function main()',submitStart);
const submitCode=source.slice(submitStart,submitEnd);
const submitFactory=localizedFunction('state','pointsState','ensureAuth','withForm','showError','document','forecastIsOpen','currentPoints','loadPoints','forecastSubmission','currentStake','api','acceptPoints','pointCount','toast','history','renderRoute',`${submitCode};return submitForecast;`);
function submitHarness(adapters={}){
  const state={user:{id:'user-a'},detail:{forecast:{id:'forecast-a',revision:3,state:'OPEN',openAt:0,closeAt:Date.now()+60000},stake:{amount:0,status:'practice'}},selection:'YES',confidence:70,sequence:1};
  const pointsState={snapshot:summary(),epoch:0,accountChanged:false};const requests=[];const notices=[];let rerenders=0;
  const submit=submitFactory(state,pointsState,adapters.auth || (async()=>true),async(form,id,operation)=>operation(),()=>{}, {},forecastIsOpen,
    ()=>pointsForUser(pointsState.snapshot,state.user?.id),adapters.load || (async()=>pointsState.snapshot),forecastSubmission,()=>state.detail.stake,
    async(path,options)=>{requests.push({path,...options});return adapters.api?adapters.api(path,options):{points:summary('user-a',950,50),stake:{amount:50,status:'committed'}};},
    (points,owner)=>{if(owner!==state.user?.id||!pointsForUser(points,owner))return false;pointsState.snapshot=points;return true;},value=>String(value),message=>notices.push(message),{replaceState(){}},()=>{rerenders+=1;});
  return {state,pointsState,requests,notices,submit:raw=>submit({querySelector:()=>({value:raw})}),rerenders:()=>rerenders};
}

test('the actual submit action sends explicit stake and the initiating account',async()=>{
  const run=submitHarness();await run.submit('50');
  assert.deepEqual(run.requests[0].body,{outcome:'YES',confidence:70,revision:3,stakePoints:50,expectedUserId:'user-a'});
  assert.equal(run.pointsState.snapshot.available,950);assert.equal(run.rerenders(),1);
});

test('insufficient Points never silently reduce the requested commitment',async()=>{
  const run=submitHarness();run.pointsState.snapshot=summary('user-a',25);
  await assert.rejects(run.submit('100'),{code:'insufficient_points'});
  assert.equal(run.requests.length,0);assert.equal(run.state.stakeRaw,'100');assert.equal(run.notices.length,0);
});

test('switching account during the auth gate prevents charging the new account',async()=>{
  const response=deferred();const run=submitHarness({auth:async()=>response.promise});
  const action=run.submit('50');run.state.user={id:'user-b'};run.pointsState.epoch+=1;response.resolve(true);await action;
  assert.equal(run.requests.length,0);
});

test('switching account during balance loading prevents submitting the old stake',async()=>{
  const entered=deferred();const response=deferred();const run=submitHarness({load:async()=>{entered.resolve();return response.promise;}});run.pointsState.snapshot=null;
  const action=run.submit('50');await entered.promise;run.state.user={id:'user-b'};run.pointsState.epoch+=1;response.resolve(summary('user-a'));await action;
  assert.equal(run.requests.length,0);
});

test('a delayed successful forecast response cannot replace the next account’s Points',async()=>{
  const entered=deferred();const response=deferred();const run=submitHarness({api:async()=>{entered.resolve();return response.promise;}});
  const action=run.submit('50');await entered.promise;run.state.user={id:'user-b'};run.pointsState.epoch+=1;run.pointsState.snapshot=summary('user-b',77);
  response.resolve({points:summary('user-a',950,50),stake:{amount:50,status:'committed'}});await action;
  assert.equal(run.pointsState.snapshot.userId,'user-b');assert.equal(run.pointsState.snapshot.available,77);assert.equal(run.notices.length,0);assert.equal(run.rerenders(),0);
});

test('account-change and Points errors offer their own recovery, not a revision warning',async()=>{
  const begin=source.indexOf('async function withForm(');const finish=source.indexOf('let shareSession=null;',begin);const code=source.slice(begin,finish);
  for(const [errorCode,expectedAction] of [['account_changed','refresh-account'],['insufficient_points','points-refresh'],['points_conflict','refresh-detail']]){
    const target={innerHTML:'',textContent:'',children:[],append(node){this.children.push(node);}};
    const document={getElementById:()=>target,createTextNode:text=>({textContent:text}),createElement:()=>({dataset:{}})};
    const state={user:{id:'user-a'}};const pointsState={epoch:0,snapshot:summary(),accountChanged:false};
    const withForm=localizedFunction('state','pointsState','document','renderPointsViews',`${code};return withForm;`)(state,pointsState,document,()=>{});
    const submit={disabled:false,innerHTML:'Record',isConnected:true};
    await withForm({id:'cast-form',querySelector:()=>submit},'cast-error',async()=>{throw Object.assign(new Error('Specific server explanation'),{status:409,code:errorCode});});
    assert.equal(target.children[1].dataset.action,expectedAction);
    assert.match(target.children[0].textContent,/Specific server explanation/);
    assert.doesNotMatch(target.children[0].textContent,/forecast has changed/i);
  }
});

test('an expired-session form refreshes after login instead of leaving a disabled old form',async()=>{
  const begin=source.indexOf('async function withForm(');const finish=source.indexOf('let shareSession=null;',begin);const code=source.slice(begin,finish);
  const state={user:{id:'user-a'}};const pointsState={epoch:0};const submit={disabled:false,innerHTML:'Record',isConnected:true};let reloads=0;
  const document={getElementById:()=>({textContent:''})};
  const withForm=localizedFunction('state','pointsState','document','resetPoints','shareDialog','updateAccount','showError','openAuth','renderRoute',`let shareSession=null;${code};return withForm;`)(state,pointsState,document,()=>{pointsState.epoch+=1;},{open:false},()=>{},()=>{},()=>{},()=>{reloads+=1;});
  await withForm({id:'cast-form',querySelector:()=>submit},'cast-error',async()=>{throw Object.assign(new Error('Expired'),{status:401});});
  assert.equal(state.user,null);assert.equal(typeof state.authResolve,'function');
  state.user={id:'user-a'};state.authResolve(true);assert.equal(reloads,1);
});
