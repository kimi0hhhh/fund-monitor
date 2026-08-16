# -*- coding: utf-8 -*-
"""
基金 AI 分析模块（投研团队流水线）
- 历史净值与技术指标
- LLM 客户端（DeepSeek 等 OpenAI 兼容）
- 多角色分析 prompt（技术/基本面/新闻/情绪 → 多空辩论 → 交易员 → 风控 → 主管）
- 预测存储与复盘
"""
import json
import os
import re
import sys
import time
import uuid
from datetime import datetime, timedelta

import requests

if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

CONFIG_FILE = os.path.join(BASE_DIR, "analysis_config.json")
HISTORY_FILE = os.path.join(BASE_DIR, "analysis_history.json")
SIGNALS_FILE = os.path.join(BASE_DIR, "signals.json")
TRADE_REVIEW_FILE = os.path.join(BASE_DIR, "trade_review.json")
TRADE_LESSONS_FILE = os.path.join(BASE_DIR, "trade_lessons.json")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Referer": "http://fundf10.eastmoney.com/",
}


# ================== 配置管理 ==================
DEFAULT_CONFIG = {
    "provider": "openai",
    "api_key": "",
    "model": "deepseek-v4-pro",
    "base_url": "https://api.deepseek.com",
}


def _upgrade_model(model):
    """把已弃用的旧模型名自动升级到 DeepSeek V4-Pro（不影响用户显式选择的 flash 等新模型）"""
    if not model or str(model).strip() in ("deepseek-chat", "deepseek-reasoner", "deepseek-coder"):
        return "deepseek-v4-pro"
    return model


def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            for k, v in DEFAULT_CONFIG.items():
                cfg.setdefault(k, v)
            cfg["model"] = _upgrade_model(cfg.get("model"))
            return cfg
        except Exception:
            pass
    return dict(DEFAULT_CONFIG)


def save_config(cfg):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def is_configured():
    cfg = load_config()
    return bool(cfg.get("api_key", "").strip())


# ================== LLM 客户端 ==================
def llm_chat(messages, temperature=0.6, max_tokens=2500):
    """OpenAI 兼容调用（DeepSeek/硅基/OpenAI/Ollama 等）"""
    cfg = load_config()
    if not cfg.get("api_key"):
        return {"ok": False, "msg": "未配置 API key，请在设置中填写"}
    try:
        from openai import OpenAI
        client = OpenAI(
            api_key=cfg["api_key"],
            base_url=cfg.get("base_url") or "https://api.deepseek.com",
        )
        model = cfg.get("model") or "deepseek-v4-pro"
        # DeepSeek 系列（pro/flash 等）默认开启 thinking，思考链会污染 JSON 输出，
        # 必须显式关闭。非 DeepSeek 的 provider（OpenAI/Ollama 等）不传，避免不兼容。
        kwargs = dict(model=model, messages=messages,
                      temperature=temperature, max_tokens=max_tokens)
        if "deepseek" in model.lower():
            kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
        r = client.chat.completions.create(**kwargs)
        msg = r.choices[0].message
        content = msg.content or ""
        # 修复隐藏 bug：不再用 reasoning_content 兜底。reasoning_content 是思考链
        # （thinking），不是最终答案，绝不能当作 JSON 返回；content 为空应明确报错。
        if not content.strip():
            return {"ok": False, "msg": "LLM 返回空内容（思考模式可能未关闭）"}
        return {"ok": True, "content": content, "usage": getattr(r, "usage", None)}
    except Exception as e:
        return {"ok": False, "msg": f"LLM 调用失败: {str(e)[:200]}"}


# ================== 历史净值数据 ==================
_history_cache = {}

def fetch_history(code, days=90):
    """拉取基金最近 N 天的日净值（按时间正序），自动分页（当日缓存）"""
    key = "%s_%s" % (code, datetime.now().strftime("%Y-%m-%d"))
    if key in _history_cache:
        return _history_cache[key][-days:]
    out = []
    page = 1
    page_size = 30
    seen_dates = set()
    while len(out) < days and page <= 6:
        try:
            r = requests.get(
                f"http://api.fund.eastmoney.com/f10/lsjz?fundCode={code}&pageIndex={page}"
                f"&pageSize={page_size}&mode=0",
                headers=HEADERS, timeout=10,
            )
            d = r.json()
            items = d.get("Data", {}).get("LSJZList", [])
            if not items:
                break
            for it in items:
                date = it.get("FSRQ", "")
                if not date or date in seen_dates:
                    continue
                seen_dates.add(date)
                try:
                    nav = float(it.get("DWJZ", 0))
                except (TypeError, ValueError):
                    continue
                try:
                    pct = float(it.get("JZZZL", 0)) if it.get("JZZZL") not in (None, "") else 0.0
                except (TypeError, ValueError):
                    pct = 0.0
                out.append({"date": date, "nav": nav, "pct": pct})
            page += 1
        except Exception:
            break
    # 接口返回是倒序，翻成正序
    out.reverse()
    _history_cache[key] = out[-90:]
    return out[-days:]


# ================== 持仓穿透 ==================
_holdings_cache = {}

def fetch_holdings(code):
    """拉取基金前十大持仓股（东财移动端接口，当日缓存）

    返回 [{code, name, weight, industry, change_type, change_pct}]
    """
    key = "%s_%s" % (code, datetime.now().strftime("%Y-%m-%d"))
    if key in _holdings_cache:
        return _holdings_cache[key]
    try:
        h = {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
                           "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 Safari/604.1",
             "Referer": "https://fundf10.eastmoney.com/"}
        url = (f"https://fundmobapi.eastmoney.com/FundMNewApi/FundMNInverstPosition"
               f"?FCODE={code}&deviceid=Wap&plat=Wap&product=EFund&version=2.0.0")
        r = requests.get(url, headers=h, timeout=10)
        d = r.json()
        stocks = (d.get("Datas") or {}).get("fundStocks", []) or []
        out = []
        for s in stocks[:10]:
            try:
                out.append({
                    "code": s.get("GPDM", ""),
                    "name": s.get("GPJC", ""),
                    "weight": float(s.get("JZBL", 0) or 0),
                    "industry": s.get("INDEXNAME", ""),
                    "change_type": s.get("PCTNVCHGTYPE", ""),
                    "change_pct": s.get("PCTNVCHG", ""),
                })
            except (TypeError, ValueError):
                continue
        _holdings_cache[key] = out
        return out
    except Exception:
        return []


# ================== 技术指标 ==================
def _ma(seq, n):
    if len(seq) < n:
        return None
    return round(sum(seq[-n:]) / n, 4)


def _rsi(seq, n=14):
    if len(seq) < n + 1:
        return None
    gains, losses = 0.0, 0.0
    for i in range(-n, 0):
        diff = seq[i] - seq[i - 1]
        if diff >= 0:
            gains += diff
        else:
            losses -= diff
    if losses == 0:
        return 100.0
    rs = gains / losses
    return round(100 - 100 / (1 + rs), 2)


