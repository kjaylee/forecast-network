import test from 'node:test';
import assert from 'node:assert/strict';
import {createNativeShare,installNativeShare,nativeSharePlugins} from '../public/native-share.mjs';

function plugins(overrides={}){
  const calls=[];
  return {calls,
    share:{async share(args){calls.push(['share',args]);return {activityType:'com.example'};}},
    filesystem:{async writeFile(args){calls.push(['writeFile',args]);return {uri:'file:///cache/'+args.path};}},
    ...overrides};
}
const png=()=>new File([new Uint8Array([137,80,78,71])],'record.png',{type:'image/png'});

test('nativeSharePlugins only reports a shell that exposes the Share plugin',()=>{
  assert.equal(nativeSharePlugins({}),null);
  assert.equal(nativeSharePlugins({Capacitor:{Plugins:{}}}),null);
  const found=nativeSharePlugins({Capacitor:{Plugins:{Share:{share(){}},Filesystem:{writeFile(){}}}}});
  assert.ok(found.share&&found.filesystem);
});

test('links share without touching the filesystem',async()=>{
  const p=plugins();const bridge=createNativeShare(p);
  await bridge.share({title:'Forecast',text:'caption',url:'https://forecast.eastsea.xyz/forecasts/f_1'});
  assert.deepEqual(p.calls.map(([name])=>name),['share']);
  assert.equal(p.calls[0][1].url,'https://forecast.eastsea.xyz/forecasts/f_1');
  assert.equal(p.calls[0][1].dialogTitle,'Forecast');
  assert.ok(!('files' in p.calls[0][1]));
});

test('a PNG is written to the cache directory and shared as a file uri',async()=>{
  const p=plugins();const bridge=createNativeShare(p);
  const file=png();
  assert.equal(bridge.canShare({files:[file]}),true);
  await bridge.share({title:'My record',text:'caption',url:'https://forecast.eastsea.xyz/creators/u1',files:[file]});
  const write=p.calls.find(([name])=>name==='writeFile')[1];
  assert.equal(write.directory,'CACHE');
  assert.equal(write.path,'share/record.png');
  assert.equal(write.recursive,true);
  assert.equal(Buffer.from(write.data,'base64').toString('hex'),'89504e47');
  const share=p.calls.find(([name])=>name==='share')[1];
  assert.deepEqual(share.files,['file:///cache/share/record.png']);
});

test('canShare rejects files when the shell has no filesystem or the type is not an image',()=>{
  const noFs=createNativeShare(plugins({filesystem:null}));
  assert.equal(noFs.canShare({files:[png()]}),false);
  assert.equal(noFs.canShare({url:'https://x'}),true);
  const bridge=createNativeShare(plugins());
  assert.equal(bridge.canShare({files:[new File(['x'],'a.txt',{type:'text/plain'})]}),false);
});

test('a cancelled sheet surfaces as AbortError so the page treats it as a dismissal',async()=>{
  const bridge=createNativeShare(plugins({share:{async share(){throw new Error('Share canceled');}}}));
  await assert.rejects(bridge.share({url:'https://x'}),error=>error.name==='AbortError');
  const failing=createNativeShare(plugins({share:{async share(){throw new Error('Unsupported url');}}}));
  await assert.rejects(failing.share({url:'ftp://x'}),error=>error.name!=='AbortError');
});

test('installNativeShare defines navigator.share only inside the shell and never over a real one',()=>{
  const root={navigator:{},Capacitor:{Plugins:{Share:{share(){}},Filesystem:{writeFile(){}}}}};
  assert.equal(installNativeShare(root),true);
  assert.equal(typeof root.navigator.share,'function');
  assert.equal(typeof root.navigator.canShare,'function');
  const browser={navigator:{share(){return 'real';}},Capacitor:{Plugins:{Share:{share(){}}}}};
  assert.equal(installNativeShare(browser),false);
  assert.equal(browser.navigator.share(),'real');
  assert.equal(installNativeShare({navigator:{}}),false);
});
