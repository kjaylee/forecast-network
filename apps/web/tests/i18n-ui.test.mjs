import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {execFileSync} from 'node:child_process';
import {captureLocaleState,restoreLocaleState} from '../public/locale-state.mjs';

const source=readFileSync(new URL('../public/app.js',import.meta.url),'utf8');
test('browser translation cannot opt in the signed and canonical app surface',()=>{
  const index=readFileSync(new URL('../public/index.html',import.meta.url),'utf8');
  assert.match(index,/<html\b[^>]*translate="no"[^>]*class="notranslate"/);
  assert.doesNotMatch(source,/translate\s*=\s*true|setAttribute\(['"]translate['"],\s*['"]yes/);
});
test('the complete browser entrypoint parses as an ES module',()=>{
  assert.doesNotThrow(()=>execFileSync(process.execPath,['--input-type=module','--check'],{input:source,stdio:['pipe','pipe','pipe']}));
});

test('locale repaint retains typed input, recovery acknowledgment and focus only in memory',()=>{
  function node(id,value,extra={}){return {id,value,name:id,checked:false,readOnly:false,type:'text',classList:{contains:()=>false},focus(){this.focused=true;},setSelectionRange(a,b){this.cursor=[a,b];},...extra};}
  const question=node('question','한글 日本語 繁體中文');
  const acknowledgment=node('recovery-saved','on',{type:'checkbox',checked:true});
  const locale=node('header-language','ko',{classList:{contains:name=>name==='language-select'}});
  const caption=node('profile-caption','old caption',{readOnly:true});
  const controls=[question,acknowledgment,locale,caption];
  const detail={open:true};
  const document={activeElement:{id:'question',selectionStart:2,selectionEnd:4},
    querySelectorAll(selector){return selector==='input,textarea,select'?controls:selector==='details'?[detail]:[];},
    getElementById(id){return controls.find(item=>item.id===id);}};
  const snapshot=captureLocaleState(document);
  assert.deepEqual(snapshot.controls.map(item=>item.id),['question','recovery-saved']);
  question.value='';acknowledgment.checked=false;detail.open=false;caption.value='new caption';locale.value='ja';
  restoreLocaleState(document,snapshot);
  assert.equal(question.value,'한글 日本語 繁體中文');assert.ok(acknowledgment.checked);assert.ok(detail.open);
  assert.equal(caption.value,'new caption');assert.equal(locale.value,'ja');assert.deepEqual(question.cursor,[2,4]);
});

for(const change of ['account','route'])test(`locale repaint discards old private inputs after a concurrent ${change} change`,async()=>{
  const begin=source.indexOf('async function changeLanguage('),end=source.indexOf('\nfunction isAppPath',begin);
  const body=source.slice(begin,end);assert.ok(begin>=0&&end>begin);
  const state={user:{id:'user-a'}},pointsState={epoch:1},location={pathname:'/profile',search:''};
  let restored=0;const main={inert:true};
  const repaint=async()=>{if(change==='account'){state.user={id:'user-b'};pointsState.epoch++;}else location.pathname='/create';};
  const make=new Function('state','pointsState','location','window','document','languageBusy','getLocale','captureLocaleState','setLocale','syncLanguageControls','renderRoute','restoreLocaleState','main',`let localePainting=false;${body};return changeLanguage;`);
  const run=make(state,pointsState,location,{scrollX:0,scrollY:0},{},()=>false,()=>'en',()=>({controls:['private input']}),()=>{},()=>{},repaint,()=>{restored++;},()=>main);
  await run('ko');assert.equal(restored,0);assert.equal(main.inert,false);
});
