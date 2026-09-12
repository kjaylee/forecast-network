import {t,getLocale,formatNumber,errorText,intlLocale,uiError} from '../public/i18n.mjs';
// Run the production functions with their real presentation dependencies.
function localizedFunction(...args){
  const compiled=new Function('t','getLocale','formatNumber','errorText','intlLocale','uiError',...args);
  return (...values)=>compiled(t,getLocale,formatNumber,errorText,intlLocale,uiError,...values);
}
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {preferredAccount} from '../public/wallet.mjs';

// Execute the production action with controlled async adapters, without starting
// the browser application or replacing the logic under test with a test copy.
const source=await readFile(new URL('../public/app.js',import.meta.url),'utf8');
function section(start,end){
  const from=source.indexOf(start);const to=source.indexOf(end,from);
  assert.ok(from>=0&&to>from,'production wallet function boundary must exist');
  return source.slice(from,to);
}
const actionSource=section('async function walletAction(','// Apply individual CSS properties');
const resetSource=section('function resetWallet(','function walletMarkup(');
const loadSource=section('async function loadWallet(','walletDiscovery.subscribe(');
const build=localizedFunction('walletState','state','api','renderWallet','signOwnershipChallenge','walletDiscovery','connectWallet','preferredAccount','observeWallet','disconnectWallet','currentPoints','loadPoints',`${resetSource}\n${loadSource}\n${actionSource}\nreturn {walletAction,resetWallet,loadWallet};`);
function deferred(){let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return {promise,resolve,reject};}
function fixture(adapters={}){
  const state={user:{id:'user-a'},sequence:1};
  const walletState={record:null,phase:'challenge',wallet:{name:'Wallet A'},accounts:[{address:'address-a'}],address:'address-a',challenge:{},generation:1,operation:0,connection:0,busy:false,error:'',notice:'',off:null};
  let renders=0;
  const controller=build(walletState,state,adapters.api || (async()=>({})),()=>{renders+=1;},adapters.sign || (async()=>({signature:'signature-a'})),{get:()=>[{name:'Wallet A'}]},adapters.connect || (async()=>[]),preferredAccount,adapters.observe || (()=>()=>{}),adapters.disconnect || (async()=>{}),adapters.currentPoints || (()=>null),adapters.loadPoints || (async()=>null));
  const changeAccount=()=>{
    state.user=null;controller.resetWallet();state.user={id:'user-b'};
    Object.assign(walletState,{record:{address:'address-b'},wallet:{name:'Wallet B'},phase:'account',busy:true,error:'new-action-error',notice:'new-action-notice'});
    walletState.operation+=1;
  };
  return {state,walletState,controller,changeAccount,renders:()=>renders};
}

for(const failed of [false,true])test(`delayed link ${failed?'failure':'success'} cannot overwrite the next user or clear their pending action`,async()=>{
  const entered=deferred();const response=deferred();
  const run=fixture({api:async path=>{assert.equal(path,'/api/wallet/link');entered.resolve();return response.promise;}});
  const action=run.controller.walletAction('wallet-sign',{});await entered.promise;
  run.changeAccount();const expected=structuredClone({...run.walletState,off:null});const renders=run.renders();
  if(failed)response.reject(new Error('Old user request failed'));else response.resolve({wallet:{address:'address-a'}});
  await action;
  assert.deepEqual(run.walletState,expected);
  assert.equal(run.renders(),renders,'stale catch/finally must not render or reset busy state');
});

test('delayed wallet connection cannot restore the previous user’s accounts',async()=>{
  const entered=deferred();const response=deferred();
  const run=fixture({connect:async()=>{entered.resolve();return response.promise;}});
  const action=run.controller.walletAction('choose-wallet',{dataset:{index:'0'}});await entered.promise;
  run.changeAccount();const expected=structuredClone(run.walletState);
  response.resolve([{address:'address-a',chains:['solana:devnet'],features:['solana:signMessage']}]);
  await action;assert.deepEqual(run.walletState,expected);
});

test('logout while signing prevents even sending the old proof to the server',async()=>{
  const entered=deferred();const response=deferred();let requests=0;
  const run=fixture({sign:async()=>{entered.resolve();return response.promise;},api:async()=>{requests+=1;return {};}});
  const action=run.controller.walletAction('wallet-sign',{});await entered.promise;
  run.changeAccount();response.resolve({signature:'old-signature'});await action;
  assert.equal(requests,0);assert.equal(run.walletState.record.address,'address-b');
});

test('a delayed wallet-link status fallback cannot overwrite the next user',async()=>{
  const entered=deferred();const response=deferred();
  const run=fixture({api:async path=>{if(path==='/api/wallet/link')return {};assert.equal(path,'/api/wallet');entered.resolve();return response.promise;}});
  const action=run.controller.walletAction('wallet-sign',{});await entered.promise;
  run.changeAccount();response.resolve({wallet:{address:'address-a'}});await action;
  assert.equal(run.walletState.record.address,'address-b');assert.equal(run.walletState.busy,true);
});

