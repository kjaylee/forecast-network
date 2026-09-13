/**
 * Android App Links for the shell. The WebView always boots on the site root, so a
 * link opened from a notification, a chat message or the share sheet arrives here as
 * a Capacitor `appUrlOpen` event (or as the launch url on a cold start) and is routed
 * through the page's own navigate(). Only same-origin http(s) urls are honoured.
 */
export function deepLinkTarget(url,origin){
  let parsed;
  try{parsed=new URL(String(url||''));}catch{return null;}
  if(parsed.origin!==origin||!/^https?:$/.test(parsed.protocol))return null;
  return parsed.href;
}

export function installNativeLinks(navigate,root=globalThis){
  const app=root.Capacitor?.Plugins?.App;
  if(!app||typeof app.addListener!=='function')return false;
  const origin=root.location.origin;
  const open=url=>{const target=deepLinkTarget(url,origin);if(target)navigate(target);};
  void app.addListener('appUrlOpen',event=>open(event?.url));
  if(typeof app.getLaunchUrl==='function')app.getLaunchUrl().then(result=>open(result?.url)).catch(()=>{});
  return true;
}
