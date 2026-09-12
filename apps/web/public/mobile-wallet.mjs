/** Local Android wallet registration. Authorization never leaves page memory. */
export function createMobileWalletRegistration({loadSdk,environment}){
  let pending;
  let LocalWallet;
  return Object.freeze({
    isLocalWallet(wallet){return Boolean(LocalWallet && wallet instanceof LocalWallet);},
    initialize(){
      const {secure,userAgent,origin}=environment();
      if(!secure || !/Android/i.test(userAgent) || !/Chrome\//i.test(userAgent) || /; wv\)|WebView/i.test(userAgent))return Promise.resolve(false);
      if(pending)return pending;
      pending=(async()=>{
        const sdk=await loadSdk();
        let authorization;
        sdk.registerMwa({
          appIdentity:{name:'Forecast',uri:origin,icon:'favicon.svg'},
          authorizationCache:{
            async get(){return authorization;},
            async set(value){authorization=value;},
            async clear(){authorization=undefined;},
          },
          chains:['solana:devnet'],
          chainSelector:{async select(chains){
            if(!chains.includes('solana:devnet'))throw new Error('Devnet unavailable');
            return 'solana:devnet';
          }},
          async onWalletNotFound(){throw Object.assign(new Error('No compatible wallet is available'),{code:'ERROR_WALLET_NOT_FOUND'});},
        });
        LocalWallet=sdk.LocalSolanaMobileWalletAdapterWallet;
        return true;
      })().catch(error=>{pending=undefined;throw error;});
      return pending;
    },
  });
}

const registration=createMobileWalletRegistration({
  loadSdk:()=>import('./mobile-wallet-sdk.mjs'),
  environment:()=>({secure:globalThis.isSecureContext===true,userAgent:globalThis.navigator?.userAgent || '',origin:globalThis.location?.origin}),
});
export const initializeMobileWallet=()=>registration.initialize();
export const isLocalMobileWallet=wallet=>registration.isLocalWallet(wallet);

/** SDK 0.6.0 exposes the MWA payload. Some wallets return only its signature;
 * the official Android client supports this case too. Never invent a message:
 * a detached response must cryptographically verify the exact requested bytes.
 */
export async function normalizeMobileSignedMessage(output,message,publicKey){
  const signed=output?.signedMessage;
  const signature=output?.signature;
  if(!(message instanceof Uint8Array) || !(signed instanceof Uint8Array) || !(signature instanceof Uint8Array) || signature.length!==64)return null;
  if(signed.length===64 && signature.every((byte,index)=>signed[index]===byte)){
    if(!(publicKey instanceof Uint8Array) || publicKey.length!==32)return null;
    try{
      const key=await crypto.subtle.importKey('raw',publicKey,'Ed25519',false,['verify']);
      return await crypto.subtle.verify('Ed25519',key,signature,message)?message:null;
    }catch{return null;}
  }
  if(signed.length!==message.length+64)return null;
  if(!message.every((byte,index)=>signed[index]===byte) || !signature.every((byte,index)=>signed[message.length+index]===byte))return null;
  return signed.subarray(0,message.length);
}
