import assert from 'node:assert/strict';
import test from 'node:test';

import {shareCode, shareUrl} from '../public/share-link.mjs';

const code = 'a10b6a16c69b05f9332cd3d7ec099ef0';
const question = 'https://forecast.example/forecasts/f_first?choose=YES';

test('an opaque code augments the question link and survives the recipient URL', () => {
  const shared = new URL(shareUrl(question, code));
  assert.equal(shared.pathname, '/forecasts/f_first');
  assert.equal(shared.searchParams.get('choose'), 'YES');
  assert.equal(shareCode(shared.search), code);
});

test('missing, ambiguous and invalid codes preserve ordinary sharing', () => {
  assert.equal(shareUrl(question, null), question);
  assert.equal(shareUrl(question, 'not-a-code'), question);
  assert.equal(shareCode('?choose=YES'), null);
  assert.equal(shareCode('?via=' + code + '&via=' + code), null);
  assert.equal(shareCode('?via=' + code.toUpperCase()), null);
});
