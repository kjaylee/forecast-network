/**
 * Native share bridge for the Android shell.
 *
 * Android WebView does not implement the Web Share API, so the share dialog's
 * "Share image" button never appears inside the Seeker app. When the Capacitor
 * shell exposes the official Share and Filesystem plugins, this installs
 * navigator.share / navigator.canShare equivalents: a PNG is written to the app
 * cache and handed to the Android share sheet as a content uri. Only the page's
 * own payload (title, caption, link, rendered card) ever reaches the plugins.
 */
const SHAREABLE_TYPES=new Set(['image/png','image/jpeg']);

export function nativeSharePlugins(root=globalThis){
  const plugins=root.Capacitor?.Plugins;
  if(!plugins?.Share||typeof plugins.Share.share!=='function')return null;
  const filesystem=plugins.Filesystem&&typeof plugins.Filesystem.writeFile==='function'?plugins.Filesystem:null;
  return {share:plugins.Share,filesystem};
}

async function base64(file){
  const bytes=new Uint8Array(await file.arrayBuffer());
  let binary='';
  for(let i=0;i<bytes.length;i+=0x8000)binary+=String.fromCharCode.apply(null,bytes.subarray(i,i+0x8000));
  return btoa(binary);
}

function cancelled(error){
  return /cancel/i.test(String(error?.message||error));
}

export function createNativeShare({share,filesystem}){
  return {
    canShare(data={}){
      const files=data.files||[];
      if(!files.length)return true;
      return Boolean(filesystem)&&files.every(file=>SHAREABLE_TYPES.has(file.type));
    },
    async share(data={}){
      const {title,text,url,files=[]}=data;
      const uris=[];
      for(const file of files){
        const written=await filesystem.writeFile({path:`share/${file.name}`,data:await base64(file),directory:'CACHE',recursive:true});
        uris.push(written.uri);
      }
      const payload={...(title?{title,dialogTitle:title}:{}),...(text?{text}:{}),...(url?{url}:{}),...(uris.length?{files:uris}:{})};
      try{
        await share.share(payload);
      }catch(error){
        // The page already treats AbortError as "the user closed the sheet".
        if(cancelled(error))throw new DOMException('Share cancelled','AbortError');
        throw error;
      }
    },
  };
}

export function installNativeShare(root=globalThis){
  const plugins=nativeSharePlugins(root);
  if(!plugins||!root.navigator||typeof root.navigator.share==='function')return false;
  const bridge=createNativeShare(plugins);
  Object.defineProperty(root.navigator,'share',{value:bridge.share,configurable:true,writable:true});
  Object.defineProperty(root.navigator,'canShare',{value:bridge.canShare,configurable:true,writable:true});
  return true;
}
