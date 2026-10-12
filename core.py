# -*- coding: utf-8 -*-
"""数据与规则核心：行情取数、分红计算、纪律触发判断。

零第三方依赖，只用 Python 3 标准库。
标的与规则全部来自 config.json，新增股票只改配置，不动代码。

用法：
    python3 core.py            # 抓数、计算，写出 data/latest.json 与 data/history.json
"""
import json
import os
import urllib.request
from datetime import datetime, timedelta

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
LATEST_PATH = os.path.join(DATA_DIR, "latest.json")
HISTORY_PATH = os.path.join(DATA_DIR, "history.json")

UA = {"User-Agent": "Mozilla/5.0"}

# 规则类型 -> 展示用的标签
RULE_KINDS = [
    ("buy_yield", "buy"),
    ("buy_price", "buy"),
    ("watch_price", "watch"),
    ("sell_yield_below", "sell"),
    ("sell_price_above", "sell"),
]


# ---------------------------------------------------------------- 配置

def load_config():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = json.load(f)
    cfg.setdefault("settings", {})
    cfg.setdefault("targets", [])
    return cfg


def rules_of(target):
    return target.get("rules") or {}


# ---------------------------------------------------------------- 工具

def tcode(code):
    """A股代码 -> 腾讯行情代码。"""
    return ("sh" if code.startswith(("6", "5", "9")) else "sz") + code


def http_get(url, headers=None, enc="utf-8"):
    req = urllib.request.Request(url, headers=headers or UA)
    return urllib.request.urlopen(req, timeout=20).read().decode(enc, "ignore")


def ensure_dirs():
    os.makedirs(DATA_DIR, exist_ok=True)


# ---------------------------------------------------------------- 行情取数

def fetch_quotes(codes):
    """一次请求拿全部标的的行情，返回 {code: {...}}。"""
    if not codes:
        return {}
    url = "https://qt.gtimg.cn/q=" + ",".join(tcode(c) for c in codes)
    raw = http_get(url, enc="gbk")
    out = {}
    for line in raw.strip().split("\n"):
        if "=" not in line:
            continue
        body = line.split("=", 1)[1].strip().strip(";").strip('"')
        f = body.split("~")
        if len(f) < 33:
            continue
        try:
            price = float(f[3])
            prev = float(f[4])
            pct = float(f[32])
        except ValueError:
            continue
        out[f[2]] = {
            "code": f[2],
            "name": f[1],
            "price": price,
            "prev_close": prev,
            "change": round(price - prev, 3),
            "change_pct": pct,
            "quote_time": f[30],
        }
    return out


# ---------------------------------------------------------------- 分红取数

def fetch_dividends(code):
    """抓取分红送配明细。每股金额 = PRETAX_BONUS_RMB / 10。"""
    url = (
        "https://datacenter-web.eastmoney.com/api/data/v1/get?"
        "reportName=RPT_SHAREBONUS_DET&columns=ALL"
        "&filter=(SECURITY_CODE%3D%22" + code + "%22)"
        "&pageSize=100&sortColumns=EX_DIVIDEND_DATE&sortTypes=-1"
    )
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://data.eastmoney.com/"}
    data = json.loads(http_get(url, headers=headers))
    rows = (data.get("result") or {}).get("data") or []
    out = []
    for r in rows:
        amt = r.get("PRETAX_BONUS_RMB")
        if amt in (None, "", 0):
            continue
        try:
            per_share = round(float(amt) / 10.0, 6)
        except (TypeError, ValueError):
            continue
        rep = (r.get("REPORT_DATE") or "")[:10]
        if not rep:
            continue
        out.append(
            {
                "report_date": rep,
                "ex_date": (r.get("EX_DIVIDEND_DATE") or "")[:10],
                "per_share": per_share,
                "progress": r.get("ASSIGN_PROGRESS") or "",
                "profile": r.get("IMPL_PLAN_PROFILE") or "",
            }
        )
    return out


# ---------------------------------------------------------------- 分红口径

def _realized(ex_date, today):
    if not ex_date:
        return False
    try:
        return datetime.strptime(ex_date, "%Y-%m-%d").date() <= today
    except ValueError:
        return False


def calc_annual(rows, today=None):
    """主口径：最近一个含年报(12-31)的完整年度，汇总该年度每股分红（只算已除权落地的）。"""
    today = today or datetime.now().date()
    annual = [r for r in rows if r["report_date"].endswith("12-31") and _realized(r["ex_date"], today)]
    if not annual:
        return None, None
    year = max(r["report_date"] for r in annual)[:4]
    total = sum(r["per_share"] for r in rows if r["report_date"][:4] == year and _realized(r["ex_date"], today))
    return year, round(total, 4)


def calc_ttm(rows, today=None):
    """辅助口径：最近12个月内已除权除息的每股分红合计。"""
    today = today or datetime.now().date()
    cutoff = today - timedelta(days=365)
    total = 0.0
    for r in rows:
        if not r["ex_date"]:
            continue
        try:
            d = datetime.strptime(r["ex_date"], "%Y-%m-%d").date()
        except ValueError:
            continue
        if cutoff <= d <= today:
            total += r["per_share"]
    return round(total, 4)


# ---------------------------------------------------------------- 触发判断

