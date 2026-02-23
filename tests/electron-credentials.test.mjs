import assert from 'node:assert/strict';
import {
  issueIntentToken,
  listContexts,
  revokeContext,
  _tokenStoreSize,
} from '../electron/credentials.mjs';

const t1 = issueIntentToken('shopping', 'nike.com');
const t2 = issueIntentToken('shopping', 'nike.com');
assert.equal(typeof t1, 'string');
assert.equal(t1.length > 10, true);
assert.equal(t1, t2);

const t3 = issueIntentToken('research', 'duckduckgo.com');
assert.notEqual(t3, t1);

const t4 = issueIntentToken('ShoPping!!', 'NIKE.COM');
assert.equal(typeof t4, 'string');
assert.equal(t4.length > 10, true);

const contexts = listContexts();
assert.equal(contexts.includes('shopping'), true);
assert.equal(contexts.includes('research'), true);

revokeContext('shopping');
const after = listContexts();
assert.equal(after.includes('shopping'), false);
assert.equal(after.includes('research'), true);
assert.equal(_tokenStoreSize() >= 1, true);

const t5 = issueIntentToken('shopping', 'nike.com');
assert.notEqual(t5, t1);

console.log('electron credential tests: ok');
