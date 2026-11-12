const HOSTS = ['https://push2.eastmoney.com', 'https://push2delay.eastmoney.com'];
const INDEX_NAMES = { '000001': '上证指数', '399001': '深证成指', '399006': '创业板指' };

async function getData(path) {
  let lastError = null;
  for (const host of HOSTS) {
    try {
      const response = await fetch(host + path, {
        headers: { 'User-Agent': 'Mozilla/5.0', Referer: 'https://quote.eastmoney.com/' },
        signal: AbortSignal.timeout(7000),
      });
      if (!response.ok) continue;
      const json = await response.json();
      if (json.rc === 0 && Array.isArray(json.data?.diff)) return json.data.diff;
      lastError = new Error('行情返回为空');
    } catch (error) {
      lastError = error;
    }
  }
  throw lastError || new Error('行情源不可用');
}

const num = (value) => {
  if (typeof value === 'number' && Number.isFinite(value)) return value;
  if (typeof value === 'string' && value !== '' && value !== '-') {
    const parsed = Number(value);
    if (Number.isFinite(parsed)) return parsed;
  }
  return null;
};
const breadthOk = (row) => row && [num(row.f104), num(row.f105), num(row.f106)].every((value) => value != null && value >= 0);

function quotePayload(rows) {
  const quotes = rows.filter((row) => num(row.f2) != null && num(row.f3) != null).map((row) => ({
    code: String(row.f12).padStart(6, '0'),
    name: row.f14 || null,
    price: num(row.f2),
    change_pct: num(row.f3),
  }));
  const times = rows.map((row) => num(row.f124)).filter((value) => value != null && value > 0);
  return {
    quotes,
    quote_time: times.length ? Math.min(...times) : null,
    fetched_at: new Date().toISOString(),
  };
}

async function readText(response) {
  const buffer = await response.arrayBuffer();
  const utf8 = new TextDecoder('utf-8').decode(buffer);
  if (!utf8.includes('\uFFFD')) return utf8;
  try {
    return new TextDecoder('gbk').decode(buffer);
  } catch (_) {
    return utf8;
  }
}

async function sinaIndices() {
  const response = await fetch('https://hq.sinajs.cn/list=s_sh000001,s_sz399001,s_sz399006', {
    headers: { 'User-Agent': 'Mozilla/5.0', Referer: 'https://finance.sina.com.cn/' },
    signal: AbortSignal.timeout(7000),
  });
  if (!response.ok) throw new Error('新浪指数不可用');
  const text = await readText(response);
  const names = { sh000001: ['000001', '上证指数'], sz399001: ['399001', '深证成指'], sz399006: ['399006', '创业板指'] };
  const indices = [];
  for (const line of text.split(';')) {
    const match = line.match(/hq_str_s_(\w+)="([^"]*)"/);
    if (!match || !names[match[1]]) continue;
    const parts = match[2].split(',');
    const price = num(parts[1]);
    const change = num(parts[3]);
    if (price == null || change == null) continue;
    indices.push({ code: names[match[1]][0], name: names[match[1]][1], price, change_pct: change });
  }
  if (indices.length < 3) throw new Error('新浪指数不完整');
  return indices;
}

async function market() {
  let rows = [];
  try {
    rows = await getData('/api/qt/ulist.np/get?fltt=2&secids=1.000001,0.399001,0.399006,0.899050&fields=f2,f3,f12,f14,f104,f105,f106,f124');
  } catch (_) {
    rows = [];
  }
  const find = (code) => rows.find((row) => String(row.f12).padStart(6, '0') === code);
  const shanghai = find('000001');
  const shenzhen = find('399001');
  const beijing = find('899050');
  let breadth = null;
  if (breadthOk(shanghai) && breadthOk(shenzhen)) {
    const breadthRows = [shanghai, shenzhen].concat(breadthOk(beijing) ? [beijing] : []);
    breadth = breadthRows.reduce((sum, row) => ({
      up: sum.up + num(row.f104),
      down: sum.down + num(row.f105),
      flat: sum.flat + num(row.f106),
    }), { up: 0, down: 0, flat: 0 });
    if (!(breadth.up + breadth.down + breadth.flat)) breadth = null;
  }
  let indices = ['000001', '399001', '399006'].map((code) => {
    const row = find(code);
    if (!row || num(row.f2) == null || num(row.f3) == null) return null;
    return { code, name: row.f14 || INDEX_NAMES[code], price: num(row.f2), change_pct: num(row.f3) };
  }).filter(Boolean);
  if (indices.length < 3) indices = await sinaIndices();
  const payload = quotePayload(rows);
  if (!breadth && !indices.length) throw new Error('行情源不可用');
  return { breadth, indices, quote_time: payload.quote_time, fetched_at: payload.fetched_at || new Date().toISOString() };
}

async function sinaQuotes(codes) {
  const symbols = codes.map((code) => `${code.startsWith('6') ? 'sh' : 'sz'}${code}`).join(',');
  const response = await fetch(`https://hq.sinajs.cn/list=${symbols}`, {
    headers: { 'User-Agent': 'Mozilla/5.0', Referer: 'https://finance.sina.com.cn/' },
    signal: AbortSignal.timeout(7000),
  });
  if (!response.ok) throw new Error('新浪个股行情不可用');
  const quotes = [];
  for (const line of (await readText(response)).split(';')) {
    const match = line.match(/hq_str_(?:sh|sz)(\d{6})="([^"]*)"/);
    if (!match) continue;
    const parts = match[2].split(',');
    const previous = num(parts[2]);
    const price = num(parts[3]);
    if (price == null || !previous) continue;
    quotes.push({
      code: match[1],
      name: parts[0] || null,
      price,
      change_pct: Math.round(((price - previous) / previous) * 10000) / 100,
    });
  }
  if (!quotes.length) throw new Error('新浪个股行情为空');
  return { quotes, quote_time: null, fetched_at: new Date().toISOString() };
}

async function quotes(requestUrl) {
  const raw = new URL(requestUrl).searchParams.get('codes') || '';
  const codes = [...new Set(raw.split(',').map((code) => code.trim()).filter((code) => /^\d{6}$/.test(code)))].slice(0, 24);
  if (!codes.length) throw new Error('缺少股票代码');
  try {
    const secids = codes.map((code) => `${code.startsWith('6') ? '1' : '0'}.${code}`).join(',');
    const payload = quotePayload(await getData(`/api/qt/ulist.np/get?fltt=2&secids=${secids}&fields=f2,f3,f12,f14,f124`));
    if (payload.quotes.length) return payload;
  } catch (_) { /* 换新浪。 */ }
  return sinaQuotes(codes);
}

function json(body, status = 200) {
  return Response.json(body, {
    status,
    headers: { 'Cache-Control': status === 200 ? 'public, max-age=10, s-maxage=10' : 'no-store' },
  });
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname !== '/api/market' && url.pathname !== '/api/quotes') return env.ASSETS.fetch(request);
    if (request.method !== 'GET') return new Response('Method not allowed', { status: 405, headers: { Allow: 'GET' } });
    try {
      return json(url.pathname === '/api/market' ? await market() : await quotes(request.url));
    } catch (_) {
      return json({ error: '行情暂时不可用，请稍后重试。' }, 503);
    }
  },
};
