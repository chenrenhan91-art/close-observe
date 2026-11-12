"""生成静态快照 web/data/snapshot.json。失败不覆盖上一份成功结果。"""

from __future__ import annotations

import argparse
import json
import logging
import math
import sys
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from pipeline.fetchers import (
    FetchError,
    fetch_balance,
    fetch_dividends,
    fetch_financials,
    fetch_fund_flow,
    fetch_index_amount,
    fetch_indices,
    fetch_kline,
    fetch_listings,
    fetch_news,
    fetch_pool,
    fetch_spot,
    fetch_trade_dates,
)
from pipeline.rules import (
    CYCLES,
    DISCLAIMER,
    MAX_REVIEW,
    MAX_SHOW,
    STRATEGIES,
    WEIGHTS,
    activity_score,
    apply_rules,
    attach_amount_rank,
    attach_industry,
    choose_thresholds,
    combine_scores,
    describe_rules,
    explain,
    flow_score,
    fundamental_score,
    is_risk_name,
    is_target_board,
    limit_volume_score,
    missing_dependency,
    proxy_technical,
    sentiment_stage,
    technical_from_bars,
    temperature,
    trading_days_listed,
)

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "web" / "data"
CACHE_DIR = ROOT / "data" / "cache" / "kline"
SHANGHAI = ZoneInfo("Asia/Shanghai")
log = logging.getLogger("close")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成收盘观察快照")
    parser.add_argument("--force", action="store_true", help="非交易日也生成最近交易日快照")
    parser.add_argument("--refresh", action="store_true", help="忽略日 K 缓存")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    publish_flag = DATA_DIR / ".publish"
    publish_flag.unlink(missing_ok=True)

    now = datetime.now(SHANGHAI)
    try:
        trade_dates = fetch_trade_dates()
    except FetchError as exc:
        log.error("%s", exc)
        return 1
    action, as_of, session, calendar_note = resolve_session(now.date(), now.time(), trade_dates, args.force)
    if action == "skip":
        log.info("%s", calendar_note or "今天不是需要发布的交易日，保留线上已有数据。")
        return 0
    log.info("交易日 %s，时段 %s", as_of, session)

    try:
        spot = fetch_spot()
        listings = fetch_listings()
    except FetchError as exc:
        log.error("%s", exc)
        return 1

    prepared = prepare_frames(spot, listings, trade_dates, as_of)
    if prepared[0] is None:
        log.error("%s", prepared[1])
        return 1
    mood, pick, coverage = prepared
    log.info("情绪样本 %s 只，选股池 %s 只，上市日期覆盖 %.0f%%", len(mood), len(pick), coverage * 100)

    notes = [
        "通过条件超过 40 只时，只对初步排序前 40 只补全日 K，再固定展示 8 只。",
        "资讯不计入总分。",
        "评分为规则筛选参考，未经历史回测验证；评分维度覆盖权重不足 70% 时不计算总分。基本面评分要求有净资产收益率和至少一项估值数据。",
        "价格、估值和成交来自新浪财经。60 日涨跌幅、年初至今涨跌幅、量比和日 K 来自腾讯财经。行业名称来自最新业绩报表里的所处行业，行业涨幅是该行业个股今日涨跌幅的中位数。涨停池、财务报表、资金流和新闻来自东方财富。上市日期来自上交所和深交所。",
    ]
    if calendar_note:
        notes.append(calendar_note)
    if session == "intraday":
        notes.append("这是盘中快照。成交额还没和近 20 日收盘均额比较，收盘后的构建会换成收盘数据。")

    limits_ok, history, sealed, limit_up, limit_down, pool_note = load_limits(trade_dates, as_of, mood)
    if pool_note:
        notes.append(pool_note)
    financials, report_date = fetch_financials(as_of)
    balance, balance_date = fetch_balance(as_of)
    dividends, dividend_date = fetch_dividends(as_of)
    fund_flow = fetch_fund_flow()
    if fund_flow is None:
        notes.append("个股资金流排名不可用。资金面改按成交额、量比和换手计算，名称写成成交活跃。")
    capital_mode = "flow" if fund_flow is not None else "activity"

    if financials is not None and "industry_report" in financials.columns:
        mood["industry"] = mood["code"].map(financials.drop_duplicates("code").set_index("code")["industry_report"])
    pick = enrich(pick, financials, balance, dividends, fund_flow, history, sealed, limits_ok)
    context = {"report_date": report_date, "balance_date": balance_date, "dividend_date": dividend_date}
    flags = {
        "limits": limits_ok,
        "industry": bool(pd.to_numeric(pick["industry_groups"], errors="coerce").fillna(0).max() > 0),
        "financials": financials is not None,
        "balance": balance is not None,
        "dividend": dividends is not None,
    }
    reviewed = review_strategies(pick, flags, context, capital_mode)
    technical = load_technical(sorted({code for item in reviewed for code in item["codes"]}), as_of, args.refresh)
    strategies, news_codes = finalize_strategies(reviewed, technical, capital_mode, context)
    news = load_news(news_codes)
    for strategy in strategies:
        for candidate in strategy["candidates"]:
            pack = news.get(candidate["code"], {"items": [], "status": "unavailable"})
            candidate["news"] = pack["items"]
            candidate["news_status"] = pack["status"]

    market = build_market(mood, session, as_of, limit_up, limit_down)
    snapshot = {
        "schema": 1,
        "meta": {
            "as_of": as_of.isoformat(),
            "generated_at": now.isoformat(timespec="minutes"),
            "session": session,
            "score_model": 2,
            "disclaimer": DISCLAIMER,
            "listing_coverage": round(coverage, 4),
            "universe": "沪深主板、创业板、科创板。已排除 ST、*ST、退市、上市首日和次日标记、上市不足 60 个交易日、停牌。不含北交所。",
            "notes": notes,
            "cycles": list(CYCLES),
        },
        "market": market,
        "strategies": strategies,
    }
    write_snapshot(snapshot)
    publish_flag.write_text(snapshot["meta"]["generated_at"], encoding="utf-8")
    log.info("已写入 %s", DATA_DIR / "snapshot.json")
    return 0


