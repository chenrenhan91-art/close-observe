"""纯规则：股票池、策略、情绪阶段、评分。不访问网络。"""

from __future__ import annotations

import math
from datetime import date, datetime

import pandas as pd

YI = 100_000_000
MAX_SHOW = 8
MAX_REVIEW = 40
MIN_BEFORE_RELAX = 5

WEIGHTS = {
    "short": {"technical": 45, "capital": 30, "industry": 15, "fundamental": 10},
    "medium": {"technical": 35, "fundamental": 30, "capital": 20, "industry": 15},
    "long": {"fundamental": 55, "industry": 15, "technical": 20, "capital": 10},
}

CYCLES = (
    {"id": "short", "label": "短线", "horizon": "大约 1–5 个交易日"},
    {"id": "medium", "label": "中线", "horizon": "大约 20–60 个交易日"},
    {"id": "long", "label": "长线", "horizon": "大约一季以上"},
)

DISCLAIMER = (
    "收盘观察只提供筛选、评分和公开资讯链接，不是投资建议，"
    "也不提供买入价、止损价、目标价或仓位。"
    "数据来自免费公开接口，可能延迟、缺失或与交易所行情不一致。"
)

STRATEGIES = (
    {
        "id": "limitup-watch",
        "cycle": "short",
        "name": "涨停后观察",
        "blurb": "看强势股在涨停之后、当天没有继续封板时的状态。",
        "needs": ("limits",),
        "base": {
            "limit_pool": True,
            "change": (-2, 5),
            "turnover": (3, 25),
            "amount_min": 1 * YI,
        },
        "relax": {
            "limit_pool": True,
            "change": (-3, 6),
            "turnover": (2, 30),
            "amount_min": 0.5 * YI,
        },
    },
    {
        "id": "industry-active",
        "cycle": "short",
        "name": "行业活跃",
        "blurb": "看当天涨幅适中、成交活跃，并且行业本身也靠前的股票。",
        "needs": ("industry",),
        "base": {
            "change": (2, 7),
            "volume_ratio_min": 1.5,
            "turnover": (3, 20),
            "amount_top": 0.15,
            "industry_top": 0.30,
        },
        "relax": {
            "change": (1, 8),
            "volume_ratio_min": 1.2,
            "turnover": (2, 25),
            "amount_top": 0.25,
            "industry_top": 0.50,
        },
    },
    {
        "id": "trend-core",
        "cycle": "medium",
        "name": "趋势中军",
        "blurb": "看市值不小、60 日趋势还在，并且收盘价站在 60 日均线之上的股票。",
        "needs": (),
        "require_ma60": True,
        "base": {
            "ret60": (5, 40),
            "ytd_gt": 0,
            "change_max": 6,
            "mcap_min": 50 * YI,
            "turnover": (1, 10),
            "amount_min": 1 * YI,
        },
        "relax": {
            "ret60": (0, 50),
            "ytd_gt": 0,
            "change_max": 8,
            "mcap_min": 30 * YI,
            "turnover": (0.5, 15),
            "amount_min": 0.5 * YI,
        },
    },
    {
        "id": "growth",
        "cycle": "medium",
        "name": "业绩成长",
        "blurb": "看最新报告期收入和利润都在增长，估值还在设定区间里的股票。",
        "needs": ("financials",),
        "base": {
            "rev_yoy_ge": 15,
            "profit_yoy_ge": 15,
            "pe_max": 40,
            "pb_max": 8,
            "ret60_gt": 0,
            "mcap_min": 50 * YI,
        },
        "relax": {
            "rev_yoy_ge": 8,
            "profit_yoy_ge": 8,
            "pe_max": 50,
            "pb_max": 10,
            "ret60_gt": 0,
            "mcap_min": 30 * YI,
        },
    },
    {
        "id": "quality",
        "cycle": "long",
        "name": "质量价值",
        "blurb": "看盈利、负债和估值同时过线的公司。",
        "needs": ("financials", "balance"),
        "base": {
            "roe_ge": 10,
            "rev_yoy_ge": 0,
            "profit_yoy_ge": 0,
            "debt_le": 60,
            "pe_max": 20,
            "pb_max": 3,
            "mcap_min": 100 * YI,
            "ret60_ge": -15,
        },
        "relax": {
            "roe_ge": 8,
            "rev_yoy_ge": 0,
            "profit_yoy_ge": 0,
            "debt_le": 70,
            "pe_max": 25,
            "pb_max": 4,
            "mcap_min": 80 * YI,
            "ret60_ge": -20,
        },
    },
    {
        "id": "dividend",
        "cycle": "long",
        "name": "高股息",
        "blurb": "看最近年报现金股息率达到设定水平、最新报告净利润同比不低于零的公司。",
        "needs": ("dividend", "financials"),
        "base": {
            "yield_ge": 3,
            "pe_max": 25,
            "profit_yoy_ge": 0,
            "mcap_min": 100 * YI,
            "ytd_ge": -20,
        },
        "relax": {
            "yield_ge": 2,
            "pe_max": 30,
            "profit_yoy_ge": 0,
            "mcap_min": 80 * YI,
            "ytd_ge": -30,
        },
    },
)


