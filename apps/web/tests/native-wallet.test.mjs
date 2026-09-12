import test from 'node:test';
import assert from 'node:assert/strict';
import {createNativeWallet,nativePluginAvailable,registerNativeWallet} from '../public/native-wallet.mjs';
import {discoverWallets,isSupportedWallet,connectWallet,signOwnershipChallenge} from '../public/wallet.mjs';

const b64=bytes=>Buffer.from(bytes).toString('base64');
const pubkey=new Uint8Array(32).fill(7);
function plugin(overrides={}){
  const calls=[];
  return {calls,
    async authorize(args){calls.push(['authorize',args]);return {accounts:[{address:'7'.repeat(44),publicKey:b64(pubkey),label:'Seed Vault'}]};},
    async signMessage(args){calls.push(['signMessage',args]);return {signature:b64(new Uint8Array(64).fill(1))};},
    async deauthorize(){calls.push(['deauthorize']);return {};},
    ...overrides};
}

test('the native wallet satisfies the app wallet contract and never exposes transactions',async()=>{
  const wallet=createNativeWallet(plugin());
  assert.ok(isSupportedWallet(wallet));
  assert.deepEqual(Object.keys(wallet.features).sort(),['solana:signMessage','standard:connect','standard:disconnect','standard:events']);
  assert.ok(!('solana:signTransaction' in wallet.features) && !('solana:signAndSendTransaction' in wallet.features));
  const accounts=await connectWallet(wallet);
  assert.equal(accounts.length,1);
  assert.equal(accounts[0].address,'7'.repeat(44));
  assert.deepEqual([...accounts[0].publicKey],[...pubkey]);
  assert.deepEqual(accounts[0].chains,['solana:devnet']);
});

test('message signing returns the exact requested bytes with a 64-byte signature',async()=>{
  const p=plugin();const wallet=createNativeWallet(p);
  const [account]=await connectWallet(wallet);
  const challenge={challengeId:'c1',address:account.address,chain:'solana:devnet',message:'forecast-network:login:v1',expiresAt:Date.now()+60000};
  const result=await signOwnershipChallenge(wallet,account,challenge);
  assert.equal(result.address,account.address);
  assert.equal(Buffer.from(result.signature,'base64').length,64);
  const sign=p.calls.find(([name])=>name==='signMessage')[1];
  assert.equal(Buffer.from(sign.message,'base64').toString(),'forecast-network:login:v1');
  assert.equal(sign.address,account.address);
});

test('bridge failures map to wallet errors and unauthorized accounts are refused',async()=>{
  const rejecting=plugin({async authorize(){throw {message:'declined',code:'ERROR_AUTHORIZATION_FAILED'};}});
  await assert.rejects(connectWallet(createNativeWallet(rejecting)),error=>error.code==='wallet_rejected');
  const wallet=createNativeWallet(plugin());
  await assert.rejects(wallet.features['solana:signMessage'].signMessage({account:{address:'x'},message:new Uint8Array(1)}),/not authorized/);
  const short=plugin({async signMessage(){return {signature:b64(new Uint8Array(10))};}});
  const w2=createNativeWallet(short);await connectWallet(w2);
  await assert.rejects(w2.features['solana:signMessage'].signMessage({account:w2.accounts[0],message:new Uint8Array(3)}),/invalid signature/);
});

test('registration only happens inside the native shell and reaches discovery either way round',()=>{
  assert.equal(nativePluginAvailable({}),false);
  assert.equal(nativePluginAvailable({Capacitor:{isNativePlatform:()=>false,Plugins:{MobileWallet:{}}}}),false);
  const target=new EventTarget();
  const root={Capacitor:{isNativePlatform:()=>true,Plugins:{MobileWallet:plugin()}},
    dispatchEvent:event=>target.dispatchEvent(event),addEventListener:(name,fn)=>target.addEventListener(name,fn)};
  globalThis.CustomEvent??=class extends Event{constructor(type,init){super(type);this.detail=init?.detail;}};
  const discovery=discoverWallets(target);           // app listens first
  assert.equal(registerNativeWallet(root),true);       // shell registers afterwards
  assert.equal(discovery.get().length,1);
  const late=new EventTarget();
  const lateRoot={...root,dispatchEvent:event=>late.dispatchEvent(event),addEventListener:(name,fn)=>late.addEventListener(name,fn)};
  registerNativeWallet(lateRoot);                      // shell registers first
  assert.equal(discoverWallets(late).get().length,1);
});