def resolve_session(today: date, now_time: time, trade_dates: list[date], force: bool):
    dates = sorted(set(trade_dates))
    last = dates[-1]
    if today in dates:
        as_of = today
        note = None
    elif last >= today:
        if not force:
            return "skip", None, None, None
        as_of = max(item for item in dates if item <= today)
        note = "今天不是交易日，这份快照用最近一个交易日。"
    else:
        return "skip", None, None, f"交易日历只更新到 {last.isoformat()}，无法确认今日是否交易，保留已有快照。"
    if as_of == today and now_time < time(9, 30):
        return "skip", None, None, "开盘前无法生成今日行情快照，保留已有快照。"
    session = "close" if as_of < today or now_time >= time(15, 5) else "intraday"
    return "run", as_of, session, note


def prepare_frames(spot: pd.DataFrame, listings: dict, trade_dates: list[date], as_of: date):
    frame = spot[spot["code"].map(is_target_board)].copy()
    frame["listed_days"] = frame["code"].map(
        lambda code: trading_days_listed(listings.get(code), as_of, trade_dates)
    )
    named = ~frame["name"].map(is_risk_name)
    coverage_base = frame[named]
    known = int(coverage_base["listed_days"].notna().sum())
    coverage = known / max(len(coverage_base), 1)
    if coverage < 0.8:
        return None, f"上市日期只覆盖了 {coverage:.0%} 的目标股票，这次不发布。", None
    traded = frame["price"].gt(0) & frame["amount"].fillna(0).gt(0)
    mood = frame[traded].copy()
    pick = frame[traded & named & frame["listed_days"].ge(60)].copy()
    return mood, pick, coverage


