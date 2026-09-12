import test from 'node:test';
import assert from 'node:assert/strict';
import {discoverWallets,isSupportedWallet,preferredAccount,connectWallet,signOwnershipChallenge,observeWallet} from '../public/wallet.mjs';

const account=(address,chain='solana:devnet')=>({address,chains:[chain],features:['solana:signMessage']});
function wallet(accounts=[account('address-a')]){
  return {name:'Test Wallet',chains:['solana:devnet'],accounts,features:{
    'standard:connect':{connect:async()=>({accounts})},
    'solana:signMessage':{signMessage:async({message})=>[{signedMessage:message,signature:new Uint8Array(64).fill(7),signatureType:'ed25519'}]},
  }};
}
const challenge={challengeId:'challenge-a',address:'address-a',chain:'solana:devnet',message:'Verify wallet ownership. Nonce: one-time.',expiresAt:200};

test('Wallet Standard discovery supports providers loaded before and after the app',()=>{
  const target=new EventTarget();
  const early=wallet();
  const late={...wallet(),name:'Late Wallet'};
  target.addEventListener('wallet-standard:app-ready',event=>event.detail.register(early));
  const discovery=discoverWallets(target);
  assert.deepEqual(discovery.get(),[early]);
  let unregister;
  target.dispatchEvent(new CustomEvent('wallet-standard:register-wallet',{detail:api=>{unregister=api.register(late);}}));
  assert.deepEqual(discovery.get(),[early,late]);
  target.dispatchEvent(new CustomEvent('wallet-standard:register-wallet',{detail:api=>api.register(late)()}));
  assert.equal(discovery.get().length,2,'duplicate registration cannot unregister the original');
  unregister();
  assert.deepEqual(discovery.get(),[early]);
  discovery.destroy();
});

test('discovery ignores unsupported wallets and selects a compatible Devnet account',()=>{
  assert.equal(isSupportedWallet({name:'No signing',chains:['solana:devnet'],features:{}}),false);
  const accounts=[account('ethereum','eip155:1'),account('mainnet','solana:mainnet'),account('devnet')];
  assert.equal(preferredAccount(accounts).address,'devnet');
  assert.equal(preferredAccount([account('ethereum','eip155:1')]),null);
});

test('connect cancellation never initiates signing',async()=>{
  const provider=wallet();let signatures=0;
  provider.features['standard:connect'].connect=async()=>{throw Object.assign(new Error('User rejected'),{code:4001});};
  provider.features['solana:signMessage'].signMessage=async()=>{signatures+=1;};
  await assert.rejects(connectWallet(provider),{code:'wallet_rejected'});
  assert.equal(signatures,0);
});

test('ownership signing returns the exact server challenge and a 64-byte signature',async()=>{
  const payload=await signOwnershipChallenge(wallet(),account('address-a'),challenge,{now:()=>100});
  assert.equal(payload.challengeId,'challenge-a');
  assert.equal(payload.address,'address-a');
  assert.equal(Buffer.from(payload.signature,'base64').length,64);
});

test('prefixed messages, wrong signatures, expired challenges and wrong accounts are rejected',async()=>{
  const provider=wallet();
  provider.features['solana:signMessage'].signMessage=async({message})=>[{signedMessage:new Uint8Array([1,...message]),signature:new Uint8Array(64)}];
  await assert.rejects(signOwnershipChallenge(provider,account('address-a'),challenge,{now:()=>100}),{code:'signed_message_mismatch'});
  provider.features['solana:signMessage'].signMessage=async({message})=>[{signedMessage:message,signature:new Uint8Array(63)}];
  await assert.rejects(signOwnershipChallenge(provider,account('address-a'),challenge,{now:()=>100}),{code:'signature_invalid'});
  await assert.rejects(signOwnershipChallenge(wallet(),account('address-a'),challenge,{now:()=>200}),{code:'challenge_expired'});
  await assert.rejects(signOwnershipChallenge(wallet(),account('address-b'),challenge,{now:()=>100}),{code:'challenge_mismatch'});
});

test('an account change while signing invalidates the pending proof',async()=>{
  const provider=wallet();let current=true;
  provider.features['solana:signMessage'].signMessage=async({message})=>{current=false;return [{signedMessage:message,signature:new Uint8Array(64)}];};
  await assert.rejects(signOwnershipChallenge(provider,account('address-a'),challenge,{now:()=>100,stillCurrent:()=>current}),{code:'account_changed'});
});

test('wallet account events are observed and unsubscribed',()=>{
  const provider=wallet();let listener;let changed=0;let unsubscribed=false;
  provider.features['standard:events']={on:(type,handler)=>{assert.equal(type,'change');listener=handler;return ()=>{unsubscribed=true;};}};
  const off=observeWallet(provider,()=>{changed+=1;});
  listener({accounts:[]});listener({name:'Unrelated'});
  assert.equal(changed,1);off();assert.equal(unsubscribed,true);
});

test('nested mobile cancellation and timeout errors keep safe application codes',async()=>{
  for(const [code,expected] of [['ERROR_ASSOCIATION_CANCELLED','wallet_rejected'],[-3,'wallet_rejected'],['ERROR_SESSION_TIMEOUT','wallet_timeout'],['ERROR_SESSION_CLOSED','wallet_timeout']]){
    const provider=wallet();
    provider.features['standard:connect'].connect=async()=>{throw new Error('Adapter request failed',{cause:new Error('Protocol failed',{cause:Object.assign(new Error('Request ended'),{code})})});};
    await assert.rejects(connectWallet(provider),{code:expected});
  }
});

test('a provider named Mobile Wallet Adapter cannot bypass exact-message validation',async()=>{
  const provider=wallet();provider.name='Mobile Wallet Adapter';
  provider.features['solana:signMessage'].signMessage=async({message})=>{
    const signature=new Uint8Array(64).fill(9);
    return [{signedMessage:new Uint8Array([...message,...signature]),signature}];
  };
  await assert.rejects(signOwnershipChallenge(provider,account('address-a'),challenge,{now:()=>100}),{code:'signed_message_mismatch'});
});
