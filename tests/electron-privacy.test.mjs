import assert from 'node:assert/strict';
import {
  sanitizeRequestHeaders,
  sanitizeResponseHeaders,
  stripTrackingFromUrl,
} from '../electron/privacy.mjs';

{
  const result = stripTrackingFromUrl('https://example.com/shoes?utm_source=x&gclid=1&q=puma');
  assert.equal(result.ok, true);
  assert.equal(result.changed, true);
  assert.equal(result.strippedCount, 2);
  assert.equal(result.url.includes('utm_source='), false);
  assert.equal(result.url.includes('gclid='), false);
  assert.equal(result.url.includes('q=puma'), true);
}

{
  const headers = sanitizeRequestHeaders(
    { referrer: 'https://origin.example', resourceType: 'mainFrame' },
    { Referer: 'https://origin.example' }
  );
  assert.equal(headers.Referer, undefined);
  assert.equal(headers['X-Genome-Surface'], '1');
}

{
  const headers = sanitizeResponseHeaders(
    { resourceType: 'image' },
    { 'Set-Cookie': ['a=1'], 'set-cookie': ['b=2'] }
  );
  assert.equal(headers['Set-Cookie'], undefined);
  assert.equal(headers['set-cookie'], undefined);
}

{
  const headers = sanitizeResponseHeaders(
    { resourceType: 'mainFrame' },
    { 'Set-Cookie': ['session=1'] }
  );
  assert.deepEqual(headers['Set-Cookie'], ['session=1']);
}

console.log('electron privacy tests: ok');

