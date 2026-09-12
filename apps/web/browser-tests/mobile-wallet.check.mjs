/** Explicit browser regression: uses an existing Playwright installation, no added runtime dependency. */
import http from 'node:http';
import {readFile} from 'node:fs/promises';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {fileURLToPath} from 'node:url';
import {dirname,resolve} from 'node:path';
const require=createRequire(import.meta.url);
const modulePath=process.env.FORECAST_PLAYWRIGHT_MODULE;
if(!modulePath)throw new Error('Set FORECAST_PLAYWRIGHT_MODULE to an installed playwright-core package; run scripts/build_mobile_wallet.mjs first');
const {chromium}=require(modulePath);
const root=resolve(dirname(fileURLToPath(import.meta.url)),'../../..');
const metadata=JSON.parse(await readFile(root+'/tmp/mobile-wallet-assets/csp.json'));
const csp="default-src 'self'; script-src 'self'; style-src 'self'; style-src-elem 'self' "+metadata.styleElementHashes.join(' ')+"; style-src-attr 'unsafe-hashes' "+metadata.styleAttributeHashes.join(' ')+"; img-src 'self' data:; connect-src 'self' "+metadata.connectSources.join(' ')+"; base-uri 'self'; object-src 'none'; upgrade-insecure-requests";
const server=http.createServer(async(req,res)=>{
  try{
    res.setHeader('Content-Security-Policy',csp);
    if(req.url==='/'){res.setHeader('Content-Type','text/html');res.end('<!doctype html><html><head><meta charset="utf-8"></head><body><main>MWA SDK test</main></body></html>');return;}
    const path=req.url==='/mobile-wallet-sdk.mjs'?root+'/tmp/mobile-wallet-assets/mobile-wallet-sdk.mjs':root+'/apps/web/public'+req.url;
    res.setHeader('Content-Type',req.url.endsWith('.json')?'application/json':'text/javascript');res.end(await readFile(path));
  }catch{res.statusCode=404;res.end();}
});
await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
const browser=await chromium.launch({executablePath:process.env.FORECAST_CHROME_EXECUTABLE,headless:true,env:{...process.env,TMPDIR:root+'/tmp'}});
try{
  const page=await browser.newPage({userAgent:'Mozilla/5.0 (Linux; Android 16) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Mobile Safari/537.36',viewport:{width:400,height:797}});
  await page.addInitScript(()=>{
    window.violations=[];document.addEventListener('securitypolicyviolation',e=>window.violations.push({directive:e.violatedDirective,blocked:e.blockedURI,sample:e.sample}));
    const original=Element.prototype.attachShadow;
    window.testShadowRoots=[];
    Element.prototype.attachShadow=function(options){const result=original.call(this,options);window.testShadowRoots.push(result);return result;};
    navigator.permissions.query=async()=>({state:'prompt',onchange:null});
  });
  await page.goto('http://127.0.0.1:'+server.address().port);
  const result=await page.evaluate(async()=>{
    const wallet=await import('/wallet.mjs');const mobile=await import('/mobile-wallet.mjs');
    const discovery=wallet.discoverWallets();await mobile.initializeMobileWallet();
    const sdkWallet=discovery.get().find(w=>mobile.isLocalMobileWallet(w));
    if(!sdkWallet)throw new Error('Actual mobile wallet was not registered');
    const message='Forecast login. Single use nonce.';
    const account={address:'test-address',chains:['solana:devnet'],features:['solana:signMessage']};
    const proxy=new Proxy(sdkWallet,{get(target,key){
      if(key==='features')return {
        'standard:connect':{connect:async()=>({accounts:[account]})},
        'solana:signMessage':{signMessage:async({message})=>{
          const signature=new Uint8Array(64).fill(21);
          return [{signedMessage:new Uint8Array([...message,...signature]),signature}];
        }},
      };
      return Reflect.get(target,key,target);
    }});
    const proof=await wallet.signOwnershipChallenge(proxy,account,{address:account.address,chain:'solana:devnet',message,expiresAt:Date.now()+60000,challengeId:'test-challenge'});
    const keys=await crypto.subtle.generateKey('Ed25519',false,['sign','verify']);
    const detachedAccount={...account,publicKey:new Uint8Array(await crypto.subtle.exportKey('raw',keys.publicKey))};
    const retained=new TextEncoder().encode(message);
    const detachedSignature=new Uint8Array(await crypto.subtle.sign('Ed25519',keys.privateKey,retained));
    const detached=new Proxy(sdkWallet,{get(target,key){
      if(key==='features')return {
        'standard:connect':{connect:async()=>({accounts:[detachedAccount]})},
        'solana:signMessage':{signMessage:async()=>[{signedMessage:detachedSignature,signature:detachedSignature}]},
      };
      return Reflect.get(target,key,target);
    }});
    const request={address:account.address,chain:'solana:devnet',message,expiresAt:Date.now()+60000,challengeId:'detached-challenge'};
    const detachedProof=await wallet.signOwnershipChallenge(detached,detachedAccount,request);
    let alteredRejected=false,staleRejected=false;
    try{await wallet.signOwnershipChallenge(detached,detachedAccount,{...request,message:'altered '+message});}catch(error){alteredRejected=error.code==='signed_message_mismatch';}
    let checks=0;
    try{await wallet.signOwnershipChallenge(detached,detachedAccount,request,{stillCurrent:()=>++checks<3});}catch(error){staleRejected=error.code==='account_changed';}
    window.connection=sdkWallet.features['standard:connect'].connect().catch(e=>{window.connectionError=e.message;});
    await new Promise(resolve=>setTimeout(resolve,150));
    return {registered:discovery.get().length,proof,detachedProof,alteredRejected,staleRejected,styles:window.testShadowRoots.map(root=>({style:root.querySelector('style')?.textContent.length,display:getComputedStyle(root.querySelector('.mobile-wallet-adapter-embedded-modal-container')).display})),violations:window.violations};
  });
  assert.equal(result.registered,1);assert.equal(result.proof.challengeId,'test-challenge');assert.equal(Buffer.from(result.proof.signature,'base64').length,64);
  assert.equal(result.detachedProof.challengeId,'detached-challenge');assert.ok(result.alteredRejected);assert.ok(result.staleRejected);
  assert.ok(result.styles.length>0);assert.ok(result.styles.every(value=>value.display==='flex'));assert.deepEqual(result.violations,[]);
  await page.evaluate(()=>{for(const root of window.testShadowRoots)root.querySelector('[data-modal-close]')?.click();});
  await page.waitForTimeout(75);
  console.log(JSON.stringify({actualSdkRegistered:true,actualSdkInstanceNormalization:true,detachedSignatureVerified:true,alteredMessageRejected:result.alteredRejected,staleAccountRejected:result.staleRejected,cspViolations:result.violations,modalStyles:result.styles,cancellation:await page.evaluate(()=>window.connectionError)}));
}finally{await browser.close();await new Promise(resolve=>server.close(resolve));}
