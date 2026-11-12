"""免费公开数据抓取。失败就抛出 FetchError，不返回假行情。"""

from __future__ import annotations

import logging
import pickle
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests

from pipeline.rules import clean_code, maybe_to_percent, normalize_yield

log = logging.getLogger("close")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Referer": "https://quote.eastmoney.com/",
}


class FetchError(RuntimeError):
    pass


def attempt(label: str, fn, tries: int = 3):
    last = None
    for index in range(tries):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - 数据源失败要重试后再放弃
            last = exc
            log.warning("%s 第 %s 次失败：%s", label, index + 1, exc)
            time.sleep(1.2 * (index + 1))
    raise FetchError(f"{label}不可用：{last}")


def _rows(diff) -> list[dict]:
    if not diff:
        return []
    if isinstance(diff, dict):
        return list(diff.values())
    return list(diff)


def _get_json(url: str, params: dict, timeout: int = 20) -> dict:
    try:
        from curl_cffi import requests as curl_requests

        response = curl_requests.get(
            url,
            params=params,
            headers={**HEADERS, "Connection": "close"},
            timeout=timeout,
            impersonate="chrome",
        )
    except Exception:
        response = requests.get(
            url,
            params=params,
            headers={**HEADERS, "Connection": "close"},
            timeout=timeout,
        )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise FetchError("返回不是 JSON 对象")
    return payload


def fetch_spot() -> pd.DataFrame:
    """全市场快照。价格和估值来自新浪，60 日涨跌幅、年初至今和量比来自腾讯。"""
    frame = _fetch_sina_spot()
    extras = _fetch_tencent_extras(frame["code"].tolist())
    if not extras.empty:
        frame = frame.drop(columns=["volume_ratio", "ret60", "ret_ytd"]).merge(extras, on="code", how="left")
    frame["industry"] = None
    log.info("快照 %s 只", len(frame))
    return frame


def _fetch_sina_spot() -> pd.DataFrame:
    rows: list[dict] = []
    for page in range(1, 90):
        batch = attempt(f"新浪行情第 {page} 页", lambda page=page: _sina_page(page), tries=4)
        if not batch:
            break
        rows.extend(batch)
        log.info("新浪行情第 %s 页，累计 %s", page, len(rows))
        if len(batch) < 80:
            break
        time.sleep(0.2)
    if len(rows) < 1000:
        raise FetchError(f"新浪行情只有 {len(rows)} 只")
    frame = pd.DataFrame(rows)
    frame["code"] = frame["code"].map(clean_code)
    frame["name"] = frame["name"].astype(str).str.strip()
    frame["price"] = pd.to_numeric(frame["trade"], errors="coerce")
    frame["change_pct"] = pd.to_numeric(frame["changepercent"], errors="coerce")
    frame["volume"] = pd.to_numeric(frame["volume"], errors="coerce")
    frame["amount"] = pd.to_numeric(frame["amount"], errors="coerce")
    frame["high"] = pd.to_numeric(frame["high"], errors="coerce")
    frame["turnover"] = pd.to_numeric(frame["turnoverratio"], errors="coerce")
    frame["pe"] = pd.to_numeric(frame["per"], errors="coerce")
    frame["pb"] = pd.to_numeric(frame["pb"], errors="coerce")
    # 新浪总市值单位是万元。
    frame["mcap"] = pd.to_numeric(frame["mktcap"], errors="coerce") * 10000
    previous = pd.to_numeric(frame["settlement"], errors="coerce")
    low = pd.to_numeric(frame["low"], errors="coerce")
    frame["amplitude"] = (frame["high"] - low) / previous * 100
    frame["volume_ratio"] = pd.NA
    frame["ret60"] = pd.NA
    frame["ret_ytd"] = pd.NA
    keep = frame[
        ["code", "name", "price", "change_pct", "volume", "amount", "amplitude", "turnover", "pe", "volume_ratio", "high", "mcap", "pb", "ret60", "ret_ytd"]
    ]
    return keep.drop_duplicates("code", keep="first").reset_index(drop=True)


def _sina_page(page: int) -> list[dict]:
    response = requests.get(
        "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData",
        params={"page": page, "num": 80, "sort": "symbol", "asc": 1, "node": "hs_a"},
        headers={"User-Agent": "Mozilla/5.0", "Referer": "https://vip.stock.finance.sina.com.cn/"},
        timeout=20,
    )
    response.raise_for_status()
    text = response.text.strip()
    if text in {"", "null", "[]"}:
        return []
    data = response.json()
    if not isinstance(data, list):
        raise FetchError("新浪行情不是列表")
    return data


