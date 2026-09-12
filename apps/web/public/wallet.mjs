/** Minimal Wallet Standard message-signing adapter. Never requests transactions.
 * Protocol: wallet-standard/core/app and anza-xyz/wallet-standard solana:signMessage.
 */
import {t} from './i18n.mjs';
import {isLocalMobileWallet,normalizeMobileSignedMessage} from './mobile-wallet.mjs';
export class WalletError extends Error {
  constructor(message,code='wallet_error'){
    const key=code==='account_changed'?'error.wallet_account_changed':'error.'+code;
    super(message);this.name='WalletError';this.code=code;this.translationKey=key;
  }
}

export function isSupportedWallet(wallet){
  return Boolean(wallet && typeof wallet.name==='string' &&
    typeof wallet.features?.['standard:connect']?.connect==='function' &&
    typeof wallet.features?.['solana:signMessage']?.signMessage==='function' &&
    Array.isArray(wallet.chains) && wallet.chains.some(chain=>String(chain).startsWith('solana:')));
}

export function solanaAccounts(accounts){
  return (Array.isArray(accounts)?accounts:[]).filter(account=>
    typeof account.address==='string' && account.address.length>0 &&
    Array.isArray(account.chains) && account.chains.some(chain=>String(chain).startsWith('solana:')) &&
    Array.isArray(account.features) && account.features.includes('solana:signMessage'));
}

export function preferredAccount(accounts){
  const compatible=solanaAccounts(accounts);
  return compatible.find(account=>account.chains.includes('solana:devnet')) || compatible[0] || null;
}

export function discoverWallets(target=window){
  const wallets=new Set();
  const listeners=new Set();
  const notify=()=>listeners.forEach(listener=>listener([...wallets]));
  const register=(...candidates)=>{
    const added=candidates.filter(wallet=>isSupportedWallet(wallet)&&!wallets.has(wallet));
    added.forEach(wallet=>wallets.add(wallet));
    if(added.length)notify();
    return ()=>{added.forEach(wallet=>wallets.delete(wallet));if(added.length)notify();};
  };
  const api=Object.freeze({register});
  const onRegister=event=>{if(typeof event.detail==='function'){try{event.detail(api);}catch{/* An incompatible provider must not break the app. */}}};
  // Listen first: providers may register synchronously in response to app-ready.
  target.addEventListener('wallet-standard:register-wallet',onRegister);
  target.dispatchEvent(new CustomEvent('wallet-standard:app-ready',{detail:api}));
  return Object.freeze({
    get:()=>[...wallets],
    subscribe(listener){listeners.add(listener);return ()=>listeners.delete(listener);},
    destroy(){target.removeEventListener('wallet-standard:register-wallet',onRegister);listeners.clear();wallets.clear();},
  });
}

function providerError(error){
  const causes=[];let current=error;
  while(current && causes.length<6 && !causes.includes(current)){causes.push(current);current=current.cause;}
  if(causes.some(cause=>cause?.code===4001 || cause?.code===-3 || ['USER_REJECTED_REQUEST','ERROR_ASSOCIATION_CANCELLED'].includes(cause?.code) || cause?.name==='AbortError' || /reject|cancel|declin/i.test(cause?.message || ''))){
    return new WalletError(t('error.wallet_rejected'),'wallet_rejected');
  }
  if(causes.some(cause=>['ERROR_SESSION_TIMEOUT','ERROR_SESSION_CLOSED'].includes(cause?.code)))return new WalletError(t('error.wallet_timeout'),'wallet_timeout');
  return error instanceof WalletError?error:new WalletError(t('error.wallet_error'),'wallet_error');
}

async function walletRequest(request){
  let timer;
  try{return await Promise.race([request,new Promise((resolve,reject)=>{
    timer=setTimeout(()=>reject(new WalletError(t('error.wallet_timeout'),'wallet_timeout')),120000);
  })]);}finally{clearTimeout(timer);}
}

export async function connectWallet(wallet){
  if(!isSupportedWallet(wallet))throw new WalletError(t('error.wallet_unsupported'),'wallet_unsupported');
  try{
    const result=await walletRequest(wallet.features['standard:connect'].connect());
    const accounts=solanaAccounts(result?.accounts || wallet.accounts);
    if(!accounts.length)throw new WalletError(t('error.account_unavailable'),'account_unavailable');
    return accounts;
  }catch(error){throw providerError(error);}
}

export function observeWallet(wallet,onChange){
  const events=wallet.features?.['standard:events'];
  return typeof events?.on==='function'?events.on('change',event=>{
    if(event && (Object.hasOwn(event,'accounts') || Object.hasOwn(event,'chains') || Object.hasOwn(event,'features')))onChange(event);
  }):()=>{};
}

function bytesEqual(left,right){
  return left instanceof Uint8Array && right instanceof Uint8Array && left.length===right.length && left.every((byte,index)=>byte===right[index]);
}

export async function signOwnershipChallenge(wallet,account,challenge,{now=Date.now,stillCurrent=()=>true}={}){
  if(!isSupportedWallet(wallet) || !solanaAccounts([account]).length)throw new WalletError(t('error.account_unavailable'),'account_unavailable');
  if(challenge.address!==account.address || challenge.chain!=='solana:devnet' || typeof challenge.message!=='string')throw new WalletError(t('error.challenge_mismatch'),'challenge_mismatch');
  if(!Number.isFinite(challenge.expiresAt) || challenge.expiresAt<=now())throw new WalletError(t('error.challenge_expired'),'challenge_expired');
  if(!stillCurrent())throw new WalletError(t('error.wallet_account_changed'),'account_changed');
  const bytes=new TextEncoder().encode(challenge.message);
  try{
    const outputs=await walletRequest(wallet.features['solana:signMessage'].signMessage({account,message:bytes}));
    if(!stillCurrent())throw new WalletError(t('error.wallet_account_changed'),'account_changed');
    const signed=outputs?.[0];
    const signedMessage=isLocalMobileWallet(wallet)?await normalizeMobileSignedMessage(signed,bytes,account.publicKey):signed?.signedMessage;
    if(!stillCurrent())throw new WalletError(t('error.wallet_account_changed'),'account_changed');
    if(outputs?.length!==1 || !bytesEqual(signedMessage,bytes))throw new WalletError(t('error.signed_message_mismatch'),'signed_message_mismatch');
    if(!(signed.signature instanceof Uint8Array) || signed.signature.length!==64 || (signed.signatureType && signed.signatureType!=='ed25519'))throw new WalletError(t('error.signature_invalid'),'signature_invalid');
    if(challenge.expiresAt<=now())throw new WalletError(t('error.challenge_expired'),'challenge_expired');
    return {challengeId:challenge.challengeId,address:account.address,signature:btoa(String.fromCharCode(...signed.signature))};
  }catch(error){throw providerError(error);}
}

export async function disconnectWallet(wallet){
  const feature=wallet?.features?.['standard:disconnect'];
  if(typeof feature?.disconnect==='function')await feature.disconnect();
}