def evaluate(target, price, y_annual):
    """按标的规则逐条判断，返回触发项列表。y_annual 为小数（0.045 = 4.5%）。"""
    rules = rules_of(target)
    div_per_share = (y_annual * price) if (y_annual is not None and price) else None
    out = []

    for key, kind in RULE_KINDS:
        for item in rules.get(key, []):
            if not isinstance(item, (list, tuple)) or len(item) != 2:
                continue
            th, action = item
            th = float(th)

            if key == "buy_yield":
                metric, label = "股息率", "≥%.2f%%" % (th * 100)
                active = y_annual is not None and y_annual >= th
                tp = round(div_per_share / th, 2) if div_per_share else None
            elif key == "buy_price":
                metric, label = "价格", "≤%.2f" % th
                active = price is not None and price <= th
                tp = th
            elif key == "watch_price":
                metric, label = "价格", "≤%.2f" % th
                active = price is not None and price <= th
                tp = th
            elif key == "sell_yield_below":
                metric, label = "股息率", "<%.2f%%" % (th * 100)
                active = y_annual is not None and y_annual < th
                tp = round(div_per_share / th, 2) if div_per_share else None
            else:  # sell_price_above
                metric, label = "价格", "≥%.2f" % th
                active = price is not None and price >= th
                tp = th

            out.append(
                {
                    "kind": kind,
                    "metric": metric,
                    "threshold": label,
                    "action": action,
                    "active": bool(active),
                    "trigger_price": tp,
                }
            )
    return out


def next_trigger_price(target, price, y_annual):
    """下一档买入触发价 = 每股分红 ÷ 目标股息率。"""
    rules = rules_of(target)
    buys = sorted(float(i[0]) for i in rules.get("buy_yield", []) if isinstance(i, (list, tuple)) and len(i) == 2)
    if not buys or price is None or y_annual is None or y_annual <= 0:
        return None
    div = y_annual * price
    for th in buys:
        tp = div / th
        if price > tp:
            return {"target_yield": th, "price": round(tp, 2)}
    return None


# ---------------------------------------------------------------- 组装

def build_state(cfg=None):
    cfg = cfg or load_config()
    settings = cfg.get("settings", {})
    targets = cfg.get("targets", [])
    codes = [t["code"] for t in targets]

    try:
        quotes = fetch_quotes(codes)
    except Exception as e:
        print("[warn] 行情接口失败：", e)
        quotes = {}

    today = datetime.now().date().isoformat()
    now = datetime.now().isoformat(timespec="seconds")

    out_targets = []
    for t in targets:
        code = t["code"]
        q = quotes.get(code)
        price = q["price"] if q else None

        div_annual = div_ttm = div_year = None
        try:
            rows = fetch_dividends(code)
            div_year, div_annual = calc_annual(rows)
            div_ttm = calc_ttm(rows)
        except Exception as e:
            print("[warn] 分红接口失败", code, e)

        y_annual = round(div_annual / price * 100, 2) if (div_annual and price) else None
        y_ttm = round(div_ttm / price * 100, 2) if (div_ttm and price) else None

        div_note = None
        if div_annual and div_ttm and div_annual > 0:
            if abs(div_ttm - div_annual) / div_annual > 0.25:
                div_note = "两口径差异大（除息节奏错位），触发判断以年度口径为准"

        y_frac = y_annual / 100 if y_annual is not None else None
        out_targets.append(
            {
                "code": code,
                "name": t.get("name", code),
                "category": t.get("category", ""),
                "held": bool(t.get("held")),
                "role": t.get("role", ""),
                "price": price,
                "change_pct": q["change_pct"] if q else None,
                "quote_time": q["quote_time"] if q else None,
                "div_year": div_year,
                "div_annual": div_annual,
                "div_ttm": div_ttm,
                "yield_annual": y_annual,
                "yield_ttm": y_ttm,
                "triggers": evaluate(t, price, y_frac),
                "next_trigger": next_trigger_price(t, price, y_frac),
                "note": rules_of(t).get("note"),
                "div_note": div_note,
            }
        )

    active_count = sum(1 for x in out_targets for tr in x["triggers"] if tr["active"])

    return {
        "ts": now,
        "quote_date": today,
        "settings": {
            "position_cap": settings.get("position_cap"),
            "cash": settings.get("cash"),
            "emergency_reserve": settings.get("emergency_reserve"),
        },
        "active_count": active_count,
        "targets": out_targets,
    }


def append_history(state):
    """把当日快照并入 data/history.json（按 日期+代码 去重更新）。"""
    ensure_dirs()
    hist = {"dates": [], "series": {}}
    if os.path.exists(HISTORY_PATH):
        try:
            with open(HISTORY_PATH, encoding="utf-8") as f:
                hist = json.load(f)
        except (ValueError, OSError):
            pass
    hist.setdefault("dates", [])
    hist.setdefault("series", {})

    date = state["quote_date"]
    if date not in hist["dates"]:
        hist["dates"].append(date)
        hist["dates"].sort()

    for t in state["targets"]:
        if t["price"] is None:
            continue
        series = hist["series"].setdefault(t["code"], [])
        rec = {
            "date": date,
            "price": t["price"],
            "yield_annual": t["yield_annual"],
            "yield_ttm": t["yield_ttm"],
        }
        for i, old in enumerate(series):
            if old.get("date") == date:
                series[i] = rec
                break
        else:
            series.append(rec)
        series.sort(key=lambda r: r["date"])

    with open(HISTORY_PATH, "w", encoding="utf-8") as f:
        json.dump(hist, f, ensure_ascii=False, indent=1)
    return len(hist["dates"])


def write_latest(state, history_days=0):
    ensure_dirs()
    state = dict(state)
    state["history_days"] = history_days
    with open(LATEST_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    return LATEST_PATH


def run():
    """完整流程：抓数 -> 计算 -> 写 latest.json + history.json。"""
    state = build_state()
    days = append_history(state)
    path = write_latest(state, days)
    print("已写出 %s（%d 个标的，触发 %d 条，历史 %d 天）"
          % (path, len(state["targets"]), state["active_count"], days))
    return state


if __name__ == "__main__":
    run()