def _fetch_tencent_extras(codes: list[str]) -> pd.DataFrame:
    rows = []
    tradable = [code for code in codes if str(code).startswith(("6", "0", "3"))]
    for start in range(0, len(tradable), 40):
        batch = tradable[start : start + 40]
        symbols = [("sh" if code.startswith("6") else "sz") + code for code in batch]
        try:
            response = requests.get(
                "https://qt.gtimg.cn/q=" + ",".join(symbols),
                headers={"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com"},
                timeout=20,
            )
            response.raise_for_status()
            response.encoding = "gbk"
        except Exception as exc:  # noqa: BLE001
            log.warning("腾讯行情批次失败：%s", exc)
            time.sleep(0.4)
            continue
        for line in response.text.split(";"):
            if "~" not in line:
                continue
            parts = line.split("~")
            if len(parts) < 72:
                continue
            rows.append(
                {
                    "code": clean_code(parts[2]),
                    "volume_ratio": _number(parts[49]),
                    "ret_ytd": _number(parts[62]),
                    "ret60": _number(parts[71]),
                }
            )
        time.sleep(0.12)
    log.info("腾讯补充 %s 只", len(rows))
    if not rows:
        return pd.DataFrame(columns=["code", "volume_ratio", "ret_ytd", "ret60"])
    return pd.DataFrame(rows).drop_duplicates("code", keep="first")


def fetch_indices() -> list[dict]:
    response = requests.get(
        "https://hq.sinajs.cn/list=s_sh000001,s_sz399001,s_sz399006",
        headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn"},
        timeout=20,
    )
    response.raise_for_status()
    response.encoding = "gbk"
    wanted = {"sh000001": "000001", "sz399001": "399001", "sz399006": "399006"}
    indices = []
    for line in response.text.split(";"):
        if "=" not in line or '"' not in line:
            continue
        symbol = line.split("=")[0].split("_")[-1]
        if symbol not in wanted:
            continue
        parts = line.split('"')[1].split(",")
        if len(parts) < 4:
            continue
        indices.append(
            {
                "code": wanted[symbol],
                "name": parts[0],
                "price": _number(parts[1]),
                "change_pct": _number(parts[3]),
            }
        )
    if len(indices) < 3:
        raise FetchError("指数行情不完整")
    return indices


def fetch_index_amount(as_of: date) -> float | None:
    """上证 + 深证最近一个完整交易日成交额，相对此前 20 日均额。盘中由调用方决定是否使用。"""
    start = (as_of - timedelta(days=90)).strftime("%Y%m%d")
    end = as_of.strftime("%Y%m%d")

    def one(secid: str) -> pd.Series:
        payload = _get_json(
            "https://push2his.eastmoney.com/api/qt/stock/kline/get",
            {
                "secid": secid,
                "ut": "7eea3edcaed734bea9cbfc24409ed989",
                "fields1": "f1,f2,f3,f4,f5,f6",
                "fields2": "f51,f52,f53,f54,f55,f56,f57",
                "klt": 101,
                "fqt": 0,
                "beg": start,
                "end": end,
            },
        )
        klines = ((payload.get("data") or {}).get("klines")) or []
        if not klines:
            raise FetchError(f"{secid} 没有成交额")
        amounts = []
        dates = []
        for line in klines:
            parts = line.split(",")
            dates.append(parts[0])
            amounts.append(float(parts[6]))
        series = pd.Series(amounts, index=pd.to_datetime(dates))
        return series

    shanghai = attempt("上证成交额", lambda: one("1.000001"))
    shenzhen = attempt("深证成交额", lambda: one("0.399001"))
    total = shanghai.add(shenzhen, fill_value=0).sort_index()
    if len(total) < 21:
        return None
    # 最后一根如果就是 as_of 且调用方仍要求比较，用它之前的 20 日。
    previous = total.iloc[:-1].tail(20)
    if previous.empty or float(previous.mean()) == 0:
        return None
    latest = float(total.iloc[-1])
    latest_day = total.index[-1].date()
    if latest_day != as_of:
        return None
    return latest / float(previous.mean())


def fetch_trade_dates() -> list[date]:
    import akshare as ak

    frame = attempt("交易日历", ak.tool_trade_date_hist_sina)
    dates = [item for item in frame["trade_date"].tolist() if item is not None]
    if len(dates) < 100:
        raise FetchError("交易日历过短")
    return sorted(dates)


