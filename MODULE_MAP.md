# fund_monitor.py · 模块地图（模块文档 / 精准定位索引）

> 用途：改代码前先查本文件定位行号，用 Read 的 offset/limit 只读目标区间，避免整读 180KB 大文件。
> 行号基准：2026-08-17 v2.0.6（托盘常驻版）。改动后行号会漂移，若失效用 `grep -n "def 函数名" fund_monitor.py` 重新定位。
> 总规模：约 3865 行 / 180KB，内嵌 HTML_MAIN（主界面）+ HTML_MINI（悬浮窗）两套前端。

---

## 文件结构总览

| 区间 | 内容 |
|---|---|
| 1-49 | 模块头注释、imports、BASE_DIR/数据文件路径、HEADERS、REFRESH_SEC |
| 50-73 | 代理配置：`_load_proxy` / `_save_proxy` / `_get_proxies`（proxy_config.json） |
| 74-131 | UI 偏好：`load_ui_config` / `save_ui_config`（ui_config.json：collapsed/mask/autostart）+ 开机自启注册表 |
| 132-193 | 数据存取：`load_data`/`save_data`（funds_data.json）、闲钱、收益率采样 |
| 194-306 | 系统工具：屏幕尺寸/工作区/置顶/**托盘创建**（_create_tray）/ 图标 |
| 307-343 | 交易日历：`_qdate` / `_next_trading_day` / `nav_date_for_now` / `_add_trading_days` |
| 344-534 | 行情估算：蛋卷估值 / 重仓股加权估算 / 指数近似兜底 / `fetch_batch` 批量 |
| 535-639 | 基准与开市判断：`fetch_benchmark_pct` / `is_market_open_today` |
| 640-1809 | **class Api（主程序核心，js_api）** |
| 1810-3529 | HTML_MAIN（主窗口界面：持仓/分析/复盘/信号/设置 全套前端） |
| 3530-3787 | HTML_MINI（悬浮窗界面） |
| 3788-3865 | `main()` 入口：窗口创建、FloatApi、托盘、刷新循环 |

---

## 全局函数（行号 → 职责）

### 配置 / 数据持久化
| 行号 | 函数 | 职责 |
|---|---|---|
| 50 | `_load_proxy()` | 读 proxy_config.json，返回代理字符串或 None |
| 59 | `_save_proxy(proxy)` | 写代理配置（空串=清除） |
| 64 | `_get_proxies()` | 构造 requests 代理参数字典 |
| 75 | `load_ui_config()` | 读 ui_config.json（collapsed/mask/autostart） |
| 85 | `save_ui_config(cfg)` | 写 UI 偏好 |
| 93 | `_autostart_enabled()` | 查注册表 Run 键开机自启状态 |
| 111 | `_autostart_set(enabled)` | 写/删注册表 Run 键 |
| 132 | `load_data()` | 读 funds_data.json（份额模型持仓） |
| 142 | `save_data(data)` | 写持仓 |
| 150 | `load_idle_cash()` | 读闲钱 |
| 164 | `save_idle_cash(amount)` | 写闲钱 |
| 172 | `load_rate_history()` | 读收益率采样（30 天） |
| 183 | `save_rate_history(data)` | 写收益率采样 |

### 系统 / Win32 / 托盘
| 行号 | 函数 | 职责 |
|---|---|---|
| 194 | `_f(v)` | 数字格式化辅助 |
| 201 | `_screen_size()` | 整屏分辨率（DPI 感知） |
| 218 | `_work_area()` | 排除任务栏后的可用区域（SPI_GETWORKAREA） |
| 244 | `_set_topmost(win, top)` | Win32 SetWindowPos 置顶/取消（线程安全） |
| 261 | `_tray_icon_image()` | 加载 app.ico → 32x32 PIL Image（含 _MEIPASS 兜底） |
| 277 | `_create_tray(api)` | 创建 pystray 托盘：左键=显示/隐藏主窗，右键=显示主窗/显示悬浮窗/彻底退出 |

### 交易日历 / 行情估算
| 行号 | 函数 | 职责 |
|---|---|---|
| 307 | `_qdate(s)` | 从行情时间串解析 YYYY-MM-DD |
| 316 | `_next_trading_day(d)` | 下一交易日（仅跳周末，忽略节假日） |
| 324 | `nav_date_for_now()` | 按 15:00 规则推断确认净值日 |
| 333 | `_add_trading_days(date_str, days)` | 往后数 N 个交易日 |
| 344 | `_fetch_estimate(code, base)` | 盘中估值主入口：蛋卷优先 → 重仓加权 → 指数近似 |
| 455 | `_fetch_top_stocks_estimate(code, base)` | **重仓股加权估算**：前十大重仓股实时涨跌按权重加权（2026Q2） |
| 496 | `_fetch_index_estimate(code, base)` | 指数/主题近似估算兜底 |
| 535 | `fetch_batch(codes)` | 批量拉行情（腾讯 qt.gtimg.cn，GBK） |
| 604 | `fetch_benchmark_pct()` | 沪深300（sh000300）当日涨跌幅，折线图基准 |
| 620 | `is_market_open_today()` | 用 sh000300 行情判断今天是否开市（节假日兼容） |

---

## class Api（640 行起，js_api 主对象）

> ⚠️ 遵守项目红线：js_api 上**绝不能挂非下划线复杂属性**（Window 引用一律 `self._main_window/_float_window/_windows`），否则 get_functions 递归崩溃。

### 状态初始化（641）
`__init__`：data/info、窗口引用、_tray、_quitting、_main_visible、分析任务池 _tasks、_state_lock、UI 偏好、闲钱、收益率、沪深300基准。

### 持仓操作（663-1026）
| 行号 | 方法 | 职责 |
|---|---|---|
| 663 | `_migrate(code)` | 旧金额模型 → 份额模型迁移 |
| 687 | `get_state()` | 前端拉全量状态 |
| 691 | `_build_state()` | 组装渲染数据（含占比/集中警示/收益计算） |
| 785 | `add_fund(code, amount, confirm_days)` | 录入持仓 |
| 824 | `buy_fund(code, amount)` | 买入（T+N 规则） |
| 851 | `sell_fund(code, amount)` | 卖出/清仓 |
| 892 | `set_confirm_days(code, days)` | 改确认规则 |
| 908 | `edit_fund(code, amount, realized)` | 编辑金额+累计收益 |
| 936 | `set_idle_cash(amount)` | 设置闲钱 |
| 949 | `cancel_pending(code, oid)` | 撤单 |
| 959 | `del_fund(code)` | 删除持仓 |
| 966 | `_confirm_orders()` | 确认到期委托 |
| 1001 | `_settle()` | 自动复利结算 |

### 刷新 / 推送 / 窗口切换（1027-1289）
| 行号 | 方法 | 职责 |
|---|---|---|
| 1027 | `refresh()` | 拉行情 → 计算 → push 到窗口 |
| 1050 | `_sample_rate()` | 盘中每分钟收益率采样 |
| 1090 | `_maybe_update_review_summary()` | 收盘后复盘汇总缓存 |
| 1117 | `push()` | 向已加载窗口 evaluate_js render() |
| 1127 | `manual_refresh()` | 手动刷新（异步） |
| 1131 | `show_floating()` | 切到悬浮窗（隐藏主窗） |
| 1145 | `show_main()` | 从悬浮窗回主窗 |
| 1156 | `hide_to_tray()` | **关闭→隐藏到托盘**（悬浮窗按钮调用） |
| 1170 | `toggle_main_visible()` | 托盘左键：显示/隐藏主窗 |
| 1183 | `_ensure_tray()` | 惰性创建托盘（缺 pystray 静默降级） |
| 1195 | `toggle_mask_amount()` | 隐藏金额开关（持久化） |
| 1204 | `toggle_autostart()` | 开机自启开关（注册表） |
| 1213 | `get_ui_status()` | 返回 UI 偏好状态 |
| 1222 | `quit_app()` | **彻底退出**：停托盘 → destroy → os._exit |
| 1239 | `toggle_pin()` | 悬浮窗置顶切换 |
| 1252 | `toggle_collapse()` | 悬浮窗收起/展开（缩条） |
| 1269 | `_apply_float_size()` | 按收起态设置尺寸/位置（工作区内） |

### AI 分析 / 复盘 / 信号（1290-1809）
| 行号 | 方法 | 职责 |
|---|---|---|
| 1290 | `get_analysis_config()` | 读 LLM 配置 |
| 1296 | `save_analysis_config(...)` | 存 LLM 配置（支持自定义模型） |
| 1307 | `get_proxy_config()` / `save_proxy_config()` | 代理读写 |
| 1316 | `_get_signal_context(code)` | 该基金历史信号（最近5条，喂 AI） |
| 1327 | `analyze_code(code)` | 单只异步分析（任务池） |
| 1375 | `analyze_all()` | 全部持仓组合分析 |
| 1496 | `get_task_status(task_id)` | 前端轮询任务进度 |
| 1499 | `list_history_dates()` | 历史分析日期 |
| 1504 | `get_history_record(date, code)` | 单条历史预测 |
| 1515 | `get_today_analysis()` | 今日分析（含持久化恢复） |
| 1543 | `get_history_full(date)` | 整日历史详情 |
| 1567 | `get_signals()` | 信号库+胜率统计 |
| 1578 | `update_signal(id, status, outcome)` | 信号审核 |
| 1604 | `del_signal(id)` | 删除信号 |
| 1613 | `clear_signals()` | 清空信号 |
| 1620 | `get_trade_reviews()` | 加减仓复盘记录+经验教训 |
| 1631 | `review_trade_reviews()` | 开市日 23:00 后自动复盘 |
| 1697 | `audit_signals()` | 打开信号页自动审核 |
| 1758 | `review_all(date_str)` | 按日期批量预测复盘 |

---

## HTML 前端（内嵌，改 UI 用）

| 区间 | 界面 | 关键点 |
|---|---|---|
| 1810-3529 | HTML_MAIN | 顶部卡片7张/持仓表/分析tab/复盘tab/信号tab/设置tab；JS 函数 `switchView`、`render`、`analyze`、`reviewAll`、`auditSignals` 等 |
| 3530-3787 | HTML_MINI | 悬浮窗：标题栏拖拽/📌置顶/🚀自启/⤓收起/✕回主窗/「隐藏到托盘」/「展开完整版」；JS `expand`、`hideToTray`、`togglePin`、`toggleBoot` |

---

## main() 入口（3788-3865）

| 行号 | 内容 |
|---|---|
| 3788 | `def main()`：屏蔽 pywebview 日志、创建 Api |
| 3798 | `class FloatApi`：悬浮窗独立轻量 js_api（代理 show_main/quit_app/hide_to_tray/manual_refresh/toggle_pin/toggle_collapse/toggle_autostart/get_ui_status/get_today_analysis） |
| 3823 | `create_window` 主窗口（1080x720，min 900x600） |
| 3828 | `create_window` 悬浮窗（320x460，frameless+on_top+hidden） |
| 3835-3837 | 绑定 api._main_window/_float_window/_windows |
| 3843 | `_on_main_closing`：主窗 ✕ 拦截 → 隐藏到托盘（_quitting 时放行） |
| 3853 | `api._ensure_tray()` 启动即建托盘 |
| 3858 | `loop()` 30s 刷新线程 |
| 3864 | `webview.start(icon=...)` 启动 |

---

## 高频改动速查（常见需求 → 去哪个函数）

| 需求 | 定位 |
|---|---|
| 改托盘菜单/图标 | `_create_tray`（277） |
| 改关闭行为 | `_on_main_closing`（3843）+ `hide_to_tray`（1156）+ `quit_app`（1222） |
| 改悬浮窗样式/按钮 | HTML_MINI（3530-3787） |
| 改主界面样式/卡片 | HTML_MAIN（1810-3529） |
| 改行情估算逻辑 | `_fetch_estimate`（344）/ 重仓加权（455）/ 指数兜底（496） |
| 改收益/份额计算 | `_build_state`（691）/ `_settle`（1001） |
| 改 AI 分析提示词 | fund_analysis.py（另一文件，见其模块文档） |
| 改复盘时机 | `review_trade_reviews`（1631）/ `is_market_open_today`（620） |
| 改数据文件字段 | `load_data`/`save_data`（132/142） |

> 其他文件索引：`fund_analysis.py` → `fund_analysis_MODULE_MAP.md`；`build.py`/`make_icon.py` → `TOOLS_MODULE_MAP.md`

> 维护约定：本文件每次大改后，用 `grep -n "^class \|^def \|^    def " fund_monitor.py` 刷新本索引（约 2 秒，成本远低于整读）。
