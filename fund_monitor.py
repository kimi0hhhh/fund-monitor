# -*- coding: utf-8 -*-
"""
基金实时监控 · T+1 加减仓版
pywebview (WebView2) + 内嵌 HTML/CSS，深色金融风面板
数据源：腾讯财经 + 蛋卷基金
"""
import json
import os
import re
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta

import requests
import webview

try:
    import fund_analysis as fa
except Exception:
    fa = None  # 缺依赖时降级，UI 调用处会友好提示

if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(BASE_DIR, "funds_data.json")
ICON_FILE = os.path.join(BASE_DIR, "app.ico")
IDLE_FILE = os.path.join(BASE_DIR, "idle_cash.json")
RATE_FILE = os.path.join(BASE_DIR, "rate_history.json")
PROXY_FILE = os.path.join(BASE_DIR, "proxy_config.json")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Referer": "http://fund.eastmoney.com/",
}
REFRESH_SEC = 30


def _load_proxy():
    """读取代理配置（proxy_config.json 的 proxy 字段），无则返回空串"""
    try:
        with open(PROXY_FILE, "r", encoding="utf-8") as f:
            return (json.load(f).get("proxy") or "").strip()
    except Exception:
        return ""


def _save_proxy(proxy):
    with open(PROXY_FILE, "w", encoding="utf-8") as f:
        json.dump({"proxy": (proxy or "").strip()}, f, ensure_ascii=False, indent=2)


def _get_proxies():
    """返回 requests 用的 proxies 字典；未配置代理返回 None。兼容仅填 host:port。"""
    p = _load_proxy()
    if not p:
        return None
    if "://" not in p:
        p = "http://" + p
    return {"http": p, "https": p}


def load_data():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_data(data):
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("保存失败:", e)


def load_idle_cash():
    """读取闲钱金额（用户可用于加减仓的闲置资金）"""
    try:
        if os.path.exists(IDLE_FILE):
            with open(IDLE_FILE, "r", encoding="utf-8") as f:
                v = json.load(f)
                if isinstance(v, dict):
                    return float(v.get("amount", 0))
                return float(v)
    except Exception:
        pass
    return 0.0


def save_idle_cash(amount):
    try:
        with open(IDLE_FILE, "w", encoding="utf-8") as f:
            json.dump({"amount": amount}, f, ensure_ascii=False)
    except Exception:
        pass


