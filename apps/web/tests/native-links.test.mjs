import test from 'node:test';
import assert from 'node:assert/strict';
import {installNativeLinks,deepLinkTarget} from '../public/native-links.mjs';

const origin='https://forecast.eastsea.xyz';

test('deepLinkTarget accepts only same-origin app paths',()=>{
  assert.equal(deepLinkTarget('https://forecast.eastsea.xyz/forecasts/f_1?x=1',origin),'https://forecast.eastsea.xyz/forecasts/f_1?x=1');
  assert.equal(deepLinkTarget('https://forecast.eastsea.xyz/creators/u_1',origin),'https://forecast.eastsea.xyz/creators/u_1');
  assert.equal(deepLinkTarget('https://evil.example/forecasts/f_1',origin),null);
  assert.equal(deepLinkTarget('javascript:alert(1)',origin),null);
  assert.equal(deepLinkTarget('not a url',origin),null);
  assert.equal(deepLinkTarget('',origin),null);
});

test('installNativeLinks routes the launch url and later opens through navigate',async()=>{
  const listeners={};const navigated=[];
  const root={location:{origin},Capacitor:{Plugins:{App:{
    addListener(name,fn){listeners[name]=fn;return Promise.resolve({remove(){}});},
    async getLaunchUrl(){return {url:'https://forecast.eastsea.xyz/forecasts/f_launch'};},
  }}}};
  assert.equal(installNativeLinks(url=>navigated.push(url),root),true);
  await new Promise(resolve=>setTimeout(resolve,0));
  assert.deepEqual(navigated,['https://forecast.eastsea.xyz/forecasts/f_launch']);
  listeners.appUrlOpen({url:'https://forecast.eastsea.xyz/creators/u_2'});
  listeners.appUrlOpen({url:'https://evil.example/creators/u_2'});
  assert.deepEqual(navigated,['https://forecast.eastsea.xyz/forecasts/f_launch','https://forecast.eastsea.xyz/creators/u_2']);
});

test('installNativeLinks is a no-op outside the shell',()=>{
  assert.equal(installNativeLinks(()=>{},{location:{origin}}),false);
  assert.equal(installNativeLinks(()=>{},{location:{origin},Capacitor:{Plugins:{}}}),false);
});