def compute_metrics(history):
    """对历史净值序列计算技术指标"""
    if not history or len(history) < 5:
        return {}
    navs = [h["nav"] for h in history]
    pcts = [h["pct"] for h in history]
    last = navs[-1]
    ma5 = _ma(navs, 5)
    ma10 = _ma(navs, 10)
    ma20 = _ma(navs, 20)
    rsi14 = _rsi(navs, 14)

    # 趋势：MA5 上穿/下穿 MA20、价格相对均线
    if ma5 and ma10 and ma20:
        trend = "多头" if ma5 > ma10 > ma20 else ("空头" if ma5 < ma10 < ma20 else "震荡")
    else:
        trend = "数据不足"

    # 最大回撤
    peak = navs[0]
    mdd = 0.0
    for n in navs:
        if n > peak:
            peak = n
        dd = (peak - n) / peak
        if dd > mdd:
            mdd = dd

    # 年化波动率（用日收益率）
    if len(pcts) >= 5:
        mean = sum(pcts) / len(pcts)
        var = sum((p - mean) ** 2 for p in pcts) / max(len(pcts) - 1, 1)
        vol = round((var ** 0.5) * (252 ** 0.5), 2)
    else:
        vol = None

    # 区间涨幅
    period_change = round((last - navs[0]) / navs[0] * 100, 2) if navs[0] else 0

    # ===== 风险度量 =====
    # Sharpe（年化收益-无风险2% / 年化波动）
    sharpe = None
    if vol and vol > 0 and len(navs) > 1:
        ann_ret = ((last / navs[0]) ** (252.0 / len(navs)) - 1) * 100
        sharpe = round((ann_ret - 2.0) / vol, 2)

    # VaR 95%（历史模拟法：日收益率 5% 分位）
    var_95 = None
    if len(pcts) >= 20:
        sp = sorted(pcts)
        var_95 = round(sp[int(len(sp) * 0.05)], 2)

    # 下行风险（仅负收益的标准差，年化）
    downside = None
    neg = [p for p in pcts if p < 0]
    if len(neg) >= 5:
        nm = sum(neg) / len(neg)
        nv = sum((p - nm) ** 2 for p in neg) / max(len(neg) - 1, 1)
        downside = round((nv ** 0.5) * (252 ** 0.5), 2)

    # 最大连跌天数
    max_down_days = 0
    cur = 0
    for p in pcts:
        if p < 0:
            cur += 1
            max_down_days = max(max_down_days, cur)
        else:
            cur = 0

    # 连涨/连跌天数
    streak = 0
    direction = None
    for h in reversed(history[-10:]):
        if h["pct"] > 0:
            if direction == "up":
                streak += 1
            else:
                direction = "up"
                streak = 1
            break
        elif h["pct"] < 0:
            if direction == "down":
                streak += 1
            else:
                direction = "down"
                streak = 1
            break

    return {
        "last_nav": last,
        "ma5": ma5, "ma10": ma10, "ma20": ma20,
        "rsi14": rsi14,
        "trend": trend,
        "max_drawdown": round(mdd * 100, 2),
        "volatility": vol,
        "period_change_pct": period_change,
        "streak_days": streak,
        "streak_dir": direction or "flat",
        "data_points": len(history),
        "sharpe": sharpe,
        "var_95": var_95,
        "downside_risk": downside,
        "max_consec_down_days": max_down_days,
    }


def compute_risk_metrics(history, bench_history=None):
    """计算风险度量指标（纯 Python，无需 AI）
    history: 基金日净值序列
    bench_history: 基准(沪深300 ETF 510300)日净值序列，可选，用于 Beta/Alpha
    """
    import math, statistics
    if not history or len(history) < 5:
        return {}
    pcts = [h["pct"] for h in history if h.get("pct") is not None]
    if len(pcts) < 5:
        return {}
    n = len(pcts)
    avg = statistics.mean(pcts)
    std = statistics.stdev(pcts) if n > 1 else 0.0
    rf_daily = 0.02 / 252  # 无风险利率 2% 年化
    sharpe = (avg - rf_daily) / std * math.sqrt(252) if std > 0 else 0.0
    var95 = -1.645 * std if std > 0 else 0.0  # 单日 95% VaR（正值=亏损%）
    # 最大连跌天数
    max_dd_days = cur = 0
    for p in pcts:
        cur = cur + 1 if p < 0 else 0
        max_dd_days = max(max_dd_days, cur)
    # 下行风险（只看负收益的标准差，年化）
    neg = [p for p in pcts if p < 0]
    downside = statistics.stdev(neg) * math.sqrt(252) if len(neg) > 1 else 0.0
    # Beta / Alpha（相对基准）
    beta = alpha = None
    if bench_history and len(bench_history) >= 5:
        bmap = {b["date"]: b.get("pct") for b in bench_history if b.get("pct") is not None}
        af, ab = [], []
        for h in history:
            bp = bmap.get(h["date"])
            if bp is not None and h.get("pct") is not None:
                af.append(h["pct"])
                ab.append(bp)
        if len(af) > 5:
            mb = statistics.mean(ab)
            cov = sum((f - avg) * (b - mb) for f, b in zip(af, ab)) / len(af)
            vb = statistics.variance(ab)
            beta = cov / vb if vb > 0 else 0.0
            alpha = ((avg - rf_daily) - beta * (mb - rf_daily)) * 252  # 年化 alpha
    return {
        "sharpe": round(sharpe, 2),
        "var95": round(var95, 2),
        "max_down_days": max_dd_days,
        "downside_risk": round(downside, 2),
        "beta": round(beta, 2) if beta is not None else None,
        "alpha": round(alpha, 2) if alpha is not None else None,
    }


def compute_risk_metrics(history, bench_history=None):
    """计算风险度量指标（纯 Python，无需 AI）

    history: 基金日净值序列
    bench_history: 基准(沪深300 ETF 510300)日净值序列，可选，用于 Beta/Alpha
    """
    import math, statistics
    if not history or len(history) < 5:
        return {}
    pcts = [h["pct"] for h in history if h.get("pct") is not None]
    if len(pcts) < 5:
        return {}
    n = len(pcts)
    avg = statistics.mean(pcts)
    std = statistics.stdev(pcts) if n > 1 else 0.0
    rf_daily = 0.02 / 252  # 无风险利率 2% 年化
    sharpe = (avg - rf_daily) / std * math.sqrt(252) if std > 0 else 0.0
    var95 = -1.645 * std if std > 0 else 0.0  # 单日 95% VaR（正值=亏损%）
    # 最大连跌天数
    max_dd_days = cur = 0
    for p in pcts:
        cur = cur + 1 if p < 0 else 0
        max_dd_days = max(max_dd_days, cur)
    # 下行风险（只看负收益的标准差，年化）
    neg = [p for p in pcts if p < 0]
    downside = statistics.stdev(neg) * math.sqrt(252) if len(neg) > 1 else 0.0
    return {
        "sharpe": round(sharpe, 2),
        "var95": round(var95, 2),
        "max_down_days": max_dd_days,
        "downside_risk": round(downside, 2),
    }