def fetch_listings() -> dict[str, date]:
    import akshare as ak

    path = _cache_path("listings")
    if path.exists():
        cached = pickle.loads(path.read_bytes())
        if len(cached) >= 1000:
            log.info("使用今日上市日期缓存 %s 只", len(cached))
            return cached
    listings: dict[str, date] = {}
    sh_main = attempt("上交所主板名单", lambda: ak.stock_info_sh_name_code(symbol="主板A股"))
    sh_star = attempt("上交所科创板名单", lambda: ak.stock_info_sh_name_code(symbol="科创板"))
    sz_list = attempt("深交所名单", lambda: ak.stock_info_sz_name_code(symbol="A股列表"))
    for frame, code_col, date_col in (
        (sh_main, "证券代码", "上市日期"),
        (sh_star, "证券代码", "上市日期"),
        (sz_list, "A股代码", "A股上市日期"),
    ):
        if code_col not in frame.columns or date_col not in frame.columns:
            raise FetchError("上市日期字段缺失")
        for code, listed in zip(frame[code_col], frame[date_col], strict=False):
            parsed = pd.to_datetime(listed, errors="coerce")
            if pd.isna(parsed):
                continue
            listings[clean_code(code)] = parsed.date()
    if len(listings) < 1000:
        raise FetchError(f"上市日期只有 {len(listings)} 条")
    log.info("上市日期 %s 只", len(listings))
    path.write_bytes(pickle.dumps(listings))
    return listings


def fetch_pool(kind: str, day: date) -> set[str]:
    import akshare as ak

    stamp = day.strftime("%Y%m%d")
    if kind == "up":
        frame = attempt(f"{stamp} 涨停池", lambda: ak.stock_zt_pool_em(date=stamp))
    else:
        frame = attempt(f"{stamp} 跌停池", lambda: ak.stock_zt_pool_dtgc_em(date=stamp))
    if frame is None or frame.empty or "代码" not in frame.columns:
        return set()
    return {clean_code(code) for code in frame["代码"].tolist()}


def fetch_financials(as_of: date) -> tuple[pd.DataFrame | None, str | None]:
    import akshare as ak

    cached = _read_pickle("financials")
    if cached is not None:
        log.info("使用今日业绩报表缓存")
        return cached
    for report in _report_candidates(as_of):
        try:
            frame = attempt(f"业绩报表 {report}", lambda report=report: ak.stock_yjbb_em(date=report), tries=2)
        except FetchError as exc:
            log.warning("%s", exc)
            continue
        if frame is None or frame.empty or "股票代码" not in frame.columns:
            continue
        frame = frame.copy()
        frame["code"] = frame["股票代码"].map(clean_code)
        frame["roe"] = maybe_to_percent(frame["净资产收益率"])
        frame["rev_yoy"] = maybe_to_percent(frame["营业总收入-同比增长"])
        frame["profit_yoy"] = maybe_to_percent(frame["净利润-同比增长"])
        if "所处行业" in frame.columns:
            frame["industry_report"] = frame["所处行业"].map(lambda value: value if isinstance(value, str) and value else None)
        else:
            frame["industry_report"] = None
        keep = frame[["code", "roe", "rev_yoy", "profit_yoy", "industry_report"]].drop_duplicates("code", keep="last")
        log.info("业绩报表 %s，%s 只", report, len(keep))
        _write_pickle("financials", (keep, report))
        return keep, report
    return None, None


def fetch_balance(as_of: date) -> tuple[pd.DataFrame | None, str | None]:
    import akshare as ak

    cached = _read_pickle("balance")
    if cached is not None:
        log.info("使用今日资产负债表缓存")
        return cached
    for report in _report_candidates(as_of):
        try:
            frame = attempt(f"资产负债表 {report}", lambda report=report: ak.stock_zcfz_em(date=report), tries=2)
        except FetchError as exc:
            log.warning("%s", exc)
            continue
        if frame is None or frame.empty or "股票代码" not in frame.columns or "资产负债率" not in frame.columns:
            continue
        frame = frame.copy()
        frame["code"] = frame["股票代码"].map(clean_code)
        frame["debt"] = maybe_to_percent(frame["资产负债率"])
        keep = frame[["code", "debt"]].drop_duplicates("code", keep="last")
        log.info("资产负债表 %s，%s 只", report, len(keep))
        _write_pickle("balance", (keep, report))
        return keep, report
    return None, None