def load_limits(trade_dates: list[date], as_of: date, mood: pd.DataFrame):
    prior = [item for item in trade_dates if item <= as_of]
    if as_of not in prior:
        prior.append(as_of)
    last3 = prior[-3:]
    try:
        sealed = fetch_pool("up", as_of)
    except FetchError as exc:
        log.warning("%s", exc)
        return False, set(), set(), None, None, "今日涨停池没有拿到，短线「涨停后观察」和涨跌停阶段今天不可用。"
    if _suspicious_empty(len(sealed), mood, "up"):
        return False, set(), set(), None, None, "今日涨停池为空，但行情里有不少接近涨停的股票，这次不采用涨停池。"
    history = set(sealed)
    got_days = 1
    for day in last3:
        if day == as_of:
            continue
        try:
            history |= fetch_pool("up", day)
            got_days += 1
        except FetchError as exc:
            log.warning("%s", exc)
    try:
        fallen = fetch_pool("down", as_of)
    except FetchError as exc:
        log.warning("%s", exc)
        fallen = None
    if fallen is not None and _suspicious_empty(len(fallen), mood, "down"):
        fallen = None
    limit_up = len(sealed)
    limit_down = None if fallen is None else len(fallen)
    if any(code.startswith(("688", "689")) for code in sealed):
        pool_note = "今日涨停池里有科创板代码，阶段统计按池内实际家数计算。"
    else:
        pool_note = "今日涨停池没有科创板代码。东方财富该池通常不含 ST 和科创板，科创板涨停不会进入「涨停后观察」。"
    if got_days < len(last3):
        pool_note += f" 近 3 个交易日的涨停池拿到了 {got_days} 天。"
    if limit_down is None:
        pool_note += " 跌停池没有拿到，今天不标注情绪阶段。"
    return True, history, sealed, limit_up, limit_down, pool_note


def _suspicious_empty(pool_size: int, mood: pd.DataFrame, side: str) -> bool:
    if pool_size > 0 or mood.empty:
        return False
    main = ~mood["code"].astype(str).str.startswith(("688", "689"))
    if side == "up":
        hot = mood["change_pct"].ge(9.5) & main
    else:
        hot = mood["change_pct"].le(-9.5) & main
    return int(hot.sum()) >= 8


def enrich(pick, financials, balance, dividends, fund_flow, history, sealed, limits_ok):
    frame = pick.copy()
    frame = _merge_table(frame, financials, ["roe", "rev_yoy", "profit_yoy", "industry_report"])
    frame = _fill_industry(frame)
    frame = attach_amount_rank(attach_industry(frame))
    frame["in_limit_pool"] = frame["code"].isin(history) if limits_ok else False
    frame["sealed_today"] = frame["code"].isin(sealed) if limits_ok else False
    frame = _merge_table(frame, balance, ["debt"])
    frame = _merge_table(frame, dividends, ["div_yield", "div_profit_yoy", "div_progress"])
    if fund_flow is not None:
        flow = fund_flow.copy()
        flow["flow_pct"] = flow["main_net"].rank(pct=True, method="average") * 100
        frame = _merge_table(frame, flow, ["main_net", "main_ratio", "flow_pct"])
    else:
        frame["main_net"] = pd.NA
        frame["main_ratio"] = pd.NA
        frame["flow_pct"] = pd.NA
    if "profit_yoy" not in frame.columns:
        frame["profit_yoy"] = pd.NA
    if "div_profit_yoy" not in frame.columns:
        frame["div_profit_yoy"] = pd.NA
    # Do not substitute last year's dividend-report growth for the latest report.
    frame["profit_yoy_used"] = frame["profit_yoy"]
    return frame


def _fill_industry(frame: pd.DataFrame) -> pd.DataFrame:
    if "industry_report" not in frame.columns:
        return frame
    industry = frame["industry"].astype("string")
    missing = industry.isna() | industry.str.strip().eq("")
    frame.loc[missing, "industry"] = frame.loc[missing, "industry_report"]
    return frame


def _merge_table(frame, table, columns):
    if table is None:
        for column in columns:
            frame[column] = pd.NA
        return frame
    slim = table[["code", *columns]].drop_duplicates("code", keep="last")
    return frame.merge(slim, on="code", how="left")


def review_strategies(pick, flags, context, capital_mode) -> list[dict]:
    reviewed = []
    for spec in STRATEGIES:
        blocked = missing_dependency(spec, flags)
        if blocked:
            reviewed.append({"spec": spec, "status": "unavailable", "reason": blocked, "codes": []})
            continue
        thresholds, relaxed, matched = choose_thresholds(pick, spec)
        chosen = pick[apply_rules(pick, thresholds)].copy()
        if not chosen.empty:
            chosen["_pre"] = chosen.apply(lambda row: _preview_score(row, spec["cycle"], capital_mode), axis=1)
            chosen = chosen.sort_values(["_pre", "amount"], ascending=[False, False], na_position="last")
        chosen = chosen.head(MAX_REVIEW)
        reviewed.append(
            {
                "spec": spec,
                "status": "ok",
                "reason": None,
                "thresholds": thresholds,
                "relaxed": relaxed,
                "matched": matched,
                "rows": chosen,
                "codes": chosen["code"].tolist(),
                "context": context,
            }
        )
        log.info("%s 符合 %s，复核 %s，放宽 %s", spec["name"], matched, len(chosen), relaxed)
    return reviewed