def compute_allocation(funds, idle_cash=0.0):
    """简化风险平价配置模型（纯 Python）

    按波动率倒数加权得出理想配置比例：波动越小 → 配比越高（稳健资产多配）。
    返回 [{code, name, cur_pct, ideal_pct, diff, advice}] + 说明
    """
    total_val = sum(f.get("value", 0) or 0 for f in funds) or 1.0
    weights = {}
    for f in funds:
        vol = (f.get("metrics") or {}).get("volatility") or 20.0
        weights[f["code"]] = 1.0 / max(vol, 1.0)
    wsum = sum(weights.values()) or 1.0
    rows = []
    for f in funds:
        cur = round((f.get("value", 0) or 0) / total_val * 100, 1)
        ideal = round(weights[f["code"]] / wsum * 100, 1)
        diff = round(ideal - cur, 1)
        advice = "加仓" if diff > 5 else ("减仓" if diff < -5 else "维持")
        rows.append({"code": f["code"], "name": f.get("name", ""),
                     "cur_pct": cur, "ideal_pct": ideal,
                     "diff": diff, "advice": advice})
    return {"rows": rows,
            "method": "简化风险平价：按波动率倒数加权，波动越小配比越高",
            "note": "量化参考，最终以 AI 综合分析与个人风险承受为准"}


# ================== 预测存储 ==================
def _load_history():
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_history(h):
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(h, f, ensure_ascii=False, indent=2)


def save_prediction(date_str, code, prediction):
    h = _load_history()
    h.setdefault(date_str, {"predictions": {}, "reports": {}})
    h[date_str]["predictions"][code] = prediction
    _save_history(h)


def save_report(date_str, code, report):
    h = _load_history()
    h.setdefault(date_str, {"predictions": {}, "reports": {}})
    h[date_str]["reports"][code] = report
    _save_history(h)


