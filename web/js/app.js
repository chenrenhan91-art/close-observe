const app = document.querySelector("#app");
let snapshot = null;
let liveMarket = null;
let liveState = "loading";
let liveRequest = false;

function h(tag, attrs, children) {
  const node = document.createElement(tag);
  Object.entries(attrs || {}).forEach(([key, value]) => {
    if (value == null) return;
    if (key === "class") node.className = value;
    else if (key === "htmlFor") node.htmlFor = value;
    else node.setAttribute(key, value);
  });
  const append = (child) => {
    if (child == null || child === false) return;
    if (Array.isArray(child)) {
      child.forEach(append);
      return;
    }
    node.append(child.nodeType ? child : document.createTextNode(String(child)));
  };
  append(children);
  return node;
}

function signedPct(value, digits = 2) {
  if (value == null || Number.isNaN(Number(value))) return "不可用";
  const number = Number(value);
  const sign = number > 0 ? "+" : "";
  return `${sign}${number.toFixed(digits)}%`;
}

function plain(value, digits = 2, suffix = "") {
  if (value == null || Number.isNaN(Number(value))) return "不可用";
  return `${Number(value).toFixed(digits)}${suffix}`;
}

function yuan(value) {
  if (value == null || Number.isNaN(Number(value))) return "不可用";
  const number = Number(value);
  const abs = Math.abs(number);
  if (abs >= 1e8) return `${(number / 1e8).toFixed(abs >= 1e10 ? 0 : 2)} 亿`;
  if (abs >= 1e4) return `${(number / 1e4).toFixed(0)} 万`;
  return number.toFixed(0);
}

function tone(value) {
  if (value == null || Number(value) === 0) return "";
  return Number(value) > 0 ? "up" : "down";
}