def _preview_score(row, cycle: str, capital_mode: str) -> float:
    scores = {
        "fundamental": fundamental_score(_val(row.get("pe")), _val(row.get("pb")), _val(row.get("roe"))),
        "technical": proxy_technical(_val(row.get("ret60"))),
        "capital": _capital(row, capital_mode),
        "industry": _val(row.get("industry_score")),
    }
    total, _notes = combine_scores(scores, cycle)
    return -1 if total is None else total


def load_technical(codes: list[str], as_of: date, refresh: bool) -> dict[str, dict | None]:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    found: dict[str, dict | None] = {}
    for index, code in enumerate(codes, start=1):
        bars = _cached_bars(code, as_of, refresh)
        found[code] = technical_from_bars(bars) if bars else None
        if index % 20 == 0 or index == len(codes):
            log.info("日 K %s/%s", index, len(codes))
    return found


def _cached_bars(code: str, as_of: date, refresh: bool) -> list[dict]:
    path = CACHE_DIR / f"{code}.json"
    stamp = as_of.isoformat()
    # A same-day intraday bar is provisional. Never use it as a closing bar.
    close_time = datetime.combine(as_of, time(15, 5), tzinfo=SHANGHAI)
    now = datetime.now(SHANGHAI)
    cache_is_closed = path.exists() and now >= close_time and datetime.fromtimestamp(path.stat().st_mtime, SHANGHAI) >= close_time
    if cache_is_closed and not refresh:
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
            if cached.get("as_of") == stamp and len(cached.get("bars") or []) >= 60 and cached["bars"][-1].get("date") == stamp:
                return cached["bars"]
        except (json.JSONDecodeError, OSError, TypeError, AttributeError):
            pass
    try:
        bars = fetch_kline(code, as_of)
    except Exception as exc:  # noqa: BLE001
        log.warning("%s 日 K 失败：%s", code, exc)
        return []
    if not bars or bars[-1].get("date") != stamp:
        log.warning("%s 日 K 未覆盖快照日期 %s，技术面不可用", code, stamp)
        return []
    if len(bars) >= 60 and now >= close_time:
        path.write_text(json.dumps({"as_of": stamp, "bars": bars}, ensure_ascii=False), encoding="utf-8")
    return bars


def finalize_strategies(reviewed, technical, capital_mode, context) -> tuple[list[dict], set[str]]:
    strategies = []
    news_codes: set[str] = set()
    capital_label = "资金面" if capital_mode == "flow" else "成交活跃"
    for item in reviewed:
        spec = item["spec"]
        if item["status"] != "ok":
            strategies.append(_empty_strategy(spec, item["reason"], capital_label))
            continue
        rows = item["rows"].copy()
        scored = []
        dropped = 0
        for _, row in rows.iterrows():
            tech = technical.get(row["code"])
            if spec.get("require_ma60") and not (tech and tech.get("above_ma60")):
                dropped += 1
                continue
            scored.append(_candidate(spec, row, tech, capital_mode, capital_label))
        scored.sort(key=lambda candidate: (candidate["score"] is None, -(candidate["score"] or 0), candidate["code"]))
        shown = scored[:MAX_SHOW]
        news_codes.update(candidate["code"] for candidate in shown)
        rules = describe_rules(spec, item["thresholds"], item["relaxed"], context)
        if spec.get("require_ma60") and len(rows):
            rules.append(f"初步名单 {len(rows)} 只里，有 {dropped} 只没有站上 60 日均线或日 K 不可用，已去掉。")
        strategies.append(
            {
                "id": spec["id"],
                "cycle": spec["cycle"],
                "cycle_label": next(cycle["label"] for cycle in CYCLES if cycle["id"] == spec["cycle"]),
                "horizon": next(cycle["horizon"] for cycle in CYCLES if cycle["id"] == spec["cycle"]),
                "name": spec["name"],
                "blurb": spec["blurb"],
                "status": "ok",
                "reason": None,
                "relaxed": item["relaxed"],
                "rules": rules,
                "matched": item["matched"],
                "reviewed": len(rows),
                "weights": WEIGHTS[spec["cycle"]],
                "capital_label": capital_label,
                "candidates": shown,
            }
        )
    return strategies, news_codes


