# -*- coding: utf-8 -*-
"""
基金实时监控 · T+1 加减仓版
pywebview (WebView2) + 内嵌 HTML/CSS，深色金融风面板
数据源：腾讯财经 + 蛋卷基金
"""
import json
import os
import re
import shutil
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta

import requests
import webview

try:
    import pystray
    from PIL import Image
    _HAS_TRAY = True
except Exception:
    _HAS_TRAY = False  # 缺 pystray 时降级：无托盘，关闭=退出

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
UI_CONFIG_FILE = os.path.join(BASE_DIR, "ui_config.json")

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
    _atomic_write(PROXY_FILE, {"proxy": (proxy or "").strip()})


def _get_proxies():
    """返回 requests 用的 proxies 字典；未配置代理返回 None。兼容仅填 host:port。"""
    p = _load_proxy()
    if not p:
        return None
    if "://" not in p:
        p = "http://" + p
    return {"http": p, "https": p}


# ---------- UI 偏好配置（收起态/隐藏金额/开机自启） ----------
def _single_instance_check():
    """单实例锁：已有一个实例在运行时，新实例直接退出（托盘常驻后重复双击 exe 会多开，窗口互相干扰）"""
    try:
        import ctypes
        mutex = ctypes.windll.kernel32.CreateMutexW(None, False, "FundMonitor_SingleInstance_Mutex")
        err = ctypes.windll.kernel32.GetLastError()
        if err == 183:  # ERROR_ALREADY_EXISTS
            return False
        return True
    except Exception:
        return True


def load_ui_config():
    """读取 UI 偏好：collapsed(历史字段，已废弃)、mask(默认隐藏金额)、autostart(开机自启)、fixed(悬浮窗固定位置)"""
    default = {"collapsed": False, "mask": True, "autostart": False, "fixed": True}
    try:
        with open(UI_CONFIG_FILE, "r", encoding="utf-8") as f:
            return {**default, **json.load(f)}
    except Exception:
        return default


def save_ui_config(cfg):
    _atomic_write(UI_CONFIG_FILE, cfg)


def _autostart_enabled():
    """读取注册表 Run 键，判断是否开机自启"""
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Microsoft\Windows\CurrentVersion\Run",
                             0, winreg.KEY_READ)
        try:
            winreg.QueryValueEx(key, "基金监控")
            return True
        except FileNotFoundError:
            return False
        finally:
            winreg.CloseKey(key)
    except Exception:
        return False


def _autostart_set(enabled):
    """写/删注册表 Run 键，实现开机自启（当前用户级，无需管理员权限）"""
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Microsoft\Windows\CurrentVersion\Run",
                             0, winreg.KEY_SET_VALUE)
        if enabled:
            exe = os.path.abspath(sys.executable)
            winreg.SetValueEx(key, "基金监控", 0, winreg.REG_SZ, f'"{exe}"')
        else:
            try:
                winreg.DeleteValue(key, "基金监控")
            except FileNotFoundError:
                pass
        winreg.CloseKey(key)
        return True
    except Exception:
        return False


def load_data():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def _atomic_write(path, data, indent=2):
    """原子写 JSON：先写临时文件再 os.replace 替换，避免进程异常退出/并发写
    导致文件损坏（如全零写入）。写前把旧文件备份为 .bak。"""
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=indent)
            f.flush()
            os.fsync(f.fileno())
        # 备份旧文件（存在时）
        if os.path.exists(path):
            try:
                shutil.copy2(path, path + ".bak")
            except Exception:
                pass
        os.replace(tmp, path)
        return True
    except Exception as e:
        print("保存失败:", e)
        # 清理临时文件
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        return False


def save_data(data):
    _atomic_write(DATA_FILE, data)


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
    _atomic_write(IDLE_FILE, {"amount": amount})


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
        _atomic_write(RATE_FILE, keep, indent=None)
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


def _work_area():
    """返回排除任务栏后的可用区域 (x, y, w, h)，逻辑像素；失败回退整屏"""
    try:
        import ctypes
        # RECT = left, top, right, bottom
        class RECT(ctypes.Structure):
            _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                        ("right", ctypes.c_long), ("bottom", ctypes.c_long)]
        rect = RECT()
        SPI_GETWORKAREA = 0x30
        ctypes.windll.user32.SystemParametersInfoW(
            SPI_GETWORKAREA, 0, ctypes.byref(rect), 0)
        try:
            scale = ctypes.windll.shcore.GetScaleFactorForDevice(0) / 100.0
        except Exception:
            scale = 1.0
        x = max(0, int(round(rect.left / scale)))
        y = max(0, int(round(rect.top / scale)))
        w = max(1, int(round((rect.right - rect.left) / scale)))
        h = max(1, int(round((rect.bottom - rect.top) / scale)))
        return x, y, w, h
    except Exception:
        sx, sy = _screen_size()
        return 0, 0, sx, sy


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


def _tray_icon_image():
    """加载托盘图标（app.ico → PIL Image，32x32）"""
    from PIL import Image as _PILImage
    path = ICON_FILE
    if not os.path.exists(path):
        # PyInstaller 打包后 app.ico 在 _MEIPASS 里
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            p2 = os.path.join(meipass, "app.ico")
            if os.path.exists(p2):
                path = p2
    img = _PILImage.open(path)
    img = img.resize((32, 32), _PILImage.LANCZOS)
    return img


def _create_tray(api):
    """创建系统托盘图标：
       - 左键单击 → 显示/隐藏主窗口
       - 右键菜单 → 显示主窗口 / 显示悬浮窗 / 彻底退出
    """
    def _show_main():
        api.show_main()
        try:
            api._main_window.show()
            api._main_visible = True
        except Exception:
            pass

    def _show_float():
        api.show_floating()

    def _quit():
        api.quit_app()

    menu = pystray.Menu(
        pystray.MenuItem("显示主窗口", lambda icon, item: _show_main()),
        pystray.MenuItem("显示悬浮窗", lambda icon, item: _show_float()),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("彻底退出", lambda icon, item: _quit()),
    )
    icon = pystray.Icon("fund_monitor", _tray_icon_image(), "基金监控", menu)
    icon.run_detached()  # 独立线程运行，不阻塞 webview 主循环
    return icon


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
# 行情代码：A股 sz/sh 前缀；港股 r_hk 前缀；美股 us 前缀；日股 jp 前缀；韩股 kr 前缀。
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
    # 华夏全球科技先锋(QDII)C（美股半导体/存储 + 港股）覆盖率 17.9%
    "024239": [("usSNDK", 3.11), ("usMU", 2.85), ("usMRVL", 1.60), ("usTSM", 1.57), ("usUMC", 1.51),
               ("usLITE", 1.51), ("usSTX", 1.49), ("usWDC", 1.46), ("r_hk01888", 1.38), ("r_hk00522", 1.37)],
    # 国富亚洲机会(QDII)（韩/台/日/港半导体，台积电台股并入美股ADR）覆盖率 35.3%
    "457001": [("kr000660", 7.95), ("kr005930", 6.47), ("usTSM", 8.93), ("usASML", 3.43),
               ("jp6981", 3.00), ("r_hk09988", 2.75), ("kr009150", 2.72)],
    "021662": [("kr000660", 7.95), ("kr005930", 6.47), ("usTSM", 8.93), ("usASML", 3.43),
               ("jp6981", 3.00), ("r_hk09988", 2.75), ("kr009150", 2.72)],
    # 天弘全球高端制造(QDII)C（日/港/A/美股半导体设备）覆盖率 21.3%
    "016665": [("jp285A", 3.88), ("r_hk02476", 3.33), ("sz300308", 2.18), ("usNVDA", 2.16),
               ("usTSM", 2.09), ("r_hk01347", 2.09), ("usGLW", 2.02), ("sz002384", 1.80),
               ("sh688498", 1.78)],
    # 易方达全球成长精选(QDII)C（全球半导体设备/存储/AI）覆盖率 47.5%
    "012922": [("usLRCX", 6.41), ("jp285A", 5.89), ("usTSM", 5.54), ("usAMD", 4.96),
               ("sz300502", 4.68), ("sz300308", 4.61), ("usSNDK", 4.46), ("usINTC", 4.26),
               ("sh688498", 3.34), ("usASML", 3.33)],
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


def fetch_batch(codes, confirm_map=None):
    """腾讯批量行情（名称/净值/日涨跌）+ 多源盘中估值（fundgz/东财/蛋卷）

    confirm_map: {code: confirm_days}，用于区分 T+N 基金——QDII 等 T+N>1 基金
    净值滞后是常态（qdate 永远不是今天），收盘后不走估值链，直接用腾讯官方数据。
    """
    if confirm_map is None:
        try:
            confirm_map = {c: int(d.get("confirm_days", 1)) for c, d in load_data().items()}
        except Exception:
            confirm_map = {}
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

    # 盘中估值兜底只在交易时段（开市日 9:30-15:00）使用；
    # 收盘后/盘前/周末直接用腾讯官方净值(p[5])与官方涨跌(p[7])，避免估算覆盖官方数据导致不准
    _now = datetime.now()
    _in_trading = (_now.weekday() < 5
                   and 9 * 60 + 30 <= _now.hour * 60 + _now.minute <= 15 * 60)
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
        if not _in_trading:
            # 收盘后/盘前/周末：官方净值已更新(qdate=今天)则保留官方数据，不走估值
            if base.get("qdate") == datetime.now().strftime("%Y-%m-%d"):
                continue
            # T+N>1（QDII 等）：净值滞后是常态，qdate 永远不是今天，
            # 官方最新净值就是「收盘」——直接用腾讯官方数据，不走估值链
            if confirm_map.get(code, 1) > 1:
                em = _fetch_em_official(code)
                if em and em.get("date") > base.get("qdate", ""):
                    base["gz"] = em["nav"]
                    base["gz_pct"] = em["pct"]
                    base["gz_time"] = em["date"]
                    base["qdate"] = em["date"]
                    base["nav"] = em["nav"]
                    base["est"] = False
                    base.pop("est_pending", None)
                    base["est_src"] = "eastmoney"
                continue
            # 腾讯滞后（qdate≠今天）时，先用东财官方净值兜底：
            # 东财 lsjz 更新通常早于腾讯行情，复盘"到了"但持仓仍预估就源于此
            em = _fetch_em_official(code)
            if em and em.get("date") == datetime.now().strftime("%Y-%m-%d"):
                base["gz"] = em["nav"]
                base["gz_pct"] = em["pct"]
                base["gz_time"] = em["date"]
                base["qdate"] = em["date"]
                base["nav"] = em["nav"]
                base["est"] = False
                base.pop("est_pending", None)
                base["est_src"] = "eastmoney"
                continue
            # 仅交易日收盘后（15:00 后）且官方净值未更新：用估值链算「今日预估」，
            # 标记 est_pending（前端显示「收盘未更新」）；盘前/周末保持官方昨日数据
            if not (is_market_open_today() and _now.hour >= 15):
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
                    base["est_pending"] = True
                    base["est_src"] = est[3] if len(est) > 3 else ""
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


_em_official_cache = {}


def _fetch_em_official(code):
    """东财最新官方净值（腾讯 qdate 滞后时兜底用）。返回 {date, nav, pct} 或 None；60 秒缓存。

    腾讯行情接口(qt.gtimg.cn)净值日期更新有时滞后（晚于东财 lsjz），收盘后持仓界面
    会因此一直显示「预估」而复盘（走东财）已"到"。此函数用于腾讯滞后时补拉官方净值。
    """
    now = time.time()
    hit = _em_official_cache.get(code)
    if hit and now - hit[0] < 60:
        return hit[1]
    out = None
    try:
        h = dict(HEADERS)
        h["Referer"] = "http://fundf10.eastmoney.com/"
        r = requests.get(
            f"http://api.fund.eastmoney.com/f10/lsjz?fundCode={code}&pageIndex=1&pageSize=1&mode=0",
            headers=h, timeout=8, proxies=_get_proxies())
        d = r.json()
        items = d.get("Data", {}).get("LSJZList", [])
        if items:
            it = items[0]
            date = it.get("FSRQ", "")
            try:
                nav = float(it.get("DWJZ", 0))
                pct = float(it.get("JZZZL", 0)) if it.get("JZZZL") not in (None, "") else 0.0
                if date and nav:
                    out = {"date": date, "nav": nav, "pct": pct}
            except (TypeError, ValueError):
                pass
    except Exception:
        pass
    _em_official_cache[code] = (now, out)
    return out


