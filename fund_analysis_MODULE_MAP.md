# fund_analysis.py · 模块地图（模块文档 / 精准定位索引）

> 用途：改代码前先查本文件定位行号，用 Read 的 offset/limit 只读目标区间，避免整读 56KB 文件。
> 行号基准：2026-08-17（同步自 GitHub main，含重仓股加权估算/自定义模型/代理等）。
> 总规模：1350 行 / 56KB。纯 Python 后端，无内嵌 HTML。由 fund_monitor.py 通过 `import fund_analysis as fa` 调用。

---

## 文件结构总览

| 区间 | 内容 |
|---|---|
| 1-51 | 模块头、imports、HEADERS、`_get_proxies`（代理） |
| 52-92 | 配置管理：LLM key/model/base_url 读写（analysis_config.json） |
| 93-122 | LLM 客户端：`llm_chat`（DeepSeek/OpenAI 兼容，thinking 关闭） |
| 123-207 | 历史净值、持仓穿透 |
| 208-448 | 技术指标 + 风险指标 + 量化配置 |
| 449-544 | 预测存储（analysis_history.json）+ 信号追踪（signals.json） |
| 545-838 | 加减仓复盘（trade_review.json/trade_lessons.json）+ 信号审核 + 历史查询 |
| 839-1134 | 多角色分析 Prompt 构建 + 单只分析 + 组合分析 |
| 1135-1175 | 组合分析（含量化配置调用） |
| 1176-1350 | 复盘引擎：预测对比/批量复盘/AI 偏差分析/汇总 |

---

## 全局函数（行号 → 职责）

### 配置 / LLM
| 行号 | 函数 | 职责 |
|---|---|---|
| 38 | `_get_proxies()` | 构造代理参数字典（读 proxy_config.json，与 fund_monitor 共用） |
| 61 | `_upgrade_model(model)` | 旧模型名自动升级（deepseek-chat → deepseek-v4-pro） |
| 68 | `load_config()` | 读 analysis_config.json（key/model/base_url） |
| 82 | `save_config(cfg)` | 写配置（支持自定义模型/API 地址） |
| 87 | `is_configured()` | 是否已配置 API key |
| 93 | `llm_chat(messages, temperature, max_tokens)` | 统一 LLM 调用；**DeepSeek 显式关 thinking**（extra_body），防思考链污染 JSON |

### 数据获取
| 行号 | 函数 | 职责 |
|---|---|---|
| 126 | `fetch_history(code, days=90)` | 东财历史净值（分页，默认每页30） |
| 172 | `fetch_holdings(code)` | 前十大持仓（移动端接口，iPhone UA + deviceid=Wap） |

### 指标计算
| 行号 | 函数 | 职责 |
|---|---|---|
| 209 | `_ma(seq, n)` | 移动平均 |
| 215 | `_rsi(seq, n)` | RSI 相对强弱指标 |
| 231 | `compute_metrics(history)` | 技术指标汇总（MA/RSI/回撤/波动率等） |
| 338 | `compute_risk_metrics(history, bench_history)` | ⚠️ **第一版定义（被覆盖）** |
| 389 | `compute_risk_metrics(history, bench_history)` | ⚠️ **第二版定义（生效）**——待办：合并去重，否则 beta/alpha 取不到 |
| 423 | `compute_allocation(funds, idle_cash)` | 风险平价量化配置（波动率倒数加权） |

### 预测 / 信号存储
| 行号 | 函数 | 职责 |
|---|---|---|
| 450 | `_load_history()` / `_save_history(h)` | analysis_history.json 读写 |
| 465 | `save_prediction(date, code, prediction)` | 存单只预测 |
| 472 | `save_report(date, code, report)` | 存单只报告 |
| 480 | `load_signals()` / `save_signals(s)` | signals.json 读写 |
| 498 | `add_signals_from_report(code, report)` | 报告→信号入库（按指纹去重） |
| 509 | `fingerprint(s)` | 信号去重指纹（基金+方向+目标） |