def _empty_strategy(spec, reason: str, capital_label: str) -> dict:
    return {
        "id": spec["id"],
        "cycle": spec["cycle"],
        "cycle_label": next(cycle["label"] for cycle in CYCLES if cycle["id"] == spec["cycle"]),
        "horizon": next(cycle["horizon"] for cycle in CYCLES if cycle["id"] == spec["cycle"]),
        "name": spec["name"],
        "blurb": spec["blurb"],
        "status": "unavailable",
        "reason": reason,
        "relaxed": False,
        "rules": [],
        "matched": 0,
        "reviewed": 0,
        "weights": WEIGHTS[spec["cycle"]],
        "capital_label": capital_label,
        "candidates": [],
    }


def _candidate(spec, row, tech, capital_mode: str, capital_label: str) -> dict:
    tech_score = None if tech is None else tech.get("score")
    scores = {
        "fundamental": fundamental_score(_val(row.get("pe")), _val(row.get("pb")), _val(row.get("roe"))),
        "technical": tech_score,
        "capital": _capital(row, capital_mode),
        "industry": _val(row.get("industry_score")),
    }
    total, score_notes = combine_scores(scores, spec["cycle"])
    facts = {
        "price": _round(row.get("price")),
        "change_pct": _round(row.get("change_pct")),
        "turnover": _round(row.get("turnover")),
        "volume_ratio": _round(row.get("volume_ratio")),
        "amount": _round(row.get("amount"), 0),
        "mcap": _round(row.get("mcap"), 0),
        "pe": _round(row.get("pe")),
        "pb": _round(row.get("pb")),
        "ret60": _round(row.get("ret60")),
        "ret_ytd": _round(row.get("ret_ytd")),
        "industry": row.get("industry") if isinstance(row.get("industry"), str) else None,
        "industry_median": _round(row.get("industry_median")),
        "industry_rank": _round(row.get("industry_rank"), 0),
        "industry_groups": int(row.get("industry_groups") or 0),
        "main_net": _round(row.get("main_net"), 0),
        "roe": _round(row.get("roe")),
        "rev_yoy": _round(row.get("rev_yoy")),
        "profit_yoy": _round(row.get("profit_yoy")),
        "profit_yoy_used": _round(row.get("profit_yoy_used")),
        "debt": _round(row.get("debt")),
        "div_yield": _round(row.get("div_yield")),
        "div_progress": None if pd.isna(row.get("div_progress")) else str(row.get("div_progress")),
        "drawdown": None if tech is None else tech.get("drawdown"),
        "above_ma60": None if tech is None else bool(tech.get("above_ma60")),
    }
    reasons, risks = explain(spec, facts, scores, capital_label)
    labels = {
        "fundamental": "基本面",
        "technical": "技术面",
        "capital": capital_label,
        "industry": "行业位置",
    }
    return {
        "code": row["code"],
        "name": row["name"],
        "score": total,
        "score_notes": score_notes,
        "dimensions": [
            {
                "id": key,
                "label": labels[key],
                "score": None if scores[key] is None else round(scores[key], 1),
                "weight": WEIGHTS[spec["cycle"]][key],
            }
            for key in ("fundamental", "technical", "capital", "industry")
        ],
        "reasons": reasons,
        "risks": risks,
        "facts": facts,
    }


def _capital(row, mode: str):
    if mode == "flow":
        if _val(row.get("main_net")) is None:
            return None
        return flow_score(_val(row.get("flow_pct")), _val(row.get("main_ratio")))
    return activity_score(_val(row.get("amount_pct")), _val(row.get("volume_ratio")), _val(row.get("turnover")))


def load_news(codes: set[str]) -> dict[str, dict]:
    found = {}
    for index, code in enumerate(sorted(codes), start=1):
        try:
            items = fetch_news(code)
            found[code] = {"items": items, "status": "ok" if items else "empty"}
        except Exception as exc:  # noqa: BLE001
            log.warning("%s 资讯失败：%s", code, exc)
            found[code] = {"items": [], "status": "unavailable"}
        if index % 20 == 0 or index == len(codes):
            log.info("资讯 %s/%s", index, len(codes))
    return found