def fetch_dividends(as_of: date) -> tuple[pd.DataFrame | None, str | None]:
    import akshare as ak

    cached = _read_pickle("dividends")
    if cached is not None:
        log.info("使用今日分红报表缓存")
        return cached
    years = [as_of.year - 1, as_of.year - 2] if as_of.month >= 5 else [as_of.year - 2, as_of.year - 3]
    for report in [f"{year}1231" for year in years]:
        try:
            frame = attempt(f"分红报表 {report}", lambda report=report: ak.stock_fhps_em(date=report), tries=2)
        except FetchError as exc:
            log.warning("%s", exc)
            continue
        if frame is None or frame.empty or "代码" not in frame.columns:
            continue
        frame = frame.copy()
        frame["code"] = frame["代码"].map(clean_code)
        frame["div_yield"] = normalize_yield(frame["现金分红-股息率"])
        frame["div_profit_yoy"] = maybe_to_percent(frame["净利润同比增长"])
        frame["div_progress"] = frame["方案进度"].map(lambda value: None if pd.isna(value) else str(value))
        keep = frame[["code", "div_yield", "div_profit_yoy", "div_progress"]].drop_duplicates("code", keep="last")
        log.info("分红报表 %s，%s 只", report, len(keep))
        _write_pickle("dividends", (keep, report))
        return keep, report
    return None, None


def fetch_fund_flow() -> pd.DataFrame | None:
    import akshare as ak

    try:
        frame = attempt("个股资金流排名", lambda: ak.stock_individual_fund_flow_rank(indicator="今日"), tries=2)
    except FetchError as exc:
        log.warning("%s", exc)
        return None
    if frame is None or frame.empty or "代码" not in frame.columns:
        return None
    frame = frame.copy()
    frame["code"] = frame["代码"].map(clean_code)
    frame["main_net"] = pd.to_numeric(frame["今日主力净流入-净额"], errors="coerce")
    frame["main_ratio"] = pd.to_numeric(frame["今日主力净流入-净占比"], errors="coerce")
    keep = frame[["code", "main_net", "main_ratio"]].drop_duplicates("code", keep="first")
    log.info("资金流 %s 只", len(keep))
    return keep


def fetch_kline(code: str, as_of: date) -> list[dict]:
    prefix = "sh" if code.startswith("6") else "sz"
    symbol = prefix + code
    response = requests.get(
        "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get",
        params={"param": f"{symbol},day,,,140,qfq"},
        headers={"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com"},
        timeout=20,
    )
    response.raise_for_status()
    payload = response.json()
    series = ((payload.get("data") or {}).get(symbol)) or {}
    raw = series.get("qfqday") or series.get("day") or []
    bars = []
    for item in raw:
        if len(item) < 6:
            continue
        bars.append({"date": item[0], "close": float(item[2]), "high": float(item[3]), "volume": float(item[5])})
    if as_of:
        bars = [bar for bar in bars if bar["date"] <= as_of.isoformat()]
    return bars


def fetch_news(code: str) -> list[dict]:
    import akshare as ak

    frame = ak.stock_news_em(symbol=code)
    if frame is None or frame.empty:
        return []
    items = []
    for row in frame.to_dict("records"):
        title = _clean_text(row.get("新闻标题"))
        url = str(row.get("新闻链接") or "")
        if not title or not url.startswith("http"):
            continue
        items.append(
            {
                "title": title,
                "time": _clean_text(row.get("发布时间")),
                "source": _clean_text(row.get("文章来源")),
                "url": url,
            }
        )
    deduped = []
    seen = set()
    for item in items:
        if item["title"] in seen:
            continue
        seen.add(item["title"])
        deduped.append(item)
    return deduped[:8]


def _report_candidates(as_of: date) -> list[str]:
    quarters = []
    for year in (as_of.year - 1, as_of.year):
        for month, day in ((3, 31), (6, 30), (9, 30), (12, 31)):
            quarters.append(date(year, month, day))
    ready = [item for item in quarters if item <= as_of]
    return [item.strftime("%Y%m%d") for item in ready[-4:]][::-1]


def _cache_path(name: str) -> Path:
    folder = Path(__file__).resolve().parents[1] / "data" / "cache"
    folder.mkdir(parents=True, exist_ok=True)
    day = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
    return folder / f"{name}-{day}.pkl"


def _read_pickle(name: str):
    path = _cache_path(name)
    if not path.exists():
        return None
    return pickle.loads(path.read_bytes())


def _write_pickle(name: str, value) -> None:
    _cache_path(name).write_bytes(pickle.dumps(value))


def _read_frame_cache(name: str) -> pd.DataFrame | None:
    path = _cache_path(name)
    if not path.exists():
        return None
    frame = pd.read_pickle(path)
    if frame is None or frame.empty:
        return None
    return frame


def _write_frame_cache(name: str, frame: pd.DataFrame) -> None:
    frame.to_pickle(_cache_path(name))


def _number(value):
    try:
        if value is None or value == "-":
            return None
        number = float(value)
    except (TypeError, ValueError):
        return None
    if pd.isna(number):
        return None
    return number


def _clean_text(value) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    return " ".join(str(value).replace("<em>", "").replace("</em>", "").split())