def clean_code(value) -> str:
    text = str(value).strip()
    if text.endswith(".0"):
        text = text[:-2]
    digits = "".join(ch for ch in text if ch.isdigit())
    if not digits:
        return text
    return digits[-6:].zfill(6)


def is_target_board(code: str) -> bool:
    return str(code).startswith(("60", "68", "00", "30"))


def is_risk_name(name: str) -> bool:
    text = str(name).upper().replace(" ", "")
    if "ST" in text or "退" in text:
        return True
    return text.startswith("N") or text.startswith("C")


def trading_days_listed(list_date, as_of: date, trade_dates: list[date]) -> int | None:
    if list_date is None or pd.isna(list_date):
        return None
    if isinstance(list_date, datetime):
        list_date = list_date.date()
    if isinstance(list_date, pd.Timestamp):
        list_date = list_date.date()
    dates = list(trade_dates)
    if as_of not in dates:
        dates.append(as_of)
        dates.sort()
    asof_i = dates.index(as_of)
    listed_i = next((i for i, item in enumerate(dates) if item >= list_date), len(dates))
    if listed_i >= len(dates):
        return 0
    return asof_i - listed_i + 1


def maybe_to_percent(series: pd.Series) -> pd.Series:
    values = pd.to_numeric(series, errors="coerce")
    sample = values.dropna()
    if sample.empty:
        return values
    if float(sample.median()) < 0.5 and float(sample.quantile(0.9)) < 2:
        return values * 100
    return values


def normalize_yield(series: pd.Series) -> pd.Series:
    values = pd.to_numeric(series, errors="coerce")
    sample = values.dropna()
    if sample.empty:
        return values
    if float(sample.median()) < 0.2:
        return values * 100
    return values


def sentiment_stage(up_ratio: float | None, limit_up: int | None, limit_down: int | None) -> str | None:
    """按冰点、高潮、退潮、修复、常态的顺序判断。涨停或跌停缺失时不猜阶段。"""
    if up_ratio is None or limit_up is None or limit_down is None:
        return None
    if up_ratio < 0.25 or (limit_down >= 50 and limit_up < 30):
        return "冰点"
    if limit_up >= 80 and limit_down <= 10 and up_ratio > 0.6:
        return "高潮"
    if limit_up < 40 and limit_down >= 20 and up_ratio < 0.45:
        return "退潮"
    if up_ratio >= 0.45 and limit_down < 20 and limit_up < 80:
        return "修复"
    return "常态"


def temperature(parts: dict[str, float | None]) -> tuple[float | None, list[str]]:
    weights = {"breadth": 0.35, "limits": 0.25, "volume": 0.20, "industry": 0.20}
    used = {key: value for key, value in parts.items() if value is not None and key in weights}
    if not used:
        return None, ["温度的输入都不可用。"]
    total_weight = sum(weights[key] for key in used)
    score = sum(weights[key] * used[key] for key in used) / total_weight
    missing = [key for key in weights if key not in used]
    labels = {"breadth": "涨跌家数", "limits": "涨跌停", "volume": "成交额对比", "industry": "行业上涨占比"}
    notes = [f"温度没有计入{labels[key]}。" for key in missing]
    return round(score, 1), notes