_market_open_cache = {}


def is_market_open_today():
    """判断今天是否开市：周末直接休市；工作日拉沪深300指数最新行情日期判断。

    指数行情 p[30] 形如 YYYYMMDDHHMMSS，是最近一个交易日。
    注意：凌晨/盘前（今天未开盘）时 p[30] 是昨天，不能据此判定今天休市——
    只有「工作日且已过 15:00 仍无今天数据」才说明今天节假日休市。
    接口失败时兜底按工作日判断。当日缓存（_build_state 高频调用）。
    """
    key = datetime.now().strftime("%Y-%m-%d")
    if key in _market_open_cache:
        return _market_open_cache[key]
    now = datetime.now()
    if now.weekday() >= 5:  # 周末休市
        _market_open_cache[key] = False
        return False
    try:
        r = requests.get("http://qt.gtimg.cn/q=sh000300", headers=HEADERS, timeout=6,
                         proxies=_get_proxies())
        r.encoding = "gbk"
        m = re.search(r'v_sh000300="([^"]*)"', r.text)
        if m:
            p = m.group(1).split("~")
            if len(p) > 30 and p[30]:
                mkt_date = p[30][:8]
                if mkt_date == now.strftime("%Y%m%d"):
                    _market_open_cache[key] = True   # 盘中有今天数据
                    return True
                if now.hour < 15:
                    _market_open_cache[key] = True   # 盘前/盘中：今天未开盘/未收盘，按工作日开市
                    return True
                _market_open_cache[key] = False      # 已过 15:00 仍无今天数据 → 节假日休市
                return False
    except Exception:
        pass
    _market_open_cache[key] = now.weekday() < 5
    return _market_open_cache[key]