def build_market(mood, session: str, as_of: date, limit_up, limit_down) -> dict:
    change = mood["change_pct"].dropna()
    up = int((change > 0).sum())
    down = int((change < 0).sum())
    flat = int((change == 0).sum())
    total = up + down + flat
    up_ratio = up / total if total else None
    industry_ratio = _industry_up_ratio(mood)
    amount_vs = None
    if session == "close":
        try:
            amount_vs = fetch_index_amount(as_of)
        except FetchError as exc:
            log.warning("%s", exc)
    parts = {
        "breadth": None if up_ratio is None else up_ratio * 100,
        "limits": None
        if limit_up is None or limit_down is None
        else (50 if limit_up + limit_down == 0 else limit_up / (limit_up + limit_down) * 100),
        "volume": limit_volume_score(amount_vs),
        "industry": None if industry_ratio is None else industry_ratio * 100,
    }
    temp, temp_notes = temperature(parts)
    stage = sentiment_stage(up_ratio, limit_up, limit_down)
    try:
        indices = fetch_indices()
    except FetchError as exc:
        log.warning("%s", exc)
        indices = []
    return {
        "temperature": temp,
        "temperature_notes": temp_notes,
        "stage": stage,
        "summary": _market_summary(up_ratio, limit_up, limit_down, amount_vs, industry_ratio, stage, session),
        "indices": indices,
        "stats": {
            "up": up,
            "down": down,
            "flat": flat,
            "up_ratio": None if up_ratio is None else round(up_ratio, 4),
            "limit_up": limit_up,
            "limit_down": limit_down,
            "amount": _round(mood["amount"].sum(), 0),
            "amount_vs_ma20": None if amount_vs is None else round(amount_vs, 3),
            "industry_up_ratio": None if industry_ratio is None else round(industry_ratio, 4),
        },
        "boards": _industry_boards(mood),
    }


def _industry_groups(frame: pd.DataFrame) -> pd.DataFrame:
    grouped = (
        frame.dropna(subset=["industry", "change_pct"])
        .groupby("industry", as_index=False)["change_pct"]
        .agg(median="median", count="count")
    )
    return grouped[grouped["count"] >= 3]


def _industry_up_ratio(frame: pd.DataFrame):
    grouped = _industry_groups(frame)
    if grouped.empty:
        return None
    return float((grouped["median"] > 0).mean())


def _industry_boards(frame: pd.DataFrame) -> list[dict]:
    grouped = _industry_groups(frame).sort_values("median", ascending=False)
    return [
        {"name": row.industry, "median": round(float(row.median), 2), "count": int(row.count)}
        for row in grouped.itertuples(index=False)
    ]


def _market_summary(up_ratio, limit_up, limit_down, amount_vs, industry_ratio, stage, session) -> str:
    bits = []
    if up_ratio is not None:
        bits.append(f"上涨家数占比 {up_ratio:.0%}")
    if limit_up is not None:
        bits.append(f"涨停 {limit_up} 家")
    if limit_down is not None:
        bits.append(f"跌停 {limit_down} 家")
    if amount_vs is not None:
        bits.append(f"成交额约为近 20 个交易日均额的 {amount_vs:.2f} 倍")
    elif session == "intraday":
        bits.append("还在盘中，成交额不与整日均额比较")
    if industry_ratio is not None:
        bits.append(f"行业上涨占比 {industry_ratio:.0%}")
    sentence = "，".join(bits) + "。" if bits else ""
    if stage:
        sentence += f"当前阶段标为{stage}。"
    else:
        sentence += "涨停或跌停数据不完整，今天不标注阶段。"
    return sentence


def write_snapshot(snapshot: dict) -> None:
    target = DATA_DIR / "snapshot.json"
    temporary = DATA_DIR / "snapshot.json.tmp"
    temporary.write_text(json.dumps(_scrub(snapshot), ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    temporary.replace(target)


def _scrub(value):
    if isinstance(value, dict):
        return {key: _scrub(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_scrub(item) for item in value]
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        return value
    if value is pd.NA:
        return None
    try:
        if pd.isna(value):
            return None
    except TypeError:
        pass
    return value


def _val(value):
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except TypeError:
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _round(value, digits=2):
    number = _val(value)
    if number is None:
        return None
    return round(number, digits)


if __name__ == "__main__":
    sys.exit(main())
