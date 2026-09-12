import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {createWalletAuthClient,validLegacyCode} from '../public/wallet-auth-client.mjs';
import {t as translate} from '../public/i18n.mjs';
import {escapeHtml as esc} from '../public/lib.mjs';

const source=readFileSync(new URL('../public/app.js',import.meta.url),'utf8');
const wallet={name:'Test Wallet'},address='11111111111111111111111111111111';
const account={address,chains:['solana:devnet'],features:['solana:signMessage']};
const challenge={challengeId:'wl_one',address,message:'Exact sign-in proof',expiresAt:2000,chain:'solana:devnet',mode:'login'};
const success={user:{id:'u1'},points:{userId:'u1'},wallet:{address,chain:'solana:devnet'}};
const deferred=()=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject};};
async function tick(){await new Promise(resolve=>setImmediate(resolve));}
function harness(options={}){
 const calls=[],accepted=[],refreshed=[],changes=[];let owner=options.initialOwner??null,listener=null;
 const api=async(path,{body})=>{
  calls.push({path,body});
  if(options.api){const override=options.api(path,body);if(override!==undefined)return override;}
  if(path.endsWith('/context'))return {expiresAt:100000};
  if(path.endsWith('/challenge'))return {...challenge,mode:body.mode};
  if(path.endsWith('/verify'))return success;
  if(path.endsWith('/cancel'))return {ok:true};
  if(path==='/api/auth/login')return {user:{id:'legacy'}};
  throw new Error(path);
 };
 const client=createWalletAuthClient({api,owner:()=>owner,now:()=>1000,
  connect:options.connect??(async()=>[account]),observe:(_wallet,callback)=>{listener=callback;return ()=>{listener=null;};},
  sign:options.sign??(async(_wallet,selected,message,{stillCurrent})=>{assert.ok(stillCurrent());assert.equal(selected.address,address);return {challengeId:message.challengeId,address,signature:'signed'};}),
  onChange:state=>changes.push(state.phase),onSuccess:async value=>{accepted.push(value);owner=value.user.id;},
  onRefresh:async()=>{refreshed.push(true);owner=options.refreshOwner??null;}});
 return {client,calls,accepted,refreshed,changes,setOwner:value=>{owner=value;},event:value=>listener?.(value)};
}
async function reviewed(h){await h.client.choose(wallet,{mode:h.mode??'login',expectedUserId:h.expectedUserId??null});await h.client.challenge();assert.equal(h.client.get().phase,'challenge');}