test('delayed unlink cannot clear the next user’s linked wallet',async()=>{
  const entered=deferred();const response=deferred();let disconnects=0;
  const run=fixture({api:async path=>{assert.equal(path,'/api/wallet/unlink');entered.resolve();return response.promise;},disconnect:async()=>{disconnects+=1;}});
  const action=run.controller.walletAction('wallet-unlink',{});await entered.promise;
  run.changeAccount();response.resolve({wallet:null});await action;
  assert.equal(run.walletState.record.address,'address-b');assert.equal(disconnects,0);assert.equal(run.walletState.busy,true);
});

test('a delayed extension disconnect cannot reset a newer action’s state',async()=>{
  const entered=deferred();const response=deferred();
  const run=fixture({api:async()=>({wallet:null}),disconnect:async()=>{entered.resolve();return response.promise;}});
  const action=run.controller.walletAction('wallet-unlink',{});await entered.promise;
  run.changeAccount();const expected=structuredClone(run.walletState);response.resolve();await action;
  assert.deepEqual(run.walletState,expected);
});

test('wallet status reads are discarded when the authenticated owner changes',async()=>{
  const response=deferred();const run=fixture({api:async()=>response.promise});
  const request=run.controller.loadWallet(1);run.changeAccount();response.resolve({wallet:{address:'address-a'}});await request;
  assert.equal(run.walletState.record.address,'address-b');assert.equal(run.renders(),0);
});

test('manual account selection does not disable provider-change invalidation during signing',async()=>{
  const signing=deferred();const response=deferred();let onChange;let linkRequests=0;
  const accounts=['address-a','address-b'].map(address=>({address,chains:['solana:devnet'],features:['solana:signMessage']}));
  const run=fixture({connect:async()=>accounts,observe:(wallet,callback)=>{onChange=callback;return ()=>{};},
    api:async path=>{if(path==='/api/wallet/challenge')return {message:'Verify address-b'};linkRequests+=1;return {};},
    sign:async()=>{signing.resolve();return response.promise;}});
  await run.controller.walletAction('choose-wallet',{dataset:{index:'0'}});
  // The actual select change handler changes selection and invalidates any proof.
  run.walletState.address='address-b';run.walletState.challenge=null;run.walletState.generation+=1;
  await run.controller.walletAction('wallet-challenge',{});
  const action=run.controller.walletAction('wallet-sign',{});await signing.promise;
  onChange({accounts:[]});
  assert.equal(run.walletState.phase,'choose');
  assert.equal(run.walletState.challenge,null);
  assert.equal(run.walletState.address,null);
  response.resolve({signature:'old-account-signature'});await action;
  assert.equal(linkRequests,0,'the invalidated signature must never reach the link endpoint');
  assert.equal(run.walletState.busy,false);
});

test('wallet mutations carry the initiating account precondition',async()=>{
  const requests=[];
  const run=fixture({api:async(path,options)=>{requests.push({path,body:options.body});return path==='/api/wallet/link'?{wallet:{address:'address-a'}}:{};}});
  await run.controller.walletAction('wallet-challenge',{});
  await run.controller.walletAction('wallet-sign',{});
  await run.controller.walletAction('wallet-unlink',{});
  assert.deepEqual(requests.map(item=>item.body.expectedUserId),['user-a','user-a','user-a']);
});

test('wallet rewards are announced only after an actual new grant is observed',async()=>{
  for(const completedBefore of [false,true,undefined]){
    const run=fixture({api:async()=>({wallet:{address:'address-a'}}),currentPoints:()=>({onboarding:{wallet:{completed:completedBefore}}}),loadPoints:async()=>({onboarding:{wallet:{completed:true,reward:500}}})});
    await run.controller.walletAction('wallet-sign',{});
    assert.equal(run.walletState.notice.includes('500 Points received'),completedBefore===false);
  }
});

test('an old reward refresh cannot change a newer account’s wallet notice',async()=>{
  const entered=deferred();const response=deferred();
  const run=fixture({api:async()=>({wallet:{address:'address-a'}}),currentPoints:()=>({onboarding:{wallet:{completed:false}}}),loadPoints:async()=>{entered.resolve();return response.promise;}});
  const action=run.controller.walletAction('wallet-sign',{});await entered.promise;run.changeAccount();const expected=structuredClone(run.walletState);
  response.resolve({onboarding:{wallet:{completed:true,reward:500}}});await action;
  assert.deepEqual(run.walletState,expected);
});

test('a wallet read from a changed shared cookie is hidden using its Points owner',async()=>{
  const run=fixture({api:async()=>({wallet:{address:'address-b'},points:{userId:'user-b'}})});
  await run.controller.loadWallet(1);
  assert.equal(run.walletState.record,null);assert.equal(run.walletState.accountChanged,true);
  assert.match(run.walletState.error,/account changed/i);
});
