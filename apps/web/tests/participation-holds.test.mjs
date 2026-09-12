import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {forecastIsOpen,escapeHtml,safeExternalUrl} from '../public/lib.mjs';
import {coreMessages} from '../public/locales/core.mjs';
import {uiMessages} from '../public/locales/ui.mjs';

const source=readFileSync(new URL('../public/app.js',import.meta.url),'utf8');
const begin=source.indexOf('function participationNotice('),end=source.indexOf('\nfunction stateBadge(',begin);
const notice=new Function('safeExternalUrl','esc','t','icon',`${source.slice(begin,end)};return participationNotice;`);

test('a participation hold closes the shared gate without rewriting OPEN or the deadline',()=>{
  const forecast={state:'OPEN',openAt:10,closeAt:100};
  assert.equal(forecastIsOpen(forecast,50),true);
  const held={...forecast,participationHold:{reason:'known_outcome_review'}};
  assert.equal(forecastIsOpen(held,50),false);
  assert.equal(held.state,'OPEN');assert.equal(held.closeAt,100);
  assert.equal(forecastIsOpen({...held,participationHold:null},50),true);
});

test('hold notice has four localized versions, safe evidence links and no premature final outcome',()=>{
  for(const lang of ['en','ko','ja','zh-Hant']){
    const t=key=>uiMessages[lang][key];
    const render=notice(safeExternalUrl,escapeHtml,t,()=>'<svg></svg>');
    const html=render({participationHold:{evidenceUrl:'https://example.org/announcement?a=1&b=2'}});
    assert.ok(html.includes(t('ui.participationHeld')));
    assert.ok(html.includes(escapeHtml(t('ui.participationReviewHint'))));
    assert.match(html,/rel="noopener noreferrer"/);
    assert.match(html,/a=1&amp;b=2/);
    assert.ok(coreMessages[lang]['error.participation_on_hold']);
    assert.ok(coreMessages[lang]['error.participation_hold_changed']);
    assert.doesNotMatch(render({participationHold:{evidenceUrl:'javascript:alert(1)'}}),/href=/);
  }
});