def limit_volume_score(amount_vs_ma20: float | None) -> float | None:
    if amount_vs_ma20 is None:
        return None
    return max(0.0, min(100.0, 50 + (amount_vs_ma20 - 1) * 60))


def in_top_fraction(rank, groups: int, fraction: float) -> bool:
    if rank is None or (isinstance(rank, float) and math.isnan(rank)) or groups <= 0:
        return False
    return float(rank) <= math.ceil(groups * fraction)


def attach_industry(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.copy()
    grouped = (
        result.dropna(subset=["industry", "change_pct"])
        .groupby("industry", as_index=False)["change_pct"]
        .agg(industry_median="median", industry_n="count")
    )
    grouped = grouped[grouped["industry_n"] >= 3].copy()
    if grouped.empty:
        result["industry_median"] = pd.NA
        result["industry_n"] = 0
        result["industry_rank"] = pd.NA
        result["industry_groups"] = 0
        result["industry_score"] = pd.NA
        return result
    grouped["industry_rank"] = grouped["industry_median"].rank(ascending=False, method="min")
    grouped["industry_groups"] = len(grouped)
    grouped["industry_score"] = grouped["industry_median"].rank(pct=True, method="average") * 100
    merged = result.drop(
        columns=[
            column
            for column in (
                "industry_median",
                "industry_n",
                "industry_rank",
                "industry_groups",
                "industry_score",
            )
            if column in result.columns
        ]
    ).merge(grouped, on="industry", how="left")
    merged["industry_n"] = merged["industry_n"].fillna(0).astype(int)
    merged["industry_groups"] = int(grouped["industry_groups"].iloc[0])
    return merged


def attach_amount_rank(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.copy()
    result["amount_rank"] = result["amount"].rank(ascending=False, method="min")
    result["amount_n"] = int(result["amount"].notna().sum())
    result["amount_pct"] = result["amount"].rank(pct=True, method="average") * 100
    return result


def _between(series: pd.Series, bounds: tuple[float, float]) -> pd.Series:
    low, high = bounds
    return series.ge(low) & series.le(high)


def apply_rules(frame: pd.DataFrame, thresholds: dict) -> pd.Series:
    mask = pd.Series(True, index=frame.index)
    if thresholds.get("limit_pool"):
        mask &= frame["in_limit_pool"].fillna(False) & ~frame["sealed_today"].fillna(False)
    if "change" in thresholds:
        mask &= _between(frame["change_pct"], thresholds["change"])
    if "change_max" in thresholds:
        mask &= frame["change_pct"].le(thresholds["change_max"])
    if "turnover" in thresholds:
        mask &= _between(frame["turnover"], thresholds["turnover"])
    if "amount_min" in thresholds:
        mask &= frame["amount"].ge(thresholds["amount_min"])
    if "volume_ratio_min" in thresholds:
        mask &= frame["volume_ratio"].ge(thresholds["volume_ratio_min"])
    if "ret60" in thresholds:
        mask &= _between(frame["ret60"], thresholds["ret60"])
    if "ret60_gt" in thresholds:
        mask &= frame["ret60"].gt(thresholds["ret60_gt"])
    if "ret60_ge" in thresholds:
        mask &= frame["ret60"].ge(thresholds["ret60_ge"])
    if "ytd_gt" in thresholds:
        mask &= frame["ret_ytd"].gt(thresholds["ytd_gt"])
    if "ytd_ge" in thresholds:
        mask &= frame["ret_ytd"].ge(thresholds["ytd_ge"])
    if "mcap_min" in thresholds:
        mask &= frame["mcap"].ge(thresholds["mcap_min"])
    if "pe_max" in thresholds:
        mask &= frame["pe"].gt(0) & frame["pe"].le(thresholds["pe_max"])
    if "pb_max" in thresholds:
        mask &= frame["pb"].gt(0) & frame["pb"].le(thresholds["pb_max"])
    if "rev_yoy_ge" in thresholds:
        mask &= frame["rev_yoy"].ge(thresholds["rev_yoy_ge"])
    if "profit_yoy_ge" in thresholds:
        mask &= frame["profit_yoy_used"].ge(thresholds["profit_yoy_ge"])
    if "roe_ge" in thresholds:
        mask &= frame["roe"].ge(thresholds["roe_ge"])
    if "debt_le" in thresholds:
        mask &= frame["debt"].le(thresholds["debt_le"])
    if "yield_ge" in thresholds:
        mask &= frame["div_yield"].ge(thresholds["yield_ge"])
    if "amount_top" in thresholds:
        mask &= frame.apply(
            lambda row: in_top_fraction(row["amount_rank"], int(row["amount_n"]), thresholds["amount_top"]),
            axis=1,
        )
    if "industry_top" in thresholds:
        mask &= frame.apply(
            lambda row: in_top_fraction(
                row["industry_rank"], int(row["industry_groups"]), thresholds["industry_top"]
            ),
            axis=1,
        )
    return mask.fillna(False)


def choose_thresholds(frame: pd.DataFrame, spec: dict) -> tuple[dict, bool, int]:
    base_count = int(apply_rules(frame, spec["base"]).sum())
    if base_count >= MIN_BEFORE_RELAX:
        return spec["base"], False, base_count
    relaxed_count = int(apply_rules(frame, spec["relax"]).sum())
    return spec["relax"], True, relaxed_count


def missing_dependency(spec: dict, flags: dict[str, bool]) -> str | None:
    labels = {
        "limits": "涨停池没有拿到，这张名单今天不可用。",
        "industry": "行业分类没有拿到，这张名单今天不可用。",
        "financials": "业绩报表没有拿到，这张名单今天不可用。",
        "balance": "资产负债表没有拿到，这张名单今天不可用。",
        "dividend": "全市场分红报表没有拿到，这张名单今天不可用。",
    }
    for need in spec["needs"]:
        if not flags.get(need, False):
            return labels[need]
    return None


def yi_text(yuan: float) -> str:
    value = yuan / YI
    if value >= 10:
        return f"{value:.0f} 亿元"
    text = f"{value:.1f}".rstrip("0").rstrip(".")
    return f"{text} 亿元"


def pretty_report(value: str | None) -> str | None:
    if not value or len(value) != 8 or not value.isdigit():
        return value
    return f"{value[:4]}-{value[4:6]}-{value[6:]}"


def describe_rules(spec: dict, thresholds: dict, relaxed: bool, context: dict | None = None) -> list[str]:
    context = context or {}
    lines: list[str] = []
    if thresholds.get("limit_pool"):
        lines.append("近 3 个交易日进过涨停池，并且不在今日涨停池。")
    if "change" in thresholds:
        low, high = thresholds["change"]
        lines.append(f"今日涨幅 {low:g}% 到 {high:g}%。")
    if "change_max" in thresholds:
        lines.append(f"今日涨幅不超过 {thresholds['change_max']:g}%。")
    if "turnover" in thresholds:
        low, high = thresholds["turnover"]
        lines.append(f"换手率 {low:g}% 到 {high:g}%。")
    if "amount_min" in thresholds:
        lines.append(f"成交额不低于 {yi_text(thresholds['amount_min'])}。")
    if "volume_ratio_min" in thresholds:
        lines.append(f"量比大于 {thresholds['volume_ratio_min']:g}。")
    if "amount_top" in thresholds:
        lines.append(f"成交额处于可交易股票池前 {thresholds['amount_top'] * 100:.0f}%。")
    if "industry_top" in thresholds:
        lines.append(
            f"所属行业今日涨幅处于全部行业前 {thresholds['industry_top'] * 100:.0f}%。"
            "行业涨幅用同行业个股涨跌幅的中位数，行业内至少 3 只股票。"
        )
    if "ret60" in thresholds:
        low, high = thresholds["ret60"]
        lines.append(f"60 日涨幅 {low:g}% 到 {high:g}%。")
    if "ret60_gt" in thresholds:
        lines.append(f"60 日涨幅大于 {thresholds['ret60_gt']:g}%。")
    if "ret60_ge" in thresholds:
        lines.append(f"60 日涨跌幅不低于 {thresholds['ret60_ge']:g}%。")
    if "ytd_gt" in thresholds:
        lines.append("年初至今上涨。")
    if "ytd_ge" in thresholds:
        lines.append(f"年初至今涨跌幅不低于 {thresholds['ytd_ge']:g}%。")
    if "mcap_min" in thresholds:
        lines.append(f"总市值不低于 {yi_text(thresholds['mcap_min'])}。")
    if "pe_max" in thresholds:
        lines.append(f"动态市盈率大于 0 且不超过 {thresholds['pe_max']:g}。")
    if "pb_max" in thresholds:
        lines.append(f"市净率大于 0 且不超过 {thresholds['pb_max']:g}。")
    if "rev_yoy_ge" in thresholds and "profit_yoy_ge" in thresholds and spec["id"] != "dividend":
        lines.append(
            f"最新报告期营收同比不低于 {thresholds['rev_yoy_ge']:g}%，"
            f"净利润同比不低于 {thresholds['profit_yoy_ge']:g}%。"
        )
    elif "profit_yoy_ge" in thresholds:
        lines.append(f"净利润同比不低于 {thresholds['profit_yoy_ge']:g}%。")
    if "roe_ge" in thresholds:
        lines.append(f"净资产收益率不低于 {thresholds['roe_ge']:g}%。")
    if "debt_le" in thresholds:
        lines.append(f"资产负债率不高于 {thresholds['debt_le']:g}%。")
    if "yield_ge" in thresholds:
        lines.append(f"最近年报现金股息率不低于 {thresholds['yield_ge']:g}%。")
    if spec.get("require_ma60"):
        lines.append("收盘价在 60 日均线之上。均线用前复权日 K，这一条不放宽。")
    report = pretty_report(context.get("report_date"))
    if report and spec["id"] in {"growth", "quality"}:
        lines.append(f"业绩报告期 {report}。")
    balance = pretty_report(context.get("balance_date"))
    if balance and spec["id"] == "quality":
        lines.append(f"资产负债表报告期 {balance}。")
    dividend = pretty_report(context.get("dividend_date"))
    if dividend and spec["id"] == "dividend":
        lines.append(f"分红报告期 {dividend}。")
    if relaxed:
        lines.append("原阈值筛出不足 5 只，已放宽到这一档。")
    return lines


def _band_pe(pe: float | None) -> float | None:
    if pe is None or (isinstance(pe, float) and math.isnan(pe)):
        return None
    if pe <= 0:
        return 18
    if pe <= 15:
        return 92
    if pe <= 25:
        return 80
    if pe <= 40:
        return 66
    if pe <= 60:
        return 48
    return 30


def _band_pb(pb: float | None) -> float | None:
    if pb is None or (isinstance(pb, float) and math.isnan(pb)):
        return None
    if pb <= 0:
        return 24
    if pb <= 1:
        return 90
    if pb <= 3:
        return 78
    if pb <= 5:
        return 64
    if pb <= 8:
        return 50
    return 32


def _band_roe(roe: float | None) -> float | None:
    if roe is None or (isinstance(roe, float) and math.isnan(roe)):
        return None
    if roe >= 20:
        return 95
    if roe >= 15:
        return 86
    if roe >= 10:
        return 74
    if roe >= 5:
        return 56
    return 34


def _blend(parts: list[tuple[float | None, float]]) -> float | None:
    used = [(score, weight) for score, weight in parts if score is not None]
    if not used:
        return None
    return sum(score * weight for score, weight in used) / sum(weight for _, weight in used)


def fundamental_score(pe, pb, roe) -> float | None:
    # Valuation alone does not establish profitability or a fundamental score.
    if _band_roe(roe) is None or (_band_pe(pe) is None and _band_pb(pb) is None):
        return None
    return _blend([(_band_pe(pe), 0.40), (_band_pb(pb), 0.25), (_band_roe(roe), 0.35)])


def activity_score(amount_pct, volume_ratio, turnover) -> float | None:
    volume_score = None
    if volume_ratio is not None and not (isinstance(volume_ratio, float) and math.isnan(volume_ratio)):
        if volume_ratio >= 3:
            volume_score = 55
        elif volume_ratio >= 1.5:
            volume_score = 85
        elif volume_ratio >= 1:
            volume_score = 72
        else:
            volume_score = 42
    turnover_score = None
    if turnover is not None and not (isinstance(turnover, float) and math.isnan(turnover)):
        if 2 <= turnover <= 8:
            turnover_score = 84
        elif 1 <= turnover < 2 or 8 < turnover <= 15:
            turnover_score = 66
        else:
            turnover_score = 40
    return _blend(
        [
            (None if amount_pct is None or (isinstance(amount_pct, float) and math.isnan(amount_pct)) else amount_pct, 0.50),
            (volume_score, 0.30),
            (turnover_score, 0.20),
        ]
    )


def flow_score(net_pct, ratio) -> float | None:
    ratio_score = None
    if ratio is not None and not (isinstance(ratio, float) and math.isnan(ratio)):
        ratio_score = max(0.0, min(100.0, (ratio + 10) / 20 * 100))
    net_score = None if net_pct is None or (isinstance(net_pct, float) and math.isnan(net_pct)) else net_pct
    return _blend([(net_score, 0.70), (ratio_score, 0.30)])


def technical_from_bars(bars: list[dict]) -> dict | None:
    if len(bars) < 60:
        return None
    window = bars[-120:]
    try:
        closes = [float(item["close"]) for item in window]
        highs = [float(item["high"]) for item in window]
        volumes = [float(item["volume"]) for item in window]
    except (KeyError, TypeError, ValueError):
        return None
    if any(not math.isfinite(v) or v <= 0 for v in closes + highs) or any(not math.isfinite(v) or v < 0 for v in volumes):
        return None
    close = closes[-1]
    ma5 = sum(closes[-5:]) / 5
    ma20 = sum(closes[-20:]) / 20
    ma60 = sum(closes[-60:]) / 60
    if close > ma5 > ma20 > ma60:
        alignment = 100
    elif close > ma20 > ma60:
        alignment = 82
    elif close > ma60:
        alignment = 64
    elif close > ma20:
        alignment = 42
    else:
        alignment = 26
    peak = max(highs) if highs else None
    drawdown = (peak - close) / peak if peak else None
    if drawdown is None:
        drawdown_score = None
    elif drawdown <= 0.08:
        drawdown_score = 92
    elif drawdown <= 0.15:
        drawdown_score = 76
    elif drawdown <= 0.25:
        drawdown_score = 56
    else:
        drawdown_score = 34
    volume_score = None
    recent = volumes[-20:]
    base = sum(recent) / len(recent)
    if base > 0:
        ratio = (sum(volumes[-5:]) / 5) / base
        if 1 <= ratio <= 1.8:
            volume_score = 86
        elif 0.8 <= ratio < 1 or 1.8 < ratio <= 2.5:
            volume_score = 66
        else:
            volume_score = 46
    score = _blend([(alignment, 0.45), (drawdown_score, 0.35), (volume_score, 0.20)])
    if score is None:
        return None
    return {
        "score": score,
        "above_ma60": close > ma60,
        "drawdown": drawdown,
        "ma60": ma60,
    }


def combine_scores(scores: dict[str, float | None], cycle: str) -> tuple[float | None, list[str]]:
    weights = WEIGHTS[cycle]
    used = {key: value for key, value in scores.items() if key in weights and value is not None and math.isfinite(value) and 0 <= value <= 100}
    if not used:
        return None, ["四个维度都不可用，没有总分。"]
    total_weight = sum(weights[key] for key in used)
    if total_weight < 70 or len(used) < 2:
        return None, [f"可用评分权重仅 {total_weight}%，不足 70%，不计算总分。"]
    total = sum(weights[key] * used[key] for key in used) / total_weight
    labels = {
        "fundamental": "基本面",
        "technical": "技术面",
        "capital": "资金面",
        "industry": "行业位置",
    }
    missing = [labels[key] for key in weights if key not in used]
    notes = [f"缺{'、'.join(missing)}，总分已按其余维度重新计算。"] if missing else []
    return round(total, 1), notes


def proxy_technical(ret60) -> float | None:
    if ret60 is None or (isinstance(ret60, float) and math.isnan(ret60)):
        return None
    return max(0.0, min(100.0, (float(ret60) + 20) / 60 * 100))


def _fmt_pct(value, digits=1) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "不可用"
    return f"{float(value):.{digits}f}%"


def _fmt_num(value, digits=2) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "不可用"
    return f"{float(value):.{digits}f}"


def explain(spec: dict, facts: dict, scores: dict[str, float | None], capital_label: str) -> tuple[list[str], list[str]]:
    reasons: list[str] = []
    if spec["id"] == "limitup-watch":
        reasons.append(f"近 3 个交易日出现在涨停池，今日涨幅 {_fmt_pct(facts.get('change_pct'))}，今天没有封死涨停。")
    elif spec["id"] == "industry-active":
        reasons.append(
            f"今日涨幅 {_fmt_pct(facts.get('change_pct'))}，量比 {_fmt_num(facts.get('volume_ratio'))}，"
            f"行业「{facts.get('industry') or '未知'}」中位数涨幅 {_fmt_pct(facts.get('industry_median'))}。"
        )
    elif spec["id"] == "trend-core":
        reasons.append(
            f"60 日涨幅 {_fmt_pct(facts.get('ret60'))}，收盘价在 60 日均线之上。"
        )
    elif spec["id"] == "growth":
        reasons.append(
            f"营收同比 {_fmt_pct(facts.get('rev_yoy'))}，净利润同比 {_fmt_pct(facts.get('profit_yoy'))}。"
        )
    elif spec["id"] == "quality":
        reasons.append(
            f"净资产收益率 {_fmt_pct(facts.get('roe'))}，资产负债率 {_fmt_pct(facts.get('debt'))}，"
            f"动态市盈率 {_fmt_num(facts.get('pe'))}。"
        )
    else:
        reasons.append(
            f"最近年报现金股息率 {_fmt_pct(facts.get('div_yield'))}，净利润同比 {_fmt_pct(facts.get('profit_yoy_used'))}。"
        )

    labeled = {
        "fundamental": "基本面",
        "technical": "技术面",
        "capital": capital_label,
        "industry": "行业位置",
    }
    available = [(key, value) for key, value in scores.items() if value is not None]
    if available:
        key, value = max(available, key=lambda item: item[1])
        reasons.append(f"{labeled[key]} {value:.0f} 分，是这四项里最高的一项。")
    if facts.get("ret60") is not None and spec["id"] != "trend-core":
        reasons.append(f"60 日涨跌幅 {_fmt_pct(facts.get('ret60'))}。")
    elif facts.get("mcap") is not None:
        reasons.append(f"总市值 {yi_text(facts['mcap'])}。")

    risks: list[str] = []
    pe = facts.get("pe")
    if pe is not None and pe <= 0:
        risks.append("动态市盈率不为正，当期利润为负或数据异常。")
    elif pe is not None and pe > 60:
        risks.append(f"动态市盈率 {_fmt_num(pe)}，高于 60。")
    debt = facts.get("debt")
    if debt is not None and debt > 70:
        risks.append(f"资产负债率 {_fmt_pct(debt)}，高于 70%。")
    turnover = facts.get("turnover")
    if turnover is not None and turnover > 15:
        risks.append(f"换手率 {_fmt_pct(turnover)}，高于 15%。")
    ret60 = facts.get("ret60")
    if ret60 is not None and ret60 > 50:
        risks.append(f"60 日涨幅 {_fmt_pct(ret60)}，价格已经离开低位。")
    drawdown = facts.get("drawdown")
    if drawdown is not None and drawdown > 0.25:
        risks.append(f"距近 120 日高点回撤 {drawdown * 100:.0f}%。")
    volume_ratio = facts.get("volume_ratio")
    if volume_ratio is not None and volume_ratio > 3:
        risks.append(f"量比 {_fmt_num(volume_ratio)}，高于 3。")
    groups = facts.get("industry_groups") or 0
    rank = facts.get("industry_rank")
    if groups and rank is not None and rank > math.ceil(groups * 0.7):
        risks.append(f"所属行业今日涨幅处于全部行业后 30%。")
    progress = facts.get("div_progress")
    if spec["id"] == "dividend" and progress and "实施" not in str(progress):
        risks.append(f"分红方案进度是「{progress}」，还不是已实施。")
    if scores.get("technical") is None:
        risks.append("近 120 日行情没有补全，技术面不可用。")
    if not risks:
        risks.append("规则没有标出额外的数字风险。资讯和公告需要自己打开原文看。")
    return reasons[:3], risks[:3]