test('wallet sign-in uses a fresh message proof and loads an existing identity without registering a guest',async()=>{
 const h=harness();await reviewed(h);await h.client.submit();
 assert.deepEqual(h.calls.map(call=>call.path),['/api/auth/wallet/context','/api/auth/wallet/challenge','/api/auth/wallet/verify']);
 assert.deepEqual(h.calls[1].body,{address,mode:'login',expectedUserId:null});
 assert.deepEqual(h.calls[2].body,{challengeId:'wl_one',address,signature:'signed'});
 assert.equal(h.accepted[0].user.id,'u1');assert.equal(h.client.get().phase,'success');assert.equal(h.client.get().challenge,null);
});
test('the selected wallet account label is only an optional new-profile display hint',async()=>{
 const other={...account,address:'22222222222222222222222222222222',label:'  spritz.skr  '};
 const h=harness({connect:async()=>[{...account,label:'First account'},other],api:(path,body)=>path.endsWith('/challenge')?{...challenge,address:body.address}:undefined});
 await h.client.choose(wallet);h.client.select(other.address);await h.client.challenge();
 assert.deepEqual(h.calls.find(call=>call.path.endsWith('/challenge')).body,{address:other.address,mode:'login',expectedUserId:null,displayName:'spritz.skr'});
 assert.equal(h.client.get().phase,'challenge');
});
test('missing or malformed wallet account labels keep the server display-name fallback',async()=>{
 for(const label of [undefined,null,32,{},'', '   ','a'.repeat(41),'bad\nname','bad\u0000name','bad\u007fname','bad\u0085name','\ud800','\udc00']){
  const h=harness({connect:async()=>[{...account,label}]});await reviewed(h);
  assert.equal(Object.hasOwn(h.calls.find(call=>call.path.endsWith('/challenge')).body,'displayName'),false);
 }
});
test('wallet display hint length counts Unicode code points and preserves valid multilingual names',async()=>{
 for(const label of ['봄바다','海 🌊','🌊'.repeat(40)]){
  const h=harness({connect:async()=>[{...account,label}]});await reviewed(h);
  assert.equal(h.calls.find(call=>call.path.endsWith('/challenge')).body.displayName,label);
 }
 const h=harness({connect:async()=>[{...account,label:'🌊'.repeat(41)}]});await reviewed(h);
 assert.equal(Object.hasOwn(h.calls.find(call=>call.path.endsWith('/challenge')).body,'displayName'),false);
});
test('migration never submits a wallet label as a replacement profile name',async()=>{
 const h=harness({initialOwner:'legacy',connect:async()=>[{...account,label:'Replacement.skr'}]});
 await h.client.choose(wallet,{mode:'migrate',expectedUserId:'legacy'});await h.client.challenge();
 assert.deepEqual(h.calls.find(call=>call.path.endsWith('/challenge')).body,{address,mode:'migrate',expectedUserId:'legacy'});
});
test('reusing the client after external logout creates a fresh context before the next sign-in',async()=>{
 const h=harness();await reviewed(h);await h.client.submit();
 h.setOwner(null);assert.equal(h.client.reset(),true);
 await reviewed(h);await h.client.submit();
 assert.equal(h.accepted.length,2);
 assert.deepEqual(h.calls.map(call=>call.path),Array(2).fill(['/api/auth/wallet/context','/api/auth/wallet/challenge','/api/auth/wallet/verify']).flat());
});
test('reset serializes a replacement bootstrap after a still-pending cookie response',async()=>{
 const first=deferred();let contexts=0;
 const h=harness({api:path=>path.endsWith('/context')&&++contexts===1?first.promise:undefined});
 const old=h.client.prepare();assert.equal(h.client.reset(),true);const replacement=h.client.prepare();await tick();
 assert.equal(h.calls.length,1);
 first.resolve({expiresAt:2000});await old;await replacement;
 assert.equal(h.calls.length,2);assert.equal(contexts,2);
});
test('both challenge creation and legacy login wait for the context cookie response',async()=>{
 for(const kind of ['wallet','legacy']){
  const pending=deferred();const h=harness({api:path=>path.endsWith('/context')?pending.promise:undefined});
  let run;
  if(kind==='wallet'){await h.client.choose(wallet);run=h.client.challenge();}
  else run=h.client.importLegacy('x'.repeat(32));
  await tick();assert.deepEqual(h.calls.map(call=>call.path),['/api/auth/wallet/context']);
  pending.resolve({expiresAt:2000});await run;
  assert.equal(h.calls[1].path,kind==='wallet'?'/api/auth/wallet/challenge':'/api/auth/login');
 }
});
test('migration binds the current owner and cannot accept another profile in the response',async()=>{
 const h=harness({initialOwner:'legacy',api:path=>path.endsWith('/verify')?{...success,user:{id:'other'},points:{userId:'other'}}:undefined});
 await h.client.choose(wallet,{mode:'migrate',expectedUserId:'legacy'});await h.client.challenge();await h.client.submit();
 assert.deepEqual(h.calls.find(call=>call.path.endsWith('/challenge')).body,{address,mode:'migrate',expectedUserId:'legacy'});
 assert.equal(h.accepted.length,0);assert.equal(h.client.get().error.code,'wallet_login_changed');
 await assert.rejects(()=>h.client.choose(wallet,{mode:'login',expectedUserId:null}),{code:'account_changed'});
});
test('concurrent clicks cannot duplicate context, challenges or signature verification',async()=>{
 const pending=deferred();const h=harness({sign:async()=>pending.promise});
 await h.client.choose(wallet);await Promise.all([h.client.challenge(),h.client.challenge()]);
 const one=h.client.submit();await h.client.submit();await h.client.challenge();
 pending.resolve({challengeId:'wl_one',address,signature:'signed'});await one;
 for(const endpoint of ['context','challenge','verify'])assert.equal(h.calls.filter(call=>call.path.endsWith('/'+endpoint)).length,1);
});
test('changed accounts during signing revoke proofs and never submit stale signatures',async()=>{
 const pending=deferred();const h=harness({sign:async()=>pending.promise});await reviewed(h);
 const run=h.client.submit();h.event({accounts:[]});await tick();
 pending.resolve({challengeId:'wl_one',address,signature:'signed'});await run;await tick();
 assert.equal(h.calls.filter(call=>call.path.endsWith('/verify')).length,0);
 assert.equal(h.calls.filter(call=>call.path.endsWith('/cancel')).length,1);assert.equal(h.refreshed.length,1);assert.equal(h.accepted.length,0);
});
test('a shared-session owner change stops the pending proof before verification',async()=>{
 const pending=deferred();const h=harness({sign:async()=>pending.promise});await reviewed(h);
 const run=h.client.submit();h.setOwner('other');pending.resolve({challengeId:'wl_one',address,signature:'signed'});await run;
 assert.equal(h.calls.filter(call=>call.path.endsWith('/verify')).length,0);assert.equal(h.accepted.length,0);
});
test('cancel waits for pending context before revocation and never starts its queued challenge',async()=>{
 const pending=deferred();const h=harness({api:path=>path.endsWith('/context')?pending.promise:undefined});
 await h.client.choose(wallet);const challengeRun=h.client.challenge();const canceled=h.client.cancel();
 assert.equal(h.client.get().phase,'canceling');assert.equal(h.client.reset(),false);
 pending.resolve({expiresAt:2000});await challengeRun;await canceled;
 assert.deepEqual(h.calls.map(call=>call.path),['/api/auth/wallet/context','/api/auth/wallet/cancel']);assert.equal(h.refreshed.length,1);
});
test('cancellation drains a verify response carrying stale session cookies before another attempt',async()=>{
 const pending=deferred();const h=harness({api:path=>path.endsWith('/verify')?pending.promise:undefined});await reviewed(h);
 const submit=h.client.submit();await tick();const canceled=h.client.cancel();await tick();
 assert.equal(h.refreshed.length,0);assert.equal(h.client.get().phase,'canceling');await h.client.choose(wallet);
 pending.resolve(success);await submit;await canceled;
 assert.equal(h.accepted.length,0);assert.equal(h.refreshed.length,1);assert.equal(h.client.get().phase,'idle');
});
test('canceling during authoritative session reload cannot finish a successful dialog',async()=>{
 const ready=deferred();let canApply=null,successes=0;
 const client=createWalletAuthClient({api:async path=>path.endsWith('/context')?{expiresAt:2000}:path.endsWith('/challenge')?challenge:path.endsWith('/verify')?success:{ok:true},
  now:()=>1000,connect:async()=>[account],observe:()=>()=>{},sign:async()=>({challengeId:challenge.challengeId,address,signature:'signed'}),
  onSuccess:async(_result,{stillCurrent})=>{await ready.promise;canApply=stillCurrent();if(canApply)successes++;}});
 await client.choose(wallet);await client.challenge();const submit=client.submit();await tick();
 assert.equal(client.get().phase,'loading_session');assert.equal(client.busy(),true);
 await client.cancel();ready.resolve();await submit;
 assert.equal(canApply,false);assert.equal(successes,0);assert.equal(client.get().phase,'idle');
});
test('the actual session refresh guards owner identity and cancellation before painting private state',async()=>{
 const start=source.indexOf('async function refreshAuthentication('),end=source.indexOf('\nasync function acceptAuthentication(',start);
 const make=new Function('api','resetWallet','resetPoints','state','acceptPoints','shareDialog','updateAccount','uiError',`let shareSession=null;${source.slice(start,end)};return refreshAuthentication;`);
 for(const variant of ['cancel','wrong-owner']){
  const state={user:{id:'existing'},me:{private:'existing'}};let changes=0;
  const run=make(async()=>({user:{id:'other'},points:{userId:'other'}}),()=>{changes++;},()=>{changes++;},state,()=>{changes++;},{open:false},()=>{changes++;},key=>new Error(key));
  if(variant==='cancel')assert.equal(await run({stillCurrent:()=>false}),null);
  else await assert.rejects(()=>run({expectedUserId:'expected'}));
  assert.equal(changes,0);assert.equal(state.user.id,'existing');
 }
});
test('failed cancellation cannot silently allow another sign-in',async()=>{
 const h=harness({api:path=>path.endsWith('/cancel')?Promise.reject({code:'network'}):undefined});
 await h.client.prepare();await assert.rejects(()=>h.client.cancel());assert.equal(h.client.get().phase,'cancel_failed');assert.equal(h.client.reset(),false);
 const before=h.calls.length;await h.client.choose(wallet);await h.client.importLegacy('x'.repeat(32));assert.equal(h.calls.length,before);
});
test('forged challenge and successful-response identities fail closed',async()=>{
 for(const change of [{address:'other'},{chain:'solana:mainnet'},{mode:'migrate'},{expiresAt:1000},{message:''},{challengeId:''}]){
  const h=harness({api:path=>path.endsWith('/challenge')?{...challenge,...change}:undefined});await h.client.choose(wallet);await h.client.challenge();await h.client.submit();
  assert.equal(h.client.get().error.code,'wallet_challenge_invalid');assert.equal(h.accepted.length,0);
 }
 for(const change of [{wallet:{address:'other',chain:'solana:devnet'}},{points:{userId:'other'}},{user:{id:''}}]){
  const h=harness({api:path=>path.endsWith('/verify')?{...success,...change}:undefined});await reviewed(h);await h.client.submit();
  assert.equal(h.client.get().error.code,'wallet_login_changed');assert.equal(h.accepted.length,0);
 }
});
test('legacy import rejects mnemonic phrases and malformed codes before making requests',async()=>{
 for(const code of ['', 'x'.repeat(31),'x'.repeat(257),'word '.repeat(24),'a'.repeat(31)+' ','<script>'.repeat(8)]){
  assert.equal(validLegacyCode(code),false);const h=harness();await assert.rejects(()=>h.client.importLegacy(code),{code:'invalid_recovery_code'});assert.equal(h.calls.length,0);
 }
 const h=harness();await h.client.importLegacy('Ab_9-'.repeat(12));assert.equal(h.accepted[0].user.id,'legacy');
 assert.equal(h.calls[1].body.recoveryCode,'Ab_9-'.repeat(12));
});
test('new default dialog has wallet actions and no guest registration or recovery generation',()=>{
 const start=source.indexOf('function authMarkup(){'),end=source.indexOf('\nasync function initializeAuthWallets(',start);
 const make=new Function('state','walletAuth','walletDiscovery','t','esc','languageControl','button','icon','date','errorText',`${source.slice(start,end)};return authMarkup;`);
 for(const locale of ['en','ko','ja','zh-Hant']){
  const state={authMode:'wallet'},auth={phase:'idle',error:null};
  const t=(key,params)=>translate(key,params,locale);
  const render=make(state,{get:()=>auth,busy:()=>false},{get:()=>[{name:'<unsafe wallet>'}]},t,esc,()=>'',(label,action)=>`<button data-action="${action}">${label}</button>`,()=>'',String,error=>error.code);
  const html=render();assert.ok(html.includes(t('ui.walletSignIn')));assert.match(html,/data-action="auth-wallet"/);assert.match(html,/&lt;unsafe wallet&gt;/);assert.doesNotMatch(html,/auth-form|recovery-saved|download-recovery|finish-auth/);
  state.authMode='legacy';const legacy=render();assert.match(legacy,/pattern="\[A-Za-z0-9_-\]\{32,256\}"/);assert.ok(legacy.includes(esc(t('ui.legacyImportHint'))));
  state.authMode='migrate';assert.ok(render().includes(esc(t('ui.walletMigrationHint'))));
 }
 assert.doesNotMatch(source,/\/api\/auth\/register|state\.recoveryCode|copy-recovery|download-recovery|finish-auth/);
 assert.match(source,/state\.me\?\.authentication\?\.method==='wallet'/);
});
