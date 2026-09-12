import test from 'node:test';
import assert from 'node:assert/strict';
import {createMobileWalletRegistration,normalizeMobileSignedMessage} from '../public/mobile-wallet.mjs';

const android={secure:true,userAgent:'Mozilla/5.0 (Linux; Android 16) Chrome/152.0.0.0 Mobile Safari/537.36',origin:'https://forecast.example'};
test('mobile initialization is local, Devnet-only, idempotent and does not persist authorization',async()=>{
  let configuration;let calls=0;
  class LocalWallet{}
  const registration=createMobileWalletRegistration({environment:()=>android,loadSdk:async()=>({LocalSolanaMobileWalletAdapterWallet:LocalWallet,registerMwa:config=>{calls++;configuration=config;}})});
  assert.deepEqual(await Promise.all([registration.initialize(),registration.initialize()]),[true,true]);
  assert.equal(calls,1);
  assert.equal(configuration.appIdentity.uri,android.origin);
  assert.deepEqual(configuration.chains,['solana:devnet']);
  assert.equal('nostrRelay' in configuration,false);
  assert.equal('remoteHostAuthority' in configuration,false);
  assert.equal(await configuration.chainSelector.select(['solana:devnet']),'solana:devnet');
  await assert.rejects(configuration.chainSelector.select(['solana:mainnet']));
  const authorization={auth_token:'test-only-token'};
  assert.equal(await configuration.authorizationCache.get(),undefined);
  await configuration.authorizationCache.set(authorization);
  assert.equal(await configuration.authorizationCache.get(),authorization);
  await configuration.authorizationCache.clear();
  assert.equal(await configuration.authorizationCache.get(),undefined);
  assert.equal(registration.isLocalWallet(new LocalWallet()),true);
  assert.equal(registration.isLocalWallet({name:'Mobile Wallet Adapter'}),false);
  await assert.rejects(configuration.onWalletNotFound(),{code:'ERROR_WALLET_NOT_FOUND'});
});

test('unsupported environments never load the mobile SDK',async()=>{
  for(const environment of [{...android,secure:false},{secure:true,userAgent:'Chrome/152.0.0.0 Windows'},{...android,userAgent:'Android Firefox/149'},{...android,userAgent:'Android; wv) Chrome/152'}]){
    let called=false;
    const registration=createMobileWalletRegistration({environment:()=>environment,loadSdk:async()=>{called=true;throw new Error('must not load');}});
    assert.equal(await registration.initialize(),false);
    assert.equal(called,false);
  }
});

test('failed bundle loading can be retried without registering twice',async()=>{
  let calls=0;
  const registration=createMobileWalletRegistration({environment:()=>android,loadSdk:async()=>{if(++calls===1)throw new Error('offline');return {registerMwa(){},LocalSolanaMobileWalletAdapterWallet:class {}};}});
  await assert.rejects(registration.initialize(),/offline/);
  assert.equal(await registration.initialize(),true);
  assert.equal(await registration.initialize(),true);
  assert.equal(calls,2);
});

test('MWA combined signed payload is normalized only with exact prefix, suffix and length',async()=>{
  const message=new TextEncoder().encode('One-time server nonce');
  const signature=new Uint8Array(64).fill(17);
  const signedMessage=new Uint8Array([...message,...signature]);
  assert.deepEqual(await normalizeMobileSignedMessage({signedMessage,signature},message),message);
  for(const value of [signedMessage.subarray(1),new Uint8Array([...signedMessage,0]),new Uint8Array([0,...signedMessage.subarray(1)])])assert.equal(await normalizeMobileSignedMessage({signedMessage:value,signature},message),null);
  assert.equal(await normalizeMobileSignedMessage({signedMessage,signature:new Uint8Array(64)},message),null);
  assert.equal(await normalizeMobileSignedMessage({signedMessage,signature:signature.subarray(1)},message),null);
});

test('signature-only mobile responses require Ed25519 proof over the exact original bytes',async()=>{
  const pair=await crypto.subtle.generateKey('Ed25519',false,['sign','verify']);
  const publicKey=new Uint8Array(await crypto.subtle.exportKey('raw',pair.publicKey));
  const message=new TextEncoder().encode('Forecast wallet sign-in\nNonce: exact-original');
  const signature=new Uint8Array(await crypto.subtle.sign('Ed25519',pair.privateKey,message));
  const output={signedMessage:signature.slice(),signature};
  assert.deepEqual(await normalizeMobileSignedMessage(output,message,publicKey),message);
  assert.equal(await normalizeMobileSignedMessage(output,new Uint8Array([32,...message]),publicKey),null);
  assert.equal(await normalizeMobileSignedMessage(output,message,new Uint8Array(32)),null);
  assert.equal(await normalizeMobileSignedMessage(output,message),null);
  const altered=signature.slice();altered[0]^=1;
  assert.equal(await normalizeMobileSignedMessage({signedMessage:altered,signature:altered},message,publicKey),null);
  assert.equal(await normalizeMobileSignedMessage({signedMessage:altered,signature},message,publicKey),null);
  assert.equal(await normalizeMobileSignedMessage({signedMessage:new Uint8Array([...signature,0]),signature},message,publicKey),null);
});