# ================== 信号追踪 ==================
def load_signals():
    try:
        if os.path.exists(SIGNALS_FILE):
            with open(SIGNALS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return []


def save_signals(signals):
    try:
        with open(SIGNALS_FILE, "w", encoding="utf-8") as f:
            json.dump(signals, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def add_signals_from_report(code, report):
    """从分析报告提取信号并去重追加到信号库

    去重规则：同一基金 + 方向 + 目标 视为相同信号（目标缺失时退化为 title），
    已存在或同报告内重复的都不再追加。
    """
    sigs = (report or {}).get("signals") or []
    if not sigs:
        return
    signals = load_signals()

    def fingerprint(s):
        t = str(s.get("target", "")).strip()
        d = str(s.get("direction", "")).strip()
        if t:
            return (code, d, t)
        return (code, d, str(s.get("title", "")).strip())

    existing = {fingerprint(s) for s in signals if s.get("code") == code}
    seen = set()
    added = 0
    for s in sigs[:5]:
        title = str(s.get("title", "")).strip()
        if not title:
            continue
        fp = fingerprint(s)
        if fp in existing or fp in seen:
            continue
        signals.append({
            "id": uuid.uuid4().hex[:10],
            "code": code,
            "title": title,
            "direction": s.get("direction", ""),
            "target": s.get("target", ""),
            "basis": s.get("basis", ""),
            "horizon": s.get("horizon", ""),
            "created": datetime.now().strftime("%Y-%m-%d"),
            "status": "active",  # active / 强化 / 弱化 / 证伪 / 兑现
            "outcome": None,     # correct / wrong
        })
        existing.add(fp)
        seen.add(fp)
        added += 1
    if added:
        save_signals(signals)


# ================== 加减仓复盘 ==================
def load_trade_reviews():
    try:
        if os.path.exists(TRADE_REVIEW_FILE):
            with open(TRADE_REVIEW_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return []


def save_trade_reviews(reviews):
    try:
        with open(TRADE_REVIEW_FILE, "w", encoding="utf-8") as f:
            json.dump(reviews, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def add_trade_review_from_report(code, name, report, nav=None, pct=None):
    """从分析报告提取加减仓建议（非 HOLD/维持）存入复盘库

    去重：同一基金 + 日期 已有 pending 记录则不重复添加（当天只留最新一条）。
    """
    act = (report or {}).get("action_suggestion") or {}
    verdict = str(act.get("verdict", "") or "").strip().upper()
    if verdict not in ("BUY", "SELL", "ADD", "REDUCE"):
        return
    position_change = str(act.get("position_change", "") or "").strip()
    if not position_change or position_change in ("持有不动", "N/A", "维持", "持有"):
        return
    reviews = load_trade_reviews()
    today = datetime.now().strftime("%Y-%m-%d")
    for r in reviews:
        if r.get("code") == code and r.get("date") == today and r.get("status") == "pending":
            return
    reviews.append({
        "id": uuid.uuid4().hex[:10],
        "code": code,
        "name": name,
        "date": today,
        "verdict": verdict,
        "position_change": position_change,
        "rationale": str(act.get("rationale", "") or "").strip(),
        "entry_zone": str(act.get("entry_zone", "") or "").strip(),
        "stop_loss": str(act.get("stop_loss", "") or "").strip(),
        "ref_nav": nav,
        "ref_pct": pct,
        "status": "pending",   # pending / reviewed
        "review": None,
    })
    save_trade_reviews(reviews)


def trade_review_stats(reviews=None):
    """加减仓复盘统计：盈利占比 = 盈利条数 / 已复盘条数"""
    if reviews is None:
        reviews = load_trade_reviews()
    total = len(reviews)
    reviewed = [r for r in reviews if r.get("status") == "reviewed"]
    profit = [r for r in reviewed if (r.get("review") or {}).get("result") == "盈利"]
    loss = [r for r in reviewed if (r.get("review") or {}).get("result") == "亏损"]
    flat = [r for r in reviewed if (r.get("review") or {}).get("result") == "持平"]
    profit_rate = round(len(profit) / len(reviewed) * 100, 1) if reviewed else None
    pnls = [float((r.get("review") or {}).get("pnl_pct") or 0) for r in reviewed]
    avg_pnl = round(sum(pnls) / len(pnls), 2) if pnls else None
    return {"total": total, "reviewed": len(reviewed), "pending": total - len(reviewed),
            "profit": len(profit), "loss": len(loss), "flat": len(flat),
            "profit_rate": profit_rate, "avg_pnl": avg_pnl}


def review_trade_advice(advice, quote):
    """AI 复盘单条加减仓建议：如果当时听从了建议，到现在会盈利还是亏损

    quote: 当前最新行情摘要（如 "易方达蓝筹 净值 2.9012 今日 +0.85%"）
    返回 {"result": "盈利/亏损/持平", "pnl_pct": 数字, "reason": "偏差原因",
          "bias_type": "偏差类型"} 或 None
    """
    sys_p = (
        "你是交易复盘分析师。给定一条过去的加减仓建议（含建议时的净值/涨跌）和当前最新行情，"
        "判断：如果当时真的听从了这条建议操作，到现在是盈利还是亏损。\n"
        "输出 JSON：{\"result\": \"盈利/亏损/持平\", \"pnl_pct\": 收益率数字(正为盈利负为亏损，单位%), "
        "\"reason\": \"盈亏原因与偏差分析(60字内)\", \"bias_type\": \"偏差类型\"}\n"
        "bias_type 只能从以下选一个：方向误判 / 时机过早或过晚 / 追涨杀跌情绪化 / 幅度误判 / 信息不足 / 其他\n"
        "注意：pnl_pct 按建议方向估算——买入/加仓看净值涨跌为正，卖出/减仓则反过来（卖在下跌前=盈利）。"
        "只输出 JSON，不要任何额外文字。"
    )
    user_p = (
        f"基金：{advice.get('name','')}（{advice.get('code','')}）\n"
        f"建议日期：{advice.get('date','-')}  操作：{advice.get('verdict','-')}（{advice.get('position_change','-')}）\n"
        f"建议理由：{advice.get('rationale','-')}\n"
        f"建议时净值：{advice.get('ref_nav','-')}  建议时今日涨跌：{advice.get('ref_pct','-')}%\n"
        f"最新行情：{quote or '暂无行情数据'}"
    )
    r = llm_chat([{"role": "system", "content": sys_p},
                  {"role": "user", "content": user_p}], temperature=0.3, max_tokens=250)
    if not r.get("ok"):
        return None
    parsed = parse_llm_json(r.get("content", ""))
    if not parsed or not parsed.get("result"):
        return None
    pnl = parsed.get("pnl_pct")
    try:
        pnl = float(pnl) if pnl not in (None, "") else None
    except (TypeError, ValueError):
        pnl = None
    bias_type = str(parsed.get("bias_type", "")).strip()
    if bias_type not in ("方向误判", "时机过早或过晚", "追涨杀跌情绪化",
                         "幅度误判", "信息不足", "其他"):
        bias_type = "其他"
    return {"result": str(parsed["result"]).strip(),
            "pnl_pct": pnl,
            "reason": str(parsed.get("reason", "")).strip(),
            "bias_type": bias_type}


def load_trade_lessons():
    """读取经验教训缓存 {"lessons": [...], "updated": "日期"}"""
    try:
        if os.path.exists(TRADE_LESSONS_FILE):
            with open(TRADE_LESSONS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def save_trade_lessons(data):
    try:
        with open(TRADE_LESSONS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def summarize_trade_lessons():
    """复盘完成后调用一次：把已复盘记录的偏差类型+原因汇总，提炼成经验教训缓存

    只在复盘批量执行后调用（不随每次分析触发），控制 token 消耗。
    """
    reviews = load_trade_reviews()
    reviewed = [r for r in reviews if r.get("status") == "reviewed"]
    if not reviewed:
        return None
    st = trade_review_stats(reviews)
    # 偏差类型分布
    bias_cnt = {}
    for r in reviewed:
        bt = ((r.get("review") or {}).get("bias_type") or "其他").strip()
        bias_cnt[bt] = bias_cnt.get(bt, 0) + 1
    bias_desc = "，".join(f"{k} {v}条" for k, v in
                          sorted(bias_cnt.items(), key=lambda x: -x[1])) or "无"
    # 取最近 12 条已复盘记录喂给 LLM
    sample = sorted(reviewed, key=lambda r: r.get("date", ""), reverse=True)[:12]
    lines = []
    for r in reversed(sample):
        rv = r.get("review") or {}
        lines.append(
            f"- {r.get('date','-')} {r.get('name','')} {r.get('verdict','')} "
            f"→ {rv.get('result','')} {rv.get('pnl_pct','')}% "
            f"[{rv.get('bias_type','其他')}] {rv.get('reason','')}")
    sys_p = (
        "你是交易复盘教练。根据用户加减仓建议的历史复盘记录（含偏差类型与原因），"
        "提炼 3-6 条可执行的交易经验教训，帮助后续分析避免同类错误。\n"
        "输出 JSON：{\"lessons\": [\"经验1\", \"经验2\", ...]}，每条 40 字内，"
        "针对偏差类型给出具体改进动作。只输出 JSON。"
    )
    user_p = (
        f"整体：共 {st['reviewed']} 条已复盘，盈利 {st['profit']} / 亏损 {st['loss']} / 持平 {st['flat']}，"
        f"盈利占比 {st['profit_rate']}%，平均 {st['avg_pnl']}%。\n"
        f"偏差类型分布：{bias_desc}\n"
        f"最近复盘明细：\n" + "\n".join(lines)
    )
    r = llm_chat([{"role": "system", "content": sys_p},
                  {"role": "user", "content": user_p}], temperature=0.4, max_tokens=500)
    lessons = []
    if r.get("ok"):
        parsed = parse_llm_json(r.get("content", ""))
        if parsed and isinstance(parsed.get("lessons"), list):
            lessons = [str(x).strip() for x in parsed["lessons"] if str(x).strip()]
    data = {"lessons": lessons, "updated": datetime.now().strftime("%Y-%m-%d"),
            "stats": {"reviewed": st["reviewed"], "profit_rate": st["profit_rate"],
                      "avg_pnl": st["avg_pnl"]}}
    save_trade_lessons(data)
    return data


def build_trade_review_context():
    """把加减仓复盘结论（盈利占比 + 偏差类型 + 经验教训 + 亏损案例）拼成文本，喂回下次分析（形成闭环）"""
    reviews = load_trade_reviews()
    reviewed = [r for r in reviews if r.get("status") == "reviewed"]
    if not reviewed:
        return None
    st = trade_review_stats(reviews)
    lines = [
        f"你的加减仓建议历史复盘：共 {st['reviewed']} 条已复盘，"
        f"听从后盈利 {st['profit']} 条 / 亏损 {st['loss']} 条（盈利占比 {st['profit_rate']}%），"
        f"平均 {st['avg_pnl']}%"
    ]
    # 偏差类型分布
    bias_cnt = {}
    for r in reviewed:
        bt = ((r.get("review") or {}).get("bias_type") or "其他").strip()
        bias_cnt[bt] = bias_cnt.get(bt, 0) + 1
    if bias_cnt:
        bias_desc = "，".join(f"{k} {v}条" for k, v in
                              sorted(bias_cnt.items(), key=lambda x: -x[1]))
        lines.append(f"偏差类型分布：{bias_desc}")
    # 经验教训（复盘完成后的缓存）
    lessons = load_trade_lessons().get("lessons") or []
    if lessons:
        lines.append("复盘经验教训：" + "；".join(lessons))
    # 最近 3 条亏损案例
    losses = [r for r in reviewed if (r.get("review") or {}).get("result") == "亏损"][-3:]
    for r in losses:
        rv = r.get("review") or {}
        lines.append(f"- {r.get('date','-')} {r.get('name','')} {r.get('verdict','')} "
                     f"亏损 {rv.get('pnl_pct')}% [{rv.get('bias_type','其他')}]：{rv.get('reason','')}")
    return "\n".join(lines)


def signal_stats():
    """信号胜率统计：已了结(兑现/证伪)信号中方向判断正确的比例"""
    signals = load_signals()
    total = len(signals)
    active = sum(1 for s in signals if s.get("status") in ("active", "强化", "弱化"))
    closed = sum(1 for s in signals if s.get("status") in ("证伪", "兑现"))
    correct = sum(1 for s in signals if s.get("outcome") == "correct")
    hit_rate = round(correct / closed * 100, 1) if closed else None
    return {"total": total, "active": active, "closed": closed,
            "correct": correct, "hit_rate": hit_rate}


def audit_signal(signal, quote):
    """AI 审核单条信号：分别判别【状态】(兑现/证伪/强化/弱化/维持原判/信息不足) 与【结果】(correct/wrong)

    quote: 最新行情摘要文本（如 "易方达蓝筹 净值 2.8541 今日 +1.82%"）
    返回 {"status": ..., "outcome": ..., "reason": ...} 或 None
    """
    sys_p = (
        "你是信号审核分析师。基于信号信息与最新行情，分别判别信号的【状态】与【结果】两个维度。\n"
        "一、状态 status（信号目标/预期是否达成）：\n"
        "- 兑现：目标已达成或明显朝预期发展\n"
        "- 证伪：目标确定无法达成，行情与预期明显相反\n"
        "- 强化：朝预期发展但尚未完全兑现，证据增强\n"
        "- 弱化：与预期背离或证据减弱，但尚未完全证伪\n"
        "- 维持原判：信号仍有效，与上次判断相比无明显变化，维持当前状态不动\n"
        "- 信息不足：最新行情数据缺失/异常/非交易时段，无法做出可靠判断，保留现状待下次审核\n"
        "二、结果 outcome（判断方向/逻辑是否正确，与状态独立）：\n"
        "- correct：方向判断正确（如看多且实际确实上涨，即使幅度未达目标也算对）\n"
        "- wrong：方向判断错误（如看多但实际下跌）\n"
        "注意：状态与结果可能不一致。例如看多只涨了一点未达目标 → 状态弱化但结果 correct；"
        "看多但大跌 → 状态证伪且结果 wrong。\n"
        "只输出 JSON，不要任何额外文字：{\"status\": \"兑现/证伪/强化/弱化/维持原判/信息不足\", \"outcome\": \"correct/wrong/空\", \"reason\": \"一句话理由(50字内)\"}。"
        "仅当 status 为「兑现」或「证伪」时 outcome 填 correct 或 wrong，其余状态 outcome 一律填空字符串 \"\"。"
    )
    user_p = (
        f"信号标题：{signal.get('title', '')}\n"
        f"方向：{signal.get('direction', '-')}  目标：{signal.get('target', '-')}\n"
        f"依据：{signal.get('basis', '-')}\n"
        f"期限：{signal.get('horizon', '-')}  创建：{signal.get('created', '-')}\n"
        f"最新行情：{quote or '暂无行情数据'}"
    )
    r = llm_chat([{"role": "system", "content": sys_p},
                  {"role": "user", "content": user_p}], temperature=0.3, max_tokens=250)
    if not r.get("ok"):
        return None
    parsed = parse_llm_json(r.get("content", ""))
    if not parsed or not parsed.get("status"):
        return None
    return {"status": str(parsed["status"]).strip(),
            "outcome": str(parsed.get("outcome", "")).strip(),
            "reason": str(parsed.get("reason", "")).strip()}


def save_portfolio_prediction(date_str, report):
    h = _load_history()
    h.setdefault(date_str, {"predictions": {}, "reports": {}})
    h[date_str]["portfolio"] = report
    _save_history(h)


def get_history(date_str=None):
    h = _load_history()
    if date_str:
        return h.get(date_str)
    return h


def list_history_dates():
    h = _load_history()
    return sorted(h.keys(), reverse=True)


# ================== 多角色分析 Prompt ==================
SYSTEM_PROMPT = """你是一支专业基金投研团队的「主理人」。团队由以下角色组成：
- 技术分析师：基于净值趋势、均线（MA5/10/20）、RSI、最大回撤、波动率判断技术面
- 基本面分析师：基于基金类型、规模、波动特征、历史业绩等做合理性评估
- 新闻分析师：基于市场环境、政策、板块情绪推断催化与风险
- 情绪分析师：基于近期涨跌幅、连涨/连跌、资金偏好评估情绪
- 多头研究员 / 空头研究员 / 研究主管：基于上述事实做多空博弈，输出 BUY/SELL/HOLD
- 交易员：给出入场区间、目标价、止损价、建议仓位
- 风控三方 + 风险主管：对激进/保守/中性风险评估后做最终裁决

你的任务：对给定的基金数据，模拟这支团队完整流水线，最终输出一份结构化的 JSON 分析报告。

要求：
1. 只输出 JSON，不要任何 markdown 代码块标记或额外文字。
2. 严格按以下 schema 输出，不要遗漏字段：

{
  "summary": "用一句话总结今日核心结论（30 字内）",
  "roles": {
    "技术分析师": "一句话技术面判断（趋势/均线/RSI）",
    "基本面分析师": "一句话基本面评估（类型/规模/估值合理度）",
    "新闻分析师": "一句话催化与政策风险判断",
    "情绪分析师": "一句话资金与情绪判断",
    "多头研究员": "一句话看多逻辑",
    "空头研究员": "一句话看空/风险逻辑",
    "研究主管": "一句话多空裁决（含明确 BUY/SELL/HOLD）",
    "交易员": "一句话交易执行建议（入场/目标/止损/仓位）",
    "风控主管": "一句话风险裁决（最终风险提示与仓位）"
  },
  "today_analysis": {
    "trend": "今日技术面定性（强势上涨/震荡偏多/震荡偏空/弱势下跌 等）",
    "key_levels": "关键支撑位 / 阻力位（用最新净值 ±百分比给出）",
    "momentum": "动量状态（RSI 区段、均线排列、量价特征）",
    "risk_flag": "风险提示（如顶背离/接近阻力/连涨透支/最大回撤偏大 等）",
    "one_liner": "今日分析的 50 字简评"
  },
  "tomorrow_forecast": {
    "direction": "UP / DOWN / FLAT",
    "expected_pct": "预期明日的涨幅百分比（±x.xx%，可负）",
    "confidence": "高 / 中 / 低",
    "reason": "预测理由（80 字内，引用技术面+情绪面依据）"
  },
  "midterm_strategy": {
    "trend": "中期(1-2周)趋势判断（偏多/偏空/震荡）",
    "target_range": "预期波动区间（净值范围，如 1.60-1.70）",
    "position_advice": "仓位建议（如 维持7成 / 降至5成 / 加至8成 / 清仓）",
    "key_levels": "中期关键位（支撑/阻力净值）",
    "confidence": "高 / 中 / 低",
    "reason": "策略依据（80字内，结合持仓穿透+技术面+风险度量）"
  },
  "action_suggestion": {
    "verdict": "BUY / SELL / HOLD / ADD / REDUCE",
    "position_change": "建议加减仓的方向与幅度（例：加仓 20% / 减仓 1/3 / 持有不动）",
    "entry_zone": "建议买入区间（用净值 ±% 表达；持有/SELL 时写 N/A）",
    "target": "目标净值或目标收益率",
    "stop_loss": "止损位（用净值 -% 表达）",
    "rationale": "加减仓理由（100 字内，权衡多空与风控后）"
  },
  "confidence_score": 0-100,
  "key_risks": ["主要风险 1", "主要风险 2"],
  "key_catalysts": ["潜在催化 1", "潜在催化 2"],
  "signals": [
    {
      "title": "信号一句话（如：半导体周期向上，007301 中期看多）",
      "direction": "看多 / 看空",
      "target": "标的（基金代码或板块）",
      "basis": "依据（一句话，后续可验证）",
      "horizon": "验证期限（如 2周）"
    }
  ]
}
"""


def build_user_prompt(code, name, holding_amount, shares, metrics, latest_gz, latest_pct, holdings=None, signal_context=None, trade_review_context=None):
    """把基金数据喂给 LLM"""
    m = metrics or {}
    parts = [
        f"基金代码: {code}",
        f"基金名称: {name}",
        f"持有金额: {holding_amount} 元（份额 {shares}）",
        f"最新估值: {latest_gz}",
        f"今日估算涨幅: {latest_pct}%",
        "",
        "技术指标（基于近 90 个交易日日净值计算）：",
    ]
    if m:
        parts += [
            f"- 数据点: {m.get('data_points')}",
            f"- 最新净值: {m.get('last_nav')}",
            f"- MA5 / MA10 / MA20: {m.get('ma5')} / {m.get('ma10')} / {m.get('ma20')}",
            f"- RSI(14): {m.get('rsi14')}",
            f"- 趋势: {m.get('trend')}",
            f"- 区间（{m.get('data_points')} 天）涨幅: {m.get('period_change_pct')}%",
            f"- 最大回撤: {m.get('max_drawdown')}%",
            f"- 年化波动率: {m.get('volatility')}%",
            f"- 连续涨跌: {m.get('streak_dir')} {m.get('streak_days')} 天",
            f"- Sharpe 比率: {m.get('sharpe')}",
            f"- 95% VaR(单日): {m.get('var_95')}%",
            f"- 下行风险(年化): {m.get('downside_risk')}%",
            f"- 最大连跌天数: {m.get('max_consec_down_days')}",
        ]
    else:
        parts.append("（历史数据不足）")
    # 持仓穿透
    if holdings:
        parts += ["", f"前十大持仓股（穿透到底层资产）："]
        for s in holdings[:10]:
            parts.append(f"- {s['code']} {s['name']} · 占比 {s['weight']}% · 行业 {s.get('industry','-')} · {s.get('change_type','')}")
    # 历史信号（AI 自己的判断记录，供参考修正，并在 confidence_score 中如实反映影响）
    if signal_context:
        parts += ["", "【你的历史信号记录（请参考修正本次判断，并在 confidence_score 中如实反映影响）】"]
        for s in signal_context:
            parts.append(
                f"- {s.get('created','-')} [{s.get('direction','-')}] {s.get('title','')} "
                f"→ 状态 {s.get('status','-')} 结果 {s.get('outcome','未了结')}")
    # 加减仓复盘结论（吸取历史教训，修正本次加减仓建议）
    if trade_review_context:
        parts += ["", "【你的加减仓建议历史复盘（请吸取亏损教训，修正本次加减仓建议）】"]
        parts.append(trade_review_context)
    parts += ["", "请模拟完整投研团队流水线，给出结构化 JSON 报告。重点输出中期(1-2周)策略而非仅明天涨跌。"]
    return "\n".join(parts)


def parse_llm_json(content):
    """从 LLM 输出中提取 JSON"""
    if not content:
        return None
    s = content.strip()
    # 去掉 markdown 代码块
    s = re.sub(r"^```(?:json)?\s*", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\s*```$", "", s)
    # 找第一个 { 到最后一个 }
    a = s.find("{")
    b = s.rfind("}")
    if a >= 0 and b > a:
        s = s[a:b + 1]
    try:
        return json.loads(s)
    except Exception:
        return None


def analyze_fund(code, name, holding_amount, shares, latest_gz, latest_pct,
                 progress_cb=None, signal_context=None, trade_review_context=None):
    """单只基金分析主流程（同步，可在后台线程中调用）

    progress_cb(msg, pct 0-100) 用于进度回调
    signal_context: 该基金历史信号列表（[{title, direction, status, outcome, created}]），供 AI 参考
    trade_review_context: 加减仓复盘结论文本，供 AI 吸取教训修正建议
    返回 dict {ok, summary, today_analysis, tomorrow_forecast, action_suggestion, raw}
    """
    if progress_cb:
        progress_cb("拉取历史净值...", 10)
    history = fetch_history(code, days=90)
    if progress_cb:
        progress_cb("计算技术指标 + 风险度量...", 25)
    metrics = compute_metrics(history)
    if progress_cb:
        progress_cb("穿透持仓股...", 33)
    holdings = fetch_holdings(code)

    if progress_cb:
        progress_cb("调用 AI 团队分析（可能 30-60 秒）...", 40)
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": build_user_prompt(
            code, name, holding_amount, shares, metrics, latest_gz, latest_pct,
            holdings, signal_context, trade_review_context)},
    ]
    r = llm_chat(messages, temperature=0.5, max_tokens=3500)
    if progress_cb:
        progress_cb("解析结果...", 85)
    if not r.get("ok"):
        return {"ok": False, "code": code, "name": name, "msg": r.get("msg", "未知错误")}

    parsed = parse_llm_json(r.get("content", ""))
    if not parsed:
        return {"ok": False, "code": code, "name": name,
                "msg": "AI 输出无法解析为 JSON", "raw": r.get("content")}

    if progress_cb:
        progress_cb("完成", 100)

    return {
        "ok": True,
        "code": code,
        "name": name,
        "metrics": metrics,
        "report": parsed,
        "raw": r.get("content"),
        "analyzed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


# ================== 组合分析 ==================
PORTFOLIO_SYSTEM_PROMPT = """你是一名专业的基金组合策略师。你面对的是用户的一整个基金持仓组合，需要从组合层面（而非单只）给出分析。

你的任务：基于每只基金的基础信息、技术指标，以及每只基金已经给出的加减仓建议，输出一份结构化的 JSON 组合分析报告。

要求：
1. 只输出 JSON，不要任何 markdown 代码块标记或额外文字。
2. 严格按以下 schema 输出，不要遗漏字段：

{
  "summary": "用一句话总结整体持仓的核心结论（40 字内）",
  "structure": {
    "sector_distribution": "板块分布分析（根据基金名称推断板块，如消费/半导体/医药/新能源/宽基等，说明各板块占比）",
    "concentration": "集中度分析（持仓是否过于集中在某板块/某风格，风险是否分散）",
    "risk_level": "整体风险评级（保守/均衡/进取/激进）",
    "diversification": "分散化程度评价（过度分散/适中/过度集中）"
  },
  "sector_adjustment": [
    {"sector": "板块名", "action": "加仓/减仓/维持", "reason": "一句话理由", "suggest_pct": "建议调整幅度（如 +10% / -15%）"}
  ],
  "portfolio_forecast": {
    "direction": "UP / DOWN / FLAT",
    "expected_pct": "预期整体明日涨跌幅（±x.xx%，可负）",
    "confidence": "高 / 中 / 低",
    "reason": "预测理由（80 字内）"
  },
  "rebalance_suggestions": ["整体调仓建议 1", "整体调仓建议 2"],
  "idle_cash_advice": {
    "suggestion": "结合各基金的加减仓建议，给出闲钱使用的一句话建议（长远配置，避免一次性全投单一标的，可分批次择机布局）",
    "deploy_now": "建议当前就投入的金额（元，数字）",
    "deploy_later": "建议暂留待机、择机再投入的金额（元，数字）",
    "deploy_target": "建议投入方向（具体基金代码或板块名，可多个，无则写 暂无）",
    "return_boost": "预计使当前组合收益变成的倍数（如 1.2 表示收益提升 20%，0.9 表示可能略降；请用倍数表达，不要用年化百分比）",
    "confidence": "该建议的可信度（高/中/低）",
    "reason": "理由（60 字内）"
  },
  "new_direction_advice": {
    "suggestion": "对新增投资方向的一句话建议（如：建议新增医药/债基等新板块做分散，或当前无需新增）",
    "target": "建议的新方向（板块/基金类型，无则写 暂无）",
    "return_boost": "预计对整体收益的提升倍数（如 1.1 表示提升 10%；请用倍数表达，不要用年化百分比）",
    "confidence": "可信度（高/中/低）",
    "reason": "理由（60 字内）"
  },
  "key_risks": ["组合主要风险 1", "组合主要风险 2"],
  "key_catalysts": ["组合潜在催化 1", "组合潜在催化 2"]
}
"""


def build_portfolio_prompt(funds, idle_cash=0.0, signal_contexts=None, trade_review_context=None):
    """把持仓组合数据喂给 LLM

    signal_contexts: {code: [历史信号...]}，组合层面的 AI 历史判断记录，
    供 AI 参考修正本次组合判断（对错的信号会影响本次结论与可信度）。
    trade_review_context: 加减仓复盘结论文本，供 AI 吸取教训修正建议。
    """
    total = sum(f.get("value", 0) or 0 for f in funds) or 1.0
    parts = ["以下是用户当前的全部持仓（共 %d 只基金，总市值 %.2f 元）：" % (len(funds), total), ""]
    for f in funds:
        m = f.get("metrics") or {}
        value = f.get("value", 0) or 0
        pct = round(value / total * 100, 1)
        parts.append(
            f"【{f.get('code')}】{f.get('name', '')}\n"
            f"- 市值 {value:.2f} 元（占比 {pct}%）\n"
            f"- 今日涨跌 {f.get('gz_pct')}%\n"
            f"- 技术：趋势={m.get('trend', '-')}, MA5/20={m.get('ma5', '-')}/{m.get('ma20', '-')}, "
            f"RSI={m.get('rsi14', '-')}, 最大回撤={m.get('max_drawdown', '-')}%, "
            f"区间涨幅={m.get('period_change_pct', '-')}%"
        )
        act = f.get("action_suggestion")
        if act:
            parts.append(
                f"- 该基金逐只分析的建议：{act.get('verdict', '-')}，"
                f"{act.get('position_change', '-')}，理由：{act.get('rationale', '-')}"
            )
        hld = f.get("holdings") or []
        if hld:
            top = "、".join(f"{s['name']}({s.get('weight','-')}%)" for s in hld[:5])
            parts.append(f"- 前五大持仓股：{top}")
    if idle_cash and idle_cash > 0:
        parts += ["", f"用户另有完全可支配的闲钱 {idle_cash:.2f} 元（用于加减仓的投资资金，无需考虑应急）。"
                      f"请【结合上面各基金的加减仓建议】从长远配置角度给出闲钱使用建议："
                      f"不要建议一次性全投单一标的，可分批次、择机布局；"
                      f"并估算这样运用闲钱可能使组合收益提升到的倍数，以及给出可信度。"]
    # 历史信号（AI 自己的判断记录，组合层面参考修正，并在结论与可信度中如实反映）
    if signal_contexts:
        parts += ["", "【你的历史信号记录（组合层面，请参考修正本次判断，并在整体可信度中如实反映）】"]
        for code, sigs in signal_contexts.items():
            for s in sigs:
                parts.append(
                    f"- {s.get('created','-')} [{s.get('direction','-')}] {code} {s.get('title','')} "
                    f"→ 状态 {s.get('status','-')} 结果 {s.get('outcome','未了结')}")
    # 加减仓复盘结论（组合层面吸取教训）
    if trade_review_context:
        parts += ["", "【你的加减仓建议历史复盘（请吸取亏损教训，修正本次组合加减仓建议）】"]
        parts.append(trade_review_context)
    parts += ["", "请给出：整体持仓结构、板块调整建议、闲钱使用建议（结合单基金加减）、新投资方向建议、整体明日预测，输出 JSON。"]
    return "\n".join(parts)


def analyze_portfolio(funds, idle_cash=0.0, progress_cb=None, signal_contexts=None, trade_review_context=None):
    """整体持仓组合分析（同步，可在后台线程调用）

    funds: [{code, name, value, gz_pct, metrics}]
    idle_cash: 用户闲置资金（可用于加减仓）
    signal_contexts: {code: [历史信号...]}，供 AI 参考修正组合判断
    trade_review_context: 加减仓复盘结论文本
    """
    if progress_cb:
        progress_cb("汇总持仓数据...", 20)
    if not funds:
        return {"ok": False, "msg": "暂无持仓"}

    if progress_cb:
        progress_cb("调用 AI 组合策略师分析...", 40)
    messages = [
        {"role": "system", "content": PORTFOLIO_SYSTEM_PROMPT},
        {"role": "user", "content": build_portfolio_prompt(funds, idle_cash, signal_contexts, trade_review_context)},
    ]
    r = llm_chat(messages, temperature=0.5, max_tokens=4000)
    if progress_cb:
        progress_cb("解析结果...", 85)
    if not r.get("ok"):
        return {"ok": False, "msg": r.get("msg", "未知错误")}

    parsed = parse_llm_json(r.get("content", ""))
    if not parsed:
        return {"ok": False, "msg": "AI 输出无法解析为 JSON", "raw": r.get("content")}

    if progress_cb:
        progress_cb("完成", 100)

    return {
        "ok": True,
        "type": "portfolio",
        "report": parsed,
        "raw": r.get("content"),
        "analyzed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


# ================== 复盘引擎 ==================
def compare_prediction(expected_dir, expected_pct_str, actual_pct):
    """通用对比：预测方向/幅度 vs 实际涨跌，返回准确率等"""
    m = re.search(r"(-?\d+(?:\.\d+)?)", str(expected_pct_str))
    expected_pct = float(m.group(1)) if m else 0.0
    expected_dir = str(expected_dir or "FLAT").upper()

    if actual_pct > 0.05:
        actual_dir = "UP"
    elif actual_pct < -0.05:
        actual_dir = "DOWN"
    else:
        actual_dir = "FLAT"

    direction_correct = (expected_dir == actual_dir)
    pct_deviation = round(actual_pct - expected_pct, 2)
    abs_deviation = round(abs(pct_deviation), 2)

    # 准确率评分：方向对+0.5，幅度误差 < 0.3% +0.5，< 0.6% +0.3
    score = 0.5 if direction_correct else 0
    if abs_deviation < 0.3:
        score += 0.5
    elif abs_deviation < 0.6:
        score += 0.3
    accuracy = round(score * 100, 1)

    return {
        "expected_pct": expected_pct,
        "expected_dir": expected_dir,
        "actual_pct": actual_pct,
        "actual_dir": actual_dir,
        "direction_correct": direction_correct,
        "pct_deviation": pct_deviation,
        "abs_deviation": abs_deviation,
        "accuracy": accuracy,
    }


def review_prediction(date_str, code, actual_pct):
    """复盘某天某只基金的预测：对比昨日预测与今日实际"""
    history = get_history(date_str)
    if not history:
        return {"ok": False, "msg": f"未找到 {date_str} 的预测记录"}
    pred = history.get("predictions", {}).get(code)
    if not pred:
        return {"ok": False, "msg": f"未找到 {date_str} {code} 的预测"}

    expected_pct_str = pred.get("tomorrow_forecast", {}).get("expected_pct", "0")
    expected_dir = pred.get("tomorrow_forecast", {}).get("direction", "FLAT")
    result = compare_prediction(expected_dir, expected_pct_str, actual_pct)
    result.update({"ok": True, "code": code, "date": date_str})
    return result


def review_portfolio(date_str, actual_pct):
    """复盘某天的组合预测：组合预测 vs 组合实际（加权涨跌）"""
    history = get_history(date_str)
    if not history:
        return {"ok": False, "msg": f"未找到 {date_str} 的预测记录"}
    portfolio = history.get("portfolio")
    if not portfolio:
        return {"ok": False, "msg": f"{date_str} 无组合预测"}

    pf = portfolio.get("portfolio_forecast", {})
    expected_pct_str = pf.get("expected_pct", "0")
    expected_dir = pf.get("direction", "FLAT")
    result = compare_prediction(expected_dir, expected_pct_str, actual_pct)
    result.update({"ok": True, "date": date_str, "is_portfolio": True})
    return result


def review_with_ai(date_str, code, actual_pct, fund_name, fund_metrics):
    """复盘 + 让 AI 分析偏差原因"""
    base = review_prediction(date_str, code, actual_pct)
    if not base.get("ok"):
        return base

    sys_p = (
        "你是一名基金复盘分析师。请基于昨日预测与今日实际表现的对比，"
        "简要分析偏差原因（100 字内，给出 1-2 条最可能的解释），"
        "只输出简短文本。"
    )
    user_p = (
        f"基金: {code} {fund_name}\n"
        f"昨日预测方向: {base['expected_dir']}, 预期涨幅: {base['expected_pct']}%\n"
        f"今日实际涨幅: {actual_pct}%\n"
        f"方向是否正确: {'是' if base['direction_correct'] else '否'}\n"
        f"幅度偏差: {base['pct_deviation']}%\n"
        f"技术状态: 趋势={fund_metrics.get('trend','-')}, "
        f"MA5={fund_metrics.get('ma5','-')}, MA20={fund_metrics.get('ma20','-')}, "
        f"RSI={fund_metrics.get('rsi14','-')}, 最大回撤={fund_metrics.get('max_drawdown','-')}%"
    )
    r = llm_chat([{"role": "system", "content": sys_p},
                  {"role": "user", "content": user_p}], temperature=0.4, max_tokens=400)
    if r.get("ok"):
        base["deviation_reason"] = r["content"].strip()
    else:
        base["deviation_reason"] = "（AI 分析暂不可用）"
    return base


def find_next_pct(history, date_str):
    """在历史净值序列（正序）里找 date_str 之后第一个交易日的涨跌幅"""
    for h in history:
        if h["date"] > date_str:
            return h["pct"], h["date"]
    return None, None


def review_all_predictions(date_str):
    """批量复盘某天全部预测（纯算术，不逐只调 LLM，速度很快）

    返回 {ok, date, results[], avg_accuracy, direction_correct_count, total}
    """
    history = get_history(date_str)
    if not history:
        return {"ok": False, "msg": f"未找到 {date_str} 的预测记录"}
    predictions = history.get("predictions", {})
    if not predictions:
        return {"ok": False, "msg": f"{date_str} 没有预测记录"}

    results = []
    for code in sorted(predictions.keys()):
        h = fetch_history(code, 60)
        actual_pct, actual_date = find_next_pct(h, date_str)
        if actual_pct is None:
            results.append({"ok": False, "code": code,
                            "msg": "尚未到复盘时间（无后续交易日数据）"})
            continue
        r = review_prediction(date_str, code, actual_pct)
        r["actual_date"] = actual_date
        results.append(r)

    ok_results = [r for r in results if r.get("ok")]
    avg_acc = round(sum(r["accuracy"] for r in ok_results) / len(ok_results), 1) if ok_results else 0
    dir_correct = sum(1 for r in ok_results if r["direction_correct"])
    return {
        "ok": True,
        "date": date_str,
        "results": results,
        "avg_accuracy": avg_acc,
        "direction_correct_count": dir_correct,
        "total": len(ok_results),
    }


def summarize_review(date_str, review_result):
    """让 AI 总结整体复盘偏差原因与改进建议"""
    if not is_configured():
        return "（未配置 LLM，无法分析偏差原因）"
    ok_results = [r for r in review_result.get("results", []) if r.get("ok")]
    if not ok_results:
        return "（无有效复盘数据）"

    lines = [f"复盘日期: {date_str}", "各基金预测 vs 实际："]
    for r in ok_results:
        lines.append(
            f"- {r.get('code')} {r.get('name', '')}: "
            f"预测 {r.get('expected_dir')} {r.get('expected_pct')}%, "
            f"实际 {r.get('actual_dir')} {r.get('actual_pct')}%, "
            f"方向{'对' if r.get('direction_correct') else '错'}, 偏差 {r.get('pct_deviation')}%")
    lines.append(
        f"整体方向正确率: {review_result.get('direction_correct_count')}/{review_result.get('total')}, "
        f"平均准确率: {review_result.get('avg_accuracy')}%")

    sys_p = (
        "你是一名基金复盘分析师。请基于整体复盘数据，简要分析预测偏差的"
        "最可能原因（100 字内，1-2 条），并给出一条改进建议。只输出简短文本，"
        "不要列表标题，直接说。"
    )
    r = llm_chat([{"role": "system", "content": sys_p},
                  {"role": "user", "content": "\n".join(lines)}],
                 temperature=0.4, max_tokens=400)
    if r.get("ok"):
        return r["content"].strip()
    return "（AI 分析暂不可用）"