### 加减仓复盘闭环
| 行号 | 函数 | 职责 |
|---|---|---|
| 546 | `load_trade_reviews()` / `save_trade_reviews(r)` | trade_review.json 读写 |
| 564 | `add_trade_review_from_report(...)` | 分析报告非持有建议→入库 |
| 599 | `trade_review_stats(reviews)` | 统计：总数/已复盘/盈利占比/平均收益 |
| 616 | `review_trade_advice(advice, quote)` | 单条建议复盘（结果/收益率/bias_type 偏差类型） |
| 661 | `load_trade_lessons()` / `save_trade_lessons(d)` | trade_lessons.json 读写 |
| 680 | `summarize_trade_lessons()` | 复盘后提炼 3-6 条经验教训 |
| 732 | `build_trade_review_context()` | 经验教训喂回下次分析（盈利占比+偏差分布+教训+亏损案例） |
| 766 | `signal_stats()` | 信号胜率统计 |
| 778 | `audit_signal(signal, quote)` | 单信号 AI 审核（兑现/证伪/强化/弱化/维持/信息不足） |

### 历史查询
| 行号 | 函数 | 职责 |
|---|---|---|
| 820 | `save_portfolio_prediction(date, report)` | 存组合预测 |
| 827 | `get_history(date_str)` | 查某日全部记录 |
| 834 | `list_history_dates()` | 全部历史日期（去重倒序） |

### 分析核心
| 行号 | 函数 | 职责 |
|---|---|---|
| 913 | `build_user_prompt(code, name, ...)` | 单只分析用户提示词（含技术指标/持仓穿透/信号上下文/复盘教训） |
| 963 | `parse_llm_json(content)` | 解析 LLM 输出 JSON（容错） |
| 982 | `analyze_fund(code, name, ...)` | 单只分析：九角色流水线 → 结构化报告 |
| 1083 | `build_portfolio_prompt(funds, idle_cash, ...)` | 组合分析提示词 |
| 1135 | `analyze_portfolio(funds, idle_cash, progress_cb, ...)` | 组合分析：逐只→组合（含板块/闲钱/新方向/量化配置） |

### 复盘引擎
| 行号 | 函数 | 职责 |
|---|---|---|
| 1177 | `compare_prediction(expected_dir, expected_pct, actual_pct)` | 方向+幅度打分（方向50+<0.3%得50/<0.6%得30） |
| 1214 | `review_prediction(date, code, actual_pct)` | 单条预测复盘 |
| 1230 | `review_portfolio(date, actual_pct)` | 组合复盘 |
| 1247 | `review_with_ai(date, code, actual_pct, name, metrics)` | AI 偏差原因分析 |
| 1277 | `find_next_pct(history, date)` | 找下一条净值 |
| 1285 | `review_all_predictions(date)` | 按日期批量复盘 |
| 1322 | `summarize_review(date, review_result)` | 复盘汇总 |

---

## 高频改动速查

| 需求 | 定位 |
|---|---|
| 改分析提示词/角色 | `build_user_prompt`（913）/ `build_portfolio_prompt`（1083） |
| 改 LLM 参数/关 thinking | `llm_chat`（93） |
| 改风险指标（Sharpe/VaR/β/α） | `compute_risk_metrics`（389，⚠️ 先处理 338 重复定义） |
| 改复盘打分规则 | `compare_prediction`（1177） |
| 改信号审核逻辑 | `audit_signal`（778）+ `signal_stats`（766） |
| 改经验教训闭环 | `summarize_trade_lessons`（680）+ `build_trade_review_context`（732） |
| 改数据接口（持仓/净值） | `fetch_history`（126）/ `fetch_holdings`（172） |
| 改配置字段 | `load_config`（68）/ `save_config`（82） |

---

## ⚠️ 已知待办（PROJECT_STATUS 记录）

- **`compute_risk_metrics` 重复定义**（338 与 389）：第二版覆盖第一版 → 若需 beta/alpha 需先合并去重，保留含基准回归的版本
- 改完后用 `grep -n "^def " fund_analysis.py` 刷新本索引（2 秒成本）