class Api:
    def __init__(self):
        self.data = load_data()
        self.info = {}
        self._windows = []
        self._main_window = None
        self._float_window = None
        self._tray = None
        self._quitting = False
        self._main_visible = True
        self._refreshed = False
        self._tasks = {}  # 分析任务池：task_id -> {status, progress, msg, result}
        self._review_summary = None  # 收盘后复盘的汇总缓存
        self._state_lock = threading.RLock()  # 保护 data/info 读写，避免加载中并发渲染错乱
        _ui = load_ui_config()
        self._float_collapsed = False  # 悬浮窗固定展开（已彻底移除收起功能）
        self._float_on_top = True  # 悬浮窗是否置顶
        self._float_fixed = bool(_ui.get("fixed", True))  # 悬浮窗固定位置（禁止拖动）
        self._mask_amount = bool(_ui.get("mask", True))  # 默认隐藏金额（总资产/闲钱打码）
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
        with self._state_lock:
            return self._build_state()

    def _get_confidence_calibration(self):
        """信心校准表（各档位历史实际正确率），供前端展示层降级标注（不改写字段）"""
        try:
            return fa.load_prediction_lessons().get("confidence_calibration") or []
        except Exception:
            return []

    def _build_state(self):
        """构造推送给前端的完整状态（调用方需持有 _state_lock）"""
        funds = []
        total, profit, count, realized = 0.0, 0.0, 0, 0.0
        holdings = 0.0  # 持仓市值（不含闲钱），用于计算各基金占比
        pending = []
        # 今日分析产出的预测（锚定成交日次日：盘中→明日，盘后→下下个交易日）
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
                                  "confidence": fc.get("confidence"),
                                  "forecast_date": pf.get("forecast_date")}
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
            # 今日分析产出的单只预测（对目标日，见 fdTxt 标注）
            pred = today_preds.get(code)
            today_pred = None
            if pred and pred.get("tomorrow_forecast"):
                tom = pred["tomorrow_forecast"]
                today_pred = {"direction": tom.get("direction"),
                              "expected_pct": tom.get("expected_pct"),
                              "forecast_date": pred.get("forecast_date")}
            funds.append({
                "code": code, "name": name, "pct": pct,
                "value": value or None,
                "has_nav": bool(gz),
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
        ready = all(f["has_nav"] for f in funds)
        if ready:
            total += self.idle_cash  # 总资产 = 持仓市值 + 闲钱
        else:
            # 有基金未拿到净值（启动加载中/估值失败）：总额显示未知，
            # 不输出 0 总资产 / 负累计收益的假数据
            total = profit = realized = None
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
            "confidence_calibration": self._get_confidence_calibration(),
            "market_open": is_market_open_today(),
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
        infos = fetch_batch(codes, {c: int(d.get("confirm_days", 1)) for c, d in self.data.items()})
        self._bench_pct = fetch_benchmark_pct()
        with self._state_lock:
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
        优先用 review_results.json 里已保存的复盘结果，避免每次启动/刷新重新复盘。
        """
        if fa is None:
            return
        today = datetime.now().strftime("%Y-%m-%d")
        if self._review_summary and self._review_summary.get("computed_on") == today:
            return
        try:
            cached = fa.load_review_results()
            for d in fa.list_history_dates():
                if d >= today:
                    continue
                result = cached.get(d)
                if result is None:
                    result = fa.review_all_predictions(d)
                if result and result.get("ok") and result.get("total"):
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
        """切换到悬浮窗：隐藏主窗口，显示置顶小窗（固定展开，不缩成条）"""
        if not self._float_window:
            return {"ok": False, "msg": "无悬浮窗"}
        try:
            self._main_window.hide()
            self._float_window.show()
            self._main_visible = False
        except Exception as e:
            print("show_floating 异常:", e)
            # 悬浮窗失败时恢复主窗口，避免用户什么都看不到
            try:
                self._main_window.show()
                self._main_visible = True
            except Exception:
                pass
            return {"ok": False, "msg": str(e)[:80]}
        return {"ok": True}

    def show_main(self):
        """从悬浮窗回到主窗口"""
        if self._float_window:
            try:
                self._float_window.hide()
                self._main_window.show()
                self._main_visible = True
            except Exception:
                pass
        return {"ok": True}

    def hide_to_tray(self):
        """关闭按钮 → 隐藏到系统托盘（程序继续后台常驻，不退出）"""
        try:
            self._main_window.hide()
        except Exception:
            pass
        try:
            self._float_window.hide()
        except Exception:
            pass
        self._main_visible = False
        self._ensure_tray()
        return {"ok": True}

    def toggle_main_visible(self):
        """托盘左键/菜单：显示或隐藏主窗口"""
        if self._main_visible:
            self.hide_to_tray()
        else:
            try:
                self._float_window.hide()
            except Exception:
                pass
            self._main_window.show()
            self._main_visible = True
        return {"ok": True}

    def _ensure_tray(self):
        """确保系统托盘图标存在（首次调用时创建）"""
        if self._tray is not None:
            return
        if not _HAS_TRAY:
            return
        try:
            self._tray = _create_tray(self)
        except Exception as e:
            print("托盘创建失败:", e)
            self._tray = None

    def toggle_mask_amount(self):
        """切换金额隐藏（总资产/闲钱打码，悬浮窗同步），偏好持久化"""
        self._mask_amount = not self._mask_amount
        cfg = load_ui_config()
        cfg["mask"] = self._mask_amount
        save_ui_config(cfg)
        self.push()
        return {"ok": True, "mask": self._mask_amount}

    def toggle_autostart(self):
        """切换开机自启（注册表 Run 键），偏好持久化"""
        enabled = not _autostart_enabled()
        ok = _autostart_set(enabled)
        cfg = load_ui_config()
        cfg["autostart"] = enabled
        save_ui_config(cfg)
        return {"ok": ok, "autostart": enabled}

    def get_ui_status(self):
        """返回 UI 偏好状态（供悬浮窗初始化）"""
        return {
            "ok": True,
            "mask": self._mask_amount,
            "autostart": _autostart_enabled(),
            "fixed": self._float_fixed,
        }

    def quit_app(self):
        """退出应用（托盘右键「彻底退出」/ 悬浮窗不再直接退出）"""
        import os as _os
        self._quitting = True
        try:
            if self._tray is not None:
                self._tray.stop()
                self._tray = None
        except Exception:
            pass
        try:
            for w in self._windows:
                w.destroy()
        except Exception:
            pass
        _os._exit(0)

    def toggle_pin(self):
        """切换悬浮窗置顶/取消置顶（保留：托盘等入口仍可用）"""
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

    def toggle_fixed(self):
        """切换悬浮窗固定位置（锁定，禁止拖动），偏好持久化"""
        self._float_fixed = not self._float_fixed
        cfg = load_ui_config()
        cfg["fixed"] = self._float_fixed
        save_ui_config(cfg)
        return {"ok": True, "fixed": self._float_fixed}

    def toggle_collapse(self):
        """已移除收起功能：悬浮窗固定展开，此方法保留兼容返回 False 状态"""
        return {"ok": True, "collapsed": False}

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

    # ---------- 数据导入/导出 ----------
    def _backup_files_map(self):
        """基础数据 json 清单：zip 内文件名 → 磁盘路径"""
        files = {
            "funds_data.json": DATA_FILE,
            "idle_cash.json": IDLE_FILE,
            "rate_history.json": RATE_FILE,
            "ui_config.json": UI_CONFIG_FILE,
            "proxy_config.json": PROXY_FILE,
        }
        if fa is not None:
            files.update({
                "analysis_config.json": fa.CONFIG_FILE,
                "analysis_history.json": fa.HISTORY_FILE,
                "signals.json": fa.SIGNALS_FILE,
                "trade_review.json": fa.TRADE_REVIEW_FILE,
                "trade_lessons.json": fa.TRADE_LESSONS_FILE,
                "prediction_lessons.json": fa.PREDICTION_LESSONS_FILE,
                "review_results.json": fa.REVIEW_RESULT_FILE,
            })
        return files

    def export_data(self):
        """把用户基础数据 json 压缩成一个 zip 文件（弹保存对话框）"""
        try:
            import zipfile
            w = self._main_window
            if w is None:
                return {"ok": False, "msg": "主窗口未就绪"}
            default_name = "基金监控数据备份_%s.zip" % datetime.now().strftime("%Y%m%d_%H%M%S")
            result = w.create_file_dialog(
                webview.SAVE_DIALOG,
                save_filename=default_name,
                file_types=("ZIP 压缩包 (*.zip)",),
            )
            if not result:
                return {"ok": False, "msg": "已取消"}
            zip_path = result if isinstance(result, str) else str(result)
            if not zip_path.lower().endswith(".zip"):
                zip_path += ".zip"
            files = self._backup_files_map()
            added = []
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for name, path in files.items():
                    if os.path.exists(path):
                        zf.write(path, name)
                        added.append(name)
            return {"ok": True, "msg": "已导出 %d 个数据文件 → %s" % (len(added), os.path.basename(zip_path)), "path": zip_path}
        except Exception as e:
            return {"ok": False, "msg": "导出失败: %s" % str(e)[:150]}

    def import_data(self):
        """从 zip 还原基础数据 json（弹打开对话框），还原后重载内存数据"""
        try:
            import zipfile
            w = self._main_window
            if w is None:
                return {"ok": False, "msg": "主窗口未就绪"}
            result = w.create_file_dialog(
                webview.OPEN_DIALOG,
                file_types=("ZIP 压缩包 (*.zip)",),
                allow_multiple=False,
            )
            if not result:
                return {"ok": False, "msg": "已取消"}
            zip_path = result[0] if isinstance(result, (list, tuple)) else str(result)
            files = self._backup_files_map()
            restored = []
            with zipfile.ZipFile(zip_path, "r") as zf:
                for name in zf.namelist():
                    base = os.path.basename(name)
                    if base in files:
                        # 写入前备份旧文件
                        try:
                            if os.path.exists(files[base]):
                                shutil.copy2(files[base], files[base] + ".bak")
                        except Exception:
                            pass
                        with zf.open(name) as src, open(files[base], "wb") as dst:
                            dst.write(src.read())
                        restored.append(base)
            # 重新加载内存数据 + 清缓存
            with self._state_lock:
                self.data = load_data()
                self.idle_cash = load_idle_cash()
                self._rate_history = load_rate_history()
            if fa is not None:
                try:
                    fa._history_cache.clear()
                    fa._holdings_cache.clear()
                except Exception:
                    pass
            self.refresh()
            self.push()
            return {"ok": True, "msg": "已还原 %d 个数据文件，持仓/行情已刷新" % len(restored), "files": restored}
        except Exception as e:
            return {"ok": False, "msg": "导入失败: %s" % str(e)[:150]}

    def test_connection(self, api_key, model=None, base_url=None):
        """测试 LLM API 连接：用当前表单配置发一条最小请求（不保存，仅验证）"""
        if fa is None:
            return {"ok": False, "msg": "fund_analysis 模块未加载"}
        cfg = fa.load_config()
        if model: cfg["model"] = model
        if base_url: cfg["base_url"] = base_url
        if api_key: cfg["api_key"] = api_key
        if not cfg.get("api_key"):
            return {"ok": False, "msg": "API Key 为空"}
        try:
            import time as _t
            t0 = _t.time()
            from openai import OpenAI
            client = OpenAI(
                api_key=cfg["api_key"],
                base_url=cfg.get("base_url") or "https://api.deepseek.com",
            )
            model_name = cfg.get("model") or "deepseek-v4-pro"
            kwargs = dict(model=model_name,
                          messages=[{"role": "user", "content": "ping"}],
                          max_tokens=8)
            if "deepseek" in model_name.lower():
                kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
            r = client.chat.completions.create(**kwargs)
            elapsed = int((_t.time() - t0) * 1000)
            content = (r.choices[0].message.content or "").strip()
            if not content:
                return {"ok": False, "msg": "连接成功但返回空内容（可能思考模式未关）", "elapsed_ms": elapsed}
            return {"ok": True, "model": model_name, "elapsed_ms": elapsed, "reply": content[:40]}
        except Exception as e:
            return {"ok": False, "msg": "LLM 调用失败: %s" % str(e)[:200]}

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

                trade_ctx = fa.build_trade_review_context()
                pred_ctx = fa.build_prediction_review_context()
                result = fa.analyze_fund(
                    code, name, amount, shares, gz, pct, progress_cb=progress,
                    signal_context=self._get_signal_context(code),
                    trade_review_context=trade_ctx,
                    prediction_review_context=pred_ctx)
                self._tasks[task_id] = {
                    "status": "done" if result.get("ok") else "error",
                    "progress": 100,
                    "result": result,
                }
                if result.get("ok"):
                    # 复盘联动：把喂给 AI 的复盘经验带回前端展示（结论依据来源）
                    result["review_context"] = {"trade": trade_ctx, "prediction": pred_ctx}
                    result["report"]["forecast_date"] = fa.compute_forecast_date()
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
                anchor = fa.ANCHOR_MAP.get(code)
                if anchor:
                    adir = fa.anchor_mom12_dir(anchor)
                    if adir in ("UP", "DOWN"):
                        metrics["anchor_mom12_dir"] = adir
                        metrics["anchor_symbol"] = anchor
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
                                        trade_review_context=fa.build_trade_review_context(),
                                        prediction_review_context=fa.build_prediction_review_context())
                    results.append(r)
                    if r.get("ok"):
                        r["review_context"] = {"trade": None, "prediction": None}  # 组合层统一展示
                        r["report"]["forecast_date"] = fa.compute_forecast_date()
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
            combo_trade_ctx = fa.build_trade_review_context()
            combo_pred_ctx = fa.build_prediction_review_context()
            portfolio_result = fa.analyze_portfolio(funds, self.idle_cash,
                                                    signal_contexts=sig_ctxs or None,
                                                    trade_review_context=combo_trade_ctx,
                                                    prediction_review_context=combo_pred_ctx)
            if portfolio_result.get("ok"):
                portfolio_result["report"]["forecast_date"] = fa.compute_forecast_date()
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
                           "review_context": {"trade": combo_trade_ctx, "prediction": combo_pred_ctx},
                           "analyzed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}}

        threading.Thread(target=worker, daemon=True).start()
        return {"ok": True, "task_id": task_id}

    def analyze_midterm_all(self):
        """异步生成全部持仓的中长期分析（约 1 个月），返回 task_id"""
        if fa is None:
            return {"ok": False, "msg": "fund_analysis 模块未加载"}
        if not fa.is_configured():
            return {"ok": False, "msg": "请先在「设置」里配置 API key"}
        codes = list(self.data.keys())
        if not codes:
            return {"ok": False, "msg": "暂无持仓，请先在持仓页添加基金"}
        task_id = uuid.uuid4().hex[:8]
        self._tasks[task_id] = {"status": "running", "progress": 0,
                                "msg": "准备生成中长期分析..."}
        total = len(codes)

        def worker():
            try:
                funds = []
                for i, code in enumerate(codes):
                    d = self.data[code]
                    info = self.info.get(code, {})
                    name = d.get("name") or info.get("name") or code
                    gz = info.get("gz")
                    shares = d.get("shares", 0) or 0
                    value = shares * gz if shares and gz else 0
                    self._tasks[task_id] = {"status": "running",
                                            "progress": round(i * 20.0 / total, 1),
                                            "msg": f"汇总持仓数据 [{i+1}/{total}]..."}
                    history = fa.fetch_history(code, 90)
                    metrics = fa.compute_metrics(history)
                    holdings = fa.fetch_holdings(code)
                    funds.append({"code": code, "name": name, "value": value,
                                  "gz_pct": info.get("gz_pct"),
                                  "ref_nav": gz, "latest_gz": gz,
                                  "metrics": metrics, "holdings": holdings})

                def progress(msg, pct_v):
                    self._tasks[task_id] = {"status": "running",
                                            "progress": round(20 + pct_v * 0.75, 1),
                                            "msg": msg}

                r = fa.analyze_midterm(funds, progress_cb=progress)
                self._tasks[task_id] = {"status": "done", "progress": 100,
                                        "result": r}
            except Exception as e:
                self._tasks[task_id] = {"status": "error", "progress": 100,
                                        "msg": f"异常: {str(e)[:150]}"}

        threading.Thread(target=worker, daemon=True).start()
        return {"ok": True, "task_id": task_id}

    def get_midterm_state(self, target_date=None):
        if fa is None:
            return {"ok": False, "msg": "模块未加载"}
        st = fa.get_midterm_state(target_date)
        if not st.get("ok"):
            return st
        # 补全历史记录里缺失的基金名（旧数据无 name 字段，用持仓名回填）
        try:
            if st.get("latest"):
                for code, spec in (st["latest"].get("funds") or {}).items():
                    if isinstance(spec, dict) and not spec.get("name"):
                        spec["name"] = (self.data.get(code, {}).get("name")
                                        or self.info.get(code, {}).get("name") or code)
        except Exception:
            pass
        return st

    def review_midterm(self, target_date):
        if fa is None:
            return {"ok": False, "msg": "模块未加载"}
        return fa.review_midterm(str(target_date).strip())

    def get_task_status(self, task_id):
        return self._tasks.get(task_id, {"status": "unknown"})

    def list_history_dates(self):
        """返回所有预测目标日（对哪天的预测），倒序，供复盘/历史按目标日选择"""
        if fa is None:
            return []
        return fa.list_forecast_dates()

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

    def get_today_analysis(self):
        """返回今天已保存的分析结果（重启后可重新渲染，实现"今日分析留在上面"）

        恢复时重建「量化配置 / 信号联动 / 复盘联动」三个区块，与分析完成时的
        完整展示保持一致——否则重启后只显示基础逐只分析，看起来像"旧版本的分析"。
        """
        if fa is None:
            return {"ok": False, "msg": "fund_analysis 模块未加载"}
        today = datetime.now().strftime("%Y-%m-%d")
        h = fa.get_history(today) or {}
        predictions = h.get("predictions", {}) or {}
        portfolio = h.get("portfolio")
        if not predictions and not portfolio:
            return {"ok": True, "has": False}
        results = []
        for code, pred in predictions.items():
            if not isinstance(pred, dict):
                continue
            name = (self.data.get(code, {}).get("name")
                    or self.info.get(code, {}).get("name") or code)
            results.append({"ok": True, "code": code, "name": name, "report": pred})
        # ---- 重建完整版联动区块（与 analyze_all 展示一致） ----
        allocation = signal_summary = review_context = None
        try:
            funds = []
            for code, d in self.data.items():
                info = self.info.get(code, {})
                gz = info.get("gz")
                shares = d.get("shares", 0) or 0
                funds.append({"code": code, "name": d.get("name") or info.get("name") or code,
                              "value": shares * gz if shares and gz else 0,
                              "gz_pct": info.get("gz_pct")})
            allocation = fa.compute_allocation(funds, self.idle_cash)
        except Exception:
            pass
        try:
            sigs = fa.load_signals()
            sig_closed = [s for s in sigs if s.get("status") in ("兑现", "证伪")]
            sig_active = [s for s in sigs if s.get("status") in ("active", "强化", "弱化")]
            sig_correct = sum(1 for s in sigs if s.get("outcome") == "correct")
            signal_summary = {
                "total": len(sigs), "active": len(sig_active), "closed": len(sig_closed),
                "correct": sig_correct,
                "hit_rate": round(sig_correct / len(sig_closed) * 100, 1) if sig_closed else None,
            }
        except Exception:
            pass
        try:
            review_context = {"trade": fa.build_trade_review_context(),
                              "prediction": fa.build_prediction_review_context()}
        except Exception:
            pass
        return {
            "ok": True,
            "has": True,
            "type": "full",
            "portfolio": {"ok": True, "report": portfolio} if portfolio else None,
            "results": results,
            "ok_count": len(results),
            "total": len(results),
            "allocation": allocation,
            "signal_summary": signal_summary,
            "review_context": review_context,
            "analyzed_at": today,
        }

    def get_history_full(self, fd):
        """返回对某目标日的完整预测（组合 + 每只基金，跨分析日聚合，取最新分析日）"""
        if fa is None:
            return None
        h = fa.get_history() or {}
        items = []
        portfolio = None
        seen = set()
        for dstr in sorted(h.keys(), reverse=True):
            day = h[dstr] or {}
            for code, pred in (day.get("predictions") or {}).items():
                if fa._fd_of(pred, dstr) == fd and code not in seen:
                    seen.add(code)
                    d = self.data.get(code, {})
                    items.append({"code": code, "name": d.get("name") or code, "report": pred})
            pf_rec = day.get("portfolio")
            if pf_rec and fa._fd_of(pf_rec, dstr) == fd and portfolio is None:
                portfolio = pf_rec
        if not items and not portfolio:
            return None
        return {
            "date": fd,
            "portfolio": portfolio,
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
        """AI 复盘待复盘的加减仓建议：如果当时听从了建议会盈利还是亏损

        按目标日（forecast_date）判断：
        - 历史漏网（目标日早于今天，如昨天/前天没自动复盘）：任何时间点击即可补复盘（净值早已更新）
        - 对今天（目标日=今天）：需 23:00 后（等当日净值更新完）
        - 未来（目标日晚于今天）：需等该日收盘后
        只在开市日复盘；休市日不复盘。旧数据无 forecast_date 的，按生成日 < 今天（原逻辑）。
        """
        if fa is None:
            return {"ok": False, "msg": "模块未加载"}
        if not fa.is_configured():
            return {"ok": False, "msg": "请先配置 LLM API key"}
        if not is_market_open_today():
            return {"ok": False, "msg": "今天休市，加减仓建议将在下一个开市日收盘后自动复盘"}
        reviews = fa.load_trade_reviews()
        today = datetime.now().strftime("%Y-%m-%d")
        now_hour = datetime.now().hour

        def _due(r):
            fd = str(r.get("forecast_date") or "").strip()
            if fd:
                if fd < today:
                    return True              # 历史漏网：随时补复盘（净值已更新）
                if fd == today:
                    return now_hour >= 23    # 对今天：等当日净值更新完（23:00 后）
                return False                 # 未来
            return str(r.get("date", "")) < today  # 旧数据：生成日早于今天

        pending = [r for r in reviews
                   if r.get("status") == "pending" and _due(r)]
        if not pending:
            future = [r for r in reviews
                      if r.get("status") == "pending"
                      and str(r.get("forecast_date") or r.get("date", "")) >= today]
            if future:
                fd = future[0].get("forecast_date") or future[0].get("date")
                return {"ok": False, "msg": f"对 {fd} 的加减仓建议需等该日收盘后（23:00）自动复盘"}
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
                target_nav = None
                if code:
                    try:
                        info = fetch_batch([code]).get(code)
                        if info:
                            # T+N 净值新鲜度校验：当前行情净值日期必须 ≥ 建议目标日，
                            # 否则（QDII 等净值滞后 1-2 天）跳过，避免用旧净值算出错误盈亏
                            fd = str(r.get("forecast_date") or r.get("date", "")).strip()
                            qd = str(info.get("qdate") or "").strip()
                            if fd and qd and qd < fd:
                                results.append({"id": r.get("id", ""), "name": r.get("name", ""),
                                                "result": "净值未更新",
                                                "pnl_pct": None,
                                                "note": f"当前净值日期 {qd} 早于目标日 {fd}（T+N 滞后），待更新后自动复盘"})
                                continue
                            quote = f"{info.get('name','')} 净值 {info.get('gz')} 今日 {info.get('gz_pct')}%"
                            target_nav = info.get("gz")
                    except Exception:
                        pass
                rv = fa.review_trade_advice(r, quote, target_nav=target_nav)
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

    def review_all(self, fd):
        """异步按目标日复盘：对 fd 的预测（跨分析日聚合）+ 组合预测（对 fd 的最近一份）"""
        if fa is None:
            return {"ok": False, "msg": "fund_analysis 模块未加载"}
        task_id = uuid.uuid4().hex[:8]
        self._tasks[task_id] = {"status": "running", "progress": 10, "msg": "复盘中..."}

        def worker():
            try:
                result = fa.review_all_predictions(fd)
                if result.get("ok"):
                    # 补充基金名称
                    for r in result["results"]:
                        d = self.data.get(r.get("code"), {})
                        r["name"] = d.get("name") or r.get("code")

                    # 组合复盘：取最近一份「对 fd 的组合预测」，对比 fd 当天持仓市值加权实际
                    combo_pred = None
                    hist_all = fa.get_history() or {}
                    for dstr in sorted(hist_all.keys(), reverse=True):
                        pf_rec = hist_all[dstr].get("portfolio")
                        if pf_rec and fa._fd_of(pf_rec, dstr) == fd:
                            combo_pred = pf_rec
                            break
                    if combo_pred:
                        total_val, weighted = 0.0, 0.0
                        for code, d in self.data.items():
                            gz = (self.info.get(code, {}) or {}).get("gz")
                            shares = d.get("shares", 0) or 0
                            value = shares * gz if shares and gz else 0
                            if value <= 0:
                                continue
                            h = fa.fetch_history(code, 60)
                            pct = next((x.get("pct") for x in h if x.get("date") == fd), None)
                            if pct is not None:
                                total_val += value
                                weighted += value * pct
                        if total_val > 0:
                            combo_pct = round(weighted / total_val, 4)
                            result["portfolio_review"] = fa.review_portfolio(
                                combo_pred, combo_pct, fd)

                    # 配置了 LLM 则总结整体偏差原因
                    self._tasks[task_id] = {"status": "running", "progress": 80,
                                            "msg": "AI 分析偏差原因..."}
                    result["deviation_reason"] = fa.summarize_review(fd, result)
                    # 预测复盘闭环：提炼方向+幅度经验教训，缓存喂给下次分析
                    self._tasks[task_id] = {"status": "running", "progress": 90,
                                            "msg": "提炼预测经验教训..."}
                    fa.summarize_prediction_lessons(fd, result)
                    # 复盘结果持久化：存到 review_results.json，下次打开直接显示，不用重新复盘
                    try:
                        _cache = fa.load_review_results()
                        _cache[fd] = result
                        fa.save_review_results(_cache)
                    except Exception:
                        pass
                self._tasks[task_id] = {
                    "status": "done" if result.get("ok") else "error",
                    "progress": 100, "result": result}
            except Exception as e:
                self._tasks[task_id] = {
                    "status": "error", "progress": 100,
                    "msg": f"复盘异常: {str(e)[:200]}"}

        threading.Thread(target=worker, daemon=True).start()
        return {"ok": True, "task_id": task_id}

    def get_review_result(self, fd):
        """返回已保存的复盘结果（review_results.json 按目标日缓存），打开复盘页直接显示"""
        if fa is None:
            return {"ok": False, "msg": "模块未加载"}
        try:
            r = fa.load_review_results().get(fd)
            if r:
                return {"ok": True, "cached": True, "result": r}
        except Exception:
            pass
        return {"ok": False, "msg": "该日暂无已保存的复盘结果"}


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
.btn-export{background:linear-gradient(135deg,#0a84ff,#5e5ce6)}
.btn-import{background:linear-gradient(135deg,#32d74b,#0a84ff)}
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
      <div class="k" id="k-profit">今日估算收益</div>
      <div class="v" id="profit">--</div>
      <div class="sub" id="profit-sub">盘中估值仅供参考 · 红涨绿跌</div>
    </div>
    <div class="card main">
      <div class="k" id="k-rate">今日估算收益率</div>
      <div class="v" id="rate">--</div>
      <div class="sub" id="estnote">每 30 秒自动刷新</div>
    </div>
    <div class="card main">
      <div class="k">下一交易日预测</div>
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
      <div class="k">预测准确率（方向/综合）</div>
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
    <button class="btn btn-export" onclick="doExport()" title="把基础数据 json 压缩成 zip 备份">⬇ 导出数据</button>
    <button class="btn btn-import" onclick="doImport()" title="从 zip 还原基础数据 json">⬆ 导入数据</button>
    <span class="tip">单击选中行 · 点表头排序 · ✎编辑改金额 · 删除需二次确认</span>
  </div>

  <div class="tablewrap">
    <table>
      <thead><tr>
        <th class="sortable" onclick="sortBy('code')">代码<span class="sarr" id="sarr-code"></span></th>
        <th class="sortable" onclick="sortBy('name')">基金名称<span class="sarr" id="sarr-name"></span></th>
        <th class="sortable" onclick="sortBy('pct')">估算涨幅<span class="sarr" id="sarr-pct"></span></th>
        <th class="sortable" onclick="sortBy('pred')">次日预测<span class="sarr" id="sarr-pred"></span></th>
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
      <button class="ana-tab" id="atab-midterm" onclick="switchAnaTab('midterm')">中长期</button>
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
        <label>选择预测目标日：</label>
        <select id="rev_date" onchange="loadCachedReview(this.value)"></select>
        <button class="btn btn-add" onclick="doReviewAll()" style="background:linear-gradient(135deg,var(--down),var(--down))">📋 复盘对所选日的全部预测</button>
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

    <div id="ana-pane-midterm" style="display:none">
      <div class="ana-bar">
        <button class="btn btn-add" onclick="doMidtermAnalyze()" style="background:linear-gradient(135deg,var(--brand),var(--purple))">🤖 生成中长期分析（约 1 个月）</button>
        <button class="btn btn-add" onclick="doMidtermReview()" style="background:linear-gradient(135deg,var(--down),var(--down))">📋 复盘最新一期</button>
        <span style="font-size:12px;color:var(--sub)">目标日 = 分析日起 20 个交易日</span>
      </div>
      <div id="mt-progress" class="sig-progress" style="display:none">
        <div class="bar-bg"><div id="mt-progress-fill" class="bar-fg" style="width:0%"></div></div>
        <div id="mt-progress-msg" class="msg">初始化...</div>
      </div>
      <div id="midterm-report" class="ana-report"></div>
      <div style="margin-top:18px;border-top:1px solid var(--line);padding-top:10px">
        <div style="font-weight:600;margin-bottom:8px">📅 历史记录</div>
        <div id="midterm-history" class="history-list"></div>
      </div>
    </div>
  </div>
</div><!-- /app -->

<div class="mask" id="mask">
  <div class="modal">
    <h3 id="m_title">买入</h3>
    <p class="note" id="m_desc"></p>
    <div class="fld" id="fld_amt" style="display:none"><label>① API Key（必填）</label></div>
    <input id="m_amt" placeholder="sk-..." onkeydown="if(event.key==='Enter')saveModal()">
    <input id="m_amt2" placeholder="累计收益（元）" style="display:none" onkeydown="if(event.key==='Enter')saveModal()">
    <div class="fld" id="fld_proxy" style="display:none"><label>② 代理地址（非必要，网络异常时可填）</label>
      <input id="m_proxy" placeholder="如 10.110.32.68:7897，留空清除" onkeydown="if(event.key==='Enter')saveModal()">
    </div>
    <div class="fld" id="fld_base_url" style="display:none"><label>③ API 地址（OpenAI 兼容）</label>
      <input id="m_base_url" placeholder="如 https://api.deepseek.com" onkeydown="if(event.key==='Enter')saveModal()">
    </div>
    <div class="fld" id="fld_model" style="display:none"><label>④ 模型名称</label>
      <input id="m_model" placeholder="如 deepseek-v4-pro" onkeydown="if(event.key==='Enter')saveModal()">
    </div>
    <div class="row">
      <button class="cancel" onclick="closeModal()">取消</button>
      <button class="ok" id="btnTest" onclick="testConnection()" style="display:none">测试连接</button>
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

// 信心展示层降级：AI 自评某档位，但历史该档位实际正确率低时标注「不可靠」
// （只加标注，不改写 confidence 字段，避免污染复盘统计口径）
function confTag(level){
  const cc=(state&&state.confidence_calibration)||[];
  if(!cc.length) return '';
  const m=cc.find(function(x){return x.level===level});
  if(!m) return '';
  const r=m.direction_correct_rate;
  if(r===undefined||r===null) return '';
  if(r<50) return ' <span style="color:var(--warn);font-size:11px" title="历史该档位实际方向正确率 '+r+'%">⚠不可靠</span>';
  return '';
}

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
function fdTxt(fd){return fd?' <span style="color:var(--sub);font-size:11px">对'+String(fd).slice(5)+'</span>':''}
function pctTag(f){
  if(!f) return '';
  if(f.est){
    if(f.est_pending) return '<small class="stale">收盘未更新</small>';
    return '<small class="stale">预估</small>';
  }
  const d=new Date();
  const t=String(d.getFullYear())+'-'+String(d.getMonth()+1).padStart(2,'0')+'-'+String(d.getDate()).padStart(2,'0');
  if(f.qdate===t) return '<small>收盘</small>';
  // T+N>1（QDII 等）：官方净值滞后是常态，qdate 永远不是今天，官方最新数据即「昨日收盘」
  if((f.confirm_days||1)>1) return '<small class="stale">昨日收盘</small>';
  // 今天已收盘但净值尚未公布（收盘后-晚间净值更新前，qdate 还是昨天）→ 不再误标「昨日收盘」
  if(state&&state.market_open&&d.getHours()>=15) return '<small class="stale">收盘待更新</small>';
  return '<small class="stale">昨日收盘</small>';
}

function render(st){
  state=st;
  const masked=!!st.mask;
  const mb=document.getElementById('maskBtn');
  if(mb) mb.textContent=masked?'👁 显示金额':'🙈 隐藏金额';
  // 收益卡片标题动态化：盘中=今日估算；收盘后净值未更新=收盘收益(预估)；收盘后净值已出=官方收盘；未开盘=昨日收盘
  const fl=st.funds||[];
  const anyEst=fl.some(f=>f.est);
  const pendingEst=fl.some(f=>f.est&&f.est_pending);
  const todayStr=String(new Date().getFullYear())+'-'+String(new Date().getMonth()+1).padStart(2,'0')+'-'+String(new Date().getDate()).padStart(2,'0');
  const allClosed=fl.length>0 && !anyEst;
  const closedToday=!!(st.market_open && new Date().getHours()>=15);
  const hasTodayNav=fl.some(f=>f.qdate===todayStr);
  let kp='今日估算收益', ks='盘中估值仅供参考 · 红涨绿跌';
  if(pendingEst){ kp='收盘收益'; ks='今日已收盘 · 净值未更新（预估）'; }
  else if(anyEst){ kp='今日估算收益'; ks='盘中估值仅供参考 · 红涨绿跌'; }
  else if(fl.length>0 && allClosed && hasTodayNav){ kp='收盘收益'; ks='今日已收盘 · 官方净值'; }
  else if(fl.length>0 && allClosed && closedToday){ kp='收盘收益'; ks='今日已收盘 · 净值待更新'; }
  else if(fl.length>0){ kp='昨日收盘收益'; ks='今日未开盘 · 最新净值'; }
  const kpEl=document.getElementById('k-profit');
  if(kpEl){kpEl.textContent=kp;}
  const ksEl=document.getElementById('profit-sub');
  if(ksEl){ksEl.textContent=ks;}
  const krEl=document.getElementById('k-rate');
  if(krEl){krEl.textContent=kp==='今日估算收益'?'今日估算收益率':(kp==='收盘收益'?'收盘收益率':'昨日收盘收益率');}
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

  // 下一交易日预测（组合层面，锚定成交日次日）
  const pf=document.getElementById('pf-forecast');
  if(st.portfolio_pred){
    pf.innerHTML='<span class="badge '+dirCls(st.portfolio_pred.direction)+'" style="font-size:14px;padding:3px 10px">'+esc(st.portfolio_pred.direction||'-')+'</span> '+esc(st.portfolio_pred.expected_pct||'-')+fdTxt(st.portfolio_pred.forecast_date);
    pf.className='v '+dirCls(st.portfolio_pred.direction);
    document.getElementById('pf-detail').innerHTML='信心 '+esc(st.portfolio_pred.confidence||'-')+confTag(st.portfolio_pred.confidence);
  }else{
    pf.textContent='--';
    pf.className='v';
    document.getElementById('pf-detail').textContent='暂无今日分析';
  }

  // 预测准确率（始终显示，带日期）——方向准确率 / 综合准确率
  const accAvg=document.getElementById('acc-avg');
  if(st.review_summary){
    const rs=st.review_summary;
    const dirRate=(rs.total?Math.round(rs.direction_correct/rs.total*100):0);
    accAvg.innerHTML='<span style="font-size:24px">'+dirRate+'%</span><span style="font-size:11px;color:var(--sub);margin:0 4px">/</span><span style="font-size:18px;color:var(--sub)">'+rs.avg_accuracy+'%</span>';
    accAvg.className='v '+(dirRate>=80?'up':(dirRate>=50?'flat':'down'));
    document.getElementById('acc-detail').textContent='对'+String(rs.date).slice(5)+'日 · 方向 '+rs.direction_correct+'/'+rs.total+' · 综合(方向50+幅度50)';
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
      '<td class="pct '+cls(f.pct)+'">'+(f.pct==null?'--':sgn(f.pct)+'%')+pctTag(f)+'</td>'+
      '<td>'+(f.today_pred?'<span class="badge '+dirCls(f.today_pred.direction)+'">'+esc(f.today_pred.direction||'')+'</span> '+esc(f.today_pred.expected_pct||'')+fdTxt(f.today_pred.forecast_date):'<span style="color:var(--sub)">--</span>')+'</td>'+
      '<td>'+(f.value?fmt(f.value):'--')+'</td>'+
      '<td>'+ratioCell+'</td>'+
      '<td class="pct '+cls(f.profit)+'">'+(f.profit==null?'--':(f.value?sgn(f.profit):'--'))+'</td>'+
      '<td class="pct '+cls(f.realized)+'">'+(f.has_nav&&f.realized?sgn(f.realized):'--')+'</td>'+
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

async function doExport(){
  const r=await pywebview.api.export_data();
  toast(r.ok?('✅ '+r.msg):('❌ '+(r.msg||'导出失败')), !r.ok);
}

function doImport(){
  // 导入会覆盖数据，用自定义 modal 二次确认（WebView2 下 confirm 不可用）
  document.getElementById('m_title').textContent='导入数据';
  document.getElementById('m_desc').textContent='从 zip 还原基础数据 json（持仓/闲钱/信号/复盘/历史），将覆盖当前数据。确认导入？';
  const inp=document.getElementById('m_amt');
  document.getElementById('m_amt2').style.display='none';
  document.getElementById('fld_amt').style.display='none';
  document.getElementById('fld_proxy').style.display='none';
  document.getElementById('fld_base_url').style.display='none';
  document.getElementById('fld_model').style.display='none';
  document.getElementById('btnTest').style.display='none';
  inp.style.display='none';
  inp.dataset.mode='';
  modalMode='import';
  document.getElementById('mask').classList.add('show');
}

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
  document.getElementById('m_base_url').style.display='none';
  document.getElementById('fld_amt').style.display='none';
  document.getElementById('fld_proxy').style.display='none';
  document.getElementById('fld_base_url').style.display='none';
  document.getElementById('fld_model').style.display='none';
  document.getElementById('btnTest').style.display='none';
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
    const model = (document.getElementById('m_model').value||'').trim() || 'deepseek-v4-pro';
    const base_url = (document.getElementById('m_base_url').value||'').trim() || '';
    const proxy = (document.getElementById('m_proxy').value||'').trim();
    const r = await pywebview.api.save_analysis_config(key, model, base_url);
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
  if(modalMode==='import'){
    closeModal();
    const r=await pywebview.api.import_data();
    toast(r.ok?('✅ '+r.msg):('❌ '+(r.msg||'导入失败')), !r.ok);
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

async function refreshMidtermState(){
  const rep=document.getElementById('midterm-report');
  if(!rep) return;
  try{
    const st=await pywebview.api.get_midterm_state();
    if(!st.ok){ rep.innerHTML='<div class="empty-state">'+esc(st.msg||'加载失败')+'</div>'; return; }
    if(st.latest){
      rep.innerHTML=renderMidtermReport(st.latest);
    }else{
      rep.innerHTML='<div class="empty-state">暂无中长期分析。点上方「🤖 生成中长期分析」创建第一份（约 1 个月视角，覆盖全部持仓）。</div>';
    }
  }catch(e){ rep.innerHTML='<div class="empty-state">加载失败：'+esc(e.message||e)+'</div>'; }
}

function renderMidtermReport(e){
  const funds=e.funds||{};
  const codes=Object.keys(funds);
  const trendCls=t=>t.indexOf('多')>=0?'up':(t.indexOf('空')>=0?'down':'flat');
  let html='';
  html+='<div class="panel" style="border:1px solid var(--line);border-radius:16px;padding:14px;margin-bottom:12px">';
  html+='<div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px">';
  html+='<div style="font-weight:600">📈 组合中长期策略<span style="font-size:11px;color:var(--sub);margin-left:8px">目标日 '+esc(e.target_date||'')+'（约 1 个月）</span></div>';
  html+='<span style="font-size:11px;color:var(--sub)">'+esc(e.analyzed_at||'')+'</span></div>';
  html+='<div style="margin-top:10px"><span class="badge '+trendCls(e.portfolio_trend||'')+'">'+esc(e.portfolio_trend||'-')+'</span> '
      +'<span style="font-size:12px;color:var(--sub);margin-left:6px">仓位建议：'+esc(e.portfolio_position||'-')+'</span></div>';
  html+='<div style="margin-top:8px;font-size:13px;color:var(--txt)">'+esc(e.summary||'')+'</div>';
  if((e.sector_advice||[]).length){
    html+='<div style="margin-top:10px;font-size:12px;color:var(--sub)">🏷 板块配置：</div><div style="display:flex;flex-wrap:wrap;gap:6px;margin-top:4px">';
    html+=(e.sector_advice||[]).map(s=>
      '<span style="border:1px solid var(--line);border-radius:8px;padding:3px 8px;font-size:12px">'
      +esc(s.sector||'')+' <b>'+(String(s.action||'').indexOf('加')>=0?'<span style="color:var(--up)">'+esc(s.action||'')+'</span>':(String(s.action||'').indexOf('减')>=0?'<span style="color:var(--down)">'+esc(s.action||'')+'</span>':esc(s.action||'')))+'</b>'
      +(s.suggest_pct?' <span style="color:var(--sub)">'+esc(s.suggest_pct)+'</span>':'')
      +'<div style="font-size:11px;color:var(--sub)">'+esc(s.reason||'')+'</div></span>').join('');
    html+='</div>';
  }
  if((e.key_risks||[]).length) html+='<div style="margin-top:8px;font-size:12px;color:var(--down)">⚠ 风险：'+esc((e.key_risks||[]).join('；'))+'</div>';
  if((e.key_catalysts||[]).length) html+='<div style="margin-top:4px;font-size:12px;color:var(--up)">⚡ 催化：'+esc((e.key_catalysts||[]).join('；'))+'</div>';
  html+='</div>';

  if(e.review && e.review.ok){
    const rv=e.review;
    html+='<div class="panel" style="border:1px solid var(--line);border-radius:16px;padding:14px;margin-bottom:12px">';
    html+='<div style="font-weight:600;margin-bottom:8px">📋 已复盘（'+esc(rv.reviewed_at||'')+'）</div>';
    html+='<div style="display:flex;gap:18px;flex-wrap:wrap">';
    html+='<div><span class="badge '+(rv.direction_rate>=50?'up':'down')+'" style="font-size:16px">'+esc(rv.direction_rate==null?'-':rv.direction_rate+'%')+'</span><div style="font-size:11px;color:var(--sub)">方向正确率（'+esc(rv.direction_correct||0)+'/'+esc(rv.direction_total||0)+'）</div></div>';
    html+='<div><span class="badge '+(rv.range_rate>=50?'up':'down')+'" style="font-size:16px">'+esc(rv.range_rate==null?'-':rv.range_rate+'%')+'</span><div style="font-size:11px;color:var(--sub)">区间命中率（'+esc(rv.range_hit||0)+'/'+esc(rv.range_total||0)+'）</div></div>';
    html+='</div></div>';
  }

  html+='<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:10px">';
  for(const code of codes){
    const f=funds[code]||{};
    const rv=(e.review&&e.review.results||[]).find(x=>x.code===code);
    html+='<div style="border:1px solid var(--line);border-radius:14px;padding:12px">';
    html+='<div style="display:flex;justify-content:space-between;align-items:center;gap:6px"><span style="font-weight:600;flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="'+esc(code)+'">'+esc(f.name||code)+'</span>'
        +'<span style="font-size:11px;color:var(--sub)">'+esc(code)+'</span>'
        +'<span class="badge '+trendCls(f.trend||'')+'">'+esc(f.trend||'-')+'</span></div>';
    html+='<div style="margin-top:6px;font-size:13px"><b>目标区间：</b>'+esc(f.target_range||'-')+'　<b>预期收益：</b>'
        +'<span class="'+(String(f.expected_ret||'').indexOf('-')>=0?'down':'up')+'">'+esc(f.expected_ret||'-')+'%</span></div>';
    html+='<div style="font-size:12px;color:var(--sub);margin-top:3px">关键位：'+esc(f.key_levels||'-')+'</div>';
    html+='<div style="font-size:12px;color:var(--sub);margin-top:3px">仓位：'+esc(f.position_advice||'-')+' · 信心 '+esc(f.confidence||'-')+'</div>';
    if(f.stop_loss||f.take_profit){
      html+='<div style="font-size:12px;margin-top:3px">止损 <span style="color:var(--down)">'+esc(f.stop_loss||'-')+'</span> · 止盈 <span style="color:var(--up)">'+esc(f.take_profit||'-')+'</span></div>';
    }
    if(f.phase_plan) html+='<div style="font-size:12px;margin-top:6px;padding:6px 8px;background:var(--panel2);border-radius:8px">🗓 '+esc(f.phase_plan)+'</div>';
    if((f.key_drivers||[]).length) html+='<div style="font-size:12px;color:var(--sub);margin-top:4px">驱动：'+esc((f.key_drivers||[]).join('、'))+'</div>';
    html+='<div style="font-size:12px;margin-top:6px;color:var(--txt)">'+esc(f.reason||'')+'</div>';
    if(rv){
      if(rv.ok){
        const dirOk=rv.dir_ok?'✅':'❌';
        const rangeOk=rv.range_hit==null?'（区间未判定）':(rv.range_hit?'🎯 区间命中':'区间未命中');
        html+='<div style="margin-top:8px;padding-top:8px;border-top:1px dashed var(--line);font-size:12px">'
            +dirOk+' 方向'+ (rv.dir_ok?'对':'错')+' · 实际 '+esc(rv.actual_pct)+'%（'+esc(rv.ref_nav)+'→'+esc(rv.target_nav)+'）<br>'+rangeOk+'</div>';
      }else{
        html+='<div style="margin-top:8px;font-size:12px;color:var(--down)">'+esc(rv.msg||'')+'</div>';
      }
    }
    html+='</div>';
  }
  html+='</div>';
  return html;
}

async function doMidtermAnalyze(){
  const prog=document.getElementById('mt-progress');
  const rep=document.getElementById('midterm-report');
  prog.style.display='block';
  document.getElementById('mt-progress-fill').style.width='0%';
  document.getElementById('mt-progress-msg').textContent='初始化...（约 1-3 分钟）';
  const r=await pywebview.api.analyze_midterm_all();
  if(!r.ok){ toast(r.msg,true); prog.style.display='none'; return; }
  while(true){
    await new Promise(res=>setTimeout(res,700));
    const st=await pywebview.api.get_task_status(r.task_id);
    document.getElementById('mt-progress-fill').style.width=(st.progress||0)+'%';
    document.getElementById('mt-progress-msg').textContent=st.msg||'处理中...';
    if(st.status==='done'){
      prog.style.display='none';
      if(st.result && st.result.ok){
        rep.innerHTML=renderMidtermReport(st.result.result);
        refreshMidtermHistory();
        toast('中长期分析完成，目标日 '+st.result.target_date);
      }else{
        rep.innerHTML='<div class="empty-state">'+esc((st.result&&st.result.msg)||'生成失败')+'</div>';
        toast('分析失败',true);
      }
      return;
    }else if(st.status==='error'){
      prog.style.display='none';
      toast(st.msg||'分析失败',true);
      return;
    }
  }
}

async function doMidtermReview(){
  const prog=document.getElementById('mt-progress');
  prog.style.display='block';
  document.getElementById('mt-progress-fill').style.width='0%';
  document.getElementById('mt-progress-msg').textContent='复盘中...';
  try{
    const st=await pywebview.api.get_midterm_state();
    if(!st.ok || !st.latest){ prog.style.display='none'; toast('暂无中长期分析可复盘',true); return; }
    const rv=await pywebview.api.review_midterm(st.latest.target_date);
    prog.style.display='none';
    if(rv && rv.ok){
      document.getElementById('midterm-report').innerHTML=renderMidtermReport(st.latest);
      refreshMidtermHistory();
      toast('复盘完成：方向正确率 '+rv.direction_rate+'%');
    }else{
      toast((rv&&rv.msg)||'复盘失败（可能目标日未到或净值未更新）',true);
    }
  }catch(e){ prog.style.display='none'; toast('复盘异常：'+esc(e.message||e),true); }
}

async function refreshMidtermHistory(){
  const list=document.getElementById('midterm-history');
  if(!list) return;
  try{
    const st=await pywebview.api.get_midterm_state();
    if(!st.ok){ return; }
    const hist=st.history||[];
    if(hist.length===0){ list.innerHTML='<div class="empty-state">暂无历史记录</div>'; return; }
    list.innerHTML=hist.map(h=>{
      const badge=h.reviewed?'<span class="badge up" style="font-size:11px">已复盘</span>':'<span class="badge flat" style="font-size:11px">待复盘</span>';
      return '<div class="h-item" onclick="loadCachedMidterm(\''+h.target_date+'\')">'
        +'<div>📅 目标日 '+esc(h.target_date)+' · '+esc(h.portfolio_trend||'')+' · '+esc(h.summary||'')+'</div>'
        +'<div class="meta">'+esc(h.analyzed_at||'')+' '+badge+' · '+esc(h.fund_count||0)+' 只 →</div></div>';
    }).join('');
  }catch(e){}
}

async function loadCachedMidterm(fd){
  if(!fd) return;
  try{
    const st=await pywebview.api.get_midterm_state(fd);
    if(!st.ok||!st.latest) return;
    document.getElementById('midterm-report').innerHTML=renderMidtermReport(st.latest);
  }catch(e){}
}

function switchAnaTab(t){
  currentAnaTab=t;
  document.getElementById('atab-today').classList.toggle('active', t==='today');
  document.getElementById('atab-history').classList.toggle('active', t==='history');
  document.getElementById('atab-review').classList.toggle('active', t==='review');
  document.getElementById('atab-signals').classList.toggle('active', t==='signals');
  document.getElementById('atab-tradereview').classList.toggle('active', t==='tradereview');
  document.getElementById('atab-midterm').classList.toggle('active', t==='midterm');
  document.getElementById('ana-pane-today').style.display=t==='today'?'block':'none';
  document.getElementById('ana-pane-history').style.display=t==='history'?'block':'none';
  document.getElementById('ana-pane-review').style.display=t==='review'?'block':'none';
  document.getElementById('ana-pane-signals').style.display=t==='signals'?'block':'none';
  document.getElementById('ana-pane-tradereview').style.display=t==='tradereview'?'block':'none';
  document.getElementById('ana-pane-midterm').style.display=t==='midterm'?'block':'none';
  if(t==='review'){ refreshReviewDates(); }
  if(t==='history'){ refreshHistoryList(); }
  if(t==='signals'){ refreshSignals(); autoAuditSignals(); }
  if(t==='tradereview'){ refreshTradeReviews(); autoReviewTrades(); }
  if(t==='midterm'){ refreshMidtermState(); }
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
    const emptyTxt=(sigFilter==='ai')?'暂无审核记录。每次打开信号页会自动审核进行中的信号（5 小时内不重复）。'
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
  document.getElementById('m_base_url').style.display='none';
  document.getElementById('fld_amt').style.display='none';
  document.getElementById('fld_proxy').style.display='none';
  document.getElementById('fld_base_url').style.display='none';
  document.getElementById('fld_model').style.display='none';
  document.getElementById('btnTest').style.display='none';
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
      localStorage.setItem('lastAutoAuditTs',String(Date.now()));
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

const AUDIT_INTERVAL=5*60*60*1000; // 自动审核节流：5 小时只自动审一次
function getLastAuditTs(){
  const v=parseInt(localStorage.getItem('lastAutoAuditTs')||'0',10);
  return Number.isFinite(v)?v:0;
}
async function autoAuditSignals(){
  // 打开信号页自动审核进行中的信号（无需按钮），5 小时内不重复触发（重启后仍生效）
  const now=Date.now();
  if(now-getLastAuditTs()<AUDIT_INTERVAL) return;
  const r=await pywebview.api.get_signals();
  if(!r.ok) return;
  const actives=(r.signals||[]).filter(s=>['active','强化','弱化'].includes(s.status));
  if(actives.length){ localStorage.setItem('lastAutoAuditTs',String(now)); auditSignals(); }
}

function flashChangedSignals(ids){
  const set=new Set(ids||[]);
  document.querySelectorAll('.sig-card').forEach(card=>{
    if(set.has(card.dataset.id)) card.classList.add('flash');
  });
}

// ===== 加减仓复盘 =====
// 结构化经验教训渲染：兼容旧字符串格式与 v2.0.37+ 的结构化对象 {bias_type,pattern,evidence,action}
function fmtLesson(x){
  if(typeof x==='string'){
    return '<div style="font-size:12.5px;line-height:1.6;margin-bottom:3px">• '+esc(x)+'</div>';
  }
  if(x && typeof x==='object'){
    const bt=esc(x.bias_type||'其他');
    const pat=esc(x.pattern||'');
    const ev=esc(x.evidence||'');
    const ac=esc(x.action||'');
    let s='<div style="font-size:12.5px;line-height:1.65;margin-bottom:4px">• '+
      '<span class="badge flat" style="font-size:10px;margin-right:5px">'+bt+'</span>'+pat;
    if(ev) s+=' <span style="color:var(--sub);font-size:11.5px">（'+ev+'）</span>';
    if(ac) s+=' <span style="color:var(--up);font-size:11.5px">→ 改进：'+ac+'</span>';
    s+='</div>';
    return s;
  }
  return '';
}
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
      lessons.map(x=>fmtLesson(x)).join('')+
      '</div>';
  }
  // 日期筛选下拉：全部 + 去重目标日倒序（优先 forecast_date，旧数据回退 date）
  const sel=document.getElementById('tr-date-filter');
  if(sel){
    const cur=sel.value;
    const dates=[...new Set((r.reviews||[]).map(x=>x.forecast_date||x.date).filter(Boolean))].sort().reverse();
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
  const revs=(r.reviews||[]).filter(x=>!selDate||(x.forecast_date||x.date)===selDate);
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
      '<span style="font-size:11px;color:var(--sub)">'+esc(rv.date||'')+(rv.forecast_date?' · 对'+esc(rv.forecast_date.slice(5))+'日':'')+'</span></div>'+
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
  const today=r.today||'';
  // 点击页面/启动即检查：有历史待复盘（目标日早于今天，如前天/昨天漏掉的）就自动补复盘；
  // 「对今天」的建议由后端按 23:00 判断；休市日由后端拦截
  const pendings=(r.reviews||[]).filter(x=>x.status==='pending' &&
    String(x.forecast_date||x.date||'') < today);
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
    document.getElementById('m_desc').textContent='填入 API Key、API 地址、模型名称；行情/接口异常时可填代理地址。支持任意 OpenAI 兼容 API。';
    const inp = document.getElementById('m_amt');
    document.getElementById('m_amt2').style.display='none';
    inp.style.display='block';
    document.getElementById('fld_amt').style.display='block';
    document.getElementById('fld_proxy').style.display='block';
    document.getElementById('fld_base_url').style.display='block';
    document.getElementById('fld_model').style.display='block';
    document.getElementById('btnTest').style.display='block';
    const prx = document.getElementById('m_proxy');
    prx.value='';
    pywebview.api.get_proxy_config().then(r2=>{
      if(r2 && r2.ok && r2.proxy) prx.value = r2.proxy;
    });
    prx.placeholder = '如 10.110.32.68:7897，留空清除';
    // API 地址
    const urlInput = document.getElementById('m_base_url');
    urlInput.value = cfg.base_url || '';
    urlInput.placeholder = '如 https://api.deepseek.com';
    // 模型名称
    const modelInput = document.getElementById('m_model');
    modelInput.value = cfg.model || 'deepseek-v4-pro';
    modelInput.placeholder = '如 deepseek-v4-pro';
    inp.value = cfg.api_key || '';
    inp.placeholder = 'sk-...';
    inp.type = 'text';
    inp.dataset.mode='settings';
    document.getElementById('mask').classList.add('show');
    setTimeout(()=>inp.focus(),50);
  });
}

async function testConnection(){
  // 用当前表单填写的配置做一次最小 LLM 请求，验证 API 连通性（不保存）
  const key = (document.getElementById('m_amt').value||'').trim();
  const model = (document.getElementById('m_model').value||'').trim() || 'deepseek-v4-pro';
  const base_url = (document.getElementById('m_base_url').value||'').trim() || '';
  if(!key){toast('请先填写 API Key', true);return;}
  const btn=document.getElementById('btnTest');
  btn.textContent='测试中...'; btn.disabled=true;
  try{
    const r = await pywebview.api.test_connection(key, model, base_url);
    if(r && r.ok) toast('✅ 连接成功：' + (r.model||model) + '（耗时 '+(r.elapsed_ms||'-')+'ms）');
    else toast('❌ ' + ((r&&r.msg)||'连接失败'), true);
  }catch(e){ toast('❌ 调用异常：'+e, true); }
  btn.textContent='测试连接'; btn.disabled=false;
}

function editIdleCash(){
  document.getElementById('m_title').textContent='设置闲钱';
  document.getElementById('m_desc').textContent='你的闲置资金（可用于加减仓）。分析时会据此给出闲钱使用建议。';
  const inp = document.getElementById('m_amt');
  document.getElementById('m_amt2').style.display='none';
  inp.style.display='block';
  document.getElementById('fld_amt').style.display='none';
  document.getElementById('fld_proxy').style.display='none';
  document.getElementById('fld_base_url').style.display='none';
  document.getElementById('fld_model').style.display='none';
  document.getElementById('btnTest').style.display='none';
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

  // ===== 复盘联动（本次组合分析参考的历史复盘数据与修正依据） =====
  const rc = result.review_context || {};
  const rcParts=[];
  if(rc.trade) rcParts.push('<div class="k">🧾 加减仓复盘参考</div><div style="font-size:12px;color:var(--sub);line-height:1.7;white-space:pre-wrap">'+esc(rc.trade)+'</div>');
  if(rc.prediction) rcParts.push('<div class="k">📈 预测复盘参考（偏差率修正依据）</div><div style="font-size:12px;color:var(--sub);line-height:1.7;white-space:pre-wrap">'+esc(rc.prediction)+'</div>');
  if(rcParts.length){
    html += '<div class="sec" style="border-left:3px solid var(--teal)"><div class="k">🧭 复盘联动（结论依据来源：本次分析已参考以下历史复盘数据）</div>'+
      rcParts.join('')+'</div>';
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
    <div class="sec"><div class="k">整体明日预测</div><div class="v"><span class="badge ${dirCls(pf.direction)}">${esc(pf.direction||'-')}</span> ${esc(pf.expected_pct||'-')}（信心 ${esc(pf.confidence||'-')}${confTag(pf.confidence)}）${fdTxt(pf.forecast_date||result.portfolio.report.forecast_date)}</div></div>
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
        <div class="item"><div class="k">明日</div><div class="v"><span class="badge ${cls(tom.direction)}">${esc(tom.direction||'-')}</span> ${esc(tom.expected_pct||'-')}${fdTxt(rep.forecast_date)}</div></div>
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
  // 复盘联动：喂给 AI 的历史复盘经验（结论依据来源说明）
  const rc = result.review_context || {};
  let rcHtml='';
  if(rc.trade || rc.prediction){
    rcHtml='<div class="metrics" style="margin-bottom:10px;line-height:1.8">🧭 复盘联动（结论依据来源）'+
      (rc.trade?'<div style="margin-top:4px">· 加减仓复盘：'+esc(rc.trade)+'</div>':'')+
      (rc.prediction?'<div style="margin-top:4px">· 预测复盘（偏差率修正依据）：'+esc(rc.prediction)+'</div>':'')+
      '</div>';
  }

  container.innerHTML = `
    ${mHtml}
    ${sigHtml}
    ${rcHtml}
    <h3>📌 核心结论</h3>
    <div class="v">${esc(r.summary||'')} <span class="badge ${verdictClass(act.verdict)}">${esc(normVerdict(act.verdict)||'')}</span> 信心 ${r.confidence_score||0}/100</div>

    ${rolesHtml(r)}

    <h3>🔍 今日分析</h3>
    <div class="sec"><div class="k">趋势定性</div><div class="v">${esc(today.trend||'-')}</div></div>
    <div class="sec"><div class="k">关键价位</div><div class="v">${esc(today.key_levels||'-')}</div></div>
    <div class="sec"><div class="k">动量</div><div class="v">${esc(today.momentum||'-')}</div></div>
    <div class="sec"><div class="k">风险提示</div><div class="v">${esc(today.risk_flag||'-')}</div></div>
    <div class="sec"><div class="k">一句话简评</div><div class="v" style="font-weight:400">${esc(today.one_liner||'-')}</div></div>

    <h3>🔮 明日预测${fdTxt(result.report.forecast_date)}</h3>
    <div class="row">
      <div class="item"><div class="k">方向</div><div class="v"><span class="badge ${cls(tom.direction)}">${esc(tom.direction||'-')}</span></div></div>
      <div class="item"><div class="k">预期涨跌</div><div class="v">${esc(tom.expected_pct||'-')}</div></div>
      <div class="item"><div class="k">信心</div><div class="v">${esc(tom.confidence||'-')}${confTag(tom.confidence)}</div></div>
    </div>
    <div class="sec"><div class="k">预测理由</div><div class="v" style="font-weight:400">${esc(tom.reason||'-')}</div></div>

    <h3>🎯 中期策略（1-2 周）</h3>
    <div class="row">
      <div class="item"><div class="k">趋势</div><div class="v">${esc(mid.trend||'-')}</div></div>
      <div class="item"><div class="k">波动区间</div><div class="v">${esc(mid.target_range||'-')}</div></div>
      <div class="item"><div class="k">仓位建议</div><div class="v">${esc(mid.position_advice||'-')}</div></div>
      <div class="item"><div class="k">信心</div><div class="v">${esc(mid.confidence||'-')}${confTag(mid.confidence)}</div></div>
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
  list.innerHTML = '<div style="color:var(--sub);font-size:12px;padding:4px 0 10px">点击目标日查看「对当日」的全部预测详情（跨分析日聚合）</div>' +
    dates.map(d=>'<div class="h-item" onclick="showHistoryRecord(\''+d+'\')"><div>📅 对 '+String(d).slice(5)+' 日的预测</div><div class="meta">查看 →</div></div>').join('');
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
  let html = '<div style="font-weight:600;margin:10px 0 12px">📅 对 ' + esc(String(rec.date).slice(5)) + ' 日的预测（跨分析日聚合）</div>';

  if(rec.portfolio){
    const pf = (rec.portfolio.portfolio_forecast) || {};
    html += '<div class="sec" style="border:1px solid var(--brand);border-radius:10px;padding:12px 14px;margin-bottom:10px">' +
      '<div style="color:var(--brand);font-weight:600;margin-bottom:6px">📊 组合预测'+fdTxt(rec.portfolio.forecast_date)+'</div>' +
      '<div class="v"><span class="badge '+dirCls(pf.direction)+'">'+esc(pf.direction||'-')+'</span> '+esc(pf.expected_pct||'-')+'（信心 '+esc(pf.confidence||'-')+confTag(pf.confidence)+'）</div>' +
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
        '<div class="item"><div class="k">明日</div><div class="v"><span class="badge '+dirCls(tom.direction)+'">'+esc(tom.direction||'-')+'</span> '+esc(tom.expected_pct||'-')+fdTxt(rep.forecast_date)+'</div></div>' +
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
  sel.innerHTML = dates.map(d=>'<option value="'+d+'">对 '+String(d).slice(5)+' 日</option>').join('');
  // 打开复盘页：直接显示最近目标日已保存的复盘结果，不用重新复盘
  if(dates.length){
    sel.value = dates[0];
    loadCachedReview(dates[0]);
  }
}

async function loadCachedReview(fd){
  const out = document.getElementById('rev-result');
  if(!out) return;
  const r = await pywebview.api.get_review_result(fd);
  if(r && r.ok){
    // 顶部提示这是已保存的复盘结果，可点按钮强制重新复盘
    out.dataset.cached = '1';
    renderReviewAll(r.result, out);
    out.insertAdjacentHTML('afterbegin',
      '<div style="font-size:12px;color:var(--sub);margin-bottom:8px">📁 已保存的复盘结果（'+esc(fd)+'）——如需更新，点上方「复盘对所选日的全部预测」重新复盘</div>');
  } else {
    out.dataset.cached = '0';
    out.innerHTML = '<div class="empty-state">该日还没有复盘结果。点上方「📋 复盘对所选日的全部预测」生成后会自动保存，下次打开直接显示。</div>';
  }
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
      ${r.deviation_reason?'<div style="margin-top:6px;padding:8px 10px;background:var(--panel2);border-radius:8px;font-size:12.5px;line-height:1.6"><b>🤖 偏差原因：</b>'+esc(r.deviation_reason)+'</div>':''}
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

  // 幅度偏差率：只算方向判对的样本（实际-预测均值），供下次预测作修正值
  const okRows=(result.results||[]).filter(r=>r.ok);
  const dirOk=okRows.filter(r=>r.direction_correct);
  const biasVal=dirOk.length?dirOk.reduce((s,r)=>s+(r.pct_deviation||0),0)/dirOk.length:null;
  const biasTxt=biasVal!=null?(biasVal>0?'+':'')+biasVal.toFixed(2)+'%':'--';
  const biasHint=biasVal!=null?(biasVal>0.05?'系统性低估·下次预测应上调':(biasVal<-0.05?'系统性高估·下次预测应下调':'幅度基本准确')):'无方向判对样本';

  // 基准对照：AI vs 动量跟涨/均值回归/历史频率/随机
  const b=result.baselines||{};
  let baseHtml='';
  if(b.sample){
    const items=[
      ['AI 预测',b.ai_rate],['动量跟涨',b.momentum_rate],['均值回归',b.reversal_rate],
      ['历史频率',b.base_rate],['随机',b.random_rate]];
    const bars=items.map(([k,v])=>
      '<div class="item" style="flex:1;min-width:86px"><div class="k" style="font-size:11px">'+k+'</div>'+
      '<div class="v" style="font-size:13px">'+(v!=null?v+'%':'--')+'</div></div>').join('');
    const exc=b.excess_vs_best;
    const excTxt=exc!=null?(exc>0?'<span class="badge up">超额 +'+exc+'pp</span>':'<span class="badge down">低于基线 '+exc+'pp</span>'):'<span class="badge flat">样本不足</span>';
    baseHtml='<div class="sec" style="border:1px solid var(--line);border-radius:10px;padding:10px 12px;margin-bottom:10px">'+
      '<div style="font-size:12px;color:var(--sub);margin-bottom:6px">📊 基准对照（方向正确率，样本 '+b.sample+'）：'+excTxt+
      '<span style="float:right;font-size:11px">AI 幅度偏差 ±'+b.ai_abs_dev+'% vs 动量 ±'+b.momentum_abs_dev+'%</span></div>'+
      '<div class="row" style="margin:0">'+bars+'</div></div>';
  }

  // 信心校准：AI 自评信心档位 vs 实际方向正确率
  const cc=result.confidence_calibration||[];
  let calibHtml='';
  if(cc.length){
    const cells=cc.map(c=>{
      const cls=c.direction_correct_rate>=60?'up':(c.direction_correct_rate>=40?'flat':'down');
      return '<div class="item" style="flex:1;min-width:86px"><div class="k" style="font-size:11px">'+esc(c.level)+'信心</div>'+
        '<div class="v" style="font-size:13px"><span class="badge '+cls+'">'+c.direction_correct_rate+'%</span>'+
        '<span style="font-size:10px;color:var(--sub)">（'+c.sample+'只）</span></div></div>';
    }).join('');
    calibHtml='<div class="sec" style="border:1px solid var(--line);border-radius:10px;padding:10px 12px;margin-bottom:10px">'+
      '<div style="font-size:12px;color:var(--sub);margin-bottom:6px">📊 信心校准（各档位实际方向正确率，高信心应明显高于低信心才可信）</div>'+
      '<div class="row" style="margin:0">'+cells+'</div></div>';
  }

  container.innerHTML = `
    <h3>📋 复盘：对 ${esc(String(result.date).slice(5))} 日 · 共 ${result.total} 只基金</h3>
    <div class="row" style="margin-bottom:12px">
      <div class="item"><div class="k">整体方向正确率</div><div class="v">${result.direction_correct_count}/${result.total}</div></div>
      <div class="item"><div class="k">平均准确率</div><div class="v"><span class="badge ${avgCls}">${result.avg_accuracy}%</span></div></div>
      <div class="item"><div class="k">幅度偏差率(方向对)</div><div class="v"><span class="badge ${biasVal!=null&&Math.abs(biasVal)>0.05?'flat':'up'}">${biasTxt}</span><div style="font-size:11px;color:var(--sub)">${biasHint}</div></div></div>
    </div>
    ${baseHtml}
    ${calibHtml}
    ${comboHtml}
    <h3>🤖 偏差原因与改进建议</h3>
    <div class="v" style="font-weight:400;line-height:1.7;margin-bottom:14px">${esc(result.deviation_reason||'（未配置 LLM，无法分析偏差原因）')}</div>
    ${rows}
    <div style="margin-top:10px;color:var(--sub);font-size:11px">准确率=方向对50%+幅度误差<0.3%得50%、<0.6%得30%；幅度偏差率=仅方向判对的基金(实际-预测)均值，为正表示系统性低估涨幅，已自动喂入下次分析作修正值；组合实际=持仓市值加权涨跌</div>
  `;
}

// 拦截 saveModal 不需要单独覆写，openSettings 直接设置 inp.dataset.mode='settings'

// 今日分析持久化：重启后把今天已保存的分析报告重新渲染到「分析」页
function renderTodayAnalysis(r){
  const rep = document.getElementById('ana-report');
  if(!rep) return;
  if(!r.has){ rep.innerHTML=''; return; }
  try{
    renderFullReport(r, rep);
  }catch(e){
    rep.innerHTML = '<div class="empty-state">今日分析数据读取失败</div>';
  }
}

window.addEventListener('pywebviewready',()=>{
  pywebview.api.manual_refresh();
  // 恢复今天已保存的分析（关闭重开后还在）
  pywebview.api.get_today_analysis().then(r=>{
    if(r && r.ok) renderTodayAnalysis(r);
  }).catch(()=>{});
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
.titlebar .btns button{cursor:pointer;border:none;border-radius:6px;width:24px;height:22px;
  font-size:12px;line-height:1;color:var(--txt);background:rgba(0,0,0,.08);padding:0}
.titlebar .btns button:hover{background:rgba(0,0,0,.1)}
.titlebar .btns button.on{background:rgba(var(--brand-rgb),.4);color:#fff}
.titlebar .btns button.mask.on{background:rgba(var(--orange-rgb),.4);color:#fff}
.titlebar .btns button.tray:hover{background:rgba(var(--sub-rgb),.3)}

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
      <button id="maskbtn" onclick="toggleMaskMini()" title="隐藏金额：开">🙈</button>
      <button id="pinbtn" class="pin on" onclick="togglePin()" title="固定位置：开（点击解锁拖动）">📌</button>
      <button class="tray" onclick="hideToTray()" title="隐藏到系统托盘（右键托盘可彻底退出）">⬇</button>
    </span>
  </div>
  <div class="sum">
    <div class="box">
      <div class="k">总资产(元)</div>
      <div class="v" id="total">--</div>
    </div>
    <div class="box">
      <div class="k" id="f-k-profit">今日收益(元)</div>
      <div class="v" id="profit">--</div>
      <div class="v" id="cum-rate" style="font-size:13px;margin-top:3px;font-weight:600">--</div>
    </div>
  </div>
  <div class="list" id="list"></div>
  <div class="bar">
    <button onclick="expand()">展开完整版</button>
  </div>
</div>

<script>
let state=null;
function cls(v){return v>0?'up':(v<0?'down':'flat')}
function fmt(v,d=2){return v==null?'--':Number(v).toLocaleString('zh-CN',{minimumFractionDigits:d,maximumFractionDigits:d})}
function sgn(v,d=2){return (v>0?'+':'')+fmt(v,d)}
function esc(s){return String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
function pctTag(f){
  if(!f) return '';
  if(f.est){
    if(f.est_pending) return '<small class="stale">收盘未更新</small>';
    return '<small class="stale">预估</small>';
  }
  const d=new Date();
  const t=String(d.getFullYear())+'-'+String(d.getMonth()+1).padStart(2,'0')+'-'+String(d.getDate()).padStart(2,'0');
  if(f.qdate===t) return '<small>收盘</small>';
  // T+N>1（QDII 等）：官方净值滞后是常态，qdate 永远不是今天，官方最新数据即「昨日收盘」
  if((f.confirm_days||1)>1) return '<small class="stale">昨日收盘</small>';
  // 今天已收盘但净值尚未公布（收盘后-晚间净值更新前，qdate 还是昨天）→ 不再误标「昨日收盘」
  if(state&&state.market_open&&d.getHours()>=15) return '<small class="stale">收盘待更新</small>';
  return '<small class="stale">昨日收盘</small>';
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

function render(st){
  state=st;
  // 收益卡片标题动态化（与主窗口一致）
  const fl=st.funds||[];
  const anyEst=fl.some(f=>f.est);
  const todayStr=String(new Date().getFullYear())+'-'+String(new Date().getMonth()+1).padStart(2,'0')+'-'+String(new Date().getDate()).padStart(2,'0');
  const fk=document.getElementById('f-k-profit');
  if(fk){
    const closedToday=!!(st.market_open && new Date().getHours()>=15);
    if(fl.some(f=>f.est&&f.est_pending)){fk.textContent='收盘收益(预估)';}
    else if(anyEst){fk.textContent='今日估算收益(元)';}
    else if(fl.length>0 && (fl.some(f=>f.qdate===todayStr)||closedToday)){fk.textContent='今日收盘收益(元)';}
    else if(fl.length>0){fk.textContent='昨日收盘收益(元)';}
    else{fk.textContent='今日收益(元)';}
  }
  const masked=!!st.mask;
  applyMaskState(masked);
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
      '<span class="pct '+cls(f.pct)+'">'+(f.pct==null?'--':sgn(f.pct)+'%')+pctTag(f)+'</span>'+
      '<span class="val">'+(f.value?fmt(f.value):'--')+'</span>'+
    '</div>'
  ).join('');
}

async function expand(){await pywebview.api.show_main()}
async function hideToTray(){await pywebview.api.hide_to_tray()}

function applyMaskState(masked){
  const b=document.getElementById('maskbtn');
  if(!b) return;
  b.classList.toggle('on', !!masked);
  b.textContent=masked?'🙈':'👁';
  b.title=masked?'隐藏金额：开（点击显示）':'隐藏金额：关（点击隐藏）';
}
async function toggleMaskMini(){
  const r=await pywebview.api.toggle_mask_amount();
  if(r && r.ok) applyMaskState(!!r.mask);
}

let _fixedBlock=null;
function setFixed(fixed){
  const b=document.getElementById('pinbtn');
  if(b){
    b.classList.toggle('on', !!fixed);
    b.title=fixed?'固定位置：开（点击解锁拖动）':'固定位置：关（点击固定）';
  }
  // 固定 = 锁定悬浮窗在当前位置：捕获阶段拦截 mousedown，阻止 pywebview 的 easy_drag 拖动
  if(fixed && !_fixedBlock){
    _fixedBlock=function(e){e.stopPropagation();};
    window.addEventListener('mousedown', _fixedBlock, true);
  } else if(!fixed && _fixedBlock){
    window.removeEventListener('mousedown', _fixedBlock, true);
    _fixedBlock=null;
  }
}
async function togglePin(){
  const r=await pywebview.api.toggle_fixed();
  if(r && r.ok) setFixed(!!r.fixed);
}

async function toggleBoot(){
  const r=await pywebview.api.toggle_autostart();
  if(r && r.ok){
    const b=document.getElementById('bootbtn');
    if(r.autostart){b.classList.add('on');b.textContent='🚀';b.title='开机自启：开（点击关闭）';}
    else{b.classList.remove('on');b.textContent='🛰';b.title='开机自启：关（点击开启）';}
  }
}

function applyBootState(on){
  const b=document.getElementById('bootbtn');
  if(!b) return;
  if(on){b.classList.add('on');b.textContent='🚀';b.title='开机自启：开（点击关闭）';}
  else{b.classList.remove('on');b.textContent='🛰';b.title='开机自启：关（点击开启）';}
}

async function toggleCollapse(){
  // 收起功能已移除，悬浮窗固定展开
  return;
}

window.addEventListener('pywebviewready',()=>{
  // 恢复保存的 UI 偏好（默认隐藏金额 + 自启状态）
  pywebview.api.get_ui_status().then(r=>{
    if(r && r.ok){
      applyBootState(!!r.autostart);
      applyMaskState(!!r.mask);
      setFixed(!!r.fixed);
    }
  });
  // 今日分析持久化：重启后把今天已保存的分析报告渲染出来
  pywebview.api.get_today_analysis().then(r=>{
    if(r && r.ok && r.has){
      if(typeof renderTodayAnalysis==='function') renderTodayAnalysis(r);
    }
  }).catch(()=>{});
  pywebview.api.manual_refresh();
});
</script>
</body>
</html>"""


def main():
    # 单实例锁：托盘常驻后重复双击 exe 会多开，窗口互相干扰（悬浮窗显示不出来/主窗口消失）
    if not _single_instance_check():
        import os as _os
        _os._exit(0)

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

        def hide_to_tray(self):
            return api.hide_to_tray()

        def manual_refresh(self):
            return api.manual_refresh()

        def toggle_pin(self):
            return api.toggle_pin()

        def toggle_fixed(self):
            return api.toggle_fixed()

        def toggle_collapse(self):
            return api.toggle_collapse()

        def toggle_autostart(self):
            return api.toggle_autostart()

        def toggle_mask_amount(self):
            return api.toggle_mask_amount()

        def get_ui_status(self):
            return api.get_ui_status()

        def get_today_analysis(self):
            return api.get_today_analysis()

    main_window = webview.create_window(
        "我的基金监控", html=HTML_MAIN, js_api=api,
        # 主窗口默认宽度按用户当前窗口实际宽度固定（1431px，2026-08-18 用户要求）
        width=1431, height=720, min_size=(900, 600),
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

    # 主窗口 ✕ 关闭 → 拦截，隐藏到系统托盘（托盘常驻，不退出）
    def _on_main_closing(*args, **kwargs):
        if api._quitting:
            return None  # 真正退出时放行
        try:
            main_window.hide()
            api._main_visible = False
        except Exception:
            pass
        api._ensure_tray()
        return False  # 取消关闭

    main_window.events.closing += _on_main_closing
    # 启动即创建托盘（保证常驻；缺 pystray 时静默降级）
    api._ensure_tray()

    def loop():
        while True:
            time.sleep(REFRESH_SEC)
            threading.Thread(target=api.refresh, daemon=True).start()

    threading.Thread(target=loop, daemon=True).start()
    webview.start(icon=ICON_FILE if os.path.exists(ICON_FILE) else None)


if __name__ == "__main__":
    main()
