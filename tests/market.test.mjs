import { test, afterEach } from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

const source = await readFile(new URL('../web/_worker.js', import.meta.url), 'utf8');
const worker = (await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`)).default;
const originalFetch = globalThis.fetch;
afterEach(() => { globalThis.fetch = originalFetch; });
const request = () => new Request('https://example.test/api/market');

test('aggregates breadth without double counting the growth index', async () => {
  const rows = [
    { f12: '000001', f14: '上证指数', f2: 3000, f3: 1, f104: 10, f105: 20, f106: 2, f124: 1700000000 },
    { f12: '399001', f14: '深证成指', f2: 10000, f3: -1, f104: 30, f105: 40, f106: 4, f124: 1700000000 },
    { f12: '399006', f14: '创业板指', f2: 2000, f3: 0, f104: 999, f105: 999, f106: 999, f124: 1700000000 },
    { f12: '899050', f14: '北证50', f2: 900, f3: 1, f104: 5, f105: 6, f106: 1, f124: 1700000000 },
  ];
  globalThis.fetch = async (url) => Response.json({ rc: 0, data: { diff: url.includes('ulist') ? rows : [{ f14: '板块', f3: 1.2, f124: 1700000000 }] } });
  const response = await worker.fetch(request(), {});
  const data = await response.json();
  assert.equal(response.status, 200);
  assert.deepEqual(data.breadth, { up: 45, down: 66, flat: 7 });
  assert.equal(data.quote_time, 1700000000);
});

test('returns an explicit failure when all providers fail', async () => {
  globalThis.fetch = async () => { throw new Error('network'); };
  const response = await worker.fetch(request(), {});
  assert.equal(response.status, 503);
  assert.equal(response.headers.get('cache-control'), 'no-store');
});