function parseRoute() {
  const parts = decodeURIComponent(location.hash.replace(/^#\/?/, "")).split("/").filter(Boolean);
  if (parts[0] === "stock") return { page: "stock", code: parts[1], strategy: parts[2] };
  if (parts.length >= 2) return { page: "home", cycle: parts[0], strategy: parts[1] };
  return { page: "home", cycle: "short", strategy: null };
}

function strategiesOf(cycle) {
  return snapshot.strategies.filter((item) => item.cycle === cycle);
}

function findStrategy(id) {
  return snapshot.strategies.find((item) => item.id === id) || null;
}

async function loadSnapshot() {
  const response = await fetch("./data/snapshot.json", { cache: "no-cache" });
  if (!response.ok) throw new Error(String(response.status));
  return response.json();
}

async function boot() {
  try {
    snapshot = await loadSnapshot();
    render();
    window.addEventListener("hashchange", render);
    setInterval(refreshLive, 60000);
    setInterval(refreshSnapshot, 5 * 60 * 1000);
    document.addEventListener("visibilitychange", () => {
      if (document.hidden) return;
      refreshLive();
      refreshSnapshot();
    });
  } catch (_error) {
    app.replaceChildren(
      h("p", { class: "banner" }, "还没有可用的行情快照。在项目目录运行 python -m pipeline.build --force 之后再打开这个页面。")
    );
  }
}

let snapshotRequest = false;

async function refreshSnapshot() {
  if (document.hidden || snapshotRequest || !snapshot) return;
  snapshotRequest = true;
  try {
    const next = await loadSnapshot();
    const stamp = next && next.meta && next.meta.generated_at;
    if (!stamp || stamp === snapshot.meta.generated_at) return;
    snapshot = next;
    render();
  } catch (_error) {
    // 检查失败时保留当前快照。
  } finally {
    snapshotRequest = false;
  }
}

function render() {
  const route = parseRoute();
  if (route.page === "stock") renderStock(route);
  else renderHome(route);
  refreshLive();
}

function refreshLive() {
  if (document.hidden) return;
  refreshMarket();
  refreshQuotes();
}

function snapshotText(text) {
  if (text == null) return "";
  return snapshot.meta.session === "intraday" ? String(text).replaceAll("收盘价", "快照价格") : String(text);
}

function riskText(item, text) {
  const risk = snapshotText(text);
  return /银行/.test(item.facts.industry || "") && risk.includes("资产负债率") ? `${risk} 银行业负债结构特殊，需结合行业情况解读。` : risk;
}

function snapshotDateLabel() {
  const today = new Intl.DateTimeFormat("en-CA", { timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit" }).format(new Date());
  return snapshot.meta.as_of === today ? "" : " · 非当日快照";
}

function masthead() {
  return h("header", { class: "mast" }, [
    h("a", { class: "brand", href: "#/short/limitup-watch", "aria-label": "盘面笔记首页" }, [h("span", { class: "brand-mark", "aria-hidden": "true" }, "盘"), h("span", null, [h("strong", null, "盘面笔记"), h("small", null, "MARKET NOTES")])]),
    h("div", { class: "mast-side" }, [h("span", { class: "mast-label" }, "选股快照"), h("strong", null, `${snapshot.meta.as_of} · ${snapshot.meta.session === "intraday" ? "盘中" : "收盘"}${snapshotDateLabel()}`)]),
  ]);
}

function isTradingTime() {
  const parts = new Intl.DateTimeFormat("en-US", { timeZone: "Asia/Shanghai", weekday: "short", hour: "numeric", minute: "numeric", hourCycle: "h23" }).formatToParts(new Date());
  const get = (type) => parts.find((part) => part.type === type)?.value;
  const minutes = Number(get("hour")) * 60 + Number(get("minute"));
  return !["Sat", "Sun"].includes(get("weekday")) && ((minutes >= 570 && minutes <= 690) || (minutes >= 780 && minutes <= 900));
}

async function refreshQuotes() {
  const nodes = [...app.querySelectorAll("[data-quote]")];
  const codes = [...new Set(nodes.map((node) => node.getAttribute("data-quote")).filter(Boolean))];
  if (!codes.length) return;
  try {
    const response = await fetch(`/api/quotes?codes=${codes.join(",")}`, { cache: "no-store" });
    if (!response.ok) throw new Error("quotes");
    const payload = await response.json();
    const byCode = new Map((payload.quotes || []).map((item) => [item.code, item]));
    nodes.forEach((node) => {
      const quote = byCode.get(node.getAttribute("data-quote"));
      if (!quote) return;
      const price = node.querySelector(".quote-price");
      const change = node.querySelector(".quote-change");
      if (price) price.textContent = Number(quote.price).toFixed(2);
      if (change) {
        change.textContent = signedPct(quote.change_pct);
        change.className = `quote-change ${tone(quote.change_pct)}`;
      }
    });
  } catch (_) {
    nodes.forEach((node) => {
      const price = node.querySelector(".quote-price");
      if (price && price.textContent === "读取中") price.textContent = "行情暂不可用";
    });
  }
}

async function refreshMarket() {
  if (liveRequest) return;
  liveRequest = true;
  try {
    const response = await fetch("/api/market", { cache: "no-store" });
    if (!response.ok) throw new Error("行情不可用");
    liveMarket = await response.json();
    liveState = "ready";
  } catch (_) {
    liveState = liveMarket ? "stale" : "error";
  } finally {
    liveRequest = false;
    const currentPanel = app.querySelector(".breadth-live");
    if (currentPanel) {
      const focused = currentPanel.contains(document.activeElement);
      const nextPanel = liveDashboard().querySelector(".breadth-live");
      currentPanel.replaceWith(nextPanel);
      if (focused) nextPanel.querySelector(".refresh").focus();
    }
  }
}

function quoteTime(value) {
  if (!value) return "行情时间未知";
  return new Intl.DateTimeFormat("zh-CN", { timeZone: "Asia/Shanghai", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23" }).format(new Date(value * 1000));
}

function liveDashboard() {
  const data = liveMarket;
  const button = h("button", { class: "refresh", type: "button" }, "刷新盘面");
  button.addEventListener("click", refreshMarket);
  const breadth = data && data.breadth;
  const total = breadth && breadth.up + breadth.down + breadth.flat;
  const ratio = total ? breadth.up / total : null;
  const description = ratio == null ? "等待市场宽度数据" : ratio >= 0.6 ? "多数个股上涨，市场参与面偏广。" : ratio < 0.4 ? "多数个股下跌，市场参与面偏弱。" : "上涨与下跌家数较为接近，市场分化。";
  const status = liveState === "loading" ? "正在读取盘面" : liveState === "error" ? "盘面数据暂不可用" : liveState === "stale" ? "刷新失败，显示上次数据" : "盘中数据";
  return h("section", { class: "dashboard", "aria-label": "市场情绪与选股依据" }, [
    h("div", { class: "sentiment-layout" }, [sentimentSnapshot(),
      h("section", { class: "breadth-live" }, [
        h("div", { class: "panel-top" }, [h("div", null, [h("p", { class: "section-kicker" }, "盘面温度计"), h("h2", null, "市场宽度")]), h("span", { class: "status-dot" }, status)]),
        h("p", { class: "breadth-description" }, description),
        ratio == null ? h("p", { class: "breadth-empty", role: "status" }, liveState === "loading" ? "正在获取行情…" : "暂无法计算上涨占比，请稍后刷新。") : h("div", { class: "breadth-content" }, [
          h("div", { class: "breadth-number" }, [h("strong", null, `${Math.round(ratio * 100)}%`), h("span", null, "个股上涨")]),
          breadthPanel(breadth),
        ]),
        h("div", { class: "live-indices" }, (data && data.indices ? data.indices : []).map((item) =>
          h("div", null, [h("b", { class: tone(item.change_pct) }, signedPct(item.change_pct)), h("span", null, `${item.name} ${item.price}`)])
        )),
        h("div", { class: "live-footer" }, [h("span", null, data && data.quote_time ? `${quoteTime(data.quote_time)} · 东方财富` : "沪深京市场 · 东方财富"), button]),
        h("p", { class: "breadth-note" }, "以上涨家数占上涨、下跌和平盘总数的比例衡量；仅反映参与面，不能单独判断趋势。")
      ])
    ])
  ]);
}

function sentimentSnapshot() {
  const market = snapshot.market || {};
  const stats = market.stats || {};
  const session = snapshot.meta.session === "intraday" ? "盘中" : "收盘";
  const value = market.temperature == null ? "—" : plain(market.temperature, 1);
  const measure = (label, value, note) => h("div", { class: "signal" }, [h("span", null, label), h("strong", null, value), h("small", null, note)]);
  const ratio = stats.up_ratio == null ? "—" : `${(stats.up_ratio * 100).toFixed(0)}%`;
  const industries = stats.industry_up_ratio == null ? "—" : `${(stats.industry_up_ratio * 100).toFixed(0)}%`;
  return h("section", { class: "sentiment-card" }, [
    h("div", { class: "panel-top" }, [h("div", null, [h("p", { class: "section-kicker" }, "情绪快照"), h("h2", null, "市场情绪")]), h("span", { class: "snapshot-date" }, `${snapshot.meta.as_of} · ${session}${snapshotDateLabel()}`)]),
    h("div", { class: "sentiment-main" }, [h("div", { class: "sentiment-score" }, [h("strong", null, value), h("span", null, "/ 100")]), h("div", { class: "sentiment-state" }, [h("span", null, "规则判断"), h("strong", null, market.stage || "数据不足"), h("small", null, "基于该时点快照")])]),
    h("div", { class: "sentiment-scale", "aria-hidden": "true" }, [h("i", { style: `width:${market.temperature == null ? 0 : Math.max(0, Math.min(100, market.temperature))}%` })]),
    h("div", { class: "scale-labels" }, [h("span", null, "偏冷"), h("span", null, "偏热")]),
    h("div", { class: "signals" }, [measure("上涨占比", ratio, "快照样本"), measure("涨停 / 跌停", stats.limit_up == null || stats.limit_down == null ? "—" : `${stats.limit_up} / ${stats.limit_down}`, "数据源涨跌停池"), measure("上涨行业", industries, "行业中位数口径")]),
    h("details", { class: "method" }, [h("summary", null, "情绪如何计算？"), h("p", null, "温度按上涨家数 35%、涨跌停 25%、成交额对比 20%、上涨行业占比 20% 加权；缺失项不计入并重新分配权重。"), h("p", null, "阶段按顺序判断：上涨占比低于 25%，或跌停至少 50 且涨停少于 30，为冰点；涨停至少 80、跌停不超过 10 且上涨占比超过 60%，为高潮；涨停少于 40、跌停至少 20 且上涨占比低于 45%，为退潮；上涨占比至少 45%、跌停少于 20 且涨停少于 80，为修复；其余为常态。缺少相关数据时不判断。阶段不能预测涨跌。"), ...(market.temperature_notes || []).map((note) => h("p", null, note)), h("p", null, "情绪快照与选股同一时间生成，不会随右侧盘面数据刷新。")]),
  ]);
}

function marketBlock() {
  const market = snapshot.market;
  const temp = market.temperature == null ? "—" : String(market.temperature);
  const marker = market.temperature == null ? 0 : Math.max(0, Math.min(100, market.temperature));
  return h("section", { class: "market-wrap" }, [
    h("div", { class: "market" }, [
      h("div", null, [
        h("div", { class: "temp" }, temp),
        h("div", { class: "stage" }, market.stage || "阶段未标注"),
      ]),
      h("div", null, [
        h("div", { class: "indices" }, (market.indices || []).map((item) =>
          h("div", null, [
            h("b", { class: tone(item.change_pct) }, signedPct(item.change_pct)),
            h("span", null, item.name),
          ])
        )),
        h("p", { class: "summary" }, market.summary),
      ]),
    ]),
    h("div", { class: "gauge", "aria-hidden": "true" }, [
      h("i", { style: `width:${marker}%` }),
      h("b", { style: `left:${marker}%` }),
    ]),
    h("div", { class: "gauge-scale" }, [
      h("span", null, "0 冷"),
      h("span", null, "温度只描述市场，不改变个股分数，也不对应仓位。"),
      h("span", null, "100 热"),
    ]),
    h("div", { class: "viz-grid" }, [
      breadthPanel(market.stats),
      limitPanel(market.stats),
      indexPanel(market.indices || []),
    ]),
    boardPanel(market.boards || []),
    strategyPanel(),
  ]);
}

function breadthPanel(stats) {
  const up = stats.up || 0;
  const down = stats.down || 0;
  const flat = stats.flat || 0;
  const total = up + down + flat || 1;
  return h("section", { class: "viz" }, [
    h("h3", null, "涨跌家数"),
    h("div", { class: "stack" }, [
      h("i", { class: "up", style: `width:${(up / total) * 100}%` }),
      h("i", { class: "flat", style: `width:${(flat / total) * 100}%` }),
      h("i", { class: "down", style: `width:${(down / total) * 100}%` }),
    ]),
    h("p", { class: "legend" }, [
      h("span", { class: "up" }, `上涨 ${up}`),
      h("span", null, `平盘 ${flat}`),
      h("span", { class: "down" }, `下跌 ${down}`),
    ]),
  ]);
}

function limitPanel(stats) {
  if (stats.limit_up == null || stats.limit_down == null) {
    return h("section", { class: "viz" }, [h("h3", null, "涨停与跌停"), h("p", { class: "note" }, "快照不可用")]);
  }
  const max = Math.max(stats.limit_up, stats.limit_down, 1);
  return h("section", { class: "viz" }, [
    h("h3", null, "涨停与跌停"),
    meterRow("涨停", stats.limit_up, max, "up"),
    meterRow("跌停", stats.limit_down, max, "down"),
  ]);
}

function indexPanel(indices) {
  const max = Math.max(0.5, ...indices.map((item) => Math.abs(Number(item.change_pct) || 0)));
  return h("section", { class: "viz" }, [
    h("h3", null, "指数涨跌"),
    ...(indices.length
      ? indices.map((item) => divergeRow(item.name, item.change_pct, max))
      : [h("p", { class: "note" }, "快照不可用")]),
  ]);
}

function boardPanel(boards) {
  const ranked = [...boards].sort((a, b) => b.median - a.median);
  const leaders = ranked.slice(0, 6);
  const laggards = ranked.length <= 6 ? [] : ranked.slice(-6).reverse();
  const max = Math.max(0.5, ...leaders.concat(laggards).map((item) => Math.abs(item.median)));
  const column = (title, items) => h("div", null, [
    h("p", { class: "note" }, title),
    ...items.map((item) => divergeRow(`${item.name} · ${item.count}只`, item.median, max)),
  ]);
  return h("section", { class: "viz viz-wide" }, [
    h("h3", null, "行业中位数涨跌"),
    h("p", { class: "note" }, "每个行业至少 3 只股票。左列是领涨，右列是领跌。"),
    ranked.length
      ? h("div", { class: "board-grid" }, [column("领涨", leaders), column("领跌", laggards)])
      : h("p", { class: "note" }, "行业分类快照不可用"),
  ]);
}

function strategyPanel() {
  const rows = snapshot.strategies;
  const max = Math.max(1, ...rows.map((item) => item.matched || 0));
  return h("section", { class: "viz viz-wide" }, [
    h("h3", null, "六张名单各自筛出多少"),
    h("div", { class: "board-grid" }, rows.map((item) =>
      h("a", { class: "meter-row", href: `#/${item.cycle}/${item.id}` }, [
        h("span", null, `${item.cycle_label} · ${item.name}`),
        h("i", null, [h("b", { style: `width:${item.status === "ok" ? ((item.matched || 0) / max) * 100 : 0}%` })]),
        h("em", null, item.status === "ok" ? String(item.matched) : "不可用"),
      ])
    )),
  ]);
}

function scorePanel(rows) {
  return h("section", { class: "viz viz-wide" }, [
    h("h3", null, "这 8 只的分数"),
    ...rows.map((item) => meterRow(item.name, item.score || 0, 100, "ink", item.score == null ? "—" : item.score.toFixed(1))),
  ]);
}

function meterRow(label, value, max, toneName, text) {
  const width = max ? Math.max(0, Math.min(100, (Number(value) / max) * 100)) : 0;
  return h("div", { class: "meter-row" }, [
    h("span", null, label),
    h("i", null, [h("b", { class: toneName, style: `width:${width}%` })]),
    h("em", null, text == null ? String(value) : text),
  ]);
}

function divergeRow(label, value, max) {
  const number = Number(value);
  const width = max ? Math.min(100, (Math.abs(number) / max) * 100) : 0;
  const negative = number < 0;
  return h("div", { class: "diverge" }, [
    h("span", null, label),
    h("div", { class: "split-track" }, [
      h("div", { class: "left" }, [negative ? h("i", { class: "down", style: `width:${width}%` }) : null]),
      h("div", { class: "right" }, [negative ? null : h("i", { class: "up", style: `width:${width}%` })]),
    ]),
    h("em", { class: tone(number) }, signedPct(number)),
  ]);
}

function stat(label, value) {
  return h("div", null, [h("b", null, value == null ? "不可用" : String(value)), h("span", null, label)]);
}

function renderHome(route) {
  const cycles = snapshot.meta.cycles;
  const cycle = cycles.some((item) => item.id === route.cycle) ? route.cycle : "short";
  const group = strategiesOf(cycle);
  const strategy = group.find((item) => item.id === route.strategy) || group[0];
  const root = h("div", null, [
    masthead(),
    liveDashboard(),
    h("div", { class: "snapshot-heading" }, [h("div", null, [h("p", { class: "eyebrow" }, "STOCK IDEAS / 策略选股"), h("h2", null, "从你的观察周期开始"), h("p", { class: "section-copy" }, "先选周期，再查看符合规则的候选股及其风险。")]), h("p", { class: "snapshot-meta" }, `名单快照 ${snapshot.meta.as_of} · ${snapshot.meta.session === "intraday" ? "盘中" : "收盘"}，非实时`) ]),
    snapshot.meta.score_model !== 2 ? h("p", { class: "score-warning" }, "当前快照仍使用旧版评分，尚未按最新数据完整性规则重算，请谨慎参考。") : null,
    h("nav", { class: "cycles" }, cycles.map((item) => {
      const first = strategiesOf(item.id)[0];
      return h("a", {
        href: `#/${item.id}/${first ? first.id : ""}`,
        class: item.id === cycle ? "active" : "",
      }, [item.label, h("small", null, item.horizon)]);
    })),
    h("div", { class: "strategy-grid" }, group.map((item) => strategyCard(item, strategy && item.id === strategy.id))),
    strategy ? listSection(strategy) : h("p", null, "这个周期没有策略。"),
    notesBlock(),
  ]);
  app.replaceChildren(root);
}

function strategyCard(strategy, active) {
  let count = "快照不可用";
  if (strategy.status === "ok") {
    count = strategy.matched ? `符合 ${strategy.matched} 只` : "没有符合的股票";
  }
  return h("a", {
    class: `strategy${active ? " active" : ""}`,
    href: `#/${strategy.cycle}/${strategy.id}`,
  }, [
    h("h2", null, [strategy.name, strategy.relaxed ? h("span", { class: "tag" }, "已放宽一档") : null]),
    h("p", null, strategy.status === "ok" ? snapshotText(strategy.blurb) : strategy.reason),
    h("div", { class: "count" }, count),
  ]);
}

function listSection(strategy) {
  const head = h("div", { class: "list-head" }, [
    h("div", null, [
      h("h2", null, strategy.name),
      h("p", null, strategy.status === "ok"
        ? `符合 ${strategy.matched} 只，日 K 复核 ${strategy.reviewed} 只，最多展示 8 只；分数只用于当前策略内比较。`
        : "这张名单在当前快照中不可用。"),
    ]),
  ]);
  if (strategy.status !== "ok") {
    return h("section", null, [head, h("p", { class: "summary" }, strategy.reason)]);
  }
  const search = h("input", { type: "search", placeholder: "代码或名称", "aria-label": "在当前名单里查找" });
  const shown = strategy.candidates.slice(0, 8);
  const body = h("div", null);
  const draw = () => {
    const query = search.value.trim().toLowerCase();
    const rows = shown.filter((item) =>
      !query || item.code.includes(query) || item.name.toLowerCase().includes(query)
    );
    body.replaceChildren(table(strategy, rows));
  };
  search.addEventListener("input", draw);
  head.append(search);
  draw();
  return h("section", null, [
    head,
    h("ul", { class: "rules" }, strategy.rules.map((rule) => h("li", null, snapshotText(rule)))),
    body,
  ]);
}

function table(strategy, rows) {
  if (!rows.length) return h("p", { class: "summary" }, "当前名单中没有匹配的股票。");
  return h("div", { class: "candidate-list" }, rows.map((item, index) => h("a", { class: "candidate", href: `#/stock/${item.code}/${strategy.id}` }, [
    h("div", { class: "candidate-rank" }, String(index + 1).padStart(2, "0")),
    h("div", { class: "candidate-main" }, [
      h("div", { class: "candidate-name" }, [h("strong", null, item.name), h("span", null, `${item.code} · ${item.facts.industry || "行业未知"}`)]),
      h("div", { class: "candidate-quote", "data-quote": item.code }, [h("span", { class: "quote-label" }, "最新价"), h("strong", { class: "quote-price" }, "读取中"), h("em", { class: "quote-change" }, "")]),
      h("p", null, snapshotText(item.reasons[0]) || "暂无上榜原因"),
      evidenceTags(item, strategy),
    ]),
    h("div", { class: "candidate-risk" }, [h("span", null, "关注风险"), h("p", null, riskText(item, item.risks[0]) || "暂无规则提示，请自行核查。")]),
    h("div", { class: "candidate-score" }, [h("span", null, item.score == null ? "数据不足" : "综合评分"), h("strong", null, item.score == null ? "—" : item.score.toFixed(1)), h("small", null, "查看依据 →")])
  ])));
}

function evidenceTags(item, strategy) {
  const dimensions = (item.dimensions || []).filter((dim) => typeof dim.score === "number" && Number.isFinite(dim.score));
  const strongest = dimensions.reduce((best, dim) => !best || dim.score > best.score ? dim : best, null);
  const facts = item.facts || {};
  const isNumber = (value) => typeof value === "number" && Number.isFinite(value);
  const options = strategy.cycle === "short" ? [
    ["量比", facts.volume_ratio, (v) => v.toFixed(2)],
    ["换手", facts.turnover, (v) => `${v.toFixed(1)}%`],
    ["60日", facts.ret60, signedPct],
  ] : strategy.cycle === "medium" ? [
    ["60日", facts.ret60, signedPct],
    ["营收同比", facts.rev_yoy, signedPct],
    ["利润同比", facts.profit_yoy_used, signedPct],
  ] : [
    ["股息率", facts.div_yield, (v) => `${v.toFixed(2)}%`],
    ["净资产收益率", facts.roe, (v) => `${v.toFixed(1)}%`],
    ["利润同比", facts.profit_yoy_used, signedPct],
  ];
  const tags = strongest ? [`最高维度 ${strongest.label} ${Math.round(strongest.score)}分`] : [];
  options.filter(([, value]) => isNumber(value)).slice(0, 2).forEach(([label, value, format]) => tags.push(`${label} ${format(value)}`));
  if (dimensions.length < (item.dimensions || []).length) tags.push("部分维度缺失");
  return tags.length ? h("div", { class: "evidence-tags" }, tags.map((tag) => h("span", null, tag))) : null;
}

function scoreBreakdown(candidate, strategy) {
  const coverage = candidate.dimensions.reduce((sum, dim) => sum + (dim.score == null ? 0 : dim.weight), 0);
  return h("section", { class: "score-breakdown" }, [
    h("div", { class: "score-breakdown-head" }, [h("div", null, [h("h3", null, "评分依据"), h("p", null, `可用维度权重 ${coverage}%；缺失维度不按零分计算。规则评分未经历史回测验证。`), snapshot.meta.score_model === 2 ? h("p", null, "可用权重不足 70% 时不计算总分；基本面需具备净资产收益率和估值数据。") : h("p", null, "该快照使用旧版评分规则，尚未按最新规则重算。")]), h("strong", null, candidate.score == null ? "—" : candidate.score.toFixed(1))]),
    h("div", { class: "score-dimensions" }, candidate.dimensions.map((dim) => h("div", { class: "score-dimension" }, [
      h("span", null, dim.label), h("strong", null, dim.score == null ? "—" : `${Math.round(dim.score)} 分`), h("small", null, `权重 ${dim.weight}%${dim.score == null ? " · 缺失" : ""}`),
    ]))),
    evidenceTags(candidate, strategy),
  ]);
}

function dimensionBar(dimension) {
  const width = dimension.score == null ? 0 : Math.max(0, Math.min(100, dimension.score));
  return h("div", { class: "dim" }, [
    h("span", null, dimension.label),
    h("i", null, [h("span", { style: `width:${width}%` })]),
    h("span", null, dimension.score == null ? "—" : Math.round(dimension.score)),
  ]);
}

function renderStock(route) {
  const strategy = findStrategy(route.strategy) || snapshot.strategies.find((item) =>
    item.candidates.some((candidate) => candidate.code === route.code)
  );
  const candidate = strategy && strategy.candidates.find((item) => item.code === route.code);
  if (!strategy || !candidate) {
    app.replaceChildren(h("div", null, [
      masthead(),
      h("p", { class: "banner" }, [
        "这只股票不在今日观察名单里。",
        h("a", { href: "#/short/limitup-watch" }, "返回名单"),
      ]),
    ]));
    return;
  }
  const facts = candidate.facts;
  const others = snapshot.strategies.filter((item) =>
    item.id !== strategy.id && item.candidates.some((row) => row.code === candidate.code)
  );
  app.replaceChildren(h("div", null, [
    masthead(),
    h("div", { class: "detail-top" }, [
      h("a", { class: "back", href: `#/${strategy.cycle}/${strategy.id}` }, `返回${strategy.name}`),
      h("h2", null, [candidate.name, h("small", { class: "tag" }, candidate.code)]),
      h("div", { class: "price-line", "data-quote": candidate.code }, [
        h("small", { class: "quote-label" }, "最新价"),
        h("strong", { class: "quote-price" }, facts.price == null ? "价格不可用" : facts.price.toFixed(2)),
        h("em", { class: `quote-change ${tone(facts.change_pct)}` }, signedPct(facts.change_pct)),
        h("small", null, `${strategy.cycle_label} · ${strategy.name} · 总分 ${candidate.score == null ? "不可用" : candidate.score.toFixed(1)}`),
      ]),
    ]),
    h("div", { class: "split" }, [
      h("section", { class: "panel" }, [
        h("h3", null, "上榜原因"),
        h("ul", null, candidate.reasons.map((item) => h("li", null, snapshotText(item)))),
        candidate.score_notes.map((item) => h("p", { class: "note" }, item)),
      ]),
      h("section", { class: "panel" }, [
        h("h3", null, "主要风险"),
        h("ul", null, candidate.risks.map((item) => h("li", null, riskText(candidate, item)))),
      ]),
    ]),
    scoreBreakdown(candidate, strategy),
    peerPanel(candidate.name, facts),
    h("div", { class: "facts" }, factCells(facts, strategy.capital_label)),
    others.length
      ? h("p", null, ["这只股票也出现在：", ...others.map((item, index) => [
          index ? "、" : "",
          h("a", { href: `#/stock/${candidate.code}/${item.id}` }, item.name),
        ])].flat())
      : null,
    notesBlock(),
  ]));
  window.scrollTo(0, 0);
}

function peerPanel(name, facts) {
  const max = Math.max(0.5, Math.abs(Number(facts.change_pct) || 0), Math.abs(Number(facts.industry_median) || 0));
  return h("section", { class: "viz" }, [
    h("h3", null, "快照涨跌和所属行业"),
    divergeRow(name, facts.change_pct, max),
    facts.industry_median == null
      ? h("p", { class: "note" }, "行业中位数不可用")
      : divergeRow(facts.industry || "所属行业", facts.industry_median, max),
  ]);
}

function weightLine(strategy) {
  const labels = { fundamental: "基本面", technical: "技术面", capital: strategy.capital_label, industry: "行业位置" };
  const text = Object.entries(strategy.weights).map(([key, weight]) => `${labels[key]} ${weight}`).join(" · ");
  return `这个周期的权重：${text}。缺数据的项目不按 0 分计入。`;
}

function factCells(facts, capitalLabel) {
  const cells = [
    ["快照价", facts.price == null ? "不可用" : facts.price.toFixed(2)],
    ["涨跌幅", signedPct(facts.change_pct)],
    ["换手率", plain(facts.turnover, 2, "%")],
    ["量比", plain(facts.volume_ratio)],
    ["成交额", yuan(facts.amount)],
    ["总市值", yuan(facts.mcap)],
    ["动态市盈率", plain(facts.pe)],
    ["市净率", plain(facts.pb)],
    ["60 日涨跌幅", signedPct(facts.ret60)],
    ["年初至今", signedPct(facts.ret_ytd)],
    ["行业", facts.industry || "不可用"],
    ["行业中位数涨幅", signedPct(facts.industry_median)],
    ...(capitalLabel === "资金面" ? [["主力净流入", yuan(facts.main_net)]] : []),
    ["净资产收益率", plain(facts.roe, 2, "%")],
    ["营收同比", signedPct(facts.rev_yoy)],
    ["净利润同比", signedPct(facts.profit_yoy_used)],
    ["资产负债率", plain(facts.debt, 2, "%")],
    ["现金股息率", plain(facts.div_yield, 2, "%")],
    ["距高点回撤", facts.drawdown == null ? "不可用" : `${(facts.drawdown * 100).toFixed(0)}%`],
    ["站上 60 日均线", facts.above_ma60 == null ? "不可用" : facts.above_ma60 ? "是" : "否"],
  ];
  return cells.map(([label, value]) => h("div", null, [h("b", null, value), h("span", null, label)]));
}

function newsList(candidate) {
  if (candidate.news_status === "unavailable") return h("p", null, "资讯接口不可用。");
  if (!candidate.news.length) return h("p", null, "今天没有搜到新闻。");
  return h("ul", { class: "news" }, candidate.news.map((item) =>
    h("li", null, [
      h("a", { href: item.url, target: "_blank", rel: "noopener noreferrer" }, item.title),
      h("small", null, [item.time, item.source].filter(Boolean).join(" · ")),
    ])
  ));
}

function notesBlock() {
  const notes = snapshot.meta.notes || [];
  return h("details", { class: "notes" }, [
    h("summary", null, "数据说明"),
    h("p", null, snapshot.meta.disclaimer),
    h("p", null, snapshot.meta.universe),
    h("ul", null, notes.map((note) => h("li", null, note))),
  ]);
}

boot();