def load_rate_history():
    """读取收益率采样历史 {date: [{t, r}, ...]}"""
    try:
        if os.path.exists(RATE_FILE):
            with open(RATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def save_rate_history(data):
    # 只保留最近 30 天，避免文件无限增长
    try:
        dates = sorted(data.keys(), reverse=True)[:30]
        keep = {d: data[d] for d in dates}
        with open(RATE_FILE, "w", encoding="utf-8") as f:
            json.dump(keep, f, ensure_ascii=False)
    except Exception:
        pass


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _screen_size():
    """返回屏幕尺寸（逻辑像素，兼容 DPI 缩放）；失败回退 1920x1080"""
    try:
        import ctypes
        user32 = ctypes.windll.user32
        w = user32.GetSystemMetrics(0)
        h = user32.GetSystemMetrics(1)
        try:
            # 物理像素 ÷ 缩放因子 = 逻辑像素（GetScaleFactorForDevice: 0=主显示器, 100/125/150...）
            scale = ctypes.windll.shcore.GetScaleFactorForDevice(0) / 100.0
        except Exception:
            scale = 1.0
        return max(1, int(round(w / scale))), max(1, int(round(h / scale)))
    except Exception:
        return 1920, 1080


def _set_topmost(win, top):
    """线程安全地设置窗口置顶/取消置顶（用 Win32 SetWindowPos，避免跨线程操作 .NET 控件）"""
    try:
        import ctypes
        hwnd = win.native.Handle.ToInt32()
        HWND_TOPMOST = -1
        HWND_NOTOPMOST = -2
        SWP_NOMOVE = 0x0002
        SWP_NOSIZE = 0x0001
        ctypes.windll.user32.SetWindowPos(
            hwnd, HWND_TOPMOST if top else HWND_NOTOPMOST,
            0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE)
        return True
    except Exception:
        return False


def _qdate(s):
    """从行情时间字符串里解析出 YYYY-MM-DD 日期"""
    m = re.match(r"(\d{4})[/-]?(\d{1,2})[/-]?(\d{1,2})", (s or "").strip())
    if not m:
        return ""
    y, mo, d = (int(x) for x in m.groups())
    return "%04d-%02d-%02d" % (y, mo, d)


def _next_trading_day(d):
    """返回下一个交易日（仅处理周末，忽略法定节假日）"""
    d = d + timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def nav_date_for_now():
    """按基金交易规则推断本次委托按哪天净值确认：
       交易日 15:00 前算今天，15:00 后及周末顺延到下一交易日"""
    now = datetime.now()
    if now.weekday() < 5 and now.hour < 15:
        return now.strftime("%Y-%m-%d")
    return _next_trading_day(now).strftime("%Y-%m-%d")


def _add_trading_days(date_str, days):
    """从 date_str（含当天不算）往后数 days 个交易日"""
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d")
    except (ValueError, TypeError):
        return date_str
    for _ in range(int(days)):
        d = _next_trading_day(d)
    return d.strftime("%Y-%m-%d")


def _fetch_estimate(code, base):
    """多源获取盘中估值，任一源成功即返回 (gz, pct, time, src)，全部失败返回 None。
    源顺序：天天基金 fundgz → 东财 FundMNFInfo（iPhone UA）→ 蛋卷（自动跟随 301）→ 指数近似。
    说明：腾讯行情盘中 p[2] 恒为 0 不提供估算；指数近似用跟踪标的（场内 ETF/指数）实时涨跌
    估算，仅适用于 ETF 联接/指数型基金（FUND_INDEX_MAP 内），作为第三方源被网络屏蔽时的兜底。"""
    # 1) 天天基金 fundgz（JSONP：jsonpgz({fundcode,gsz,gszzl,gztime,...})）
    try:
        r = requests.get(f"https://fundgz.1234567.com.cn/js/{code}.js",
                         headers=HEADERS, timeout=6, proxies=_get_proxies())
        m = re.search(r"jsonpgz\(\s*(\{.*\})\s*\)", r.text)
        if m:
            d = json.loads(m.group(1))
            gz = _f(d.get("gsz"))
            if gz:
                return gz, _f(d.get("gszzl")), str(d.get("gztime", "")), "fundgz"
    except Exception:
        pass
    # 2) 东财移动端 FundMNFInfo（需手机 UA，桌面 UA 被反爬）
    try:
        url = (f"https://fundmobapi.eastmoney.com/FundMNewApi/FundMNFInfo"
               f"?FCODE={code}&deviceid=Wap&plat=Wap&product=EFund&version=6.2.8")
        r = requests.get(url, headers={"User-Agent": (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1")},
            timeout=6, proxies=_get_proxies())
        d = r.json()
        if d.get("Success"):
            data = d.get("Data") or {}
            gz = _f(data.get("gsz"))
            if gz:
                return gz, _f(data.get("gszzl")), str(data.get("gztime", "")), "em"
    except Exception:
        pass
    # 3) 蛋卷（danjuanapp.com 已 301 → danjuanfunds.com，requests 自动跟随）
    try:
        r = requests.get(f"https://danjuanapp.com/djapi/fund/estimate-nav/{code}",
                         headers=HEADERS, timeout=6, proxies=_get_proxies())
        items = (r.json().get("data") or {}).get("items") or []
        if items:
            d = items[-1]
            gz = _f(d.get("nav") or d.get("gsz"))
            pct = _f(d.get("percentage") or d.get("gszzl") or d.get("pct"))
            t = str(d.get("time") or d.get("date") or "")
            if gz:
                if pct is None and base.get("nav"):
                    pct = round((gz - base["nav"]) / base["nav"] * 100, 2)
                return gz, pct, t, "dj"
    except Exception:
        pass
    # 4) 主动基金重仓股加权估算（真实持仓实时涨跌加权，比单指数准）
    est = _fetch_top_stocks_estimate(code, base)
    if est:
        return est
    # 5) 指数/主题近似（ETF 联接/指数基金 → 跟踪标的实时涨跌）
    return _fetch_index_estimate(code, base)


# 基金代码 → 跟踪标的行情代码（替代估值源：腾讯/新浪实时行情近似）
# kind：idx=指数/ETF联接（较准）｜theme=主动混合/QDII（主题/海外市场近似，粗略）
# 行情代码：sz/sh=股票ETF与指数（~分隔，涨跌幅 p[32]）；us/hk=海外指数（~分隔）；hf_=外盘期货（,分隔）
FUND_INDEX_MAP = {
    # ---- 指数 / ETF 联接（idx）----
    "025857": ("sz159326", "idx"),  # 华夏中证电网设备ETF联接C → 电网设备ETF华夏
    "017193": ("sh512400", "idx"),  # 天弘中证工业有色金属联接C → 有色金属ETF南方
    "016786": ("sz159845", "idx"),  # 鹏华中证1000指数增强C → 中证1000ETF华夏
    "014881": ("sz159770", "idx"),  # 天弘中证机器人联接C → 机器人ETF天弘
    "018897": ("sz159732", "idx"),  # 易方达消费电子ETF联接C → 消费电子ETF华夏
    "022485": ("sh000510", "idx"),  # 国金中证A500指数增强A → 中证A500指数
    "011840": ("sz159819", "idx"),  # 天弘中证人工智能联接C → 人工智能ETF易方达
    "008087": ("sh515050", "idx"),  # 华夏中证5G通信联接C → 通信ETF华夏
    "017412": ("sz159781", "idx"),  # 创金合信中证科创创业50增强A → 科创创业ETF易方达
    "000217": ("sh518880", "idx"),  # 华安黄金ETF联接C → 黄金ETF华安
    "002963": ("sh518880", "idx"),  # 易方达黄金ETF联接C → 黄金ETF华安
    # ---- QDII（theme：用海外市场近似，A股盘中取昨晚/实时海外行情）----
    "024239": ("usNDX", "theme"),   # 华夏全球科技先锋(QDII)C → 纳斯达克100
    "457001": ("hkHSI", "theme"),   # 国富亚洲机会(QDII)A → 恒生指数（港股盘中实时）
    "021662": ("hkHSI", "theme"),   # 国富亚洲机会(QDII)C → 恒生指数
    "163208": ("hf_CL", "theme"),   # 诺安油气能源(QDII-FOF-LOF) → 纽约原油
    "016665": ("usINX", "theme"),   # 天弘全球高端制造(QDII)C → 标普500
    "012922": ("usNDX", "theme"),   # 易方达全球成长精选(QDII)C → 纳斯达克100
    # ---- 主动混合（theme：主题近似，仅供参考）----
}


# 主动混合基金 → 最新季报前十大重仓股（代码, 权重%）。用实时行情按权重加权估算，
# 比套用单一行业指数准得多。数据来源：2026-06-30 二季报。
# 行情代码：A股 sz/sh 前缀；港股 r_hk 前缀；美股 us 前缀。
FUND_TOP_STOCKS = {
    # 华夏军工安全C（军工电子/材料）
    "013566": [("sh688385", 9.78), ("sh688281", 9.02), ("sz000962", 8.97), ("sh688375", 8.60),
               ("sh600562", 8.09), ("sz300593", 8.00), ("sz002025", 6.93), ("sh600482", 6.00),
               ("sh600378", 5.60), ("sz302132", 5.14)],
    # 德邦半导体产业C（半导体设备/芯片）
    "014320": [("sh603986", 6.84), ("sh688256", 5.89), ("sh688012", 5.62), ("sh688167", 5.32),
               ("sh688981", 4.85), ("sz300604", 4.83), ("r_hk01347", 4.79), ("sz002371", 4.19),
               ("sz300054", 4.18), ("sh688041", 3.85)],
    # 东方阿尔法科技智选C（存储/芯片设计）
    "025500": [("sh603986", 8.31), ("sz300475", 7.30), ("sz301308", 6.90), ("sz300223", 6.82),
               ("sz300788", 6.80), ("sz001309", 6.79), ("sh688766", 6.32), ("sh688008", 4.88),
               ("sh688110", 4.27), ("sh688123", 3.95)],
    # 财通成长优选C（光模块/PCB/电子材料）
    "021528": [("sz300502", 9.47), ("sh688519", 8.75), ("sh688498", 8.63), ("sz300408", 7.93),
               ("sz301511", 7.92), ("sz301377", 7.82), ("sz301200", 7.10), ("sz000636", 6.80),
               ("sh605376", 4.28), ("sh603186", 3.61)],
    # 中航机遇领航C（光通信/光模块）
    "018957": [("sz300502", 9.91), ("sz300308", 9.41), ("sz300394", 9.07), ("sh688048", 7.42),
               ("sh600183", 6.77), ("sh600105", 6.46), ("sh688313", 5.57), ("sh601869", 4.87),
               ("sh601138", 4.73), ("sz000725", 3.77)],
}


def _fetch_top_stocks_estimate(code, base):
    """主动基金重仓股加权估算：前十大重仓股实时涨跌按权重加权。
    估算净值 = 昨日官方净值 × (1 + 加权涨跌幅% / 覆盖率)；
    覆盖率=前十大权重合计，权重未满100%部分按加权涨跌外推。
    返回 (gz, pct, time, "holdings")，失败返回 None。"""
    stocks = FUND_TOP_STOCKS.get(code)
    if not stocks or not base.get("nav"):
        return None
    try:
        q = ",".join(tc for tc, _w in stocks)
        r = requests.get(f"http://qt.gtimg.cn/q={q}", headers=HEADERS,
                         timeout=6, proxies=_get_proxies())
        r.encoding = "gbk"
        # 解析各代码的涨跌幅 p[32]（A股/港股均为 ~ 分隔）
        pct_map = {}
        for m in re.finditer(r'v_(\w+)="([^"]*)"', r.text):
            p = m.group(2).split("~")
            if len(p) > 32:
                v = _f(p[32])
                if v is not None:
                    pct_map[m.group(1)] = v
        if not pct_map:
            return None
        # 按权重加权（只统计成功取到行情的股票）
        total_w = 0.0
        weighted = 0.0
        for tc, w in stocks:
            if tc in pct_map:
                weighted += pct_map[tc] * w
                total_w += w
        if total_w <= 0:
            return None
        # 覆盖率不足 100%，按已有重仓股加权涨跌外推整体涨跌
        pct = weighted / total_w
        gz = round(base["nav"] * (1 + pct / 100.0), 4)
        return gz, round(pct, 2), datetime.now().strftime("%Y-%m-%d %H:%M"), "holdings"
    except Exception:
        pass
    return None


def _fetch_index_estimate(code, base):
    """用基金跟踪标的实时行情近似估算盘中净值。
    估算净值 = 昨日官方净值 × (1 + 标的涨跌幅%)；返回 (gz, pct, time, "idx"/"theme")。
    格式兼容：sz/sh/us/hk 波浪线分隔（涨跌幅 p[32]）；hf_ 外盘期货逗号分隔（现价/昨收算涨跌）。"""
    item = FUND_INDEX_MAP.get(code)
    if not item or not base.get("nav"):
        return None
    tcode, kind = item
    try:
        if tcode.startswith("hf_"):
            # 外盘期货（腾讯 hf_CL 等，逗号分隔）：[0]现价 [7]昨收 [12]日期
            r = requests.get(f"http://qt.gtimg.cn/q={tcode}", headers=HEADERS,
                             timeout=6, proxies=_get_proxies())
            r.encoding = "gbk"
            m = re.search(r'v_(\w+)="([^"]*)"', r.text)
            if m:
                p = m.group(2).split(",")
                if len(p) > 7 and _f(p[0]) and _f(p[7]):
                    price, last = _f(p[0]), _f(p[7])
                    pct = round((price - last) / last * 100, 2)
                    gz = round(base["nav"] * (1 + pct / 100.0), 4)
                    return gz, pct, datetime.now().strftime("%Y-%m-%d %H:%M"), kind
            return None
        # 股票/ETF/指数（波浪线分隔）：p[3]现价 p[32]涨跌幅
        r = requests.get(f"http://qt.gtimg.cn/q={tcode}", headers=HEADERS,
                         timeout=6, proxies=_get_proxies())
        r.encoding = "gbk"
        m = re.search(r'v_(\w+)="([^"]*)"', r.text)
        if m:
            p = m.group(2).split("~")
            if len(p) > 32 and _f(p[32]) is not None:
                pct = _f(p[32])
                gz = round(base["nav"] * (1 + pct / 100.0), 4)
                return gz, pct, datetime.now().strftime("%Y-%m-%d %H:%M"), kind
    except Exception:
        pass
    return None


def fetch_batch(codes):
    """腾讯批量行情（名称/净值/日涨跌）+ 多源盘中估值（fundgz/东财/蛋卷）"""
    result = {}
    try:
        q = ",".join(f"jj{c}" for c in codes)
        r = requests.get(f"http://qt.gtimg.cn/q={q}", headers=HEADERS, timeout=8,
                          proxies=_get_proxies())
        r.encoding = "gbk"
        for line in r.text.split(";"):
            m = re.search(r'v_jj(\d{6})="([^"]*)"', line)
            if not m:
                continue
            code, body = m.group(1), m.group(2)
            p = body.split("~")
            if len(p) < 9 or not p[1]:
                continue
            gz = _f(p[2])
            nav = _f(p[5])
            result[code] = {
                "code": code, "name": p[1],
                "gz": gz if gz else (nav if nav else None),
                "gz_pct": _f(p[7]),
                "gz_time": p[8] if len(p) > 8 else "",
                "qdate": _qdate(p[8] if len(p) > 8 else ""),
                "nav": nav,
                "est": False,
            }
    except Exception:
        pass

    for code in codes:
        base = result.get(code)
        if base is None:
            try:
                r = requests.get(f"http://qt.gtimg.cn/q=jj{code}",
                                 headers=HEADERS, timeout=8, proxies=_get_proxies())
                r.encoding = "gbk"
                m = re.search(r'v_jj(\d{6})="([^"]*)"', r.text)
                if m:
                    p = m.group(2).split("~")
                    if len(p) >= 9 and p[1]:
                        gz = _f(p[2])
                        nav = _f(p[5])
                        result[code] = {
                            "code": code, "name": p[1],
                            "gz": gz if gz else (nav if nav else None),
                            "gz_pct": _f(p[7]),
                            "gz_time": p[8], "nav": nav, "est": False,
                            "qdate": _qdate(p[8]),
                        }
            except Exception:
                pass
            continue
        try:
            est = _fetch_estimate(code, base)
            if est:
                base["gz"] = est[0]
                if est[1] is not None:
                    base["gz_pct"] = est[1]
                if est[2]:
                    base["gz_time"] = est[2]
                    base["qdate"] = _qdate(est[2])
                base["est"] = True
                base["est_src"] = est[3] if len(est) > 3 else ""
        except Exception:
            pass
    return result


def fetch_benchmark_pct():
    """获取沪深300指数当日涨跌幅（腾讯行情 sh000300，GBK），用于基准对比线"""
    try:
        r = requests.get("http://qt.gtimg.cn/q=sh000300", headers=HEADERS, timeout=6,
                         proxies=_get_proxies())
        r.encoding = "gbk"
        m = re.search(r'v_sh000300="([^"]*)"', r.text)
        if m:
            p = m.group(1).split("~")
            if len(p) > 32 and p[32]:
                return _f(p[32])
    except Exception:
        pass
    return None


def is_market_open_today():
    """判断今天是否开市：拉沪深300指数最新行情日期，与今天比较（兼容周末+法定节假日）

    指数行情 p[30] 形如 YYYYMMDDHHMMSS，就是最近一个交易日（开市日盘中有当天数据）。
    接口失败时兜底按周末判断。
    """
    try:
        r = requests.get("http://qt.gtimg.cn/q=sh000300", headers=HEADERS, timeout=6,
                         proxies=_get_proxies())
        r.encoding = "gbk"
        m = re.search(r'v_sh000300="([^"]*)"', r.text)
        if m:
            p = m.group(1).split("~")
            if len(p) > 30 and p[30]:
                return p[30][:8] == datetime.now().strftime("%Y%m%d")
    except Exception:
        pass
    return datetime.now().weekday() < 5


class Api:
    def __init__(self):
        self.data = load_data()
        self.info = {}
        self._windows = []
        self._main_window = None
        self._float_window = None
        self._refreshed = False
        self._tasks = {}  # 分析任务池：task_id -> {status, progress, msg, result}
        self._review_summary = None  # 收盘后复盘的汇总缓存
        self._float_collapsed = False  # 悬浮窗是否收起（缩小到右下角）
        self._float_on_top = True  # 悬浮窗是否置顶
        self._mask_amount = False  # 是否隐藏金额数字（总资产/闲钱打码，悬浮窗同步）
        self.idle_cash = load_idle_cash()  # 闲钱（可用于加减仓的闲置资金）
        self._rate_history = load_rate_history()  # 盘中收益率采样
        self._bench_pct = None  # 沪深300当日涨跌幅（基准对比线）

    # ---- 旧数据迁移 ----
    def _migrate(self, code):
        """把旧版本的金额模型迁移到份额模型"""
        d = self.data.get(code)
        if not isinstance(d, dict):
            return
        if "shares" in d:
            return
        amount = d.get("amount", 0.0) or 0.0
        info = self.info.get(code, {})
        gz = info.get("gz") or d.get("pend_gz")
        if not gz:
            return
        shares = amount / gz
        old_realized = d.get("realized", 0.0) or 0.0
        d["shares"] = round(shares, 4)
        d["bought"] = round(amount - old_realized, 2)
        d["sold"] = 0.0
        if "amount" in d:
            del d["amount"]
        if "realized" in d:
            del d["realized"]
        d.setdefault("pending", [])

    # ---- JS 可调用的接口 ----
    def get_state(self):
        funds = []
        total, profit, count, realized = 0.0, 0.0, 0, 0.0
        holdings = 0.0  # 持仓市值（不含闲钱），用于计算各基金占比
        pending = []
        # 今日预测（当天做过分析才有）
        today_preds = {}
        portfolio_pred = None
        if fa is not None:
            today_hist = fa.get_history(datetime.now().strftime("%Y-%m-%d")) or {}
            today_preds = today_hist.get("predictions", {})
            pf = today_hist.get("portfolio")
            if pf and pf.get("portfolio_forecast"):
                fc = pf["portfolio_forecast"]
                portfolio_pred = {"direction": fc.get("direction"),
                                  "expected_pct": fc.get("expected_pct"),
                                  "confidence": fc.get("confidence")}
        for code in sorted(self.data.keys()):
            d = self.data[code]
            info = self.info.get(code, {})
            name = d.get("name") or info.get("name")
            if not name:
                name = "未找到（检查代码）" if self._refreshed else "加载中..."
            pct = info.get("gz_pct")
            gz = info.get("gz")
            shares = d.get("shares", 0.0) or 0.0
            value = shares * gz if shares and gz else 0.0
            pf = None
            if pct is not None and value:
                pf = value - value / (1 + pct / 100.0)
                profit += pf
            total += value
            holdings += value
            count += 1
            bought = d.get("bought", 0.0) or 0.0
            sold = d.get("sold", 0.0) or 0.0
            cum = value + sold - bought
            realized += cum
            # 待确认列表
            for o in d.get("pending", []):
                pending.append({
                    "id": o.get("id", ""),
                    "code": code,
                    "name": name,
                    "type": o.get("type"),
                    "amount": o.get("amount"),
                    "shares": o.get("shares"),
                    "nav_date": o.get("nav_date"),
                    "confirm_date": o.get("confirm_date", o.get("nav_date", "")),
                    "time": o.get("time", ""),
                })
            # 今日预测
            pred = today_preds.get(code)
            today_pred = None
            if pred and pred.get("tomorrow_forecast"):
                tom = pred["tomorrow_forecast"]
                today_pred = {"direction": tom.get("direction"),
                              "expected_pct": tom.get("expected_pct")}
            funds.append({
                "code": code, "name": name, "pct": pct,
                "value": value or None,
                "ratio": round(value / holdings * 100, 1) if holdings and value else 0.0,
                "profit": pf,
                "realized": cum,
                "shares": shares,
                "gz": gz,
                "gz_time": info.get("gz_time", ""),
                "qdate": info.get("qdate", ""),
                "est": info.get("est", False),
                "est_src": info.get("est_src", ""),
                "found": code in self.info,
                "pending_count": len(d.get("pending", [])),
                "confirm_days": int(d.get("confirm_days", 1)),
                "today_pred": today_pred,
            })
        total += self.idle_cash  # 总资产 = 持仓市值 + 闲钱
        return {
            "funds": funds,
            "total": total,
            "profit": profit,
            "realized": realized,
            "count": count,
            "pending": pending,
            "time": time.strftime("%H:%M:%S"),
            "interval": REFRESH_SEC,
            "portfolio_pred": portfolio_pred,
            "review_summary": self._review_summary,
            "idle_cash": self.idle_cash,
            "rate_points": self._rate_history.get(datetime.now().strftime("%Y-%m-%d"), []),
            "mask": self._mask_amount,
        }

    def add_fund(self, code, amount, confirm_days=1):
        """录入已有持仓：按当前净值直接折算成份额"""
        code = str(code).strip()
        if not re.fullmatch(r"\d{6}", code):
            return {"ok": False, "msg": "请输入 6 位基金代码"}
        try:
            amount = float(amount) if str(amount).strip() else 0.0
        except (TypeError, ValueError):
            return {"ok": False, "msg": "金额必须是数字"}
        if amount < 0:
            return {"ok": False, "msg": "金额不能为负数"}
        if code in self.data:
            return {"ok": False, "msg": "该基金已存在，请用「买入」加仓或「卖出」减仓"}

        info = fetch_batch([code]).get(code)
        if not info:
            return {"ok": False, "msg": "未找到该基金，请检查代码"}
        # 折算价：优先估算净值，退官方净值，再退昨日净值；都取不到则报错，避免份额为 0
        price = info.get("gz") or info.get("nav")
        if not price:
            return {"ok": False, "msg": "暂无法获取该基金净值，请稍后再试"}
        shares = amount / price if amount else 0.0
        self.data[code] = {
            "name": info.get("name", ""),
            "shares": round(shares, 4),
            "bought": round(amount, 2),
            "sold": 0.0,
            "confirm_days": int(confirm_days),
            "pending": [],
            "pend_date": info.get("qdate", ""),
            "pend_pct": info.get("gz_pct"),
            "pend_gz": price,
            "navmap": {info.get("qdate", ""): price} if info.get("qdate") and price else {},
        }
        save_data(self.data)
        self.info[code] = info
        self.push()
        return {"ok": True, "msg": f"已添加 {info.get('name', code)}"}

    def buy_fund(self, code, amount):
        """申购：按该基金的确认规则（T+N），到账日确认后折算为份额"""
        code = str(code).strip()
        if code not in self.data:
            return {"ok": False, "msg": "请先添加基金"}
        try:
            amount = float(amount)
            if amount <= 0:
                raise ValueError
        except (TypeError, ValueError):
            return {"ok": False, "msg": "买入金额必须大于 0"}
        cd = int(self.data[code].get("confirm_days", 1))
        nd = nav_date_for_now()
        confirm_date = _add_trading_days(nd, cd)
        order = {
            "id": uuid.uuid4().hex[:10],
            "type": "buy",
            "amount": round(amount, 2),
            "nav_date": nd,
            "confirm_date": confirm_date,
            "time": time.strftime("%H:%M"),
        }
        self.data[code].setdefault("pending", []).append(order)
        save_data(self.data)
        self.push()
        return {"ok": True, "msg": f"买入委托已提交，按 {nd} 净值，预计 {confirm_date} 确认到账"}

    def sell_fund(self, code, amount):
        """赎回：按当前净值估算份额，按该基金确认规则到账后扣减"""
        code = str(code).strip()
        if code not in self.data:
            return {"ok": False, "msg": "请先添加基金"}
        shares = self.data[code].get("shares", 0.0) or 0.0
        if shares <= 0:
            return {"ok": False, "msg": "当前没有可赎回份额"}

        is_all = str(amount).strip().lower() in ("all", "全部", "")
        if is_all:
            sell_shares = shares
        else:
            try:
                amt = float(amount)
                if amt <= 0:
                    raise ValueError
            except (TypeError, ValueError):
                return {"ok": False, "msg": "卖出金额必须大于 0，或输入「全部」"}
            gz = self.info.get(code, {}).get("gz")
            if not gz:
                return {"ok": False, "msg": "暂无净值，无法估算份额，请稍后再试"}
            sell_shares = min(shares, amt / gz)

        cd = int(self.data[code].get("confirm_days", 1))
        nd = nav_date_for_now()
        confirm_date = _add_trading_days(nd, cd)
        order = {
            "id": uuid.uuid4().hex[:10],
            "type": "sell",
            "shares": round(sell_shares, 4),
            "amount": None,
            "nav_date": nd,
            "confirm_date": confirm_date,
            "time": time.strftime("%H:%M"),
        }
        self.data[code].setdefault("pending", []).append(order)
        save_data(self.data)
        self.push()
        return {"ok": True, "msg": f"赎回委托已提交，按 {nd} 净值，预计 {confirm_date} 确认到账"}

    def set_confirm_days(self, code, days):
        """修改某只基金的申赎确认规则（0=T+0，1=T+1，2=T+2）"""
        code = str(code).strip()
        if code not in self.data:
            return {"ok": False, "msg": "基金不存在"}
        try:
            days = int(days)
        except (TypeError, ValueError):
            return {"ok": False, "msg": "确认天数必须是整数"}
        if days < 0 or days > 5:
            return {"ok": False, "msg": "确认天数应在 0~5 之间"}
        self.data[code]["confirm_days"] = days
        save_data(self.data)
        self.push()
        return {"ok": True, "msg": f"已设为 T+{days} 确认" if days else "已设为 T+0 当天确认"}

    def edit_fund(self, code, amount, realized):
        """手动编辑持仓：直接设置持有金额（市值）与累计收益

        - 持有金额 → 按当前净值反推份额
        - 累计收益 → 反推成本基数 bought（累计收益 = 市值 + 已赎回 - 总投入）
        """
        code = str(code).strip()
        if code not in self.data:
            return {"ok": False, "msg": "基金不存在"}
        try:
            amount = float(amount)
            realized = float(realized)
        except (TypeError, ValueError):
            return {"ok": False, "msg": "请输入有效数字"}
        if amount < 0:
            return {"ok": False, "msg": "持有金额不能为负"}
        d = self.data[code]
        info = self.info.get(code, {})
        gz = info.get("gz") or info.get("nav") or d.get("pend_gz")
        if not gz or gz <= 0:
            return {"ok": False, "msg": "暂无净值，无法折算份额"}
        sold = d.get("sold", 0.0) or 0.0
        d["shares"] = round(amount / gz, 4)
        d["bought"] = round(amount + sold - realized, 2)
        save_data(self.data)
        self.push()
        return {"ok": True, "msg": "已更新持仓"}

    def set_idle_cash(self, amount):
        """设置闲钱金额（用户可用于加减仓的闲置资金）"""
        try:
            amount = float(amount)
        except (TypeError, ValueError):
            return {"ok": False, "msg": "请输入有效数字"}
        if amount < 0:
            return {"ok": False, "msg": "闲钱不能为负"}
        self.idle_cash = round(amount, 2)
        save_idle_cash(self.idle_cash)
        self.push()
        return {"ok": True, "msg": "闲钱已更新", "idle_cash": self.idle_cash}

    def cancel_pending(self, code, oid):
        if code not in self.data:
            return {"ok": False, "msg": "基金不存在"}
        before = len(self.data[code].get("pending", []))
        self.data[code]["pending"] = [o for o in self.data[code].get("pending", []) if o.get("id") != oid]
        after = len(self.data[code]["pending"])
        save_data(self.data)
        self.push()
        return {"ok": True, "msg": "已撤单" if after < before else "未找到该委托"}

    def del_fund(self, code):
        self.data.pop(code, None)
        self.info.pop(code, None)
        save_data(self.data)
        self.push()
        return {"ok": True}

    def _confirm_orders(self):
        """到账日后，把待确认委托按确认净值日的净值折算"""
        for code in list(self.data.keys()):
            d = self.data[code]
            pending = d.get("pending", [])
            if not pending:
                continue
            info = self.info.get(code, {})
            qdate = info.get("qdate", "")
            if not qdate:
                continue
            navmap = d.get("navmap", {})
            keep = []
            for o in pending:
                nd = o.get("nav_date", "")
                cf = o.get("confirm_date", nd)  # 兼容旧数据（默认 T+1）
                if qdate >= cf:
                    # 优先使用 navmap 里确认净值日那天的收盘价，否则用当前净值近似
                    price = navmap.get(nd) or info.get("gz") or d.get("pend_gz")
                    if not price:
                        keep.append(o)
                        continue
                    if o["type"] == "buy":
                        add_shares = o["amount"] / price
                        d["shares"] = round((d.get("shares", 0.0) or 0.0) + add_shares, 4)
                        d["bought"] = round((d.get("bought", 0.0) or 0.0) + o["amount"], 2)
                    else:
                        sh = min(o.get("shares", 0.0) or 0.0, d.get("shares", 0.0) or 0.0)
                        val = round(sh * price, 2)
                        d["shares"] = round((d.get("shares", 0.0) or 0.0) - sh, 4)
                        d["sold"] = round((d.get("sold", 0.0) or 0.0) + val, 2)
                else:
                    keep.append(o)
            d["pending"] = keep

    def _settle(self):
        """记录每日收盘价到 navmap，为 T+1 确认提供净值"""
        for code, info in self.info.items():
            d = self.data.get(code)
            if not isinstance(d, dict):
                continue
            qdate = info.get("qdate", "")
            gz = info.get("gz")
            if not qdate:
                continue
            if "navmap" not in d:
                d["navmap"] = {}
            if gz is not None:
                d["navmap"][qdate] = gz
            if not d.get("pend_date"):
                d["pend_date"] = qdate
                d["pend_pct"] = info.get("gz_pct")
                d["pend_gz"] = gz
            elif qdate > d["pend_date"]:
                d["pend_date"] = qdate
                d["pend_pct"] = info.get("gz_pct")
                d["pend_gz"] = gz
            elif qdate == d["pend_date"]:
                d["pend_pct"] = info.get("gz_pct")
                d["pend_gz"] = gz

    def refresh(self):
        codes = list(self.data.keys())
        if not codes:
            self._refreshed = True
            self.push()
            return
        infos = fetch_batch(codes)
        self._bench_pct = fetch_benchmark_pct()
        for c, info in infos.items():
            self.info[c] = info
            if info.get("name"):
                self.data.setdefault(c, {}).setdefault("name", info["name"])
        for c in codes:
            self._migrate(c)
        self._confirm_orders()
        self._settle()
        self._refreshed = True
        save_data(self.data)
        self._maybe_update_review_summary()
        self._sample_rate()
        self.push()

    def _sample_rate(self):
        """盘中（交易日 9:30-15:00）采样当前今日估算收益率，用于折线图"""
        now = datetime.now()
        if now.weekday() >= 5:
            return
        hm = now.hour * 60 + now.minute
        if hm < 9 * 60 + 30 or hm > 15 * 60:
            return
        holdings = 0.0
        profit = 0.0
        for code, d in self.data.items():
            info = self.info.get(code, {})
            gz = info.get("gz")
            shares = d.get("shares", 0.0) or 0.0
            value = shares * gz if shares and gz else 0.0
            holdings += value
            pct = info.get("gz_pct")
            if pct is not None and value:
                profit += value - value / (1 + pct / 100.0)
        if holdings <= 0:
            return
        denom = holdings - profit
        if denom <= 0:
            return
        rate = profit / denom * 100
        today = now.strftime("%Y-%m-%d")
        t = now.strftime("%H:%M")
        points = self._rate_history.setdefault(today, [])
        # 同一分钟去重（30 秒刷新会采到同一分钟，覆盖最后一个）
        if points and points[-1]["t"] == t:
            points[-1]["r"] = round(rate, 3)
            if self._bench_pct is not None:
                points[-1]["b"] = self._bench_pct
        else:
            pt = {"t": t, "r": round(rate, 3)}
            if self._bench_pct is not None:
                pt["b"] = self._bench_pct
            points.append(pt)
        save_rate_history(self._rate_history)

    def _maybe_update_review_summary(self):
        """复盘最近一个有实际结果的历史预测，缓存方向正确率与平均准确率

        始终尝试（不限定收盘后），找到最近一个能复盘出结果的日期（有后续交易日数据）。
        """
        if fa is None:
            return
        today = datetime.now().strftime("%Y-%m-%d")
        if self._review_summary and self._review_summary.get("computed_on") == today:
            return
        try:
            for d in fa.list_history_dates():
                if d >= today:
                    continue
                result = fa.review_all_predictions(d)
                if result.get("ok") and result.get("total"):
                    self._review_summary = {
                        "computed_on": today,
                        "date": d,
                        "direction_correct": result["direction_correct_count"],
                        "total": result["total"],
                        "avg_accuracy": result["avg_accuracy"],
                    }
                    break
        except Exception:
            pass

    def push(self):
        st = json.dumps(self.get_state(), ensure_ascii=False)
        for w in self._windows:
            try:
                # 只推给已加载的窗口，避免未就绪窗口 evaluate_js 阻塞
                if w.events.loaded.is_set():
                    w.evaluate_js(f"render({st})")
            except Exception:
                pass

    def manual_refresh(self):
        threading.Thread(target=self.refresh, daemon=True).start()
        return {"ok": True}

    def show_floating(self):
        """切换到悬浮窗：隐藏主窗口，显示置顶小窗"""
        if self._float_window:
            try:
                self._main_window.hide()
                self._float_window.show()
            except Exception:
                pass
        return {"ok": True}

    def show_main(self):
        """从悬浮窗回到主窗口"""
        if self._float_window:
            try:
                self._float_window.hide()
                self._main_window.show()
            except Exception:
                pass
        return {"ok": True}

    def toggle_mask_amount(self):
        """切换金额隐藏（总资产/闲钱打码，悬浮窗同步）"""
        self._mask_amount = not self._mask_amount
        self.push()
        return {"ok": True, "mask": self._mask_amount}

    def quit_app(self):
        """退出应用"""
        import os as _os
        try:
            for w in self._windows:
                w.destroy()
        except Exception:
            pass
        _os._exit(0)

    def toggle_pin(self):
        """切换悬浮窗置顶/取消置顶"""
        w = self._float_window
        if not w:
            return {"ok": False, "msg": "无悬浮窗"}
        try:
            self._float_on_top = not self._float_on_top
            _set_topmost(w, self._float_on_top)
            return {"ok": True, "on_top": self._float_on_top}
        except Exception as e:
            self._float_on_top = not self._float_on_top
            return {"ok": False, "msg": str(e)[:80]}

    def toggle_collapse(self):
        """收起/展开悬浮窗：收起时缩成小条贴屏幕右下角，展开恢复"""
        w = self._float_window
        if not w:
            return {"ok": False, "msg": "无悬浮窗"}
        try:
            self._float_collapsed = not self._float_collapsed
            sw, sh = _screen_size()
            if self._float_collapsed:
                cw, ch = 260, 72
            else:
                cw, ch = 320, 460
            w.resize(cw, ch)
            # 坐标防越界：DPI 缩放/多显示器下避免窗口飞出屏幕外（移出后点不到展开按钮，表现为"无响应"）
            x = max(0, sw - cw - 14)
            y = max(0, sh - ch - 14)
            w.move(x, y)
            return {"ok": True, "collapsed": self._float_collapsed}
        except Exception as e:
            self._float_collapsed = not self._float_collapsed
            return {"ok": False, "msg": str(e)[:80]}

    # ---------- 分析模式 API ----------
    def get_analysis_config(self):
        if fa is None:
            return {"ok": False, "msg": "fund_analysis 模块未加载"}
        cfg = fa.load_config()
        return {"ok": True, "config": cfg, "configured": fa.is_configured()}

    def save_analysis_config(self, api_key, model=None, base_url=None, provider=None):
        if fa is None:
            return {"ok": False, "msg": "fund_analysis 模块未加载"}
        cfg = fa.load_config()
        if provider: cfg["provider"] = provider
        if model: cfg["model"] = model
        if base_url is not None: cfg["base_url"] = base_url
        if api_key: cfg["api_key"] = api_key
        fa.save_config(cfg)
        return {"ok": True, "configured": fa.is_configured()}

    def get_proxy_config(self):
        """读取代理配置"""
        return {"ok": True, "proxy": _load_proxy()}

    def save_proxy_config(self, proxy):
        """保存代理配置（空串=清除代理）"""
        _save_proxy(proxy or "")
        return {"ok": True, "proxy": _load_proxy()}

    def _get_signal_context(self, code):
        """获取某基金的历史信号（供分析参考，最近 5 条）"""
        if fa is None:
            return None
        sigs = [s for s in fa.load_signals() if s.get("code") == code]
        if not sigs:
            return None
        return [{"title": s.get("title", ""), "direction": s.get("direction", ""),
                 "status": s.get("status", ""), "outcome": s.get("outcome"),
                 "created": s.get("created", "")} for s in sigs[-5:]]

    def analyze_code(self, code):
        """异步分析单只基金：返回 task_id，前端轮询 get_task_status"""
        if fa is None:
            return {"ok": False, "msg": "fund_analysis 模块未加载"}
        if not fa.is_configured():
            return {"ok": False, "msg": "请先在「设置」里配置 API key"}
        if code not in self.data:
            return {"ok": False, "msg": "基金不在持仓中"}
        task_id = uuid.uuid4().hex[:8]
        self._tasks[task_id] = {"status": "running", "progress": 0, "msg": "初始化..."}

        d = self.data[code]
        info = self.info.get(code, {})
        name = d.get("name") or info.get("name") or code
        amount = d.get("bought", 0) or 0
        shares = d.get("shares", 0) or 0
        gz = info.get("gz")
        pct = info.get("gz_pct")

        def worker():
            try:
                def progress(msg, pct_v):
                    self._tasks[task_id] = {
                        "status": "running", "progress": pct_v, "msg": msg}

                result = fa.analyze_fund(
                    code, name, amount, shares, gz, pct, progress_cb=progress,
                    signal_context=self._get_signal_context(code),
                    trade_review_context=fa.build_trade_review_context())
                self._tasks[task_id] = {
                    "status": "done" if result.get("ok") else "error",
                    "progress": 100,
                    "result": result,
                }
                if result.get("ok"):
                    today = datetime.now().strftime("%Y-%m-%d")
                    fa.save_prediction(today, code, result["report"])
                    fa.save_report(today, code, result.get("raw", ""))
                    fa.add_signals_from_report(code, result["report"])
                    fa.add_trade_review_from_report(code, name, result["report"], nav=gz, pct=pct)
            except Exception as e:
                self._tasks[task_id] = {
                    "status": "error", "progress": 100,
                    "msg": f"分析异常: {str(e)[:200]}"}

        threading.Thread(target=worker, daemon=True).start()
        return {"ok": True, "task_id": task_id}

    def analyze_all(self):
        """异步分析全部持仓：先组合分析（结构/板块/整体预测），再逐只分析"""
        if fa is None:
            return {"ok": False, "msg": "fund_analysis 模块未加载"}
        if not fa.is_configured():
            return {"ok": False, "msg": "请先在「设置」里配置 API key"}
        codes = list(self.data.keys())
        if not codes:
            return {"ok": False, "msg": "暂无持仓，请先在持仓页添加基金"}
        task_id = uuid.uuid4().hex[:8]
        self._tasks[task_id] = {"status": "running", "progress": 0,
                                "msg": "准备分析全部持仓..."}
        total = len(codes)

        def worker():
            results = []
            today = datetime.now().strftime("%Y-%m-%d")
            total = len(codes)

            # 第一步：汇总各基金数据 + 技术指标
            funds = []
            for i, code in enumerate(codes):
                d = self.data[code]
                info = self.info.get(code, {})
                name = d.get("name") or info.get("name") or code
                gz = info.get("gz")
                shares = d.get("shares", 0) or 0
                value = shares * gz if shares and gz else 0
                self._tasks[task_id] = {"status": "running",
                                        "progress": round(i * 10.0 / total, 1),
                                        "msg": f"汇总持仓数据 [{i+1}/{total}]..."}
                history = fa.fetch_history(code, 60)
                metrics = fa.compute_metrics(history)
                holdings = fa.fetch_holdings(code)
                funds.append({"code": code, "name": name, "value": value,
                              "gz_pct": info.get("gz_pct"), "metrics": metrics,
                              "holdings": holdings})

            # 第二步：逐只分析（拿到每只的加减仓建议）
            for i, code in enumerate(codes):
                d = self.data[code]
                info = self.info.get(code, {})
                name = d.get("name") or info.get("name") or code
                amount = d.get("bought", 0) or 0
                shares = d.get("shares", 0) or 0
                gz = info.get("gz")
                pct = info.get("gz_pct")
                base_pct = 10 + i * 60.0 / max(total, 1)

                def progress(msg, pct_v, _i=i, _base=base_pct):
                    self._tasks[task_id] = {
                        "status": "running",
                        "progress": round(_base + pct_v * 0.6 / total, 1),
                        "msg": f"逐只分析 [{_i+1}/{total}] {msg}"}

                try:
                    r = fa.analyze_fund(code, name, amount, shares, gz, pct,
                                        progress_cb=progress,
                                        signal_context=self._get_signal_context(code),
                                        trade_review_context=fa.build_trade_review_context())
                    results.append(r)
                    if r.get("ok"):
                        fa.save_prediction(today, code, r["report"])
                        fa.save_report(today, code, r.get("raw", ""))
                        fa.add_signals_from_report(code, r["report"])
                        fa.add_trade_review_from_report(code, name, r["report"], nav=gz, pct=pct)
                except Exception as e:
                    results.append({"ok": False, "code": code, "name": name,
                                    "msg": f"异常: {str(e)[:120]}"})

            # 第三步：把逐只加减建议合并进组合数据，再做组合分析（含闲钱 + 新方向）
            for r in results:
                if r.get("ok"):
                    for f in funds:
                        if f["code"] == r["code"]:
                            f["action_suggestion"] = r["report"].get("action_suggestion")
                            break
            # 组合层面的历史信号上下文（供 AI 参考修正组合判断）
            sig_ctxs = {}
            for code in codes:
                sc = self._get_signal_context(code)
                if sc:
                    sig_ctxs[code] = sc

            self._tasks[task_id] = {"status": "running", "progress": 75,
                                    "msg": "AI 组合策略师分析中（含闲钱 + 新方向）..."}
            portfolio_result = fa.analyze_portfolio(funds, self.idle_cash,
                                                    signal_contexts=sig_ctxs or None,
                                                    trade_review_context=fa.build_trade_review_context())
            if portfolio_result.get("ok"):
                fa.save_portfolio_prediction(today, portfolio_result["report"])

            # 量化配置模型（风险平价简化版）
            allocation = fa.compute_allocation(funds, self.idle_cash)

            # 组合信号统计（供前端展示「信号联动」块）
            all_sigs = fa.load_signals()
            sig_closed = [s for s in all_sigs if s.get("status") in ("兑现", "证伪")]
            sig_active = [s for s in all_sigs if s.get("status") in ("active", "强化", "弱化")]
            sig_correct = sum(1 for s in all_sigs if s.get("outcome") == "correct")
            sig_summary = {
                "total": len(all_sigs),
                "active": len(sig_active),
                "closed": len(sig_closed),
                "correct": sig_correct,
                "hit_rate": round(sig_correct / len(sig_closed) * 100, 1) if sig_closed else None,
            }

            ok_count = sum(1 for r in results if r.get("ok"))
            self._tasks[task_id] = {
                "status": "done", "progress": 100,
                "result": {"type": "full", "portfolio": portfolio_result,
                           "allocation": allocation,
                           "results": results, "ok_count": ok_count,
                           "total": total,
                           "signal_summary": sig_summary,
                           "analyzed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}}

        threading.Thread(target=worker, daemon=True).start()
        return {"ok": True, "task_id": task_id}

    def get_task_status(self, task_id):
        return self._tasks.get(task_id, {"status": "unknown"})

    def list_history_dates(self):
        if fa is None:
            return []
        return fa.list_history_dates()

    def get_history_record(self, date_str, code):
        if fa is None:
            return None
        h = fa.get_history(date_str)
        if not h:
            return None
        return {
            "prediction": h.get("predictions", {}).get(code),
            "report": h.get("reports", {}).get(code),
        }

    def get_history_full(self, date_str):
        """返回某天的完整预测（组合 + 每只基金）"""
        if fa is None:
            return None
        h = fa.get_history(date_str)
        if not h:
            return None
        # 补充基金名称
        predictions = h.get("predictions", {})
        items = []
        for code, pred in predictions.items():
            d = self.data.get(code, {})
            items.append({
                "code": code,
                "name": d.get("name") or code,
                "report": pred,
            })
        return {
            "date": date_str,
            "portfolio": h.get("portfolio"),
            "items": items,
        }

    # ---------- 信号追踪 API ----------
    def get_signals(self):
        """返回全部投资信号 + 胜率统计"""
        if fa is None:
            return {"ok": False, "msg": "模块未加载"}
        signals = fa.load_signals()
        # 补充基金名称
        for s in signals:
            d = self.data.get(s.get("code", ""), {})
            s["fund_name"] = d.get("name") or s.get("code")
        return {"ok": True, "signals": signals, "stats": fa.signal_stats()}

    def update_signal(self, signal_id, status, outcome=None):
        """更新信号状态（强化/弱化/证伪/兑现）与结果（correct/wrong）

        手动操作时结果自动配套：兑现→correct、证伪→wrong、强化/弱化→清空结果
        （AI 审核时才独立判别 outcome；手动点兑现/证伪即表示用户认为判断正确/错误）
        """
        if fa is None:
            return {"ok": False, "msg": "模块未加载"}
        signals = fa.load_signals()
        for s in signals:
            if s.get("id") == signal_id:
                if status:
                    s["status"] = status
                    s["last_audit"] = status
                    if status == "兑现":
                        s["outcome"] = "correct"
                    elif status == "证伪":
                        s["outcome"] = "wrong"
                    else:  # 强化/弱化：进行中，结果置空
                        s["outcome"] = None
                if outcome:
                    s["outcome"] = outcome
                fa.save_signals(signals)
                return {"ok": True, "stats": fa.signal_stats()}
        return {"ok": False, "msg": "信号不存在"}

    def del_signal(self, signal_id):
        """删除单个信号"""
        if fa is None:
            return {"ok": False, "msg": "模块未加载"}
        signals = fa.load_signals()
        signals = [s for s in signals if s.get("id") != signal_id]
        fa.save_signals(signals)
        return {"ok": True, "stats": fa.signal_stats()}

    def clear_signals(self):
        """清空全部信号"""
        if fa is None:
            return {"ok": False, "msg": "模块未加载"}
        fa.save_signals([])
        return {"ok": True, "stats": fa.signal_stats()}

    def get_trade_reviews(self):
        """获取加减仓复盘记录 + 统计"""
        if fa is None:
            return {"ok": False, "msg": "模块未加载", "reviews": [], "stats": {}}
        reviews = fa.load_trade_reviews()
        return {"ok": True, "reviews": reviews, "stats": fa.trade_review_stats(reviews),
                "lessons": fa.load_trade_lessons(),
                "market_open": is_market_open_today(),
                "market_closed": is_market_open_today() and datetime.now().hour >= 23,
                "today": datetime.now().strftime("%Y-%m-%d")}

    def review_trade_reviews(self):
        """AI 复盘所有待复盘的加减仓建议：如果当时听从了建议会盈利还是亏损

        只在开市日收盘后（23:00 后，等基金当日净值更新完）复盘：休市日不复盘（净值未更新），
        盘中不复盘（当日净值未定盘），且只复盘建议日期早于今天的。
        """
        if fa is None:
            return {"ok": False, "msg": "模块未加载"}
        if not fa.is_configured():
            return {"ok": False, "msg": "请先配置 LLM API key"}
        if not is_market_open_today():
            return {"ok": False, "msg": "今天休市，加减仓建议将在下一个开市日收盘后自动复盘"}
        if datetime.now().hour < 23:
            return {"ok": False, "msg": "基金当日净值尚未更新完，复盘将在 23:00 后自动进行"}
        reviews = fa.load_trade_reviews()
        today = datetime.now().strftime("%Y-%m-%d")
        pending = [r for r in reviews
                   if r.get("status") == "pending" and str(r.get("date", "")) < today]
        if not pending:
            has_today = any(r.get("status") == "pending"
                            and str(r.get("date", "")) == today for r in reviews)
            if has_today:
                return {"ok": False, "msg": "今天的加减仓建议需等下一开市日收盘后才能复盘"}
            return {"ok": False, "msg": "没有待复盘的加减仓建议"}
        task_id = uuid.uuid4().hex[:8]
        self._tasks[task_id] = {"status": "running", "progress": 0,
                                "msg": "AI 复盘加减仓建议中..."}

        def worker():
            results = []
            total = len(pending)
            for i, r in enumerate(pending):
                self._tasks[task_id] = {"status": "running",
                                        "progress": round(i * 100.0 / total, 1),
                                        "msg": f"复盘建议 [{i+1}/{total}] {r.get('name','')[:16]}"}
                code = r.get("code", "")
                quote = None
                if code:
                    try:
                        info = fetch_batch([code]).get(code)
                        if info:
                            quote = f"{info.get('name','')} 净值 {info.get('gz')} 今日 {info.get('gz_pct')}%"
                    except Exception:
                        pass
                rv = fa.review_trade_advice(r, quote)
                if rv:
                    r["status"] = "reviewed"
                    r["review"] = rv
                    results.append({"id": r.get("id", ""), "name": r.get("name", ""),
                                    "result": rv.get("result"), "pnl_pct": rv.get("pnl_pct"),
                                    "bias_type": rv.get("bias_type", "")})
                else:
                    results.append({"id": r.get("id", ""), "name": r.get("name", ""),
                                    "result": "复盘失败", "pnl_pct": None})
            fa.save_trade_reviews(reviews)
            # 复盘完成后提炼经验教训缓存（喂回下次分析形成闭环）
            try:
                fa.summarize_trade_lessons()
            except Exception:
                pass
            self._tasks[task_id] = {"status": "done", "progress": 100,
                                    "result": {"results": results, "stats": fa.trade_review_stats(reviews)}}

        threading.Thread(target=worker, daemon=True).start()
        return {"ok": True, "task_id": task_id}

    def audit_signals(self):
        """AI 自动审核所有进行中的信号（兑现/证伪/强化/弱化/维持原判/信息不足）"""
        if fa is None:
            return {"ok": False, "msg": "模块未加载"}
        if not fa.is_configured():
            return {"ok": False, "msg": "请先配置 LLM API key"}
        signals = fa.load_signals()
        active = [s for s in signals if s.get("status") in ("active", "强化", "弱化")]
        if not active:
            return {"ok": False, "msg": "没有进行中的信号可审核"}
        task_id = uuid.uuid4().hex[:8]
        self._tasks[task_id] = {"status": "running", "progress": 0,
                                "msg": "AI 审核信号中..."}

        def worker():
            results = []
            total = len(active)
            before = {s.get("id"): (s.get("status"), s.get("outcome")) for s in active}
            for i, s in enumerate(active):
                self._tasks[task_id] = {"status": "running",
                                        "progress": round(i * 100.0 / total, 1),
                                        "msg": f"审核信号 [{i+1}/{total}] {s.get('title','')[:20]}"}
                code = s.get("code", "")
                quote = None
                if code:
                    try:
                        info = fetch_batch([code]).get(code)
                        if info:
                            quote = f"{info.get('name','')} 净值 {info.get('gz')} 今日 {info.get('gz_pct')}%"
                    except Exception:
                        pass
                judge = fa.audit_signal(s, quote)
                if judge and judge.get("status") in ("兑现", "证伪", "强化", "弱化", "维持原判", "信息不足"):
                    status = judge["status"]
                    oc = judge.get("outcome", "")
                    if status in ("兑现", "证伪"):
                        # 已了结：状态 + AI 判别的结果
                        s["status"] = status
                        s["outcome"] = oc if oc in ("correct", "wrong") else None
                    elif status in ("强化", "弱化"):
                        # 进行中：更新状态，清空结果
                        s["status"] = status
                        s["outcome"] = None
                    else:
                        # 维持原判 / 信息不足：状态与结果保持不动，留待下次再审
                        pass
                    s["audit_reason"] = judge.get("reason", "")
                    s["last_audit"] = status
                    results.append({"id": s.get("id", ""), "title": s.get("title", ""), "code": code,
                                    "status": status, "reason": judge.get("reason", ""),
                                    "changed": before.get(s.get("id")) != (s.get("status"), s.get("outcome"))})
                else:
                    results.append({"id": s.get("id", ""), "title": s.get("title", ""), "code": code,
                                    "status": "审核失败", "reason": "AI 返回异常", "changed": False})
            fa.save_signals(signals)
            self._tasks[task_id] = {"status": "done", "progress": 100,
                                    "result": {"results": results, "stats": fa.signal_stats()}}

        threading.Thread(target=worker, daemon=True).start()
        return {"ok": True, "task_id": task_id}

    def review_all(self, date_str):
        """异步批量复盘：组合预测 + 全部持仓预测（按日期一起复盘）"""
        if fa is None:
            return {"ok": False, "msg": "fund_analysis 模块未加载"}
        task_id = uuid.uuid4().hex[:8]
        self._tasks[task_id] = {"status": "running", "progress": 10, "msg": "复盘中..."}

        def worker():
            try:
                result = fa.review_all_predictions(date_str)
                if result.get("ok"):
                    # 补充基金名称
                    for r in result["results"]:
                        d = self.data.get(r.get("code"), {})
                        r["name"] = d.get("name") or r.get("code")

                    # 组合复盘：计算组合实际涨跌（持仓市值加权）
                    hist = fa.get_history(date_str)
                    if hist and hist.get("portfolio"):
                        total_val, weighted = 0.0, 0.0
                        for code, d in self.data.items():
                            gz = (self.info.get(code, {}) or {}).get("gz")
                            shares = d.get("shares", 0) or 0
                            value = shares * gz if shares and gz else 0
                            if value <= 0:
                                continue
                            h = fa.fetch_history(code, 60)
                            pct, _ = fa.find_next_pct(h, date_str)
                            if pct is not None:
                                total_val += value
                                weighted += value * pct
                        if total_val > 0:
                            combo_pct = round(weighted / total_val, 4)
                            result["portfolio_review"] = fa.review_portfolio(
                                date_str, combo_pct)

                    # 配置了 LLM 则总结整体偏差原因
                    self._tasks[task_id] = {"status": "running", "progress": 80,
                                            "msg": "AI 分析偏差原因..."}
                    result["deviation_reason"] = fa.summarize_review(date_str, result)
                self._tasks[task_id] = {
                    "status": "done" if result.get("ok") else "error",
                    "progress": 100, "result": result}
            except Exception as e:
                self._tasks[task_id] = {
                    "status": "error", "progress": 100,
                    "msg": f"复盘异常: {str(e)[:200]}"}

        threading.Thread(target=worker, daemon=True).start()
        return {"ok": True, "task_id": task_id}


HTML_MAIN = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta http-equiv="X-UA-Compatible" content="ie=edge">
<style>
:root{
  --bg:#dedee3; --panel:#ffffff; --panel2:#f2f2f7;
  --line:rgba(0,0,0,.18);
  --txt:#1c1c1e; --sub:#8e8e93;
  --up:#ff3b30; --down:#34c759; --brand:#007aff;
  --orange:#ff9500; --purple:#5856d6;
  --scrollbar:#c7c7cc; --track-bg:#d9d9de;
  --thead-bg:#f7f7f9; --placeholder:#a1a1a6;
  --bg-glow:#dfe5f0; --glass-highlight:rgba(255,255,255,.9);
  --brand-rgb:0,122,255; --up-rgb:255,59,48; --down-rgb:52,199,89;
  --orange-rgb:255,149,0; --purple-rgb:88,86,214; --sub-rgb:142,142,147;
}
body.dark{
  --bg:#0b0f1a; --panel:#131a2b; --panel2:#0f1524;
  --line:rgba(255,255,255,.07);
  --txt:#e8edf7; --sub:#8a94ad;
  --up:#ff5252; --down:#26d07c; --brand:#5b8cff;
  --orange:#f5a623; --purple:#8f6bff;
  --scrollbar:#2a3350; --track-bg:#1c2740;
  --thead-bg:#111830; --placeholder:#5a6480;
  --bg-glow:#1b2b55; --glass-highlight:rgba(255,255,255,.06);
  --brand-rgb:91,140,255; --up-rgb:255,82,82; --down-rgb:38,208,124;
  --orange-rgb:245,166,35; --purple-rgb:143,107,255; --sub-rgb:138,148,173;
}
*{margin:0;padding:0;box-sizing:border-box;font-family:"Microsoft YaHei UI","Segoe UI",sans-serif}
html,body{height:100%}
body{background:radial-gradient(1200px 500px at 20% -10%,var(--bg-glow) 0%,var(--bg) 55%);
     color:var(--txt);overflow:hidden;font-size:14px}
#app{display:flex;flex-direction:column;height:100vh;padding:18px 22px;gap:14px}
#view-holdings{display:flex;flex-direction:column;gap:14px;flex:1;overflow:hidden;min-height:0}
#view-analysis{flex-direction:column}

/* ---------- 顶部标题 ---------- */
.header{display:flex;align-items:center;gap:10px}
.header .logo{width:34px;height:34px;border-radius:10px;flex:none;
  background:linear-gradient(135deg,var(--brand),var(--purple));
  display:flex;align-items:center;justify-content:center;font-size:17px}
.header h1{font-size:17px;font-weight:600;letter-spacing:.5px}
.header .dot{width:8px;height:8px;border-radius:50%;background:var(--down);
  box-shadow:0 0 8px var(--down);margin-left:2px}
.header .status{margin-left:auto;color:var(--sub);font-size:12px;display:flex;align-items:center;gap:14px}
.header button{cursor:pointer;border:none;border-radius:8px;padding:7px 16px;font-size:13px;
  background:linear-gradient(135deg,var(--brand),var(--brand));color:#fff}
.header button:active{transform:scale(.96)}

/* ---------- 汇总卡片 ---------- */
.cards{display:grid;grid-template-columns:repeat(4,1fr);gap:14px}
.card{background:var(--panel);
  border:1px solid var(--line);border-radius:16px;padding:16px 20px;position:relative;overflow:hidden;min-height:120px;
  box-shadow:inset 0 1px 0 var(--glass-highlight),0 2px 12px rgba(0,0,0,.1)}
.card.main{grid-column:span 1;min-height:140px;padding:20px 22px}
.card.main .v{font-size:34px}
/* 折叠卡片区：只保留主卡行（总资产 + 今日收益），释放空间给持仓表格 */
body.cards-collapsed .card:not(.main){display:none}
.card::after{content:"";position:absolute;right:-30px;top:-30px;width:110px;height:110px;
  border-radius:50%;background:radial-gradient(circle,rgba(var(--brand-rgb),.15),transparent 70%)}
.card .k{color:var(--sub);font-size:12px;margin-bottom:8px;letter-spacing:1px}
.card .v{font-size:28px;font-weight:700;font-variant-numeric:tabular-nums;line-height:1.1}
.card .v small{font-size:13px;font-weight:400;color:var(--sub);margin-left:4px}
.card .sub{margin-top:6px;font-size:12px;color:var(--sub)}
.card.idle-card{cursor:pointer;border-color:rgba(var(--orange-rgb),.4)}
.card.idle-card:hover{border-color:rgba(var(--orange-rgb),.8);background:linear-gradient(180deg,rgba(var(--orange-rgb),.08),rgba(var(--orange-rgb),.03))}
.card.idle-card::after{background:radial-gradient(circle,rgba(var(--orange-rgb),.2),transparent 70%)}
.idle-edit{font-size:12px;color:var(--orange);margin-left:4px}
.card.rate-card{grid-column:span 1;cursor:pointer}
.card.rate-card.expanded{grid-column:1/-1}
.rate-chart{width:100%;height:60px;margin-top:8px}
.card.rate-card.expanded .rate-chart{height:210px}
.rate-chart svg{width:100%;height:100%;display:block}
.up{color:var(--up)} .down{color:var(--down)} .flat{color:var(--sub)}

/* ---------- 添加栏 ---------- */
.addbar{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.addbar input{background:var(--panel2);border:1px solid var(--line);border-radius:9px;
  color:var(--txt);padding:9px 13px;font-size:13px;outline:none;width:120px;transition:.2s}
.addbar input:focus{border-color:var(--brand);box-shadow:0 0 0 3px rgba(var(--brand-rgb),.12)}
.addbar input::placeholder{color:var(--placeholder)}
.addbar select{background:var(--panel2);border:1px solid var(--line);border-radius:9px;
  color:var(--txt);padding:9px 10px;font-size:13px;outline:none;cursor:pointer}
.addbar .btn{cursor:pointer;border:none;border-radius:9px;padding:9px 16px;font-size:13px;font-weight:600;color:#fff;transition:.15s}
.addbar .btn:active{transform:scale(.96)}
.btn-add{background:linear-gradient(135deg,var(--up),var(--orange))}
.btn-buy{background:linear-gradient(135deg,var(--brand),var(--brand))}
.btn-sell{background:linear-gradient(135deg,var(--down),var(--down))}
.addbar .btn.btn-del{background:rgba(var(--up-rgb),.14);border:1px solid rgba(var(--up-rgb),.6);color:var(--up)}
.addbar .btn.btn-del:hover{background:rgba(var(--up-rgb),.26);border-color:rgba(var(--up-rgb),.85)}
.btn-rule{background:linear-gradient(135deg,var(--purple),var(--brand))}
.btn-edit{background:linear-gradient(135deg,var(--orange),var(--orange))}
.addbar .divider{width:1px;height:24px;background:var(--line);align-self:center}
.addbar .tip{color:var(--sub);font-size:12px;margin-left:auto}

/* ---------- 表格 ---------- */
.tablewrap{flex:1;overflow:auto;border:1px solid var(--line);border-radius:16px;
  background:var(--panel2)}
table{width:100%;border-collapse:collapse}
thead th{position:sticky;top:0;background:var(--thead-bg);color:var(--sub);font-weight:500;
  font-size:12px;padding:11px 14px;text-align:right;border-bottom:1px solid var(--line);z-index:2;white-space:nowrap}
thead th:nth-child(1){text-align:left}
thead th:nth-child(2){text-align:left}
thead th.sortable{cursor:pointer;user-select:none}
thead th.sortable:hover{color:var(--brand)}
thead th.sortable.sorted{color:var(--brand)}
.sarr{font-size:10px;margin-left:3px}
tbody td{padding:12px 14px;text-align:right;border-bottom:1px solid rgba(0,0,0,.04);
  font-variant-numeric:tabular-nums;white-space:nowrap}
tbody tr{cursor:default;transition:background .15s}
tbody tr:hover{background:rgba(var(--brand-rgb),.08)}
tbody tr.sel{background:rgba(var(--brand-rgb),.12)}
tbody td:nth-child(1){text-align:left;color:var(--sub);font-size:13px}
tbody td:nth-child(2){text-align:left;font-weight:600}
tbody tr:last-child td{border-bottom:none}
.badge{font-size:10px;padding:2px 7px;border-radius:20px;margin-left:8px;font-weight:400;vertical-align:1px}
.badge.flat{background:rgba(var(--sub-rgb),.15);color:var(--sub)}
.badge.est{background:rgba(var(--orange-rgb),.12);color:var(--orange)}
.badge.nav{background:rgba(var(--sub-rgb),.15);color:var(--sub)}
.stale{color:var(--sub);font-size:10px;margin-left:4px;font-weight:400;opacity:.85}
.badge.pending{background:rgba(var(--up-rgb),.12);color:var(--up)}
.badge.rule{background:rgba(var(--purple-rgb),.12);color:var(--purple)}
.badge.warn{background:rgba(var(--orange-rgb),.15);color:var(--orange)}
.pct{font-weight:700}
.empty{display:flex;flex-direction:column;align-items:center;justify-content:center;
  height:100%;color:var(--sub);gap:10px;font-size:13px}
.empty .ic{font-size:42px;opacity:.5}

/* ---------- 待确认 ---------- */
.pendingbox{margin-top:8px;border:1px solid var(--line);border-radius:14px;background:var(--panel2);padding:12px 16px;max-height:140px;overflow:auto}
.pendingbox.hidden{display:none}
.pendingbox .phead{font-size:13px;font-weight:600;margin-bottom:8px;color:var(--txt)}
.pendingitem{display:flex;align-items:center;justify-content:space-between;padding:8px 0;border-bottom:1px solid rgba(0,0,0,.04);font-size:13px}
.pendingitem:last-child{border-bottom:none}
.pendingitem .meta{color:var(--sub)}
.pendingitem button{border:1px solid var(--line);background:transparent;color:var(--txt);border-radius:6px;padding:4px 12px;font-size:12px;cursor:pointer}
.pendingitem button:hover{border-color:var(--up);color:var(--up)}

/* ---------- 弹窗 ---------- */
.mask{position:fixed;inset:0;background:rgba(0,0,0,.4);backdrop-filter:blur(3px);
  display:none;align-items:center;justify-content:center;z-index:99}
.mask.show{display:flex}
.modal{background:var(--panel);border:1px solid var(--line);border-radius:16px;
  padding:26px 28px;width:360px;box-shadow:0 24px 60px rgba(0,0,0,.5)}
.modal h3{font-size:15px;margin-bottom:10px}
.modal .note{color:var(--sub);font-size:12px;margin-bottom:14px;min-height:18px}
.modal input{width:100%;background:var(--panel2);border:1px solid var(--line);border-radius:9px;
  color:var(--txt);padding:10px 13px;font-size:14px;outline:none;margin-bottom:16px}
.modal input:focus{border-color:var(--brand)}
.modal select{width:100%;background:var(--panel2);border:1px solid var(--line);border-radius:9px;
  color:var(--txt);padding:10px 13px;font-size:14px;outline:none;margin-bottom:16px}
.modal select:focus{border-color:var(--brand)}
.modal .row{display:flex;gap:10px;justify-content:flex-end}
.modal button{cursor:pointer;border:none;border-radius:9px;padding:9px 20px;font-size:13px;font-weight:600}
.modal .ok{background:linear-gradient(135deg,var(--brand),var(--brand));color:#fff}
.modal .cancel{background:transparent;color:var(--sub);border:1px solid var(--line)}
.toast{position:fixed;top:20px;left:50%;transform:translateX(-50%) translateY(-80px);
  background:var(--track-bg);border:1px solid var(--line);color:var(--txt);border-radius:10px;
  padding:11px 22px;font-size:13px;transition:.3s;z-index:100;box-shadow:0 10px 30px rgba(0,0,0,.4)}
.toast.show{transform:translateX(-50%) translateY(0)}
.toast.err{border-color:rgba(var(--up-rgb),.5)}
::-webkit-scrollbar{width:8px}
::-webkit-scrollbar-thumb{background:var(--scrollbar);border-radius:4px}

/* ---------- 分析模式 ---------- */
.tabs{display:flex;gap:6px;margin-left:18px}
.tabs .tab{background:transparent;border:1px solid var(--line);color:var(--sub);
  padding:6px 14px;border-radius:8px;cursor:pointer;font-size:13px}
.tabs .tab.active{background:var(--brand);border-color:var(--brand);color:#fff}
.ana-tabs{display:flex;gap:8px;align-items:center;padding:6px 0 12px;border-bottom:1px solid var(--line);margin-bottom:14px}
.ana-tab{background:var(--panel2);border:1px solid var(--line);color:var(--sub);
  padding:7px 16px;border-radius:8px;cursor:pointer;font-size:13px}
.ana-tab.active{background:var(--brand);border-color:var(--brand);color:#fff}
.ana-hint{background:rgba(var(--orange-rgb),.08);border:1px solid rgba(var(--orange-rgb),.3);
  border-radius:10px;padding:14px 18px;color:var(--orange);margin-bottom:12px}
.ana-bar{display:flex;gap:10px;align-items:center;flex-wrap:wrap;
  background:var(--panel);border:1px solid var(--line);border-radius:12px;
  padding:12px 14px;margin-bottom:14px}
.ana-bar label{color:var(--sub);font-size:13px}
.ana-bar select,.ana-bar input{background:var(--panel2);border:1px solid var(--line);
  border-radius:8px;color:var(--txt);padding:8px 12px;font-size:13px;outline:none}
.ana-bar .btn{cursor:pointer;border:none;border-radius:8px;padding:8px 16px;
  font-size:13px;font-weight:600;color:#fff}
.ana-progress{margin:0 0 14px}
.ana-progress .bar-bg{height:8px;background:var(--panel2);border-radius:4px;overflow:hidden}
.ana-progress .bar-fg{height:100%;background:linear-gradient(90deg,var(--brand),var(--down));transition:width .3s}
.ana-progress .msg{color:var(--sub);font-size:12px;margin-top:6px}

/* 信号页：进度条 / 筛选 / 闪烁 */
.sig-progress{margin:0 0 12px}
.sig-progress .bar-bg{height:8px;background:var(--panel2);border-radius:4px;overflow:hidden}
.sig-progress .bar-fg{height:100%;background:linear-gradient(90deg,var(--brand),var(--down));transition:width .3s}
.sig-progress .msg{color:var(--sub);font-size:12px;margin-top:6px}
.sig-filter{display:flex;gap:6px;margin-bottom:10px;flex-wrap:wrap}
.sig-filter .fbtn{border:1px solid var(--line);background:var(--panel);color:var(--sub);
  padding:4px 14px;border-radius:16px;font-size:12px;cursor:pointer;transition:.15s}
.sig-filter .fbtn.active{background:var(--brand);border-color:var(--brand);color:#fff}
@keyframes sig-flash{
  0%,100%{box-shadow:inset 0 1px 0 var(--glass-highlight),0 2px 12px rgba(0,0,0,.1)}
  50%{box-shadow:0 0 0 4px rgba(var(--brand-rgb),.55),0 2px 12px rgba(0,0,0,.1)}
}
.sig-card.flash{animation:sig-flash 0.9s ease-in-out 4}
.ana-report{background:var(--panel);border:1px solid var(--line);border-radius:12px;
  padding:18px 22px}
.ana-report h3{font-size:15px;margin:16px 0 8px;color:var(--brand)}
.ana-report h3:first-child{margin-top:0}
.ana-report .sec{margin-bottom:14px}
.ana-report .row{display:flex;gap:18px;flex-wrap:wrap;margin:6px 0}
.ana-report .row .item{flex:1;min-width:160px}
.ana-report .k{color:var(--sub);font-size:12px;margin-bottom:4px}
.ana-report .v{font-size:14px;font-weight:600}
.ana-report ul{margin:6px 0 0 18px;line-height:1.8}
.ana-report .badge{display:inline-block;padding:3px 10px;border-radius:12px;
  font-size:12px;font-weight:700;margin-right:6px}
.ana-report .badge.up{background:rgba(var(--up-rgb),.12);color:var(--up)}
.ana-report .badge.down{background:rgba(var(--down-rgb),.12);color:var(--down)}
.ana-report .badge.flat{background:rgba(var(--sub-rgb),.15);color:var(--sub)}
.ana-report .metrics{background:var(--panel2);border-radius:8px;padding:10px 14px;
  font-size:12px;color:var(--sub);margin-bottom:10px}
.history-list .h-item{display:flex;align-items:center;justify-content:space-between;
  background:var(--panel);border:1px solid var(--line);border-radius:10px;
  padding:10px 16px;margin-bottom:8px;cursor:pointer;font-size:13px}
.history-list .h-item:hover{border-color:var(--brand)}
.history-list .h-item .meta{color:var(--sub);font-size:12px}
.empty-state{color:var(--sub);text-align:center;padding:40px 0;font-size:13px}
.role-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin:6px 0 10px}
.role-card{background:var(--panel2);border:1px solid var(--line);border-radius:8px;padding:8px 12px}
.role-card .rn{color:var(--brand);font-size:12px;font-weight:600;margin-bottom:4px}
.role-card .rv{font-size:12px;color:var(--txt);line-height:1.5}

/* ---------- 信号 tab ---------- */
.sig-stats{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:14px}
.sig-stat{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:10px 14px;text-align:center;
  box-shadow:inset 0 1px 0 var(--glass-highlight),0 2px 12px rgba(0,0,0,.1)}
.sig-stat .n{font-size:20px;font-weight:700;font-variant-numeric:tabular-nums}
.sig-stat .l{font-size:11px;color:var(--sub);margin-top:2px}
.sig-card{background:var(--panel);border:1px solid var(--line);border-radius:12px;
  padding:14px 16px;margin-bottom:10px;position:relative;overflow:hidden;
  box-shadow:inset 0 1px 0 var(--glass-highlight),0 2px 12px rgba(0,0,0,.1)}
.sig-card::before{content:"";position:absolute;left:0;top:0;bottom:0;width:3px;background:rgba(var(--sub-rgb),.4)}
.sig-card.bull::before{background:var(--up)}
.sig-card.bear::before{background:var(--down)}
.sig-card .sig-head{display:flex;align-items:center;gap:8px;margin-bottom:6px}
.sig-card .sig-title{font-weight:700;font-size:14px;flex:1}
.sig-card .sig-meta{font-size:12px;color:var(--sub);margin-bottom:6px;line-height:1.6}
.sig-card .sig-basis{font-size:12px;color:var(--txt);background:var(--panel2);border-radius:8px;padding:8px 12px;margin-bottom:10px;line-height:1.6}
.sig-card .sig-actions{display:flex;gap:6px;flex-wrap:wrap}
.sig-card .sig-actions button{border:1px solid var(--line);background:transparent;color:var(--txt);
  border-radius:6px;padding:4px 12px;font-size:12px;cursor:pointer;transition:.15s}
.sig-card .sig-actions button:hover{border-color:var(--brand);color:var(--brand)}
.sig-status{font-size:11px;padding:2px 8px;border-radius:10px;font-weight:600;flex:none}
.sig-status.active{background:rgba(var(--brand-rgb),.12);color:var(--brand)}
.sig-status.strong{background:rgba(var(--up-rgb),.12);color:var(--up)}
.sig-status.weak{background:rgba(var(--sub-rgb),.15);color:var(--sub)}
.sig-status.hit{background:rgba(var(--down-rgb),.12);color:var(--down)}
.sig-status.miss{background:rgba(var(--sub-rgb),.2);color:var(--sub)}
.sig-del{border:none;background:transparent;color:var(--sub);font-size:12px;cursor:pointer;
  padding:2px 6px;border-radius:4px;flex:none;line-height:1}
.sig-del:hover{color:var(--up);background:rgba(var(--up-rgb),.1)}
.sig-clear-row{display:flex;justify-content:flex-end;margin-bottom:10px}
.sig-clear{border:1px solid rgba(var(--up-rgb),.5);background:rgba(var(--up-rgb),.08);color:var(--up);
  border-radius:6px;padding:5px 14px;font-size:12px;cursor:pointer;transition:.15s}
.sig-clear:hover{background:rgba(var(--up-rgb),.16);border-color:rgba(var(--up-rgb),.8)}
.sig-audit{border:1px solid rgba(var(--brand-rgb),.5);background:rgba(var(--brand-rgb),.08);color:var(--brand);
  border-radius:6px;padding:5px 14px;font-size:12px;cursor:pointer;transition:.15s;margin-right:8px}
.sig-audit:hover{background:rgba(var(--brand-rgb),.16);border-color:rgba(var(--brand-rgb),.8)}
.sig-audit-reason{font-size:12px;color:var(--brand);background:rgba(var(--brand-rgb),.06);
  border-radius:6px;padding:6px 10px;margin-bottom:8px;line-height:1.5}
</style>
</head>
<body>
<div id="app">
  <div class="header">
    <div class="logo">📈</div>
    <h1>我的基金监控</h1><span class="dot"></span>
    <div class="tabs">
      <button class="tab active" id="tab-holdings" onclick="switchView('holdings')">持仓</button>
      <button class="tab" id="tab-analysis" onclick="switchView('analysis')">分析</button>
    </div>
    <div class="status">
      <span id="updatetime">--</span>
      <button id="themeBtn" onclick="toggleTheme()" title="切换深色/浅色主题">🌙</button>
      <button id="collapseCards" onclick="toggleCards()" title="收起/展开汇总卡片">收起卡片</button>
      <button id="maskBtn" onclick="toggleMask()" title="隐藏/显示总资产与闲钱金额">🙈 隐藏金额</button>
      <button id="floatbtn" onclick="showFloating()">悬浮窗</button>
      <button onclick="doRefresh()">立即刷新</button>
    </div>
  </div>

  <div id="view-holdings">

  <div class="cards">
    <div class="card main">
      <div class="k">总资产</div>
      <div class="v" id="total">--<small>元</small></div>
      <div class="sub" id="count">--</div>
    </div>
    <div class="card main">
      <div class="k">今日估算收益</div>
      <div class="v" id="profit">--</div>
      <div class="sub">盘中估值仅供参考 · 红涨绿跌</div>
    </div>
    <div class="card main">
      <div class="k">今日估算收益率</div>
      <div class="v" id="rate">--</div>
      <div class="sub" id="estnote">每 30 秒自动刷新</div>
    </div>
    <div class="card main">
      <div class="k">今日总体预测</div>
      <div class="v" id="pf-forecast">--</div>
      <div class="sub" id="pf-detail">暂无今日分析</div>
    </div>
    <div class="card">
      <div class="k">累计收益</div>
      <div class="v" id="realized">--</div>
      <div class="sub">市值 + 已赎回 - 总投入</div>
    </div>
    <div class="card idle-card" onclick="editIdleCash()" title="点击设置闲钱">
      <div class="k">闲钱 <span class="idle-edit">✎</span></div>
      <div class="v" id="idle-cash">--<small>元</small></div>
      <div class="sub">已计入总资产 · 点击设置</div>
    </div>
    <div class="card">
      <div class="k">预测准确率</div>
      <div class="v" id="acc-avg">--</div>
      <div class="sub" id="acc-detail">暂无复盘数据</div>
    </div>
    <div class="card rate-card" onclick="toggleRateChart()" title="点击展开/收起">
      <div class="k">今日收益率走势 <span class="idle-edit">⤢</span></div>
      <div class="rate-chart" id="rate-chart"></div>
    </div>
  </div>

  <div class="addbar">
    <input id="in_code" maxlength="6" placeholder="基金代码" onkeydown="if(event.key==='Enter')document.getElementById('in_amt').focus()">
    <input id="in_amt" placeholder="持有金额（元）" onkeydown="if(event.key==='Enter')doAdd()">
    <select id="in_days" title="申赎确认规则">
      <option value="1" selected>T+1 确认</option>
      <option value="0">T+0 当天</option>
      <option value="2">T+2 确认</option>
    </select>
    <button class="btn btn-add" onclick="doAdd()">＋ 录入持仓</button>
    <button class="btn btn-buy" onclick="doBuy()">买入</button>
    <button class="btn btn-sell" onclick="doSell()">卖出</button>
    <button class="btn btn-rule" onclick="doRule()">改规则</button>
    <button class="btn btn-edit" onclick="doEdit()">✎ 编辑</button>
    <button class="btn btn-del" onclick="doDel()">删除</button>
    <span class="divider"></span>
    <span class="tip">单击选中行 · 点表头排序 · ✎编辑改金额 · 删除需二次确认</span>
  </div>

  <div class="tablewrap">
    <table>
      <thead><tr>
        <th class="sortable" onclick="sortBy('code')">代码<span class="sarr" id="sarr-code"></span></th>
        <th class="sortable" onclick="sortBy('name')">基金名称<span class="sarr" id="sarr-name"></span></th>
        <th class="sortable" onclick="sortBy('pct')">估算涨幅<span class="sarr" id="sarr-pct"></span></th>
        <th class="sortable" onclick="sortBy('pred')">今日预测<span class="sarr" id="sarr-pred"></span></th>
        <th class="sortable" onclick="sortBy('value')">持有金额(元)<span class="sarr" id="sarr-value"></span></th>
        <th class="sortable" onclick="sortBy('ratio')">占比<span class="sarr" id="sarr-ratio"></span></th>
        <th class="sortable" onclick="sortBy('profit')">今日收益(元)<span class="sarr" id="sarr-profit"></span></th>
        <th class="sortable" onclick="sortBy('realized')">累计收益(元)<span class="sarr" id="sarr-realized"></span></th>
        <th class="sortable" onclick="sortBy('shares')">持有份额<span class="sarr" id="sarr-shares"></span></th>
        <th class="sortable" onclick="sortBy('gz')">估算净值<span class="sarr" id="sarr-gz"></span></th>
      </tr></thead>
      <tbody id="tbody"></tbody>
    </table>
    <div class="empty" id="empty" style="display:none">
      <div class="ic">🗒️</div>
      还没有添加基金，输入代码和金额点「录入持仓」开始
    </div>
  </div>

  <div class="pendingbox hidden" id="pendingbox">
    <div class="phead">待确认交易</div>
    <div id="pendinglist"></div>
  </div>
  </div><!-- /view-holdings -->

  <div id="view-analysis" style="display:none;flex:1;overflow:auto;">
    <div class="ana-tabs">
      <button class="ana-tab active" id="atab-today" onclick="switchAnaTab('today')">今日分析</button>
      <button class="ana-tab" id="atab-history" onclick="switchAnaTab('history')">历史预测</button>
      <button class="ana-tab" id="atab-review" onclick="switchAnaTab('review')">复盘</button>
      <button class="ana-tab" id="atab-signals" onclick="switchAnaTab('signals')">信号</button>
      <button class="ana-tab" id="atab-tradereview" onclick="switchAnaTab('tradereview')">加减仓复盘</button>
      <button class="ana-tab" id="atab-settings" onclick="openSettings()" style="margin-left:auto;">⚙ 设置</button>
    </div>

    <div id="ana-config-hint" style="display:none" class="ana-hint">
      <div class="hint-text">⚠ 未配置 LLM API key，请先点右上角「设置」填入。</div>
    </div>

    <div id="ana-pane-today">
      <div class="ana-bar">
        <label>选择基金：</label>
        <select id="ana_fund_select"></select>
        <button class="btn btn-add" onclick="doAnalyze()" style="background:linear-gradient(135deg,var(--brand),var(--purple))">🤖 开始分析</button>
      </div>
      <div id="ana-progress" style="display:none" class="ana-progress">
        <div class="bar-bg"><div id="ana-bar" class="bar-fg" style="width:0%"></div></div>
        <div id="ana-msg" class="msg">初始化...</div>
      </div>
      <div id="ana-report" class="ana-report"></div>
    </div>

    <div id="ana-pane-history" style="display:none;">
      <div id="history-list" class="history-list"></div>
    </div>

    <div id="ana-pane-review" style="display:none">
      <div class="ana-bar">
        <label>选择预测日期：</label>
        <select id="rev_date"></select>
        <button class="btn btn-add" onclick="doReviewAll()" style="background:linear-gradient(135deg,var(--down),var(--down))">📋 复盘当天全部</button>
      </div>
      <div id="rev-progress" style="display:none" class="ana-progress">
        <div class="bar-bg"><div id="rev-bar" class="bar-fg" style="width:0%"></div></div>
        <div id="rev-msg" class="msg">复盘中...</div>
      </div>
      <div id="rev-result" class="ana-report"></div>
    </div>

    <div id="ana-pane-signals" style="display:none">
      <div id="sig-progress" class="sig-progress" style="display:none">
        <div class="bar-bg"><div id="sig-progress-fill" class="bar-fg" style="width:0%"></div></div>
        <div id="sig-progress-msg" class="msg">初始化...</div>
      </div>
      <div id="signal-list" class="history-list"></div>
    </div>

    <div id="ana-pane-tradereview" style="display:none">
      <div class="ana-bar">
        <label>筛选日期：</label>
        <select id="tr-date-filter" onchange="refreshTradeReviews()">
          <option value="">全部日期</option>
        </select>
        <span id="tr-open-hint" style="font-size:12px;color:var(--sub)"></span>
      </div>
      <div id="tr-progress" class="sig-progress" style="display:none">
        <div class="bar-bg"><div id="tr-progress-fill" class="bar-fg" style="width:0%"></div></div>
        <div id="tr-progress-msg" class="msg">初始化...</div>
      </div>
      <div id="trade-review-list" class="history-list"></div>
    </div>
  </div>
</div><!-- /app -->

<div class="mask" id="mask">
  <div class="modal">
    <h3 id="m_title">买入</h3>
    <p class="note" id="m_desc"></p>
    <input id="m_amt" placeholder="金额（元）" onkeydown="if(event.key==='Enter')saveModal()">
    <input id="m_amt2" placeholder="累计收益（元）" style="display:none" onkeydown="if(event.key==='Enter')saveModal()">
    <input id="m_proxy" placeholder="代理地址（可选）" style="display:none" onkeydown="if(event.key==='Enter')saveModal()">
    <select id="m_model" style="display:none">
      <option value="deepseek-v4-pro">DeepSeek V4-Pro · 深度推理（推荐）</option>
      <option value="deepseek-v4-flash">DeepSeek V4-Flash · 更快更省</option>
    </select>
    <div class="row">
      <button class="cancel" onclick="closeModal()">取消</button>
      <button class="ok" onclick="saveModal()">确认</button>
    </div>
  </div>
</div>
<div class="toast" id="toast"></div>

<script>
let state=null, selCode=null, modalMode='add', modalCode=null;
let sorts=[]; // 多级排序：[{key, dir}]，dir=-1 降序(大在前)、1 升序；数组顺序即排序优先级

function cls(v){return v>0?'up':(v<0?'down':'flat')}
function dirCls(v){return v==='UP'?'up':(v==='DOWN'?'down':'flat')}

// 归一化 AI 返回的 verdict（可能是中文），映射到标准 HOLD/BUY/SELL
function normVerdict(v){
  v=String(v||'').trim();
  if(/持有|观望|不动|维持|HOLD/i.test(v)) return 'HOLD';
  if(/买入|加仓|增持|申购|建仓|BUY|ADD/i.test(v)) return 'BUY';
  if(/卖出|减仓|减持|赎回|清仓|SELL|REDUCE/i.test(v)) return 'SELL';
  return v;
}
function verdictClass(v){
  const n=normVerdict(v);
  if(n==='BUY') return 'up';
  if(n==='SELL') return 'down';
  return 'flat';
}

// 取某列用于排序的值
function sortVal(f,key){
  if(key==='pred'){
    const p=f.today_pred;
    if(!p) return -Infinity;
    const dm={UP:3,FLAT:2,DOWN:1};
    return (dm[p.direction]||0)*1000 + (parseFloat(p.expected_pct)||0);
  }
  if(key==='ratio') return f.value||-Infinity;
  if(key==='code'||key==='name') return f[key]||'';
  return (f[key]==null)?-Infinity:f[key];
}

function sortBy(key){
  const idx=sorts.findIndex(s=>s.key===key);
  if(idx<0){
    sorts.push({key:key,dir:-1});           // 新增：追加为次优先级，默认降序
  } else if(sorts[idx].dir===-1){
    sorts[idx].dir=1;                        // 降序 → 升序
  } else {
    sorts.splice(idx,1);                     // 升序 → 移除该列排序
  }
  updateSortHeader();
  render(state);
}

function updateSortHeader(){
  document.querySelectorAll('.sarr').forEach(a=>a.textContent='');
  document.querySelectorAll('thead th.sortable').forEach(th=>th.classList.remove('sorted'));
  sorts.forEach((s,i)=>{
    const el=document.getElementById('sarr-'+s.key);
    if(el){
      el.textContent=(i+1)+(s.dir<0?'▼':'▲');
      el.parentElement.classList.add('sorted');
    }
  });
}
function fmt(v,d=2){return v==null?'--':Number(v).toLocaleString('zh-CN',{minimumFractionDigits:d,maximumFractionDigits:d})}
function sgn(v,d=2){return (v>0?'+':'')+fmt(v,d)}

function render(st){
  state=st;
  const masked=!!st.mask;
  const mb=document.getElementById('maskBtn');
  if(mb) mb.textContent=masked?'👁 显示金额':'🙈 隐藏金额';
  // 汇总
  document.getElementById('total').innerHTML=(masked?'****':fmt(st.total))+'<small>元</small>';
  const p=document.getElementById('profit');
  p.textContent=(st.profit>0?'+':'')+fmt(st.profit);
  p.className='v '+cls(st.profit);
  const r=document.getElementById('rate');
  const holdings=st.total-(st.idle_cash||0);
  const rate=holdings>0?st.profit/(holdings-st.profit)*100:null;
  r.textContent=rate==null?'--':sgn(rate)+'%';
  r.className='v '+cls(rate);
  document.getElementById('count').textContent='持有 '+st.count+' 只基金';
  document.getElementById('updatetime').textContent='更新于 '+st.time;
  document.getElementById('estnote').textContent=(st.funds.length&&!st.funds.some(f=>f.est))
    ? '⚠ 盘中估值不可用，显示最新净值'
    : '每 '+st.interval+' 秒自动刷新';
  const rz=document.getElementById('realized');
  rz.textContent=(st.realized>0?'+':'')+fmt(st.realized);
  rz.className='v '+cls(st.realized);
  const ic=document.getElementById('idle-cash');
  ic.innerHTML=(masked?'****':(st.idle_cash!=null?fmt(st.idle_cash):'--'))+'<small>元</small>';

  // 今日总体预测（组合层面）
  const pf=document.getElementById('pf-forecast');
  if(st.portfolio_pred){
    pf.innerHTML='<span class="badge '+dirCls(st.portfolio_pred.direction)+'" style="font-size:14px;padding:3px 10px">'+esc(st.portfolio_pred.direction||'-')+'</span> '+esc(st.portfolio_pred.expected_pct||'-');
    pf.className='v '+dirCls(st.portfolio_pred.direction);
    document.getElementById('pf-detail').textContent='信心 '+esc(st.portfolio_pred.confidence||'-');
  }else{
    pf.textContent='--';
    pf.className='v';
    document.getElementById('pf-detail').textContent='暂无今日分析';
  }

  // 预测准确率（始终显示，带日期）
  const accAvg=document.getElementById('acc-avg');
  if(st.review_summary){
    const rs=st.review_summary;
    accAvg.textContent=rs.avg_accuracy+'%';
    accAvg.className='v '+(rs.avg_accuracy>=80?'up':(rs.avg_accuracy>=50?'flat':'down'));
    document.getElementById('acc-detail').textContent='复盘 '+rs.date+' · 方向对 '+rs.direction_correct+'/'+rs.total;
  }else{
    accAvg.textContent='--';
    accAvg.className='v';
    document.getElementById('acc-detail').textContent='暂无复盘数据';
  }

  // 表格（支持排序）
  const tb=document.getElementById('tbody');
  tb.innerHTML='';
  document.getElementById('empty').style.display=st.funds.length?'none':'flex';
  let funds=st.funds.slice();
  if(sorts.length){
    funds.sort((a,b)=>{
      for(const s of sorts){
        const av=sortVal(a,s.key);
        const bv=sortVal(b,s.key);
        if(av===bv) continue;
        return (av>bv?1:-1)*s.dir;
      }
      return 0;
    });
  }
  for(const f of funds){
    const tr=document.createElement('tr');
    if(f.code===selCode)tr.className='sel';
    const badge=f.est
      ? (f.est_src==='idx'?'<span class="badge est">指数近似</span>'
        : f.est_src==='holdings'?'<span class="badge est">重仓估算</span>'
        : f.est_src==='theme'?'<span class="badge est">主题近似</span>'
        : '<span class="badge est">盘中估值</span>')
      : '<span class="badge nav">最新净值</span>';
    const cd='<span class="badge rule">T+'+(f.confirm_days||1)+'</span>';
    const pb=f.pending_count?'<span class="badge pending">确认中 '+f.pending_count+'</span>':'';
    // 仓位占比 + 集中度警示（>30% 高亮）
    const ratio=holdings>0&&f.value?(f.value/holdings*100):0;
    const warnRatio=ratio>=30;
    const ratioCell='<div style="display:flex;align-items:center;gap:6px">'+
      '<div style="width:48px;height:6px;background:var(--track-bg);border-radius:3px;overflow:hidden;flex:none">'+
        '<div style="width:'+Math.min(ratio,100).toFixed(0)+'%;height:100%;background:'+(warnRatio?'var(--orange)':'var(--brand)')+';border-radius:3px"></div></div>'+
      '<span style="font-size:12px;color:'+(warnRatio?'var(--orange)':'var(--sub)')+'">'+(f.value?ratio.toFixed(1)+'%':'--')+'</span>'+
      (warnRatio?'<span class="badge warn">集中</span>':'')+
      '</div>';
    tr.innerHTML=
      '<td>'+f.code+'</td>'+
      '<td>'+esc(f.name)+badge+cd+pb+'</td>'+
      '<td class="pct '+cls(f.pct)+'">'+(f.pct==null?'--':sgn(f.pct)+'%')+(f.est?'':'<small class="stale">'+(f.qdate?f.qdate.slice(5):'昨收')+'</small>')+'</td>'+
      '<td>'+(f.today_pred?'<span class="badge '+dirCls(f.today_pred.direction)+'">'+esc(f.today_pred.direction||'')+'</span> '+esc(f.today_pred.expected_pct||''):'<span style="color:var(--sub)">--</span>')+'</td>'+
      '<td>'+(f.value?fmt(f.value):'--')+'</td>'+
      '<td>'+ratioCell+'</td>'+
      '<td class="pct '+cls(f.profit)+'">'+(f.profit==null?'--':(f.value?sgn(f.profit):'--'))+'</td>'+
      '<td class="pct '+cls(f.realized)+'">'+(f.realized?sgn(f.realized):'--')+'</td>'+
      '<td>'+(f.shares?fmt(f.shares,4):'--')+'</td>'+
      '<td>'+(f.gz?'<span style="color:var(--sub)">'+f.gz.toFixed(4)+'</span>':'--')+'</td>';
    tr.onclick=()=>{selCode=f.code;render(state)};
    tb.appendChild(tr);
  }

  // 收益率走势折线图
  renderRateChart(st.rate_points || [], rateChartExpanded);

  // 待确认
  const pbox=document.getElementById('pendingbox');
  const plist=document.getElementById('pendinglist');
  if(st.pending && st.pending.length){
    pbox.classList.remove('hidden');
    plist.innerHTML=st.pending.map(o=>
      '<div class="pendingitem">'+
        '<div><b>'+esc(o.code+' '+o.name)+'</b> <span class="meta">'+(o.type==='buy'?'买入':'赎回')+' '+
        (o.type==='buy'?fmt(o.amount)+' 元':fmt(o.shares,4)+' 份')+' · '+o.nav_date+' 净值 · '+o.confirm_date+' 到账</span></div>'+
        '<button onclick="cancelPending(\''+o.code+'\',\''+o.id+'\')">撤单</button>'+
      '</div>'
    ).join('');
  }else{
    pbox.classList.add('hidden');
    plist.innerHTML='';
  }
}
function esc(s){return String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}

let rateChartExpanded=false;
function toggleRateChart(){
  rateChartExpanded=!rateChartExpanded;
  const card=document.querySelector('.card.rate-card');
  if(card) card.classList.toggle('expanded', rateChartExpanded);
  renderRateChart((state&&state.rate_points)||[], rateChartExpanded);
}

function renderRateChart(points, expanded){
  const box=document.getElementById('rate-chart');
  if(!box) return;
  if(!points || points.length===0){
    box.innerHTML='<div style="color:var(--sub);font-size:12px;padding:40px 0;text-align:center">交易时间内自动采样<br>（9:30-15:00，每分钟一点）</div>';
    return;
  }
  const W=1000, H=170, padL=56, padR=22, padT=16, padB=30;
  const plotW=W-padL-padR, plotH=H-padT-padB;
  const START=9*60+30, END=15*60, SPAN=END-START;
  function xOf(t){const p=String(t).split(':');const m=(+p[0])*60+(+p[1])-START;return padL+m/SPAN*plotW;}
  const rates=points.map(p=>p.r);
  const hasBench=points.some(p=>p.b!=null);
  // 范围同时纳入沪深300，保证两条线同一坐标系
  let mn=Math.min(0,...rates), mx=Math.max(0,...rates);
  if(hasBench){
    const bv=points.map(p=>p.b).filter(v=>v!=null);
    if(bv.length){mn=Math.min(mn,...bv); mx=Math.max(mx,...bv);}
  }
  if(mx-mn<0.2){const c=(mx+mn)/2;mn=c-0.5;mx=c+0.5;}
  const pad=(mx-mn)*0.18; mn-=pad; mx+=pad;
  function yOf(r){return padT+(mx-r)/(mx-mn)*plotH;}
  const y0=yOf(0);
  const up=rates[rates.length-1]>=0;
  const color=up?'var(--up)':'var(--down)';
  const d=points.map((p,i)=>(i?'L':'M')+xOf(p.t).toFixed(1)+' '+yOf(p.r).toFixed(1)).join(' ');
  const dots=points.map(p=>'<circle cx="'+xOf(p.t).toFixed(1)+'" cy="'+yOf(p.r).toFixed(1)+'" r="2" fill="'+color+'"/>').join('');
  // 沪深300 对比线（蓝虚线）
  let benchPath='', benchLabel='';
  if(hasBench){
    const bp=points.filter(p=>p.b!=null);
    benchPath=bp.map((p,i)=>(i?'L':'M')+xOf(p.t).toFixed(1)+' '+yOf(p.b).toFixed(1)).join(' ');
    const lb=bp[bp.length-1];
    if(expanded && lb){
      benchLabel='<text x="'+xOf(lb.t).toFixed(1)+'" y="'+(yOf(lb.b)-8).toFixed(1)+'" text-anchor="middle" font-size="12" font-weight="600" fill="var(--brand)">沪深300 '+lb.b.toFixed(2)+'%</text>';
    }
  }
  const last=points[points.length-1];
  const lx=xOf(last.t), ly=yOf(last.r);
  // 展开时才显示刻度与数值
  let labels='';
  if(expanded){
    const xticks=['09:30','10:30','11:30','13:00','14:00','15:00'];
    let xt='';
    xticks.forEach(t=>{xt+='<text x="'+xOf(t).toFixed(1)+'" y="'+(H-9)+'" text-anchor="middle" font-size="11" fill="var(--sub)">'+t+'</text>';});
    const yt='<text x="'+(padL-8)+'" y="'+(y0+3)+'" text-anchor="end" font-size="11" fill="var(--sub)">0%</text>'+
      '<text x="'+(padL-8)+'" y="'+(padT+8)+'" text-anchor="end" font-size="11" fill="var(--sub)">'+mx.toFixed(2)+'%</text>'+
      '<text x="'+(padL-8)+'" y="'+(padT+plotH-2)+'" text-anchor="end" font-size="11" fill="var(--sub)">'+mn.toFixed(2)+'%</text>';
    const legend='<line x1="'+padL+'" y1="11" x2="'+(padL+16)+'" y2="11" stroke="'+color+'" stroke-width="2"/>'+
      '<text x="'+(padL+20)+'" y="14" font-size="11" fill="'+color+'">组合收益率</text>'+
      '<line x1="'+(padL+104)+'" y1="11" x2="'+(padL+120)+'" y2="11" stroke="var(--brand)" stroke-width="1.5" stroke-dasharray="4 3"/>'+
      '<text x="'+(padL+124)+'" y="14" font-size="11" fill="var(--brand)">沪深300</text>';
    labels=legend+xt+yt+
      '<text x="'+lx.toFixed(1)+'" y="'+(ly-10).toFixed(1)+'" text-anchor="middle" font-size="12" font-weight="600" fill="'+color+'">'+last.r.toFixed(2)+'%</text>';
  }
  box.innerHTML='<svg viewBox="0 0 '+W+' '+H+'" preserveAspectRatio="none">'+
    '<line x1="'+padL+'" y1="'+y0.toFixed(1)+'" x2="'+(W-padR)+'" y2="'+y0.toFixed(1)+'" stroke="var(--scrollbar)" stroke-width="1.5" stroke-dasharray="6 4"/>'+
    (benchPath?'<path d="'+benchPath+'" fill="none" stroke="var(--brand)" stroke-width="1.5" stroke-dasharray="6 5" stroke-linejoin="round" stroke-linecap="round"/>':'')+
    '<path d="'+d+'" fill="none" stroke="'+color+'" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>'+
    dots+
    '<circle cx="'+lx.toFixed(1)+'" cy="'+ly.toFixed(1)+'" r="4.5" fill="'+color+'" stroke="var(--bg)" stroke-width="1.5"/>'+
    benchLabel+
    labels+
    '</svg>';
}

function toast(msg,err){
  const t=document.getElementById('toast');
  t.textContent=msg;t.className='toast show'+(err?' err':'');
  setTimeout(()=>t.className='toast'+(err?' err':''),2200);
}

function codeValue(){return document.getElementById('in_code').value.trim();}
function amtValue(){return document.getElementById('in_amt').value.trim();}

async function doAdd(){
  const code=codeValue();
  const amt=amtValue();
  if(!/^\d{6}$/.test(code))return toast('请输入 6 位基金代码',true);
  const days=document.getElementById('in_days').value;
  const r=await pywebview.api.add_fund(code,amt,days);
  if(r.ok){toast(r.msg);document.getElementById('in_code').value='';document.getElementById('in_amt').value='';}
  else toast(r.msg,true);
}

function doRule(){if(!selCode)return toast('先单击选中一行',true);openModal('rule')}
function doEdit(){if(!selCode)return toast('先单击选中一行',true);openModal('edit')}

async function showFloating(){
  await pywebview.api.show_floating();
}

function doBuy(){openModal('buy')}
function doSell(){openModal('sell')}

function openModal(mode){
  modalMode=mode; modalCode=selCode || codeValue();
  if(!/^\d{6}$/.test(modalCode)){
    const c=codeValue(); if(/^\d{6}$/.test(c)) modalCode=c; else return toast('请输入 6 位基金代码',true);
  }
  const f=(state && state.funds.find(x=>x.code===modalCode))||{};
  const inp=document.getElementById('m_amt');
  const inp2=document.getElementById('m_amt2');
  inp2.style.display='none';
  document.getElementById('m_proxy').style.display='none';
  document.getElementById('m_model').style.display='none';
  inp.style.display='block';
  inp.dataset.mode='';
  if(mode==='delete'){
    document.getElementById('m_title').textContent='删除持仓';
    document.getElementById('m_desc').textContent=modalCode+' '+(f.name||'')+' · 删除后无法恢复，确认删除？';
    inp.style.display='none';
  }else if(mode==='rule'){
    document.getElementById('m_title').textContent='修改确认规则';
    document.getElementById('m_desc').textContent=modalCode+' '+(f.name||'')+' · 输入 0=T+0当天、1=T+1、2=T+2';
    inp.value=(f.confirm_days!=null?f.confirm_days:1); inp.placeholder='确认天数（0/1/2）';
  }else if(mode==='edit'){
    document.getElementById('m_title').textContent='编辑持仓';
    document.getElementById('m_desc').textContent=modalCode+' '+(f.name||'')+' · 直接调整持有金额与累计收益';
    inp.value=(f.value!=null?f.value:''); inp.placeholder='持有金额（元）';
    inp2.value=(f.realized!=null?f.realized:0); inp2.style.display='block'; inp2.placeholder='累计收益（元，可负）';
    inp.dataset.mode='';
  }else{
    document.getElementById('m_title').textContent=mode==='buy'?'买入加仓':(mode==='sell'?'赎回减仓':'录入持仓');
    document.getElementById('m_desc').textContent=modalCode+' '+(f.name||'')+(mode==='sell'?(f.shares?'（当前 '+fmt(f.shares,4)+' 份，输入「全部」可清仓）':''):'');
    inp.value=''; inp.placeholder=mode==='sell'?'卖出金额（元）或输入「全部」':'金额（元）';
    inp.dataset.mode='';
  }
  document.getElementById('mask').classList.add('show');
  setTimeout(()=>inp.focus(),50);
}

async function saveModal(){
  const inp = document.getElementById('m_amt');
  // 设置模式：保存 LLM API key + 代理
  if(inp && inp.dataset && inp.dataset.mode==='settings'){
    const key = inp.value.trim();
    if(!key){ toast('API key 不能为空', true); return; }
    const model = document.getElementById('m_model').value;
    const proxy = (document.getElementById('m_proxy').value||'').trim();
    const r = await pywebview.api.save_analysis_config(key, model);
    const r2 = await pywebview.api.save_proxy_config(proxy);
    closeModal();
    toast((r.ok&&r2.ok)?'设置已保存':'保存失败', (r.ok&&r2.ok)?false:true);
    if(r.ok) checkAnaConfig();
    return;
  }
  // 闲钱模式
  if(inp && inp.dataset && inp.dataset.mode==='idle'){
    const v = inp.value.trim();
    if(v==='' || isNaN(parseFloat(v))){ toast('请输入有效金额', true); return; }
    const r = await pywebview.api.set_idle_cash(v);
    closeModal();
    toast(r.ok?r.msg:'保存失败', r.ok?false:true);
    return;
  }
  if(modalMode==='clear-signals'){
    const r=await pywebview.api.clear_signals();
    closeModal();
    toast(r.ok?'已清空全部信号':'清空失败', r.ok?false:true);
    if(r.ok) refreshSignals();
    return;
  }
  // 原有买入/卖出/改规则/编辑
  const v = inp.value.trim();
  if(!modalCode)return;
  if(modalMode==='delete'){
    const r=await pywebview.api.del_fund(modalCode);
    closeModal();
    if(r.ok){selCode=null;toast('已删除');}
    else toast(r.msg||'删除失败',true);
    return;
  }
  let r;
  if(modalMode==='buy') r=await pywebview.api.buy_fund(modalCode,v);
  else if(modalMode==='sell') r=await pywebview.api.sell_fund(modalCode,v);
  else if(modalMode==='rule') r=await pywebview.api.set_confirm_days(modalCode,v);
  else if(modalMode==='edit') r=await pywebview.api.edit_fund(modalCode, v, document.getElementById('m_amt2').value.trim());
  if(r.ok){closeModal();toast(r.msg);}
  else toast(r.msg,true);
}

async function cancelPending(code,id){
  const r=await pywebview.api.cancel_pending(code,id);
  toast(r.msg,r.ok?false:true);
}

function doDel(){
  if(!selCode)return toast('先单击选中一行',true);
  openModal('delete');
}

function closeModal(){document.getElementById('mask').classList.remove('show')}
document.getElementById('mask').addEventListener('click',e=>{if(e.target.id==='mask')closeModal()});

async function doRefresh(){toast('正在刷新…');await pywebview.api.manual_refresh()}

let cardsCollapsed=false;
function toggleCards(){
  cardsCollapsed=!cardsCollapsed;
  document.body.classList.toggle('cards-collapsed', cardsCollapsed);
  const b=document.getElementById('collapseCards');
  if(b){b.textContent=cardsCollapsed?'展开卡片':'收起卡片';b.title=cardsCollapsed?'展开汇总卡片':'收起汇总卡片';}
}

async function toggleMask(){
  await pywebview.api.toggle_mask_amount();
  // 状态通过 push → render(st) 自动更新两个窗口，无需手动改
}

let darkMode=false;
function applyTheme(dark){
  darkMode=dark;
  document.body.classList.toggle('dark', dark);
  const b=document.getElementById('themeBtn');
  if(b){b.textContent=dark?'☀️':'🌙';b.title=dark?'切换到浅色主题':'切换到深色主题';}
}
function toggleTheme(){
  applyTheme(!darkMode);
  try{localStorage.setItem('fund_theme', darkMode?'dark':'light');}catch(e){}
}
(function(){
  let t='light';
  try{t=localStorage.getItem('fund_theme')||'light';}catch(e){}
  if(t==='dark') applyTheme(true);
})();

// ---------- 分析模式 ----------
let currentView='holdings', currentAnaTab='today';

function switchView(v){
  currentView=v;
  document.getElementById('view-holdings').style.display=v==='holdings'?'flex':'none';
  document.getElementById('view-analysis').style.display=v==='analysis'?'flex':'none';
  document.getElementById('tab-holdings').classList.toggle('active', v==='holdings');
  document.getElementById('tab-analysis').classList.toggle('active', v==='analysis');
  if(v==='analysis'){ refreshAnaSelects(); checkAnaConfig(); refreshHistoryList(); }
}

function switchAnaTab(t){
  currentAnaTab=t;
  document.getElementById('atab-today').classList.toggle('active', t==='today');
  document.getElementById('atab-history').classList.toggle('active', t==='history');
  document.getElementById('atab-review').classList.toggle('active', t==='review');
  document.getElementById('atab-signals').classList.toggle('active', t==='signals');
  document.getElementById('atab-tradereview').classList.toggle('active', t==='tradereview');
  document.getElementById('ana-pane-today').style.display=t==='today'?'block':'none';
  document.getElementById('ana-pane-history').style.display=t==='history'?'block':'none';
  document.getElementById('ana-pane-review').style.display=t==='review'?'block':'none';
  document.getElementById('ana-pane-signals').style.display=t==='signals'?'block':'none';
  document.getElementById('ana-pane-tradereview').style.display=t==='tradereview'?'block':'none';
  if(t==='review'){ refreshReviewDates(); }
  if(t==='history'){ refreshHistoryList(); }
  if(t==='signals'){ refreshSignals(); autoAuditSignals(); }
  if(t==='tradereview'){ refreshTradeReviews(); autoReviewTrades(); }
}

async function refreshSignals(){
  const box=document.getElementById('signal-list');
  if(!box) return;
  const r=await pywebview.api.get_signals();
  if(!r.ok){ box.innerHTML='<div class="empty-state">'+esc(r.msg||'加载失败')+'</div>'; return; }
  const st=r.stats||{};
  const hr=st.hit_rate;
  let html='<div class="sig-stats">'+
    '<div class="sig-stat"><div class="n">'+(st.total||0)+'</div><div class="l">总信号</div></div>'+
    '<div class="sig-stat"><div class="n">'+(st.active||0)+'</div><div class="l">进行中</div></div>'+
    '<div class="sig-stat"><div class="n">'+(st.closed||0)+'</div><div class="l">已了结</div></div>'+
    '<div class="sig-stat"><div class="n '+(hr==null?'':(hr>=50?'up':'down'))+'">'+(hr!=null?hr+'%':'--')+'</div><div class="l">胜率</div></div>'+
    '</div>';
  const sigs=r.signals||[];
  const filterDefs=[
    ['all','全部'],['ai','有审核记录'],['维持原判','维持'],['兑现','兑现'],
    ['证伪','证伪'],['强化','强化'],['弱化','弱化'],['信息不足','信息不足']
  ];
  html+='<div class="sig-filter">'+
    filterDefs.map(f=>'<button class="fbtn '+(sigFilter===f[0]?'active':'')+'" onclick="setSigFilter(\''+f[0]+'\')">'+f[1]+'</button>').join('')+
    '</div>';
  function matchSig(s,f){
    if(f==='all') return true;
    if(f==='ai') return !!s.audit_reason;
    const st=s.last_audit || (s.status==='active'?'':s.status);
    return st===f;
  }
  let list=sigs.filter(s=>matchSig(s,sigFilter));
  if(!list.length){
    const emptyTxt=(sigFilter==='ai')?'暂无审核记录。每次打开信号页会自动审核进行中的信号。'
      :(sigFilter==='all')?'暂无信号。每次分析后，AI 会提取可追踪的投资信号到这里，供持续验证。'
      :'暂无「'+sigFilter+'」状态的信号。';
    box.innerHTML=html+'<div class="empty-state">'+emptyTxt+'</div>';
    return;
  }
  html+='<div class="sig-clear-row">'+
    '<button class="sig-clear" onclick="clearSignals()">🗑 清空全部信号（'+sigs.length+'）</button>'+
    '</div>';
  for(const s of list){
    const bull=s.direction==='看多', bear=s.direction==='看空';
    const dirBadge=bull?'<span class="badge up">看多</span>':(bear?'<span class="badge down">看空</span>':'<span class="badge flat">观望</span>');
    let stCls='active', stTxt=s.status||'进行中';
    if(s.status==='强化')stCls='strong';
    else if(s.status==='弱化')stCls='weak';
    else if(s.status==='兑现')stCls='hit';
    else if(s.status==='证伪')stCls='miss';
    const stBadge='<span class="sig-status '+stCls+'">'+esc(stTxt)+'</span>';
    let act='';
    if(s.status==='active'||s.status==='强化'||s.status==='弱化'){
      act='<button onclick="sigAction(\''+s.id+'\',\'兑现\')">✓ 兑现</button>'+
          '<button onclick="sigAction(\''+s.id+'\',\'证伪\')">✗ 证伪</button>'+
          '<button onclick="sigAction(\''+s.id+'\',\'强化\')">▲ 强化</button>'+
          '<button onclick="sigAction(\''+s.id+'\',\'弱化\')">▼ 弱化</button>';
    }else if(s.outcome==='correct'||s.outcome==='wrong'){
      act='<span class="badge '+(s.outcome==='correct'?'up':'down')+'">'+(s.outcome==='correct'?'判断正确':'判断错误')+'</span>';
    }else{
      act='<span class="badge flat" title="结果待定，可点「AI 审核信号」自动判定">结果待定</span>';
    }
    html+='<div class="sig-card '+(bull?'bull':(bear?'bear':''))+'" data-id="'+esc(s.id)+'">'+
      '<div class="sig-head">'+dirBadge+'<span class="sig-title">'+esc(s.title)+'</span>'+stBadge+
        '<button class="sig-del" onclick="delSignal(\''+s.id+'\')" title="删除该信号">✕</button></div>'+
      '<div class="sig-meta">'+esc(s.fund_name||s.code||'')+' · 目标 '+esc(s.target||'-')+' · 期限 '+esc(s.horizon||'-')+' · 创建 '+esc(s.created||'')+'</div>'+
      '<div class="sig-basis">'+esc(s.basis||'-')+'</div>'+
      (s.audit_reason?'<div class="sig-audit-reason">🤖 '+esc(s.audit_reason)+'</div>':'')+
      '<div class="sig-actions">'+act+'</div>'+
      '</div>';
  }
  box.innerHTML=html;
}

async function sigAction(id,status){
  const r=await pywebview.api.update_signal(id,status);
  toast(r.ok?'已更新':'更新失败', r.ok?false:true);
  refreshSignals();
}
async function delSignal(id){
  const r=await pywebview.api.del_signal(id);
  toast(r.ok?'已删除':'删除失败', r.ok?false:true);
  refreshSignals();
}
function clearSignals(){
  document.getElementById('m_title').textContent='清空全部信号';
  document.getElementById('m_desc').textContent='将删除全部信号，此操作不可恢复，确认清空？';
  document.getElementById('m_amt').style.display='none';
  document.getElementById('m_amt').dataset.mode='';
  document.getElementById('m_amt2').style.display='none';
  document.getElementById('m_model').style.display='none';
  modalMode='clear-signals';
  document.getElementById('mask').classList.add('show');
}

let sigAuditing=false;
async function auditSignals(){
  if(sigAuditing) return;
  sigAuditing=true;
  try{
    await doAuditSignals();
  }finally{
    sigAuditing=false;
  }
}

async function doAuditSignals(){
  const r=await pywebview.api.audit_signals();
  if(!r.ok){toast(r.msg||'审核失败',true);return;}
  const taskId=r.task_id;
  const prog=document.getElementById('sig-progress');
  if(prog) prog.style.display='block';
  while(true){
    await new Promise(res=>setTimeout(res,600));
    const st=await pywebview.api.get_task_status(taskId);
    if(st.status==='done'){
      if(prog) prog.style.display='none';
      const res=st.result||{};
      const cnt={};
      const changed=[];
      (res.results||[]).forEach(x=>{cnt[x.status]=(cnt[x.status]||0)+1;if(x.changed)changed.push(x.id);});
      const sum=Object.keys(cnt).map(k=>k+' '+cnt[k]).join(' · ');
      toast('审核完成：'+sum);
      refreshSignals();
      if(changed.length) setTimeout(()=>flashChangedSignals(changed), 300);
      return;
    }else if(st.status==='error'){
      if(prog) prog.style.display='none';
      toast(st.msg||'审核失败',true);
      return;
    }else{
      if(prog){
        const f=document.getElementById('sig-progress-fill');
        const m=document.getElementById('sig-progress-msg');
        if(f) f.style.width=(st.progress||0)+'%';
        if(m) m.textContent=st.msg||'审核中...';
      }
    }
  }
}

let lastAutoAudit=0;
async function autoAuditSignals(){
  // 打开信号页自动审核进行中的信号（无需按钮），60 秒内不重复触发
  const now=Date.now();
  if(now-lastAutoAudit<60000) return;
  const r=await pywebview.api.get_signals();
  if(!r.ok) return;
  const actives=(r.signals||[]).filter(s=>['active','强化','弱化'].includes(s.status));
  if(actives.length){ lastAutoAudit=now; auditSignals(); }
}

function flashChangedSignals(ids){
  const set=new Set(ids||[]);
  document.querySelectorAll('.sig-card').forEach(card=>{
    if(set.has(card.dataset.id)) card.classList.add('flash');
  });
}

// ===== 加减仓复盘 =====
async function refreshTradeReviews(){
  const box=document.getElementById('trade-review-list');
  if(!box) return;
  const r=await pywebview.api.get_trade_reviews();
  if(!r.ok){ box.innerHTML='<div class="empty-state">'+esc(r.msg||'加载失败')+'</div>'; return; }
  const st=r.stats||{};
  const pr=st.profit_rate;
  let html='<div class="sig-stats">'+
    '<div class="sig-stat"><div class="n">'+(st.total||0)+'</div><div class="l">加减仓建议</div></div>'+
    '<div class="sig-stat"><div class="n">'+(st.reviewed||0)+'</div><div class="l">已复盘</div></div>'+
    '<div class="sig-stat"><div class="n '+(pr==null?'':(pr>=50?'up':'down'))+'">'+(pr!=null?pr+'%':'--')+'</div><div class="l">盈利占比</div></div>'+
    '<div class="sig-stat"><div class="n '+(st.avg_pnl==null?'':(st.avg_pnl>=0?'up':'down'))+'">'+(st.avg_pnl!=null?(st.avg_pnl>0?'+':'')+st.avg_pnl+'%':'--')+'</div><div class="l">平均收益</div></div>'+
    '</div>';
  // 经验教训区块（复盘完成后自动提炼，喂回下次分析形成闭环）
  const lessons=(r.lessons&&r.lessons.lessons)||[];
  if(lessons.length){
    html+='<div style="background:var(--panel2);border:1px solid var(--line);border-radius:12px;padding:10px 12px;margin-bottom:10px">'+
      '<div style="font-size:12px;color:var(--sub);margin-bottom:6px">📚 复盘经验教训（自动喂入下次分析）'+
      (r.lessons.updated?' · '+esc(r.lessons.updated):'')+'</div>'+
      lessons.map(x=>'<div style="font-size:12.5px;line-height:1.6;margin-bottom:3px">• '+esc(x)+'</div>').join('')+
      '</div>';
  }
  // 日期筛选下拉：全部 + 去重日期倒序
  const sel=document.getElementById('tr-date-filter');
  if(sel){
    const cur=sel.value;
    const dates=[...new Set((r.reviews||[]).map(x=>x.date).filter(Boolean))].sort().reverse();
    sel.innerHTML='<option value="">全部日期</option>'+
      dates.map(d=>'<option value="'+d+'">'+d+'</option>').join('');
    if(dates.includes(cur)) sel.value=cur;
  }
  // 复盘状态提示：休市 / 未收盘 / 正常
  const hint=document.getElementById('tr-open-hint');
  if(hint){
    if(!r.market_open) hint.textContent='· 今天休市，下一开市日收盘后自动复盘';
    else if(!r.market_closed) hint.textContent='· 今日净值未更新完，23:00 后自动复盘';
    else hint.textContent='';
  }
  const selDate=sel?sel.value:'';
  const revs=(r.reviews||[]).filter(x=>!selDate||x.date===selDate);
  if(!revs.length){
    box.innerHTML=html+'<div class="empty-state">'+((r.reviews||[]).length?'该日期暂无加减仓建议':'暂无加减仓建议。每次分析后，AI 的非持有（加仓/减仓/买入/卖出）建议会自动收集到这里复盘。')+'</div>';
    return;
  }
  const list=[...revs].reverse();
  for(const rv of list){
    const vCls=(rv.verdict==='BUY'||rv.verdict==='ADD')?'up':(rv.verdict==='SELL'||rv.verdict==='REDUCE'?'down':'flat');
    const vTxt={'BUY':'买入','SELL':'卖出','ADD':'加仓','REDUCE':'减仓'}[rv.verdict]||rv.verdict;
    let reviewHtml='';
    if(rv.status==='reviewed' && rv.review){
      const res=rv.review.result;
      const resCls=res==='盈利'?'up':(res==='亏损'?'down':'flat');
      const pnl=rv.review.pnl_pct;
      const bt=rv.review.bias_type;
      reviewHtml='<div class="sig-audit-reason">'+
        '📊 复盘：<b class="'+resCls+'">'+esc(res)+'</b>'+
        (pnl!=null?' <b class="'+resCls+'">'+(pnl>0?'+':'')+pnl+'%</b>':'')+
        (bt?' <span class="badge flat" style="font-size:10px">'+esc(bt)+'</span>':'')+
        '<div style="margin-top:3px">'+esc(rv.review.reason||'')+'</div></div>';
    }else{
      reviewHtml='<div style="font-size:12px;color:var(--sub);margin-top:4px">⏳ 待复盘（下一开市日自动 AI 复盘）</div>';
    }
    html+='<div class="sig-card" style="border-left:3px solid var(--'+(vCls==='up'?'up':(vCls==='down'?'down':'sub'))+')">'+
      '<div class="sig-head"><span class="badge '+vCls+'">'+vTxt+'</span><span class="sig-title">'+esc(rv.name||rv.code)+'</span>'+
      '<span style="font-size:11px;color:var(--sub)">'+esc(rv.date||'')+'</span></div>'+
      '<div class="sig-meta">'+esc(rv.position_change||'-')+' · 建议时净值 '+esc(rv.ref_nav!=null?rv.ref_nav:'-')+'</div>'+
      (rv.rationale?'<div class="sig-basis">理由：'+esc(rv.rationale)+'</div>':'')+
      reviewHtml+
      '</div>';
  }
  box.innerHTML=html;
}

let trReviewing=false;
async function reviewTradeReviews(){
  if(trReviewing) return;
  trReviewing=true;
  try{
    const r=await pywebview.api.review_trade_reviews();
    if(!r.ok){ toast(r.msg||'复盘失败',true); return; }
    const taskId=r.task_id;
    const prog=document.getElementById('tr-progress');
    if(prog) prog.style.display='block';
    while(true){
      await new Promise(res=>setTimeout(res,600));
      const st=await pywebview.api.get_task_status(taskId);
      if(st.status==='done'){
        if(prog) prog.style.display='none';
        const res=st.result||{};
        const cnt={};
        (res.results||[]).forEach(x=>{cnt[x.result]=(cnt[x.result]||0)+1;});
        const sum=Object.keys(cnt).map(k=>k+' '+cnt[k]).join(' · ');
        toast('加减仓复盘完成：'+sum);
        refreshTradeReviews();
        return;
      }else if(st.status==='error'){
        if(prog) prog.style.display='none';
        toast(st.msg||'复盘失败',true);
        return;
      }else{
        if(prog){
          const f=document.getElementById('tr-progress-fill');
          const m=document.getElementById('tr-progress-msg');
          if(f) f.style.width=(st.progress||0)+'%';
          if(m) m.textContent=st.msg||'复盘中...';
        }
      }
    }
  }finally{
    trReviewing=false;
  }
}

let lastAutoReview=0;
async function autoReviewTrades(){
  const now=Date.now();
  if(now-lastAutoReview<60000) return;
  const r=await pywebview.api.get_trade_reviews();
  if(!r.ok) return;
  // 只在开市日收盘后自动复盘（后端也会校验，这里提前跳过不打扰）
  if(!r.market_open || !r.market_closed) return;
  const today=r.today||'';
  const pendings=(r.reviews||[]).filter(x=>x.status==='pending' && x.date<today);
  if(pendings.length){ lastAutoReview=now; reviewTradeReviews(); }
}

let sigFilter='all';
function setSigFilter(f){
  sigFilter=f;
  refreshSignals();
}

async function checkAnaConfig(){
  const r = await pywebview.api.get_analysis_config();
  const hint = document.getElementById('ana-config-hint');
  hint.style.display = r.configured ? 'none' : 'block';
}

function refreshAnaSelects(){
  const sel = document.getElementById('ana_fund_select');
  if(!sel) return;
  const prev = sel.value;
  const funds = state&&state.funds||[];
  sel.innerHTML = '<option value="__all__">【全部持仓】组合 + 逐只分析（'+(funds.length)+' 只）</option>' +
    funds.map(f=>'<option value="'+f.code+'">'+esc(f.code)+' '+esc(f.name)+'</option>').join('');
  if(prev && sel.querySelector('option[value="'+prev+'"]')) sel.value = prev;
}

function openSettings(){
  pywebview.api.get_analysis_config().then(r=>{
    const cfg = r.config || {};
    document.getElementById('m_title').textContent='分析设置（LLM API + 代理）';
    document.getElementById('m_desc').textContent='填入 API key 并选择模型；行情/接口访问异常时，可在此填代理地址（如 10.110.32.68:7897）。';
    const inp = document.getElementById('m_amt');
    document.getElementById('m_amt2').style.display='none';
    const prx = document.getElementById('m_proxy');
    prx.style.display='block';
    prx.value='';
    pywebview.api.get_proxy_config().then(r2=>{
      if(r2 && r2.ok && r2.proxy) prx.value = r2.proxy;
    });
    prx.placeholder = '代理地址（可选，如 10.110.32.68:7897，留空清除）';
    const sel = document.getElementById('m_model');
    sel.style.display='block';
    sel.value = (cfg.model==='deepseek-v4-flash') ? 'deepseek-v4-flash' : 'deepseek-v4-pro';
    inp.value = cfg.api_key || '';
    inp.placeholder = 'API Key';
    inp.type = 'text';
    inp.dataset.mode='settings';
    document.getElementById('mask').classList.add('show');
    setTimeout(()=>inp.focus(),50);
  });
}

function editIdleCash(){
  document.getElementById('m_title').textContent='设置闲钱';
  document.getElementById('m_desc').textContent='你的闲置资金（可用于加减仓）。分析时会据此给出闲钱使用建议。';
  const inp = document.getElementById('m_amt');
  document.getElementById('m_amt2').style.display='none';
  document.getElementById('m_proxy').style.display='none';
  document.getElementById('m_model').style.display='none';
  inp.value = (state && state.idle_cash) ? state.idle_cash : '';
  inp.placeholder = '闲钱金额（元）';
  inp.type = 'text';
  inp.dataset.mode='idle';
  document.getElementById('mask').classList.add('show');
  setTimeout(()=>inp.focus(),50);
}

async function doAnalyze(){
  const sel = document.getElementById('ana_fund_select');
  if(!sel || !sel.value) return toast('请选择基金', true);
  const code = sel.value;
  const prog = document.getElementById('ana-progress');
  const rep = document.getElementById('ana-report');
  prog.style.display='block';
  rep.innerHTML='';
  document.getElementById('ana-bar').style.width='0%';
  // 耗时提示（按模型）
  (async()=>{
    try{
      const cfg=await pywebview.api.get_analysis_config();
      const model=(cfg.config||{}).model||'';
      const eta=model.indexOf('flash')>=0?'约 1-3 分钟':'约 5-8 分钟';
      document.getElementById('ana-msg').textContent='初始化...（'+eta+'）';
    }catch(e){
      document.getElementById('ana-msg').textContent='初始化...';
    }
  })();

  const r = (code==='__all__')
    ? await pywebview.api.analyze_all()
    : await pywebview.api.analyze_code(code);
  if(!r.ok){ toast(r.msg, true); prog.style.display='none'; return; }
  const taskId = r.task_id;
  while(true){
    await new Promise(res=>setTimeout(res, 600));
    const st = await pywebview.api.get_task_status(taskId);
    if(st.status==='done'){
      prog.style.display='none';
      let sigInfo=null;
      if(code!=='__all__'){
        try{
          const sr=await pywebview.api.get_signals();
          sigInfo=(sr.signals||[]).filter(x=>x.code===code);
        }catch(e){}
      }
      if(st.result && st.result.type==='full') renderFullReport(st.result, rep);
      else renderReport(st.result, rep, sigInfo);
      refreshHistoryList();
      toast('分析完成');
      return;
    } else if(st.status==='error'){
      prog.style.display='none';
      toast(st.msg || '分析失败', true);
      rep.innerHTML = '<div class="empty-state">分析失败：' + esc(st.msg||'') + '</div>';
      return;
    } else {
      document.getElementById('ana-bar').style.width = (st.progress||0) + '%';
      document.getElementById('ana-msg').textContent = st.msg || '进行中...';
    }
  }
}

function rolesBrief(r){
  const roles = r.roles || {};
  const keys = Object.keys(roles);
  if(!keys.length) return '';
  const icon = {技术分析师:'🔵',基本面分析师:'🟢',新闻分析师:'🟣',情绪分析师:'🟠',多头研究员:'🐂',空头研究员:'🐻',研究主管:'⚖️',交易员:'💱',风控主管:'🛡️'};
  return '<div class="role-grid">' +
    keys.map(k=>'<div class="role-card"><div class="rn">'+(icon[k]||'·')+' '+esc(k)+'</div><div class="rv">'+esc(roles[k]||'-')+'</div></div>').join('') +
    '</div>';
}

function renderFullReport(result, container){
  const cls = v => v==='UP'||v==='BUY'||v==='ADD' ? 'up' : (v==='DOWN'||v==='SELL'||v==='REDUCE' ? 'down' : 'flat');
  const dirCls = v => v==='UP' ? 'up' : (v==='DOWN' ? 'down' : 'flat');
  let html = '<div class="metrics">✅ 组合 + 逐只分析完成 · ' + result.ok_count + '/' + result.total + ' 只 · ' + esc(result.analyzed_at||'') + '</div>';

  // ===== 信号联动（组合层面：AI 历史判断对本次组合分析的影响） =====
  const ss = result.signal_summary || {};
  if(ss.total>0){
    const hrCls = (ss.hit_rate!=null && ss.hit_rate>=50) ? 'up' : 'down';
    html += `
    <div class="sec" style="border-left:3px solid var(--brand)"><div class="k">📡 信号联动（本次组合分析已参考历史信号）</div>
      <div style="font-size:12px;color:var(--sub);margin-top:5px">历史信号 <b>${ss.total}</b> 条 · 进行中 ${ss.active} · 已了结 ${ss.closed} · 胜率 <b class="${hrCls}">${(ss.hit_rate!=null?ss.hit_rate+'%':'--')}</b></div>
      <div style="font-size:12px;color:var(--sub);margin-top:3px">每只基金的历史信号对错（兑现/证伪/强化/弱化）已计入本次组合判断与整体可信度</div>
    </div>`;
  }

  // ===== 组合分析 =====
  if(result.portfolio && result.portfolio.ok){
    const r = result.portfolio.report || {};
    const st = r.structure || {};
    const pf = r.portfolio_forecast || {};
    const ic = r.idle_cash_advice || {};
    const nd = r.new_direction_advice || {};
    const icHtml = (ic && ic.suggestion) ? `
    <div class="sec" style="border-left:3px solid var(--orange)"><div class="k">💰 闲钱使用建议（结合单基金加减）</div>
      <div class="v" style="font-weight:400">${esc(ic.suggestion||'')}</div>
      <div style="font-size:12px;color:var(--sub);margin-top:5px">当前投入 ${(ic.deploy_now!=null?ic.deploy_now:'-')} 元 · 待机 ${(ic.deploy_later!=null?ic.deploy_later:'-')} 元 → ${esc(ic.deploy_target||'-')}</div>
      <div style="font-size:12px;color:var(--sub)">📈 收益提升至 ${esc(ic.return_boost||'-')} 倍 · 可信度：${esc(ic.confidence||'-')}</div>
      <div style="font-size:12px;color:var(--sub)">理由：${esc(ic.reason||'-')}</div>
    </div>` : '';
    const ndHtml = (nd && nd.suggestion) ? `
    <div class="sec" style="border-left:3px solid var(--purple)"><div class="k">🧭 新方向建议</div>
      <div class="v" style="font-weight:400">${esc(nd.suggestion||'')}</div>
      <div style="font-size:12px;color:var(--sub);margin-top:5px">方向：${esc(nd.target||'-')}</div>
      <div style="font-size:12px;color:var(--sub)">📈 收益提升至 ${esc(nd.return_boost||'-')} 倍 · 可信度：${esc(nd.confidence||'-')}</div>
      <div style="font-size:12px;color:var(--sub)">理由：${esc(nd.reason||'-')}</div>
    </div>` : '';
    // 量化配置建议（风险平价简化版）
    const alloc = result.allocation || {};
    const allocRows = (alloc.rows||[]).map(a=>{
      const c2 = a.advice==='加仓'?'up':(a.advice==='减仓'?'down':'flat');
      return '<div class="role-card"><div class="rn">'+esc(a.code+' '+a.name)+' <span class="badge '+c2+'">'+esc(a.advice)+' '+(a.diff>0?'+':'')+a.diff+'%</span></div><div class="rv">当前 '+a.cur_pct+'% → 理想 '+a.ideal_pct+'%</div></div>';
    }).join('');
    const allocHtml = (alloc.rows && alloc.rows.length) ? `
    <div class="sec" style="border-left:3px solid var(--down)"><div class="k">⚖️ 量化配置建议（风险平价）</div>
      <div class="role-grid">${allocRows}</div>
      <div style="font-size:12px;color:var(--sub);margin-top:6px">${esc(alloc.method||'')} · ${esc(alloc.note||'')}</div>
    </div>` : '';
    const adjRows = (r.sector_adjustment||[]).map(a=>{
      const c2 = a.action==='加仓'?'up':(a.action==='减仓'?'down':'flat');
      return '<div class="role-card"><div class="rn">'+esc(a.sector||'')+' <span class="badge '+c2+'">'+esc(a.action||'')+' '+(a.suggest_pct||'')+'</span></div><div class="rv">'+esc(a.reason||'')+'</div></div>';
    }).join('');
    html += `
    <h3>📊 组合分析</h3>
    <div class="sec"><div class="k">核心结论</div><div class="v">${esc(r.summary||'-')}</div></div>
    <div class="sec"><div class="k">板块分布</div><div class="v" style="font-weight:400">${esc(st.sector_distribution||'-')}</div></div>
    <div class="sec"><div class="k">集中度 / 分散化</div><div class="v" style="font-weight:400">${esc(st.concentration||'-')} · ${esc(st.diversification||'-')}</div></div>
    <div class="sec"><div class="k">整体风险评级</div><div class="v">${esc(st.risk_level||'-')}</div></div>
    <div class="sec"><div class="k">整体明日预测</div><div class="v"><span class="badge ${dirCls(pf.direction)}">${esc(pf.direction||'-')}</span> ${esc(pf.expected_pct||'-')}（信心 ${esc(pf.confidence||'-')}）</div></div>
    <div class="sec"><div class="k">预测理由</div><div class="v" style="font-weight:400">${esc(pf.reason||'-')}</div></div>
    <div class="sec"><div class="k">板块调整建议</div><div class="role-grid">${adjRows||'<span class="ts">无</span>'}</div></div>
    <div class="sec"><div class="k">整体调仓建议</div><ul>${(r.rebalance_suggestions||[]).map(x=>'<li>'+esc(x)+'</li>').join('')||'<li>-</li>'}</ul></div>
    ${icHtml}
    ${ndHtml}
    ${allocHtml}
    `;
  } else {
    html += '<div class="empty-state">组合分析失败：' + esc((result.portfolio&&result.portfolio.msg)||'') + '</div>';
  }

  // ===== 逐只分析（排序：明确操作 > HOLD有附加 > HOLD纯持有 > 失败；再按可信度高→低、金额大→小） =====
  html += '<h3>📋 逐只分析</h3>';
  const results=(result.results||[]).slice();
  function resultTier(r){
    if(!r.ok) return 3;  // 失败
    const act=(r.report && r.report.action_suggestion) ? r.report.action_suggestion : {};
    const isHold = normVerdict(act.verdict)==='HOLD';
    if(!isHold) return 0;  // 明确操作（买入/卖出/加仓/减仓等）
    // HOLD 类：判断是否有附加内容（附条件调整建议）
    const txt=[act.position_change, act.rationale, (r.report&&r.report.summary)||''].join(' ');
    if(/回调|回踩|加仓|减仓|止盈|止损|突破|破位|分批|建仓|补仓|买点|卖点|低吸|高抛|择机|逢低|逢高|跌破|站稳/.test(txt)) return 1;
    return 2;  // HOLD 纯持有
  }
  results.sort((a,b)=>{
    const ta=resultTier(a), tb=resultTier(b);
    if(ta!==tb) return ta-tb;  // 档位小者在前
    const ca=(a.ok && a.report && a.report.confidence_score)||0;
    const cb=(b.ok && b.report && b.report.confidence_score)||0;
    if(ca!==cb) return cb-ca;  // 可信度高在前
    const va=((state&&state.funds.find(x=>x.code===a.code))||{}).value||0;
    const vb=((state&&state.funds.find(x=>x.code===b.code))||{}).value||0;
    return vb-va;  // 金额大在前
  });
  for(const r of results){
    if(!r.ok){
      html += '<div class="sec" style="border-left:3px solid var(--up);padding-left:12px"><div class="v">' + esc(r.code+' '+(r.name||'') + '：分析失败 ' + esc(r.msg||'')) + '</div></div>';
      continue;
    }
    const rep = r.report || {};
    const act = rep.action_suggestion || {};
    const tom = rep.tomorrow_forecast || {};
    html += `
    <div class="sec" style="border:1px solid var(--line);border-radius:10px;padding:12px 14px;margin-bottom:10px">
      <div style="font-weight:700;margin-bottom:4px">${esc(r.code)} ${esc(r.name)}</div>
      <div class="row" style="margin:4px 0">
        <div class="item"><div class="k">操作</div><div class="v"><span class="badge ${verdictClass(act.verdict)}">${esc(normVerdict(act.verdict)||'-')}</span></div></div>
        <div class="item"><div class="k">加减仓</div><div class="v">${esc(act.position_change||'-')}</div></div>
        <div class="item"><div class="k">明日</div><div class="v"><span class="badge ${cls(tom.direction)}">${esc(tom.direction||'-')}</span> ${esc(tom.expected_pct||'-')}</div></div>
        <div class="item"><div class="k">信心</div><div class="v">${rep.confidence_score||0}/100</div></div>
      </div>
      <div class="v" style="font-weight:400;color:var(--sub)">${esc(rep.summary||'')}</div>
      ${rolesBrief(rep)}
    </div>`;
  }
  html += '<div style="color:var(--sub);font-size:11px">仅供研究参考，不构成投资建议</div>';
  container.innerHTML = html;
}

function rolesHtml(r){
  const brief = rolesBrief(r);
  return brief ? '<h3>👥 各角色看法</h3>' + brief : '';
}

function renderReport(result, container, sigInfo){
  if(!result || !result.ok){
    container.innerHTML = '<div class="empty-state">分析失败：' + esc((result&&result.msg)||'未知') + '</div>';
    return;
  }
  const r = result.report || {};
  const today = r.today_analysis || {};
  const tom = r.tomorrow_forecast || {};
  const mid = r.midterm_strategy || {};
  const act = r.action_suggestion || {};
  const cls = v => v==='UP'||v==='BUY'||v==='ADD' ? 'up' : (v==='DOWN'||v==='SELL'||v==='REDUCE' ? 'down' : 'flat');
  const metrics = result.metrics || {};
  const mHtml = metrics.data_points ? `
    <div class="metrics">
      📊 数据 ${metrics.data_points} 天 · MA5/10/20=${metrics.ma5||'-'}/${metrics.ma10||'-'}/${metrics.ma20||'-'} ·
      RSI(14)=${metrics.rsi14||'-'} · 趋势=${metrics.trend||'-'} · 最大回撤=${metrics.max_drawdown||'-'}% · 波动率=${metrics.volatility||'-'}% ·
      区间涨幅 ${metrics.period_change_pct||'-'}% · ${metrics.streak_dir==='up'?'连涨':metrics.streak_dir==='down'?'连跌':'平稳'} ${metrics.streak_days||0} 天 ·
      Sharpe=${metrics.sharpe||'-'} · VaR95=${metrics.var_95||'-'}% · 下行风险=${metrics.downside_risk||'-'}% · 最大连跌=${metrics.max_consec_down_days||'-'}天
    </div>` : '';

  // 信号联动：历史信号对本次分析可信度的影响
  let sigHtml='';
  if(sigInfo && sigInfo.length){
    const closed=sigInfo.filter(x=>x.status==='兑现'||x.status==='证伪');
    const correct=closed.filter(x=>x.outcome==='correct').length;
    const rate=closed.length?Math.round(correct/closed.length*100):null;
    sigHtml='<div class="metrics" style="margin-bottom:10px">📡 信号联动：历史信号 '+sigInfo.length+' 条 · 已了结 '+closed.length+' · 胜率 '+(rate!=null?rate+'%':'--')+' · 本次分析已参考历史信号并计入可信度</div>';
  }

  container.innerHTML = `
    ${mHtml}
    ${sigHtml}
    <h3>📌 核心结论</h3>
    <div class="v">${esc(r.summary||'')} <span class="badge ${verdictClass(act.verdict)}">${esc(normVerdict(act.verdict)||'')}</span> 信心 ${r.confidence_score||0}/100</div>

    ${rolesHtml(r)}

    <h3>🔍 今日分析</h3>
    <div class="sec"><div class="k">趋势定性</div><div class="v">${esc(today.trend||'-')}</div></div>
    <div class="sec"><div class="k">关键价位</div><div class="v">${esc(today.key_levels||'-')}</div></div>
    <div class="sec"><div class="k">动量</div><div class="v">${esc(today.momentum||'-')}</div></div>
    <div class="sec"><div class="k">风险提示</div><div class="v">${esc(today.risk_flag||'-')}</div></div>
    <div class="sec"><div class="k">一句话简评</div><div class="v" style="font-weight:400">${esc(today.one_liner||'-')}</div></div>

    <h3>🔮 明日预测</h3>
    <div class="row">
      <div class="item"><div class="k">方向</div><div class="v"><span class="badge ${cls(tom.direction)}">${esc(tom.direction||'-')}</span></div></div>
      <div class="item"><div class="k">预期涨跌</div><div class="v">${esc(tom.expected_pct||'-')}</div></div>
      <div class="item"><div class="k">信心</div><div class="v">${esc(tom.confidence||'-')}</div></div>
    </div>
    <div class="sec"><div class="k">预测理由</div><div class="v" style="font-weight:400">${esc(tom.reason||'-')}</div></div>

    <h3>🎯 中期策略（1-2 周）</h3>
    <div class="row">
      <div class="item"><div class="k">趋势</div><div class="v">${esc(mid.trend||'-')}</div></div>
      <div class="item"><div class="k">波动区间</div><div class="v">${esc(mid.target_range||'-')}</div></div>
      <div class="item"><div class="k">仓位建议</div><div class="v">${esc(mid.position_advice||'-')}</div></div>
      <div class="item"><div class="k">信心</div><div class="v">${esc(mid.confidence||'-')}</div></div>
    </div>
    <div class="sec"><div class="k">中期关键位</div><div class="v" style="font-weight:400">${esc(mid.key_levels||'-')}</div></div>
    <div class="sec"><div class="k">策略依据</div><div class="v" style="font-weight:400">${esc(mid.reason||'-')}</div></div>

    <h3>💡 加减仓建议</h3>
    <div class="row">
      <div class="item"><div class="k">操作</div><div class="v"><span class="badge ${verdictClass(act.verdict)}">${esc(normVerdict(act.verdict)||'-')}</span></div></div>
      <div class="item"><div class="k">加减仓</div><div class="v">${esc(act.position_change||'-')}</div></div>
      <div class="item"><div class="k">入场区间</div><div class="v">${esc(act.entry_zone||'-')}</div></div>
      <div class="item"><div class="k">目标</div><div class="v">${esc(act.target||'-')}</div></div>
      <div class="item"><div class="k">止损</div><div class="v">${esc(act.stop_loss||'-')}</div></div>
    </div>
    <div class="sec"><div class="k">理由</div><div class="v" style="font-weight:400">${esc(act.rationale||'-')}</div></div>

    <h3>⚠ 关键风险 / 催化</h3>
    <div class="row">
      <div class="item"><div class="k">风险</div><ul>${(r.key_risks||[]).map(x=>'<li>'+esc(x)+'</li>').join('')||'<li>-</li>'}</ul></div>
      <div class="item"><div class="k">催化</div><ul>${(r.key_catalysts||[]).map(x=>'<li>'+esc(x)+'</li>').join('')||'<li>-</li>'}</ul></div>
    </div>
    <div style="margin-top:14px;color:var(--sub);font-size:11px">生成时间：${esc(result.analyzed_at||'')} · 仅供研究参考，不构成投资建议</div>
  `;
}

async function refreshHistoryList(){
  const list = document.getElementById('history-list');
  if(!list) return;
  const dates = await pywebview.api.list_history_dates();
  if(!dates.length){ list.innerHTML='<div class="empty-state">暂无历史预测，先做一次分析</div>'; return; }
  list.innerHTML = '<div style="color:var(--sub);font-size:12px;padding:4px 0 10px">点击日期查看当日预测详情</div>' +
    dates.map(d=>'<div class="h-item" onclick="showHistoryRecord(\''+d+'\')"><div>📅 '+d+'</div><div class="meta">查看预测 →</div></div>').join('');
}

async function showHistoryRecord(date){
  const list = document.getElementById('history-list');
  if(!list) return;
  const rec = await pywebview.api.get_history_full(date);
  if(!rec){ toast('未找到该日预测', true); return; }
  list.innerHTML = '<div class="h-item" onclick="refreshHistoryList()" style="border-color:var(--brand);cursor:pointer"><div>← 返回列表</div></div>' + renderHistoryDetail(rec);
}

function renderHistoryDetail(rec){
  const cls = v => v==='UP'||v==='BUY'||v==='ADD' ? 'up' : (v==='DOWN'||v==='SELL'||v==='REDUCE' ? 'down' : 'flat');
  const dirCls = v => v==='UP' ? 'up' : (v==='DOWN' ? 'down' : 'flat');
  let html = '<div style="font-weight:600;margin:10px 0 12px">📅 ' + esc(rec.date) + ' 的预测</div>';

  if(rec.portfolio){
    const pf = (rec.portfolio.portfolio_forecast) || {};
    html += '<div class="sec" style="border:1px solid var(--brand);border-radius:10px;padding:12px 14px;margin-bottom:10px">' +
      '<div style="color:var(--brand);font-weight:600;margin-bottom:6px">📊 组合预测</div>' +
      '<div class="v"><span class="badge '+dirCls(pf.direction)+'">'+esc(pf.direction||'-')+'</span> '+esc(pf.expected_pct||'-')+'（信心 '+esc(pf.confidence||'-')+'）</div>' +
      (pf.reason ? '<div class="v" style="font-weight:400;color:var(--sub);margin-top:4px">'+esc(pf.reason)+'</div>' : '') +
      '</div>';
  }

  for(const it of rec.items || []){
    const rep = it.report || {};
    const act = rep.action_suggestion || {};
    const tom = rep.tomorrow_forecast || {};
    html += '<div class="sec" style="border:1px solid var(--line);border-radius:10px;padding:12px 14px;margin-bottom:10px">' +
      '<div style="font-weight:700;margin-bottom:4px">'+esc(it.code)+' '+esc(it.name)+'</div>' +
      '<div class="row" style="margin:4px 0">' +
        '<div class="item"><div class="k">操作</div><div class="v"><span class="badge '+cls(act.verdict)+'">'+esc(act.verdict||'-')+'</span></div></div>' +
        '<div class="item"><div class="k">明日</div><div class="v"><span class="badge '+dirCls(tom.direction)+'">'+esc(tom.direction||'-')+'</span> '+esc(tom.expected_pct||'-')+'</div></div>' +
        '<div class="item"><div class="k">信心</div><div class="v">'+(rep.confidence_score||0)+'/100</div></div>' +
      '</div>' +
      '<div class="v" style="font-weight:400;color:var(--sub)">'+esc(rep.summary||'')+'</div>' +
    '</div>';
  }
  return html || '<div class="empty-state">该日无预测记录</div>';
}

async function refreshReviewDates(){
  const sel = document.getElementById('rev_date');
  if(!sel) return;
  const dates = await pywebview.api.list_history_dates();
  sel.innerHTML = dates.map(d=>'<option value="'+d+'">'+d+'</option>').join('');
}

async function doReviewAll(){
  const ds = document.getElementById('rev_date');
  if(!ds.value) return toast('请选择预测日期', true);
  const prog = document.getElementById('rev-progress');
  const out = document.getElementById('rev-result');
  prog.style.display='block';
  out.innerHTML='';
  document.getElementById('rev-msg').textContent='复盘中...';
  document.getElementById('rev-bar').style.width='10%';

  const r = await pywebview.api.review_all(ds.value);
  if(!r.ok){ toast(r.msg, true); prog.style.display='none'; return; }
  const taskId = r.task_id;
  while(true){
    await new Promise(res=>setTimeout(res, 600));
    const st = await pywebview.api.get_task_status(taskId);
    if(st.status==='done'){
      prog.style.display='none';
      renderReviewAll(st.result, out);
      return;
    } else if(st.status==='error'){
      prog.style.display='none';
      toast(st.msg || '复盘失败', true);
      return;
    } else {
      document.getElementById('rev-bar').style.width = (st.progress||0) + '%';
      document.getElementById('rev-msg').textContent = st.msg || '进行中...';
    }
  }
}

function renderReviewAll(result, container){
  if(!result || !result.ok){
    container.innerHTML = '<div class="empty-state">复盘失败：' + esc((result&&result.msg)||'未知') + '</div>';
    return;
  }
  const dirCls = v => v==='UP' ? 'up' : (v==='DOWN' ? 'down' : 'flat');
  const avgCls = result.avg_accuracy>=80?'up':(result.avg_accuracy>=50?'flat':'down');
  const rows = (result.results||[]).map(r=>{
    if(!r.ok){
      return '<div class="sec" style="border-left:3px solid var(--orange);padding-left:12px;margin-bottom:8px">'+esc(r.code)+'：'+esc(r.msg||'')+'</div>';
    }
    const accCls = r.accuracy>=80?'up':(r.accuracy>=50?'flat':'down');
    return `
    <div class="sec" style="border:1px solid var(--line);border-radius:10px;padding:12px 14px;margin-bottom:8px">
      <div style="font-weight:700;margin-bottom:6px">${esc(r.code)} ${esc(r.name||'')}</div>
      <div class="row" style="margin:4px 0">
        <div class="item"><div class="k">预测</div><div class="v"><span class="badge ${dirCls(r.expected_dir)}">${r.expected_dir}</span> ${r.expected_pct}%</div></div>
        <div class="item"><div class="k">实际</div><div class="v"><span class="badge ${dirCls(r.actual_dir)}">${r.actual_dir}</span> ${r.actual_pct}%</div></div>
        <div class="item"><div class="k">方向</div><div class="v">${r.direction_correct?'<span class="badge up">✓ 对</span>':'<span class="badge down">✗ 错</span>'}</div></div>
        <div class="item"><div class="k">偏差</div><div class="v">${r.pct_deviation>0?'+':''}${r.pct_deviation}%</div></div>
        <div class="item"><div class="k">准确率</div><div class="v"><span class="badge ${accCls}">${r.accuracy}%</span></div></div>
      </div>
    </div>`;
  }).join('');

  // 组合复盘
  let comboHtml = '';
  if(result.portfolio_review && result.portfolio_review.ok){
    const p = result.portfolio_review;
    const accCls = p.accuracy>=80?'up':(p.accuracy>=50?'flat':'down');
    comboHtml = `
    <h3>📊 组合复盘</h3>
    <div class="row" style="margin-bottom:12px">
      <div class="item"><div class="k">组合预测</div><div class="v"><span class="badge ${dirCls(p.expected_dir)}">${p.expected_dir}</span> ${p.expected_pct}%</div></div>
      <div class="item"><div class="k">组合实际</div><div class="v"><span class="badge ${dirCls(p.actual_dir)}">${p.actual_dir}</span> ${p.actual_pct}%</div></div>
      <div class="item"><div class="k">方向</div><div class="v">${p.direction_correct?'<span class="badge up">✓ 对</span>':'<span class="badge down">✗ 错</span>'}</div></div>
      <div class="item"><div class="k">偏差</div><div class="v">${p.pct_deviation>0?'+':''}${p.pct_deviation}%</div></div>
      <div class="item"><div class="k">准确率</div><div class="v"><span class="badge ${accCls}">${p.accuracy}%</span></div></div>
    </div>`;
  }

  container.innerHTML = `
    <h3>📋 复盘：${esc(result.date)} · 共 ${result.total} 只基金</h3>
    <div class="row" style="margin-bottom:12px">
      <div class="item"><div class="k">整体方向正确率</div><div class="v">${result.direction_correct_count}/${result.total}</div></div>
      <div class="item"><div class="k">平均准确率</div><div class="v"><span class="badge ${avgCls}">${result.avg_accuracy}%</span></div></div>
    </div>
    ${comboHtml}
    <h3>🤖 偏差原因与改进建议</h3>
    <div class="v" style="font-weight:400;line-height:1.7;margin-bottom:14px">${esc(result.deviation_reason||'（未配置 LLM，无法分析偏差原因）')}</div>
    ${rows}
    <div style="margin-top:10px;color:var(--sub);font-size:11px">准确率=方向对50%+幅度误差<0.3%得50%、<0.6%得30%；组合实际=持仓市值加权涨跌</div>
  `;
}

// 拦截 saveModal 不需要单独覆写，openSettings 直接设置 inp.dataset.mode='settings'

window.addEventListener('pywebviewready',()=>{
  pywebview.api.manual_refresh();
  // 开市日净值更新完（23:00）后自动复盘一次加减仓建议；休市日/已更新完则不设
  const now=new Date();
  const closeAt=new Date(now.getFullYear(),now.getMonth(),now.getDate(),23,0,5);
  const delay=closeAt-now;
  if(delay>0){
    setTimeout(()=>{ autoReviewTrades(); }, delay);
  }else{
    // 已过 23:00 才启动：等 3 秒补一次（休市日 autoReviewTrades 内部会跳过，不打扰）
    setTimeout(()=>{ autoReviewTrades(); }, 3000);
  }
});
</script>
</body>
</html>"""


HTML_MINI = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta http-equiv="X-UA-Compatible" content="ie=edge">
<style>
:root{
  --bg:#dedee3; --panel:#ffffff; --panel2:#f2f2f7;
  --line:rgba(0,0,0,.18);
  --txt:#1c1c1e; --sub:#8e8e93;
  --up:#ff3b30; --down:#34c759; --brand:#007aff;
  --orange:#ff9500; --purple:#5856d6;
  --scrollbar:#c7c7cc; --track-bg:#d9d9de;
  --thead-bg:#f7f7f9; --placeholder:#a1a1a6;
  --bg-glow:#dfe5f0; --glass-highlight:rgba(255,255,255,.9);
  --brand-rgb:0,122,255; --up-rgb:255,59,48; --down-rgb:52,199,89;
  --orange-rgb:255,149,0; --purple-rgb:88,86,214; --sub-rgb:142,142,147;
}
body.dark{
  --bg:#0b0f1a; --panel:#131a2b; --panel2:#0f1524;
  --line:rgba(255,255,255,.07);
  --txt:#e8edf7; --sub:#8a94ad;
  --up:#ff5252; --down:#26d07c; --brand:#5b8cff;
  --orange:#f5a623; --purple:#8f6bff;
  --scrollbar:#2a3350; --track-bg:#1c2740;
  --thead-bg:#111830; --placeholder:#5a6480;
  --bg-glow:#1b2b55; --glass-highlight:rgba(255,255,255,.06);
  --brand-rgb:91,140,255; --up-rgb:255,82,82; --down-rgb:38,208,124;
  --orange-rgb:245,166,35; --purple-rgb:143,107,255; --sub-rgb:138,148,173;
}
*{margin:0;padding:0;box-sizing:border-box;font-family:"Microsoft YaHei UI","Segoe UI",sans-serif}
html,body{height:100%}
body{background:var(--bg);color:var(--txt);overflow:hidden;font-size:13px;
  border:1px solid rgba(var(--brand-rgb),.25);border-radius:12px;user-select:none}
#mini{display:flex;flex-direction:column;height:100vh;padding:12px 14px;gap:10px}

/* 顶部标题栏（可拖动 + 控制按钮） */
.titlebar{flex:none;display:flex;align-items:center;height:30px}
.titlebar .drag{flex:1;height:100%;display:flex;align-items:center;gap:6px;
  font-size:12px;font-weight:600;color:var(--sub);cursor:move}
.titlebar .btns{display:flex;gap:6px;flex:none}
.titlebar .btns button{cursor:pointer;border:none;border-radius:6px;width:26px;height:22px;
  font-size:12px;line-height:1;color:var(--txt);background:rgba(0,0,0,.08);padding:0}
.titlebar .btns button:hover{background:rgba(0,0,0,.1)}
.titlebar .btns button.pin.on{background:rgba(var(--brand-rgb),.4);color:#fff}
.titlebar .btns button.cls:hover{background:rgba(var(--up-rgb),.45)}

/* 收起态（缩小到右下角） */
.collapsed{display:none;flex:1;align-items:center;gap:10px}
.collapsed .c-info{flex:1;display:flex;flex-direction:column;gap:2px;min-width:0}
.collapsed .c-item{font-size:11px;color:var(--sub);white-space:nowrap}
.collapsed .c-item b{color:var(--txt);font-size:13px;font-variant-numeric:tabular-nums;margin-left:4px}
.collapsed .expand{cursor:pointer;border:none;border-radius:8px;width:34px;height:34px;
  font-size:16px;color:#fff;background:linear-gradient(135deg,var(--brand),var(--brand));flex:none}
body.collapsed .titlebar{display:none}
body.collapsed .sum{display:none}
body.collapsed .list{display:none}
body.collapsed .bar{display:none}
body.collapsed .collapsed{display:flex}
body.collapsed #mini{padding:10px 12px;gap:0}

/* 汇总 */
.sum{display:grid;grid-template-columns:1.1fr 1fr;gap:10px}
.box{background:var(--panel);
  border:1px solid var(--line);border-radius:12px;padding:10px 14px;
  box-shadow:inset 0 1px 0 var(--glass-highlight),0 2px 12px rgba(0,0,0,.1)}
.box .k{color:var(--sub);font-size:11px;margin-bottom:4px}
.box .v{font-size:22px;font-weight:700;font-variant-numeric:tabular-nums}
.up{color:var(--up)} .down{color:var(--down)} .flat{color:var(--sub)}

/* 列表 */
.list{flex:1;overflow:auto;display:flex;flex-direction:column;gap:6px}
.item{display:flex;align-items:center;gap:8px;padding:8px 10px;
  background:var(--panel2);border:1px solid var(--line);border-radius:10px;
  box-shadow:inset 0 1px 0 var(--glass-highlight),0 2px 10px rgba(0,0,0,.09)}
.item .code{color:var(--sub);font-size:11px;flex:none}
.item .name{flex:1;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.item .pct{font-weight:700;font-size:13px;min-width:58px;text-align:right}
.item .pct .stale{color:var(--sub);font-size:10px;margin-left:4px;font-weight:400;opacity:.85}
.item .val{color:var(--sub);font-size:12px;min-width:70px;text-align:right}
.empty{color:var(--sub);text-align:center;padding:20px 0;font-size:12px}

/* 底部 */
.bar{flex:none;display:flex;gap:8px}
.bar button{flex:1;cursor:pointer;border:none;border-radius:8px;padding:8px 0;
  font-size:12px;font-weight:600;color:#fff;background:linear-gradient(135deg,var(--brand),var(--brand))}
.bar button.close{background:linear-gradient(135deg,var(--sub),var(--sub))}
::-webkit-scrollbar{width:6px}
::-webkit-scrollbar-thumb{background:var(--scrollbar);border-radius:3px}
</style>
</head>
<body>
<div id="mini">
  <div class="titlebar">
    <span class="drag pywebview-drag-region">📈 基金悬浮窗</span>
    <span class="btns">
      <button id="themeBtn" onclick="toggleTheme()" title="切换深色/浅色主题">🌙</button>
      <button id="pinbtn" class="pin on" onclick="togglePin()" title="取消置顶">📌</button>
      <button id="foldbtn" onclick="toggleCollapse()" title="缩小到右下角">⤓</button>
      <button class="cls" onclick="expand()" title="关闭（回到主窗口）">✕</button>
    </span>
  </div>
  <div class="sum">
    <div class="box">
      <div class="k">总资产(元)</div>
      <div class="v" id="total">--</div>
    </div>
    <div class="box">
      <div class="k">今日收益(元)</div>
      <div class="v" id="profit">--</div>
      <div class="v" id="cum-rate" style="font-size:13px;margin-top:3px;font-weight:600">--</div>
    </div>
  </div>
  <div class="list" id="list"></div>
  <div class="bar">
    <button class="close" onclick="quit()">退出</button>
    <button onclick="expand()">展开完整版</button>
  </div>
  <div class="collapsed">
    <div class="c-info">
      <span class="c-item">总资产<b id="c-total">--</b></span>
      <span class="c-item">今日收益<b id="c-profit">--</b></span>
    </div>
    <button class="expand" onclick="toggleCollapse()" title="展开">⤢</button>
  </div>
</div>

<script>
let state=null;
function cls(v){return v>0?'up':(v<0?'down':'flat')}
function fmt(v,d=2){return v==null?'--':Number(v).toLocaleString('zh-CN',{minimumFractionDigits:d,maximumFractionDigits:d})}
function sgn(v,d=2){return (v>0?'+':'')+fmt(v,d)}
function esc(s){return String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}

let darkMode=false;
function applyTheme(dark){
  darkMode=dark;
  document.body.classList.toggle('dark', dark);
  const b=document.getElementById('themeBtn');
  if(b){b.textContent=dark?'☀️':'🌙';b.title=dark?'切换到浅色主题':'切换到深色主题';}
}
function toggleTheme(){
  applyTheme(!darkMode);
  try{localStorage.setItem('fund_theme', darkMode?'dark':'light');}catch(e){}
}
(function(){
  let t='light';
  try{t=localStorage.getItem('fund_theme')||'light';}catch(e){}
  if(t==='dark') applyTheme(true);
})();

function render(st){
  state=st;
  const masked=!!st.mask;
  document.getElementById('total').textContent=masked?'****':fmt(st.total);
  const p=document.getElementById('profit');
  p.textContent=(st.profit>0?'+':'')+fmt(st.profit);
  p.className='v '+cls(st.profit);
  // 累计收益率：累计收益 / 净投入（持仓市值 - 累计收益）
  const mv=st.total-(st.idle_cash||0);        // 持仓市值
  const netIn=mv-(st.realized||0);             // 净投入
  const cr=netIn>0?((st.realized||0)/netIn*100):null;
  const ce=document.getElementById('cum-rate');
  ce.textContent=cr==null?'--':'累计 '+sgn(cr)+'%';
  ce.className='v '+cls(cr);
  // 收起态也同步
  document.getElementById('c-total').textContent=masked?'****':fmt(st.total);
  const cp=document.getElementById('c-profit');
  cp.textContent=(st.profit>0?'+':'')+fmt(st.profit);
  cp.className=cls(st.profit);
  const list=document.getElementById('list');
  if(!st.funds.length){list.innerHTML='<div class="empty">暂无持仓</div>';return;}
  // 排序：先按涨幅降序，涨幅相同再按持仓占比降序
  const sorted=[...st.funds].sort((a,b)=>{
    const pa=a.pct==null?-Infinity:a.pct, pb=b.pct==null?-Infinity:b.pct;
    if(pb!==pa) return pb-pa;
    return (b.ratio||0)-(a.ratio||0);
  });
  list.innerHTML=sorted.map(f=>
    '<div class="item">'+
      '<span class="code">'+f.code+'</span>'+
      '<span class="name">'+esc(f.name)+'</span>'+
      '<span class="pct '+cls(f.pct)+'">'+(f.pct==null?'--':sgn(f.pct)+'%')+(f.est?'':'<small class="stale">'+(f.qdate?f.qdate.slice(5):'昨收')+'</small>')+'</span>'+
      '<span class="val">'+(f.value?fmt(f.value):'--')+'</span>'+
    '</div>'
  ).join('');
}

async function expand(){await pywebview.api.show_main()}
async function quit(){await pywebview.api.quit_app()}

async function togglePin(){
  const r=await pywebview.api.toggle_pin();
  if(r && r.ok){
    const b=document.getElementById('pinbtn');
    if(r.on_top){b.classList.add('on');b.textContent='📌';b.title='取消置顶';}
    else{b.classList.remove('on');b.textContent='📍';b.title='置顶';}
  }
}

async function toggleCollapse(){
  const r=await pywebview.api.toggle_collapse();
  if(r && r.ok){
    document.body.classList.toggle('collapsed', r.collapsed);
    const b=document.getElementById('foldbtn');
    b.textContent = r.collapsed ? '⤢' : '⤓';
    b.title = r.collapsed ? '展开' : '缩小到右下角';
  }
}

window.addEventListener('pywebviewready',()=>{
  pywebview.api.manual_refresh();
});
</script>
</body>
</html>"""


def main():
    # 屏蔽 pywebview 内部对 window.native 序列化的无害报错日志
    import logging
    logging.getLogger("pywebview").setLevel(logging.CRITICAL)
    logging.getLogger("webview").setLevel(logging.CRITICAL)

    api = Api()

    # 悬浮窗用独立的轻量 js_api：pywebview 5.4 两个窗口共享同一 js_api 会触发
    # "'Api' cannot be converted to Rectangle" 的底层 bug，导致未响应
    class FloatApi:
        def show_main(self):
            return api.show_main()

        def quit_app(self):
            return api.quit_app()

        def manual_refresh(self):
            return api.manual_refresh()

        def toggle_pin(self):
            return api.toggle_pin()

        def toggle_collapse(self):
            return api.toggle_collapse()

    main_window = webview.create_window(
        "我的基金监控", html=HTML_MAIN, js_api=api,
        width=1080, height=720, min_size=(900, 600),
        background_color="#f2f2f7",
    )
    float_window = webview.create_window(
        "基金悬浮窗", html=HTML_MINI, js_api=FloatApi(),
        width=320, height=460,
        frameless=True, on_top=True,
        hidden=True,
        background_color="#f2f2f7",
    )
    api._main_window = main_window
    api._float_window = float_window
    api._windows = [main_window, float_window]

    def loop():
        while True:
            time.sleep(REFRESH_SEC)
            threading.Thread(target=api.refresh, daemon=True).start()

    threading.Thread(target=loop, daemon=True).start()
    webview.start(icon=ICON_FILE if os.path.exists(ICON_FILE) else None)


if __name__ == "__main__":
    main()
