/** Wallet Standard wallet backed by the native Mobile Wallet Adapter bridge.
 *
 * Inside the Android shell the page cannot use the browser MWA flow (WebViews are
 * excluded by design), so the shell exposes a Capacitor plugin `MobileWallet` that
 * runs the official Android client library. This module wraps that plugin as a
 * Wallet Standard wallet limited to devnet message signing; the rest of the app's
 * wallet code stays unchanged. No transaction feature is ever exposed.
 */
const CHAIN='solana:devnet';
const ICON='data:image/svg+xml;base64,'+btoa('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" rx="16" fill="#2456ed"/><path d="M21 46V26c0-10 7-15 19-12v8c-8-2-10 0-10 5v1h11v8H30v10Z" fill="white"/></svg>');

function fromBase64(text){
  if(typeof text!=='string')throw new Error('Native wallet returned no bytes');
  const binary=atob(text);
  return Uint8Array.from(binary,char=>char.charCodeAt(0));
}
function toBase64(bytes){return btoa(String.fromCharCode(...bytes));}
function bridgeError(error){
  const code=error?.code || error?.data?.code;
  const wrapped=new Error(error?.message || 'Native wallet request failed');
  if(code)wrapped.code=code;
  return wrapped;
}

export function nativePluginAvailable(root=globalThis){
  const capacitor=root.Capacitor;
  return Boolean(capacitor && typeof capacitor.isNativePlatform==='function' && capacitor.isNativePlatform()
    && capacitor.Plugins && capacitor.Plugins.MobileWallet);
}

/** Build the wallet object from a plugin with authorize/signMessage/deauthorize. */
export function createNativeWallet(plugin,{label='Seeker wallet'}={}){
  let accounts=[];
  const listeners=new Set();
  const emit=()=>{for(const listener of listeners){try{listener({accounts});}catch{/* listener errors must not break the wallet */}}};
  const toAccount=item=>Object.freeze({
    address:item.address,publicKey:fromBase64(item.publicKey),chains:[CHAIN],
    features:['solana:signMessage'],label:item.label || label,
  });
  const wallet={
    version:'1.0.0',name:'Seeker Wallet',icon:ICON,chains:[CHAIN],
    get accounts(){return accounts;},
    features:{
      'standard:connect':{version:'1.0.0',async connect(){
        let result;
        try{result=await plugin.authorize({chain:CHAIN});}catch(error){throw bridgeError(error);}
        const next=(result?.accounts || []).filter(item=>typeof item?.address==='string' && typeof item?.publicKey==='string').map(toAccount);
        if(!next.length)throw Object.assign(new Error('No account was authorized'),{code:'ERROR_WALLET_NOT_FOUND'});
        accounts=next;emit();
        return {accounts};
      }},
      'standard:disconnect':{version:'1.0.0',async disconnect(){
        try{await plugin.deauthorize();}catch{/* local state is cleared regardless */}
        accounts=[];emit();
      }},
      'standard:events':{version:'1.0.0',on(event,listener){
        if(event!=='change')return ()=>{};
        listeners.add(listener);return ()=>listeners.delete(listener);
      }},
      'solana:signMessage':{version:'1.0.0',async signMessage(...inputs){
        const outputs=[];
        for(const input of inputs){
          if(!(input?.message instanceof Uint8Array))throw new Error('Message bytes are required');
          if(!accounts.some(account=>account.address===input.account?.address))throw Object.assign(new Error('Account is not authorized'),{code:'ERROR_AUTHORIZATION_FAILED'});
          let result;
          try{result=await plugin.signMessage({address:input.account.address,message:toBase64(input.message)});}catch(error){throw bridgeError(error);}
          const signature=fromBase64(result?.signature);
          if(signature.length!==64)throw new Error('Native wallet returned an invalid signature');
          outputs.push({signedMessage:input.message,signature,signatureType:'ed25519'});
        }
        return outputs;
      }},
    },
  };
  return wallet;
}

/** Register the native wallet with the page's Wallet Standard discovery. */
export function registerNativeWallet(root=globalThis){
  if(!nativePluginAvailable(root))return false;
  const wallet=createNativeWallet(root.Capacitor.Plugins.MobileWallet);
  const register=api=>{api.register(wallet);};
  root.dispatchEvent(new CustomEvent('wallet-standard:register-wallet',{detail:register}));
  root.addEventListener('wallet-standard:app-ready',event=>{if(typeof event.detail?.register==='function')register(event.detail);});
  return true;
}
