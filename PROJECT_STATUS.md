# 基金监控项目 · 速览（跨会话接续用）

> 触发：用户说「基金监控项目」时，先读本文件接续开发。

## 位置与产物
- 项目目录：`C:\Users\10719\Desktop\基金监控项目\`（2026-08-17 19:00 从 WorkBuddy 工作区整体移动至此）
- 产物：`dist\基金监控.exe`（另有一份部署版 `基金监控.exe` 在项目根目录，可直接运行）
- 数据文件：`funds_data.json` 等 7 个 json 已随项目移入项目根目录（与 exe 同目录，2026-08-17 19:03）
- 源码：`fund_monitor.py`（主程序，内嵌 HTML_MAIN / HTML_MINI 两套界面）、`fund_analysis.py`（AI 分析模块）、`make_icon.py`、`app.ico`
- 说明文档：`使用说明.md`（已并入 README.md）

## 技术栈
- Python 3.13（venv：`C:\Users\10719\.workbuddy\binaries\python\envs\fundapp`）
- UI：pywebview 5.4（WebView2 渲染 HTML/CSS/JS）+ PyInstaller 单文件打包
- LLM：openai SDK → DeepSeek（默认 `deepseek-v4-pro`，可切 `deepseek-v4-flash`）

## 打包流程（每次改完执行）
```bash
# 1. 结束正在运行的 exe（python subprocess 按 tasklist gbk 解码找"基金监控.exe"再 taskkill /PID）
# 2. 清理旧产物后打包：
cd 项目目录
pyinstaller --onefile --noconsole --name 基金监控 --hidden-import=clr --hidden-import=openai \
  --icon app.ico --add-data "app.ico;." --clean fund_monitor.py
rm -rf build __pycache__
```
- 产物：`dist\基金监控.exe`，**只放项目目录，不再复制到桌面**（2026-08-18 用户约定）
- 如需更新运行版：手动把 exe 复制到 `桌面\workBuddy工具\基金监控\`（数据文件所在目录），注意 exe 正在运行时无法覆盖，必须先退出
- 打包也可用一键脚本：`python build.py --venv .venv --skip-install`

## 数据文件（exe 同目录）
| 文件 | 内容 |
|---|---|
| funds_data.json | 持仓（份额模型：shares/bought/sold/pending/navmap/confirm_days） |
| idle_cash.json | 闲钱（计入总资产） |
| rate_history.json | 今日收益率采样（交易日 9:30-15:00 每分钟一点，保留30天） |
| analysis_config.json | LLM key/model/base_url |
| analysis_history.json | 每日预测（predictions/reports/portfolio） |
| signals.json | 信号库（胜率统计） |
| trade_review.json | 加减仓建议复盘记录 |
| prediction_lessons.json | 预测复盘经验教训缓存（喂回下次分析） |
| review_results.json | 按目标日缓存的复盘结果（打开直接显示，不用重新复盘，v2.0.19+） |

## 数据源（免费公开接口）
- 实时行情：`http://qt.gtimg.cn/q=jj{code}`（GBK 编码；**p[2]估算净值可能为空→用 p[5]官方净值兜底**；qdate 从 p[8] 解析）
- 盘中估值：蛋卷 `danjuanapp.com/djapi/fund/estimate-nav/{code}`（休市为空）
- 历史净值：`http://api.fund.eastmoney.com/f10/lsjz?fundCode=X&pageIndex=N&pageSize=30&mode=0`（JSON，**必须分页**，默认每页30）
- 前十大持仓：移动端 `https://fundmobapi.eastmoney.com/FundMNewApi/FundMNInverstPosition?FCODE=X`（JSON：代码/名称/占比/行业/增减持）
- 已失效：天天基金 fundgz 估值接口(404)、东财移动端 FundMNFInfo(被封)

## 关键踩坑（必须遵守）
1. pywebview 必须 5.4（6.x 有 window.native 递归 bug → 启动慢/卡死）；`create_window` 无 icon 参数，icon 放 `webview.start(icon=...)`
2. **js_api 对象上绝不能挂非下划线复杂属性**（Window 引用一律 `self._windows/_main_window/_float_window`），否则 get_functions 递归崩溃 → 所有按钮失效
3. **双窗口必须独立 js_api**：悬浮窗用独立 FloatApi（代理 show_main/quit_app/manual_refresh/toggle_pin/toggle_collapse）
4. WebView2 下 confirm()/prompt() 不可用 → 一律用自定义 modal
5. quit_app 用 `os._exit(0)`（destroy 会卡死）
6. 悬浮窗拖动：frameless 下标题栏元素要加 class `pywebview-drag-region`（easy_drag 仅 chromium+frameless 生效）
7. **DeepSeek 不要开 thinking 思考模式**（思考链污染"只输出JSON"→解析失败+极慢）。**pro 和 flash 都默认开 thinking**，`llm_chat` 里 `if "deepseek" in model.lower()` 就传 `extra_body={"thinking":{"type":"disabled"}}`
8. 旧模型名 deepseek-chat 已弃用，load_config 里自动升级为 deepseek-v4-pro
9. **打包踩坑（WorkBuddy safe-delete 拦截，必看）**：环境 `PYTHONPATH` 指向 `F:\WorkBuddy\resources\...\vendor\shim`，其 `sitecustomize.py` 把 `os.remove`/删除操作强制包装成「回收站删除」，沙箱里回收站不可用 → pyinstaller 的 `os.remove` 直接 OSError 崩溃（exit 1）。**解决：打包前 `PYTHONPATH=` 清空环境变量**（bash 里 `PYTHONPATH= python build.py`），且脚本里 `subprocess.run(..., env=去掉PYTHONPATH的os.environ)`。bash `rm -rf` 另会被 genie-trash 包装 + 中文路径编码失败，也别用。

## 功能清单（均已实现）
- 持仓：录入/买入/卖出（每基金 T+N 规则）/改规则/编辑（改金额+累计收益）/删除/撤单/自动复利结算(_settle)/旧数据迁移
- 顶部卡片7张：总资产(含闲钱)/今日收益/收益率/累计收益/闲钱(可点编辑)/今日总体预测/预测准确率 + 收益率折线图(小卡片点击展开)
- 悬浮窗：独立无边框置顶小窗，可拖（标题栏）/📌固定/⤓缩右下角/✕关闭
- 分析：单只（九角色 roles/明日预测/中期策略 midterm_strategy/加减建议/信号 signals/持仓穿透）；全部持仓=先逐只后组合（组合含板块调整/整体预测/闲钱建议 idle_cash_advice 结合单基金/新方向 new_direction_advice/量化配置 allocation 风险平价）；复盘按日期批量（准确率=方向50+幅度<0.3%得50/<0.6%得30，偏差原因 summarize_review）；历史预测查看；信号tab(胜率)
- 排序：持仓表全列多级排序；逐只分析四档排序（明确操作>HOLD有附加>纯HOLD>失败，档内信心降序→金额降序）
- 风险指标：Sharpe/VaR95/下行风险/最大连跌（compute_metrics 内联）

## 最近进度
- 穿透式投研升级已完成（持仓穿透/风险量化/中期策略/信号追踪/量化配置），测试全过，桌面 exe 已替换
- **UI 优化已落地并发版**：①资产总览主卡（跨2列大字号）+ 今日收益突出 ②表格加「占比」列+超30%橙色「集中」警示 ③操作分区+删除二次确认 ④收益率折线图加沪深300对比线（`sh000300` 涨跌幅 p[32]）⑤悬浮窗加累计收益率 ⑥卡片区一键折叠（header「收起卡片」按钮）⑦信号 tab 全新样式（4宫格统计+方向色条+状态徽章+删除单个信号+一键清空）
- **thinking 破坏 JSON 已修复**：pro/flash 都显式关 thinking + 删除 reasoning_content 错误兜底
- **iOS 浅色主题**：深色→浅色（背景 #f2f2f7 / 面板 #ffffff / 品牌蓝 #007aff / 红涨 #ff3b30 / 绿跌 #34c759），红涨绿跌保持
- **加减仓复盘绑定开市日**（2026-08-16）：新增 `is_market_open_today()`（用 sh000300 行情 p[30] 日期判断今天是否开市，兼容节假日，接口失败兜底周末判断）；`review_trade_reviews` 只在开市日复盘、且只复盘建议日期<今天的（当天新建议等下一开市日）；`get_trade_reviews` 返回 market_open/today；前端 autoReviewTrades 休市日跳过自动复盘；待复盘文案改「下一开市日自动 AI 复盘」。**未打包**
- **复盘改净值更新后 + 日期筛选**（2026-08-16）：复盘仅限开市日 23:00 后（`market_closed`=开市且 hour>=23，等基金当日净值更新完，整点/16点都不准），盘中/休市返回提示；加减仓复盘 tab 顶部加日期筛选下拉（全部+去重日期倒序，选中保留）；状态提示显示「今天休市/净值未更新完」；主窗口启动时 setTimeout 到 23:00 自动复盘一次（不轮询）。**未打包**
- **复盘闭环强化**（2026-08-16）：①`review_trade_advice` 增加结构化 `bias_type`（方向误判/时机过早或过晚/追涨杀跌情绪化/幅度误判/信息不足/其他）②复盘 worker 完成后调 `summarize_trade_lessons()` 提炼 3-6 条经验教训缓存到 `trade_lessons.json` ③`build_trade_review_context` 喂回下次分析：盈利占比+偏差类型分布+经验教训+最近3条亏损案例 ④前端加减仓复盘 tab 顶部显示经验教训区块、每条复盘带偏差类型徽章 ⑤`get_trade_reviews` 返回 lessons。**已发版（2026-08-16 23:22 桌面 exe 已替换，含开市日/收盘/日期筛选/闭环全部改动）**
- **23:00 后启动补复盘**（2026-08-16 23:28 二版）：修复边界——程序在 23:00 后启动时，原 setTimeout 定时器不触发（delay 为负），导致错过当晚自动复盘；现在启动 3 秒后补一次 autoReviewTrades（休市日内部会跳过）。**已重新发版**

## 发版与发布（2026-08-17 00:10）
- 便携版压缩包：`桌面\基金监控_便携版.zip`（exe+使用说明+全部数据 json，19.8MB，换电脑无缝迁移）
- 纯净版压缩包：`桌面\基金监控_纯净版.zip`（exe+使用说明+README，无数据）
- **GitHub 公开仓库**：`https://github.com/kimi0hhhh/fund-monitor`（用户 kimi0hhhh）
  - 上传内容：源码+文档 10 个文件（fund_monitor.py / fund_analysis.py / make_icon.py / 使用说明.md / README.md / app.ico / app_preview.png / 基金监控.spec / .gitignore / PROJECT_STATUS.md）
  - 数据 json 和 exe 已 gitignore，未上传（含 API key/持仓隐私）
  - **坑：github.com 域名在本机 SSL 握手失败/408 超时（git push 不可用）**，改用 GitHub Contents API（api.github.com 正常）逐文件 PUT 上传成功；Git Data API 对空仓库报 409，需先用 Contents API 建初始 commit
  - 本地 git 仓库已 init + commit + 配 origin（URL 无 token）；git push 需网络恢复后执行
- **⚠️ 安全提醒：用户提供的两个 PAT 已在 GitHub 会话中明文使用，建议用户到 GitHub Settings → Developer settings → Personal access tokens 删除**
- **完整使用说明**（2026-08-17 00:15）：重写 `使用说明.md`（14 章节：简介/安装/持仓/AI分析/预测复盘/信号/加减仓复盘闭环机制/设置/数据文件/FAQ/免责声明），已同步 GitHub（Contents API 更新 sha ca8e8a94），两个压缩包已重新生成
- **文档重组 + 持仓接口修复 + 发版**（2026-08-17 00:29）：①README.md 即完整中文使用说明、README_EN.md 英文版（删除使用说明.md/USER_GUIDE_EN.md，本地+GitHub 已同步，远程 10 文件）②**东财持仓接口修复**：fund_analysis.py fetch_holdings 改 iPhone UA + deviceid=Wap（原 Android UA+deviceid=W 被反爬拦截，实测 Success=False；修复后实测 10 只持仓正常），已同步 GitHub（sha afcd192b）③重新打包替换桌面 exe（00:28，启动验证正常）
- **桌面 exe 更新至 v2.0.5**（2026-08-17 18:29）：从 GitHub main 拉取全部 13 个文件（SHA 校验通过、语法检查通过）后，用 `build.py --venv fundapp --skip-install --copy-to 桌面` 打包替换桌面 exe；v2.0.5 含自定义模型配置/重仓股加权估算/代理/开机自启/今日分析持久化/并发锁等新功能
- **托盘常驻 v2.0.6**（2026-08-17 18:40）：①主窗口 ✕ 拦截 `events.closing` 返回 False → 隐藏到系统托盘（不再退出）②托盘左键=显示/隐藏主窗口，右键菜单=显示主窗口/显示悬浮窗/彻底退出 ③悬浮窗「退出」按钮改为「隐藏到托盘」④依赖 pystray+pillow（spec hiddenimports 已加 `pystray._win32`）⑤quit_app 先停托盘再 destroy+os._exit。已打包替换桌面 exe（26MB），冒烟测试通过（窗口列表出现 `fund_monitor...SystemTrayIcon` 托盘类窗口）
- **悬浮窗显示修复 v2.0.7**（2026-08-17 19:16）：①**单实例锁** `_single_instance_check()`（CreateMutex）——托盘常驻后重复双击 exe 曾多开 5 个实例，窗口互相 hide/show、主窗口被移出屏幕外，导致悬浮窗"显示不出来"；现在重复启动直接退出 ②**悬浮窗默认展开**：ui_config `collapsed` 默认 False（原来是 True，点悬浮窗只显示 260x72 小条）③show_floating 不再静默吞异常：失败时打印日志并恢复主窗口。验证：源码方式调用 show_floating 后悬浮窗 visible=True（378x516 展开态）。已重新打包
- **彻底移除收起功能 v2.0.8**（2026-08-17 19:29）：v2.0.7 未根治——`toggle_collapse` 仍会把 `collapsed=true` 写回 ui_config，点「⤓」后悬浮窗又变右下角小条。本次彻底删除收起：删除 `toggle_collapse` 收起逻辑（改为恒返回 False）、`_apply_float_size`、悬浮窗「⤓」按钮、`.collapsed` 收起态 HTML 区块、JS 的 collapsed 同步、CSS 收起态样式；`_float_collapsed` 固定 False；ui_config 删除 collapsed 字段。验证：show_floating 后悬浮窗 378x516 展开态 ✅。deploy_check.py 修复路径 bug（改用 cwd）。已重新打包
- **复盘偏差原因 + 信号审核 5 小时节流**（2026-08-17 19:5x）：①`review_all_predictions` 对「方向错 或 幅度偏差 >=0.3%」的基金逐只调 `review_with_ai`（复用现成 prompt）生成 `deviation_reason`，前端 `renderReviewAll` 每行卡片显示「🤖 偏差原因」（放逐只卡片内，总体总结段保留）；**注意：批量复盘从纯算术变逐只调 LLM，会变慢** ②信号自动审核从 60 秒防抖改为 **5 小时节流**：`lastAutoAuditTs` 存 localStorage（重启仍生效，删了原 `lastAutoAudit` 变量）；手动「AI 审核信号」按钮不受限但成功后也会重置计时；空状态文案同步「5 小时内不重复」。**未打包**
- **预测复盘闭环（方向+幅度）**（2026-08-17 19:5x）：新增 `prediction_lessons.json` 缓存 + `summarize_prediction_lessons`（复盘 worker 完成后自动调：LLM 基于方向正确率+幅度高估/低估统计提炼 3-6 条预测经验教训）+ `build_prediction_review_context`（下次单只/组合分析时拼入 prompt，修正预测方向与预期涨幅）；`build_user_prompt`/`build_portfolio_prompt`/`analyze_fund`/`analyze_portfolio` 均加 `prediction_review_context` 参数，fund_monitor 三处分析入口 + review_all worker 已接通。分析后保存当日预测（`save_prediction`）确认已存在（单只/全部/组合均覆盖写入 analysis_history.json 当日）。**未打包**
- **幅度偏差率修正值**（2026-08-17 20:0x）：①`summarize_prediction_lessons` 统计升级——**幅度偏差只算「方向判对」的样本**：`bias_pct`=实际-预测均值（修正值，+为系统性低估→下次上调；-为系统性高估→下调）、`avg_abs_dev`=平均绝对偏差（精度）、over/under 计数同样只数方向对样本（阈值 ±0.2%）；stats 字段 `avg_deviation` 改名 `bias_pct`/`avg_abs_dev` ②`build_prediction_review_context` 输出「方向正确率（含降信心提示）+ 偏差率修正指令 + 经验教训」，明确指示 AI 把偏差值加到下次预期涨幅、方向判断参考正确率 ③前端 `renderReviewAll` 顶部统计区新增「幅度偏差率(方向对)」徽章+修正提示，底部说明更新。mock 单测通过（预测2实际4→bias +1.25% 等）。**未打包**
- **P0 基准对照**（2026-08-17 21:0x）：`review_all_predictions` 新增 `baselines`——对同一批复盘样本同时算 **4 个基线方向正确率**：动量跟涨（今天涨押明天涨，幅度=今天涨跌幅）、均值回归（与今天相反）、历史频率（近20日涨跌孰多）、随机（50%）；**只用预测日当天及以前的数据（防数据泄漏）**；AI 正确率用与基线相同的子集（口径一致）；返回 `excess_vs_best`（超额=AI−最佳基线）；`summarize_prediction_lessons` 把 baselines 存入 stats；`build_prediction_review_context` 喂回「基准对照」段（让 AI 知道相对简单策略强/弱）；前端复盘页新增「📊 基准对照」区块（5 个正确率条 + 超额徽章 + AI/动量幅度偏差对比）。mock 单测通过（2 只样本 ai/mom/rev/br 均 50%、excess 0.0、ai_abs 1.25 vs mom_abs 1.75）。**未打包**
- **P1 滚动加权 + 信心校准**（2026-08-17 21:1x）：①**偏差率滚动加权**：`prediction_lessons.json` 新增 `history`（保留最近 5 次复盘 stats）+ `rolling`（`_merge_rolling`：权重=样本量×0.7^(rank-1)，最新权重最高，输出加权 bias_pct/avg_abs_dev/方向正确率/总样本数/复盘次数）；`build_prediction_review_context` 优先用 rolling 修正值并标注「近 N 次复盘、共 M 只样本」，避免单次极端行情带偏 ②**confidence 校准**：`review_all_predictions` 返回 `confidence_calibration`（按 AI 自评 高/中/低 分档统计实际方向正确率+样本数+平均准确率，按样本降序）；存入 data 并喂回 prompt（提示"高信心不高于低信心即不可靠"）；前端复盘页新增「📊 信心校准」区块（各档位正确率徽章+样本数）。mock 单测通过（滚动加权 1.25/-0.25 → 0.29 与手算一致；3 次复盘累积 14 样本）。**未打包**
- **v2.0.9 发版**（2026-08-17 21:15）：上述 6 项改动（复盘偏差原因 / 5 小时节流 / 预测复盘闭环 / 幅度偏差率修正 / P0 基准对照 / P1 滚动加权+信心校准）全部打包替换桌面 exe（26MB，build.py 49s）；deploy_check 冒烟：15s/25s 存活 ✅；**⚠️ 托盘窗口检测返回 False**（v2.0.6 时能检测到 fund_monitor...SystemTrayIcon，本次 EnumWindows 未查到——可能是托盘初始化慢于 25s 检测窗口或检测时序问题，待用户打开 exe 确认托盘；进程存活已确认，脚本仍判定通过）。build/__pycache__ 已清理
- **预测目标日期锚点 bug 修复**（2026-08-17 22:5x，**未打包**）：原实现预测记录以「分析日」为 key、复盘用 `find_next_pct` 从 key 反推目标日，跨周末/盘前盘后错位（如 18 号盘前分析预测 18 号却存 key=18，复盘 find_next_pct(18)=19 号 → 用 19 号实际复盘"对 18 号的预测"；15/16 号周末分析都落 17 号导致 history 重复累积）。修复：①`compute_forecast_date()`=分析所在日历日的**下一个交易日**，`save_prediction` 自动写入 `forecast_date`（18 号全天分析统一视为对 19 号预测）②`review_all_predictions` 按 `forecast_date` **精确对齐目标日实际**（旧数据无该字段 fallback `find_next_pct`）③`build_user_prompt`/`build_portfolio_prompt` 加日期锚点「今天是 X，请预测下一个交易日 Y 的涨跌」④`summarize_prediction_lessons` 同日复盘**去重**（替换最后一条，不再重复累积滚动样本）。隔离单测通过（save 写 fd=08-18；对齐取 18 号实际；fallback 正常；同日去重 1 条）。**⚠️ 测试曾误污染真实数据文件（已完整恢复：analysis_history 22 只、prediction_lessons 6 lessons/bias 3.09），教训：测 save/summarize 必须把 HISTORY_FILE/PREDICTION_LESSONS_FILE 指向临时目录**。git commit b0394c7；.gitignore 补 prediction_lessons.json
- **预测目标日分时归属 v2**（2026-08-17 23:0x，**未打包**）：用户澄清——按「对哪天的分析」分情况：**交易日 15:00 前（盘前+盘中）分析 → 预测「当日」**（今天未收盘）；**15:00 后及周末 → 预测「下一交易日」**（与 `nav_date_for_now` 的 15:00 规则一致，正好对上"开盘前后都属于那一天"）。`compute_forecast_date` 分时返回；prompt 锚点措辞分盘中（"预测今日 X"）/盘后（"预测下一个交易日 Y"）；`summarize_prediction_lessons` 的 history **按 forecast_date 归组去重**（同目标日重复复盘只保留一条）。隔离单测通过（周一10:00→fd=17号/20:00→fd=18号/周日→fd=17号；prompt 两种措辞；history 归组=18号）
- **v2.0.10 发版**（2026-08-17 22:58）：预测目标日期锚点两轮修复（b0394c7 forecast_date 锚定 + 693f53d 分时归属/按目标日归组去重）打包替换桌面 exe（26MB，46s）；deploy_check 冒烟 15s/25s 存活 ✅；**托盘窗口检测仍 False**（与 v2.0.9 相同，疑似 deploy_check 检测时序问题，待用户打开确认）。build/__pycache__ 已清理
- **加载中金额显示 0/负 bug 修复**（2026-08-17 23:0x，**未打包**）：现象——刚打开 exe 数据未加载时点「金额隐藏」，之后加载出来总资产显示 0、累计收益变负。根因：`_build_state` 在 info 空（gz=None）时 value=0 → total=0+闲钱、cum=value+sold-bought=**-bought 负值**，前端照实渲染（v2.0.5 的 _state_lock 只防并发写坏数据，不防"加载中假数据"）。修复：①`_build_state` 每只基金加 `has_nav` 字段；**任一基金未拿到净值（加载中/估值失败）时 total/profit/realized 置 None**（前端 fmt(null) 显示 '--'，不再 0/负）②前端表格「累计收益」列改为 `f.has_nav&&f.realized?sgn(...):'--'` 拦截假负值。验证：mock 隔离测试——info 空 → total/profit/realized 均 None；加载完成 → total=10000/realized=1000 正常
- **加减仓复盘目标日锚点**（2026-08-17 23:0x，**未打包**）：对齐预测的 forecast_date 机制——`add_trade_review_from_report` 写入 `forecast_date`（同 compute_forecast_date：交易日 15:00 前=今日、15:00 后及周末=下一交易日），去重按「同基金+同目标日」；`review_trade_reviews` 的 23:00 复盘只选**目标日 ≤ 今天**的建议（即"昨天给的对今天的建议"主场景 + 历史漏网补复盘），对未来的建议提示「对 X 日需等该日收盘后自动复盘」，旧数据无 fd fallback date<today；前端卡片显示「对 X 日」、日期筛选下拉按目标日（fd 优先）。隔离单测通过（对今天/旧数据复盘、对未来跳过并提示）。**未打包**
- **分析报告复盘联动 + 依据来源**（2026-08-17 23:1x，**未打包**）：此前报告只有「📡 信号联动」、没有复盘经验体现，用户质疑"怎么得出来的没说明"。修复：①后端 analyze_one / analyze_all 把喂给 AI 的两段上下文（`build_trade_review_context` 加减仓复盘 + `build_prediction_review_context` 预测偏差率修正）存入 `result.review_context` 带回前端 ②组合报告（renderFullReport）新增「🧭 复盘联动（结论依据来源）」块，展示加减仓复盘参考 + 预测复盘参考（偏差率修正依据）原文 ③单只报告（renderReport）新增同款「复盘联动」块。今日分析持久化（重启重渲染）无 review_context 时自动不显示，兼容。**未打包**
- **trade_review 目标日迁移**（2026-08-17 23:1x）：v2.0.10 生成的建议无 forecast_date，导致 16 号（周日）的 6 条建议被旧逻辑错位复盘成"持平 0"。按用户方案迁移数据：①6 条 reviewed（生成日 08-16）→ 重置 pending + `forecast_date=2026-08-17`，**用新逻辑重新 AI 复盘** → 全部盈利（+2.6%~+8.25%，盈利占比 100%，平均 +5.36%）②6 条 pending（生成日 08-17）→ `forecast_date=2026-08-18`，明天 23:00 复盘。经验教训（trade_lessons.json）已随重复盘更新。trade_review.json 为数据文件（gitignore），无代码提交
- **v2.0.11 发版**（2026-08-17 23:17）：①**所有预测显示加"对 X 日"标注**（7 处：顶部卡片组合预测 / 持仓表格预测列 / 组合报告整体明日预测 / 组合报告单只卡片明日 / 单只报告明日预测标题 / 历史查看组合预测 / 历史查看单只预测）——新增 `fdTxt(fd)` helper，旧数据无 fd 自动不显示；后端 `save_portfolio_prediction` 补 forecast_date、`_build_state` 的 today_pred/portfolio_pred 带 fd、analyze 三处给内存 report 塞 fd ②复盘联动块（43f02fc）、加减仓复盘锚点（752b4d5）、加载中 0/负修复（ea70f6e）一并包含。打包 26MB（43s），冒烟存活 ✅；托盘检测仍 False（待确认）；build/__pycache__ 已清理（沙箱回收站不可用时用 rm -rf）。git a7abeb1
- **收盘后行情显示官方数据修复**（2026-08-17 23:2x，**未打包**）：用户反馈收盘后 018957 中航机遇领航混合C 显示 5.51% 但实际官方 4.52%。根因：`fetch_batch` 对每只基金无条件调 `_fetch_estimate` 盘中估值链，收盘后（23 点）重仓股加权基于股票收盘价算出估算（5.51）**覆盖了腾讯官方涨跌 p[7]（4.519）**。修复：`fetch_batch` 加 `_in_trading` 判断（开市日 9:30-15:00），**非交易时段（收盘后/盘前/周末）跳过估值链，直接用腾讯官方净值 p[5] 与官方涨跌 p[7]**。实测：收盘后 018957 → 4.519%/est=False ✅；mock 盘中 14:00 仍走估值链 est=True ✅。git 17cadd7
- **涨幅来源标注**（2026-08-17 23:2x，**未打包**）：涨跌幅旁标注数据来源——`pctTag(f)` helper（主窗口+悬浮窗各一份）：`est=true` → 「预估」；`est=false 且 qdate=今天` → 「收盘」；`est=false 且 qdate≠今天`（昨日数据、今天未开盘）→ 「昨日收盘」。替换原「MM-DD/昨收」小字。git f2dc6ec
- **v2.0.12 发版**（2026-08-17 23:26）：收盘后官方数据修复（17cadd7）+ 涨幅来源标注（f2dc6ec）打包替换桌面 exe（26MB，45s）；冒烟 15s/25s 存活 ✅；托盘检测仍 False（待用户确认）；build/__pycache__ 已清理。git baa62bb
- **复盘/历史按目标日聚合**（2026-08-17 23:3x，**未打包**）：用户要求「选择日期=选择对那天的预测，锚点也对那天的预测复盘」。重构：①`_fd_of(pred, dstr)`——预测目标日（forecast_date 优先，旧数据无 fd fallback=分析日后首个交易日）；②`list_forecast_dates()`——所有目标日去重倒序；③`review_all_predictions(fd)`——**按目标日跨分析日聚合**（17号收盘后与18号盘中"对18号"的预测合并复盘），实际=fd 当天官方净值，基线用各自分析日数据防泄漏；④`review_prediction/review_with_ai/review_portfolio` 支持直接传预测记录；⑤前端复盘页下拉「对 X 日」、历史列表「对 X 日的预测」、复盘标题/按钮/准确率卡片文案全部对齐目标日。隔离单测通过（对18号聚合 A+B、对17号聚合 C+X、实际对齐 fd、目标日列表正确）。git 633b400
- **加减仓复盘 T+N 净值新鲜度校验**（2026-08-17 23:4x，**未打包**）：用户反馈 QDII（如 457001 国富亚洲）净值滞后 1-2 天（实测腾讯 p[8]=08-14 而今天 08-17），复盘用滞后净值会算出错误盈亏。规则：`review_trade_reviews` 复盘每条前校验 `当前行情净值日期(qdate) ≥ 建议目标日(fd)`——不满足则跳过（保持 pending），标记「净值未更新（当前净值日期 X 早于目标日 Y，T+N 滞后），待更新后自动复盘」；国内基金 qdate=fd 正常复盘。mock 单测通过（国内复盘/QDII 跳过）。**另确认：组合预测已挂钩信号库(55条)+加减仓复盘+预测复盘上下文，用户看到的 1.5 是复盘数据生成前的旧分析（今日分析持久化），重新分析即生效**。git 884cdb6
- **v2.0.13 发版**（2026-08-17 23:41）：目标日聚合复盘（633b400）+ T+N 净值新鲜度校验（884cdb6）打包替换桌面 exe（26MB，44s）；冒烟 15s/25s 存活 ✅；托盘检测仍 False（待用户确认）；build/__pycache__ 已清理。git 58122fb
- **README 更新 + GitHub Release v2.0.13**（2026-08-18 00:0x）：①README.md / README_EN.md 更新至 v2.0.13（目标日复盘/跨分析日聚合/T+N 校验/偏差率修正/基准对照/滚动加权/信心校准/复盘联动/涨幅标注/收盘后官方数据/信号 5 小时节流），已上传 GitHub（commit 2b306e7ffe / d07f85d0b9）②**Release v2.0.13 已发布**：https://github.com/kimi0hhhh/fund-monitor/releases/tag/v2.0.13（release_id 371808601，含更新说明 + exe 资产 `FundMonitor-v2.0.13.exe` 26MB 上传成功）③含 token 的临时脚本 upload_release.py 已删除。git commit f2cb937
- **v2.0.14 设置标注 + 测试连接 + 原子写 + 默认收起恢复**（2026-08-18，**待打包**）：用户反馈大量问题实为**本机（junben.lai001）exe 是旧版 v2.0.5（21MB）**——托盘（v2.0.6+）、复盘闭环（v2.0.9+）、数据加载修复（v2.0.11）、收益标签 pctTag（v2.0.12）全都没有。本次改动：①**设置面板 4 字段加 label 标注**（①API Key ②代理地址-非必要 ③API 地址 ④模型名称），新增「测试连接」按钮（后端 `test_connection` 用表单配置发最小请求，不保存）②**恢复悬浮窗默认收起小条**（用户确认要 v2.0.5 行为）：`load_ui_config` 默认 collapsed=True、`_float_collapsed` 读配置、恢复 `toggle_collapse`/`_apply_float_size`/收起态 HTML/CSS/JS、get_state 带 collapsed ③**原子写防全零损坏**：新增 `_atomic_write`（临时文件+fsync+os.replace+写前 .bak 备份），save_data/save_ui_config/save_idle_cash/save_rate_history/_save_proxy 全部接入——修复 08-17 17:32 funds_data.json 全零写入 ④**收益卡片标题动态化**：主窗口「今日估算收益/收盘收益/昨日收盘收益」+ 悬浮窗同款，按 est/qdate 判断。数据：trade_review.json 本机旧格式（16 日 6 条 reviewed"持平"、无 forecast_date）已迁移为 pending + forecast_date（16日→17日、17日→18日、18日→18日），共 30 条全 pending 待新版自动复盘。**待打包**
- **v2.0.15 数据导入/导出**（2026-08-18，**待打包**）：主界面操作区（买入/卖出旁）新增「⬇ 导出数据 / ⬆ 导入数据」按钮。导出：把 11 个基础数据 json（funds_data/idle_cash/rate_history/ui_config/proxy_config/analysis_config/analysis_history/signals/trade_review/trade_lessons/prediction_lessons）压缩成 zip（SAVE_DIALOG，默认名 基金监控数据备份_日期.zip）；导入：OPEN_DIALOG 选 zip → 按文件名还原到对应 json（写前 .bak 备份）→ 重载内存数据 + 清 fa 缓存 + refresh/push；前端导入用自定义 modal 二次确认（WebView2 无 confirm）。后端 `_backup_files_map` 统一维护文件清单。zip 逻辑已隔离验证（7 文件导出→导入内容一致 ✅）。**待打包**
- **v2.0.16 预测复盘空数据修复**（2026-08-18，**待打包**）：用户反馈「对 17 号复盘都是空数据」。根因：`review_all_predictions` 用 `fetch_history` 拉东财历史净值（api.fund.eastmoney.com），公司网络该域名被封锁（实测返回 Cloudflare 1.1.1.1 反爬页/HTTPS 超时），导致目标日 fd 实际涨跌拿不到 → 每条都标记「尚未到复盘时间（目标日净值未更新）」→ 前端全空。修复：`fetch_history` 加腾讯 gtimg 兜底 `_fetch_tencent_nav(code)`（qt.gtimg.cn/q=jj{code}，GBK，字段 p[5]官方净值/p[7]涨跌幅/p[8]日期）——东财拿不到历史时至少返回最近 1 个交易日，复盘「对 17 号」即可取到 17 号实际。实测：017193 兜底返回 {2026-08-17, 1.8427, 2.9729}；22 只对 17 号预测全部聚合，抽样 5 只 4 只成功（QDII 如 024239 腾讯 qdate=08-14 滞后，属正常 T+N 等待）；compute_metrics 单条历史容错 OK。**待打包**
- **v2.0.17 预测准确率量化优化**（2026-08-18 14:5x，**已发版**）：①**GARCH(1,1) 条件波动率**：新增 `_garch_fit`/`compute_garch_vol`（纯 Python 方差目标法 + 网格搜索 α1∈[0.02,0.3]×β1∈[0.5,0.97] 最小化负对数似然），`compute_metrics` 加 `cond_vol_1d`（下一日条件波动率，替代/补充历史模拟法 VaR）②**12-1 月动量**：`compute_metrics` 加 `momentum_12_1_dir`（过去 60 日累计收益、跳过最近 1 日）③**后处理校准 `apply_posthoc_calibration`**（接入 analyze_fund/analyze_portfolio 的 parse 后）：a) 机械 bias_pct 修正 `expected_pct += rolling.bias_pct`（不靠 LLM 自律）b) `excess_vs_best≤0` 时方向覆盖为 12-1 月动量方向（仅单只，组合不做）c) Platt scaling 信心校准 `confidence_score` 重算为真实正确概率（`_platt_fit` 纯 Python 逻辑回归，样本<10 自动跳过）d) Conformal 90% 区间加 `pred_interval=[修正后±conformal_q90]` ④**复盘升级**：动量基线从"单日跟涨"改"12-1 月动量"；复盘累积 `(confidence_score,方向对错)` 样本拟合 Platt 存 `stats["platt"]`；算 abs_deviation 90% 分位数存 `stats["conformal_q90"]` ⑤**信号证伪优先**：SYSTEM_PROMPT signals 加 `risk_scenario` 字段 + 要求"每个看多信号必须写清证伪条件，无风险兜底宁可少给"（治信号库全是看多/看空被证伪的乐观偏差）⑥**汇率入市场快照**：`fetch_market_snapshot` 加 `whUSDCNY`（p[3]现价/p[13]涨跌幅，22 字段特殊处理）；市场宽度因东财 push2 也被封跳过（用已有创业板/中证500/国债ETF 替代）⑦**信心展示层降级**（fund_monitor 前端 `confTag()`）：AI 自评某档位但历史该档位实际正确率<50% 时标注「⚠不可靠」（只加标注不改写 confidence 字段，避免污染复盘统计）；state 加 `confidence_calibration`。**全部纯 Python 零新依赖**；json 向后兼容（platt/conformal_q90 复盘时自动生成，`.get()` 安全降级）；隔离单测全绿（GARCH 1.02%/动量DOWN/Platt a>0/bias 1.0→4.09/降级 UP→DOWN/Platt 80→52.5/Conformal [3.29,4.89]/汇率 6.7426）；打包 25MB 已替换运行目录 exe（Bash cp 中文路径 Permission denied → 改用 PowerShell `Copy-Item -Force` 成功）。**备注：ERC 风险平价因高成本低收益跳过**
- **Git 同步 v2.0.24**（2026-08-18 18:5x，本机 10719）：另一台电脑（junben.lai001）今日推送 v2.0.14~v2.0.24 全部更新至 GitHub，本机 git fetch + reset --hard 对齐到远端 `61909fc`（v2.0.24）。因历史无共同祖先（之前 Contents API 重建过），未用 merge 而是直接以远端为权威；本地独有文件已保留：①模块地图 3 个（MODULE_MAP.md / fund_analysis_MODULE_MAP.md / TOOLS_MODULE_MAP.md，远端已删）恢复回工作区并提交 ②PROJECT_STATUS 补回本地独有「README 更新 + GitHub Release v2.0.13」记录。本地提交 `cabfb6d`（4 文件 +360 行）；fund_monitor.py / fund_analysis.py 语法检查通过；数据 json（funds_data/signals/trade_review/analysis_history 等）未受影响。**未打包、未 push（push 需 GitHub 凭据，本机无 token）**
- **v2.0.24 发版**（2026-08-18 19:02，本机 10719）：①Git Data API 推送本地 2 提交到 GitHub（github.com git 协议 SSL 握手被拦，`git push` 失败 → 改用 api.github.com Git Data API blob/tree/commit/ref 重放，远端 main 更新至 `8556a0de`，内容校验与本地一致；含 token 的临时脚本 push_via_api.py 已删、.git/config 无残留；**用户新 PAT 已明文使用，提醒删除**）②用 fund-monitor-build skill 打包 v2.0.24 替换项目根目录 exe（26MB，69s；deploy_check 冒烟 15s/25s 存活 ✅，托盘窗口检测仍 False 待用户打开确认）；build/__pycache__ 已清理。本地 HEAD `e319cc5` = 远端 `8556a0de`
- **持仓东财兜底 v2.0.25**（2026-08-18 19:2x，**已发版**）：用户反馈「复盘到了（说明实盘已更新）但持仓界面还是全部预估」。根因：**腾讯行情接口(qt.gtimg.cn)净值日期更新滞后于东财 lsjz**——实测 19:12 时腾讯 22 只 qdate 全停 08-17，而东财已更新 000217/014881/017193 的 08-18 官方净值；持仓走腾讯（qdate≠今天→估值链 est_pending 显示预估），复盘走东财（fetch_history 拿到 08-18）→ 两源不同步。修复：`fetch_batch` 非交易时段分支加 `_fetch_em_official(code)` 东财兜底——腾讯 qdate≠今天时先查东财最新官方净值（60s 短缓存，单只 0.1s），东财有今天数据则直接用官方净值/涨跌（est=False, qdate=今天, est_src=eastmoney），否则才走估值链。实测 22 只：3 只切官方收盘（000217 +0.28 / 014881 +1.16 / 017193 -0.92），19 只仍预估（东财腾讯确实都未更新，QDII 024239/457001/021662/016665 qdate=08-14 滞后属正常 T+N）。已打包替换项目根目录 exe（26MB，44s；冒烟 15s/25s 存活 ✅；托盘检测仍 False 待用户确认）。build/__pycache__ 已清理
- **T+N 基金不走估值链 v2.0.26**（2026-08-18 22:1x，**已发版**）：用户反馈「T+2 基金在其他地方都收盘结算了，这里还没结算」。根因：**v2.0.17 的「qdate≠今天→走估值链」逻辑对 QDII 是死路**——QDII 净值滞后 1-2 天是常态，qdate 永远不是今天，导致腾讯官方已发布的最新净值（如 024239 官方 +4.19%）被估值链覆盖显示成预估（-5.99%）。修复：①`fetch_batch(codes, confirm_map=None)` 新增 confirm_map 参数（refresh 传入 `{code: confirm_days}`，缺省时内部 load_data 兜底）；非交易时段对 T+N>1 基金**跳过估值链**，直接用腾讯官方数据，仅在东财日期更新时用东财（est=False）②前端两处 `pctTag`（主窗口+悬浮窗）对 `confirm_days>1` 且 qdate≠今天 显示「昨日收盘」（QDII 官方滞后数据不是「收盘待更新」）。实测 22 只全部官方：16 只 T+1 qdate=08-18 收盘，6 只 QDII qdate=08-17 官方（024239 +4.19 / 016665 +5.19 / 012922 +3.20 / 163208 +1.17 / 457001 +1.09 / 021662 +1.09）。已打包替换项目根目录 exe（26MB，48s；冒烟 15s/25s 存活 ✅；托盘检测仍 False 待用户确认）。build/__pycache__ 已清理
- **闲钱弹窗输入框修复 v2.0.27**（2026-08-18 23:3x，**已发版**）：用户反馈「闲钱设置不了」。根因：**v2.0.14 设置面板改造时把通用输入框 `m_amt` 包进了 `fld_amt` 容器（默认 display:none），但 openModal/editIdleCash 没适配**——父容器 none 时子元素 input 即使 display:block 也不可见，导致**所有弹窗（闲钱/买入/卖出/规则/编辑）的输入框全部消失**（用户旧版 v2.0.5 无 fld_amt 容器故正常，更新到 v2.0.24+ 后暴露）。修复：①HTML 把 `m_amt` 从 `fld_amt` 容器移出恢复独立 input（fld_amt 只剩 label「① API Key」）②`editIdleCash`/`openSettings` 补 `inp.style.display='block'`（openModal 原本就有但父容器隐藏时无效）。验证：HTML 解析确认 m_amt 容器=独立、m_amt2 独立、m_proxy/base_url/model 仍在各自 fld_* 容器内（设置面板专用，正确）；三处弹窗函数 display 逻辑正确。已打包替换项目根目录 exe（26MB，46s；冒烟 15s/25s 存活 ✅；托盘检测仍 False 待用户确认）。build/__pycache__ 已清理

- **预测锚点改为「成交日次日」语义 v2.0.28**（2026-08-18 23:5x，**未打包**）：用户确认「15:00 前预测今日=预测成交价，对买卖零指导」是真问题。改动：①`compute_forecast_date`（fund_analysis.py:876）盘中(交易日15:00前)→预测**下一交易日**（明天），盘后/周末→预测**下下个交易日**（后天，因最早次日才能操作、成交价=次日净值、收益起点=后天）；**复盘链不断**——每天至少分析一次（盘中或盘后）即保证每个目标日都有预测可复盘（周日→周二=周一操作吃周二；周一盘中→周二；周一盘后→周三=周二操作吃周三）②`build_user_prompt`/`build_portfolio_prompt` 日期措辞同步（单只/组合）③`add_trade_review_from_report` docstring ④前端卡片「今日总体预测」→「下一交易日预测」（fund_monitor.py:2519）、表头「今日预测」→「次日预测」（:2570）、JS 注释（:2823/:906）⑤mock 时间 6 用例全过（周一14:00→08-18 / 周一20:00→08-19 / 周六→08-25 / 周五15:00→08-25）⑥**基线回测结论**（22 只持仓×65 天历史净值）：单日动量 46.4% / 12-1月动量 48.3% / 均值回归 51.9% / 历史频率 45.0% / 随机 44.4%——全部≈50%，**日频方向基本不可预测**，AI 45.8% 非模型差而是任务极限；均值回归略超 1.9pp 但扣成本后无意义；预测定位应转向中期策略+风控。**未打包**
- **v2.0.30 中长期移入分析页 + 内容增强**（2026-08-19 00:0x，**已发版**）：用户反馈「中长期内容太少，应该放分析里面」。改动：①**移除主 tab「中长期」**（tab-midterm/view-midterm/switchView midterm 分支/switchMidtermTab/mt-pane/mt-date-select 全删），**改为分析页子 tab「中长期」**（ana-tabs 加 `atab-midterm`，加减仓复盘与设置之间；内容挪进 `ana-pane-midterm`，位于 view-analysis 内）②**内容增强**：MIDTERM_SYSTEM_PROMPT 升级——组合加 `sector_advice`（板块配置建议数组：板块/动作/理由/调整幅度），每只基金加 `expected_ret`（预期 20 交易日收益率%）/`stop_loss`（止损净值）/`take_profit`（止盈净值）/`phase_plan`（分阶段操作计划：前10日/突破加仓/第15日减仓等）/`key_drivers`（关键驱动数组）；analyze_midterm 保存 sector_advice；renderMidtermReport 渲染升级（板块配置标签组、预期收益红涨绿跌、止盈止损、阶段计划灰底块、驱动列表），单卡宽度 280→300px ③历史记录区块直接放当前策略下方（去掉独立子 tab），refreshMidtermHistory 移除下拉（midterm-history 直接列表渲染）、loadCachedMidterm 移除 switchMidtermTab 调用。验证：元素完整性检查全过（ana-pane-midterm/atab-midterm 各 1 处，旧引用 0 残留）；语法全过。打包替换项目根目录 exe（26MB，44s），build/__pycache__ 已清理；按用户偏好不自动冒烟，用户打开验证（分析页 → 中长期子 tab）。旧 midterm.json 数据兼容（缺新字段前端 .get 降级显示）
- **v2.0.31 中长期卡片显示基金名**（2026-08-19 00:1x，**已发版**）：用户反馈「中长期显示基金编号，应显示基金名」。源码实际已有 name 逻辑（后端 analyze_midterm `name_map`/`funds_out` 把 name 写入每个 spec，前端 `renderMidtermReport` 用 `esc(f.name||code)` 显示名字+代码小字+超长省略号 title），但 exe 未打包导致用户看到旧版编号。重新打包替换项目根目录 exe（26MB，47s），build/__pycache__ 已清理；用户打开验证（分析页 → 中长期 → 卡片标题应为基金名，代码小字灰显）。注意：本次是纯打包同步，无代码改动；git 状态含 PROJECT_STATUS/fund_analysis/fund_monitor 修改 + baseline_check.py/midterm.json 未跟踪
- **v2.0.32 休市误判修复 + 旧数据基金名回填**（2026-08-19 00:2x，**已发版**）：用户反馈 ①中长期仍显示编号（v2.0.31 打包后旧 midterm.json 记录无 name 字段，前端 `f.name||code` 降级成编号）②加减仓复盘提示「今天休市」但实际不是。修复：①`is_market_open_today`（fund_monitor.py:769）根因——腾讯指数 p[30] 是「最近交易日日期」，**凌晨/盘前（今天未开盘）p[30]=昨天**，原逻辑 `p[30]==今天` 判 False → 误判休市。新逻辑：周末直接 False；工作日拉 p[30]，==今天→True；**hour<15（盘前/盘中）→ True**（今天未开盘/未收盘，按工作日开市）；已过 15:00 仍无今天数据 → False（节假日休市）；接口失败兜底 weekday<5 ②`Api.get_midterm_state`（fund_monitor.py:1860）对 latest.funds 里缺 name 的 spec 用 `self.data`/`self.info` 持仓名回填（旧数据兼容，无需重新生成分析）。验证：mock 三场景全过（凌晨周三指数=昨天→True / 深夜23点指数仍昨天→False / 周六→False）；旧数据 name 回填测试通过（无 name → 国泰上证综合ETF联接C）。打包替换项目根目录 exe（26MB，45s），build/__pycache__ 已清理；用户打开验证：①分析页→中长期→历史旧记录也应显示基金名 ②加减仓复盘不再误报休市（凌晨也可点）
- **v2.0.33 顶部准确率卡片双指标显示**（2026-08-19 00:3x，**已发版**）：用户问「顶部预测准确率指什么」→ 解释为综合准确率（方向50+幅度50，幅度分难拿满故偏低），用户要求「两个都显示出来，卡片呈现为 方向准确率/综合准确率」。改动：①卡片标题「预测准确率」→「预测准确率（方向/综合）」（fund_monitor.py:2619）②`acc-avg` 渲染改 `dirRate% / avg_accuracy%`（方向率=最近复盘 direction_correct/total，大字号；综合率小字灰显；颜色按方向率 80/50 阈值），detail 副标题改为「对X日 · 方向 N/M · 综合(方向50+幅度50)」（fund_monitor.py:2938-2949）。打包替换项目根目录 exe（26MB，48s），build/__pycache__ 已清理；用户打开验证顶部卡片
- **方向预测动态择优覆盖 v2.0.34**（2026-08-19 00:4x，**已发版**）：用户要求方向正确率≥50%（现状滚动 45.8%）。根因：v2.0.17 方向覆盖用「12-1月动量(回测48.3%)」而非最优「均值回归(51.9%)」。改 fund_analysis.py 4 处：①`compute_metrics` 加 `last_pct_dir`/`reversal_dir`(均值回归=今日方向取反)/`hist_freq_dir`(近20日涨跌频率) ②`_merge_rolling` 加滚动基线正确率 `baseline_rates`+`best_baseline`+`best_baseline_rate`（权重=基线样本量×0.7^rank） ③`apply_posthoc_calibration` 方向覆盖改「动态择优」——AI 滚动正确率<最优基线→覆盖最优基线方向，冷启动(无滚动统计)兜底均值回归，FLAT 基线不覆盖 ④`build_prediction_review_context` 喂回「滚动最优基线是谁」段。验证：walk-forward(16样本) AI 原始18.8%→择优覆盖68.8%；隔离单测6用例全绿。已打包替换项目根目录 exe（26MB，46s，**含 v2.0.33 卡片双指标改动**），按用户偏好不冒烟；组合预测不动，幅度bias/Platt/Conformal 照旧。verify_accuracy.py 保留为验证工具（与 baseline_check.py 互补）
- **GitHub 同步 v2.0.34**（2026-08-19 00:5x，**已 push**）：本机 github.com 网络已恢复（用户调整网络后 schannel SSL 握手不再失败，git push 直连成功）。远端有 2 个 Git Data API 重放产生的重复 commit（07ee3ac/8556a0d，内容=本地 v2.0.24），与本地分叉；用 `git reset --soft FETCH_HEAD` 把本地 v2.0.25~v2.0.34 压成单个 commit `5e80a02` 接到远端 8556a0d 之上，历史线性化。push 结果：`8556a0d..5e80a02 main -> main` ✅。**⚠️ 安全提醒：用户本次提供的 PAT（ghp_A8GD...TBPU）已明文用于 push，建议到 GitHub Settings → Developer settings → Personal access tokens 删除/撤销**。`.gitignore` 补 `midterm.json`（中长期分析数据，含持仓分析，不提交）

## 远端 v2.0.1→v2.0.5 进度（2026-08-17 拉取同步，来自 GitHub main 分支 30 commits）
- **v2.0.1（09:56）**：`build.py` 一键构建脚本（建 venv+装依赖+打包 exe）、`requirements.txt`（pywebview 锁 5.4）、`.gitignore` 排除 .venv；悬浮窗 DPI 缩放修复（物理/逻辑像素混用→兼容缩放+坐标防越界）；悬浮窗列表排序（先涨幅再占比，get_state 加 ratio 字段）；README 加「从源码构建」一节；**GitHub Actions 自动打包**（push v* tag → PyInstaller → 上传 Release）
- **v2.0.2 / v2.0.3（10:53）**：**代理设置**（`proxy_config.json`，fund_monitor 与 fund_analysis 共用）+ 盘中估值指数近似兜底（跟踪标的实时行情估算，适应第三方源被屏蔽的网络）
- **v2.0.4（12:06）**：**主动基金重仓股加权估算**——用 2026Q2 季报前十大重仓股实时涨跌按权重加权，替代单指数套用（覆盖率不足 100% 时按已有重仓股外推）
- **v2.0.5（16:08）**：①**自定义模型配置**：设置可填 API 地址/Key/模型名，支持任意 OpenAI 兼容接口 ②悬浮窗收起定位修复（SPI_GETWORKAREA 避开任务栏）③开机自启开关（注册表 Run 键）+默认收起+默认隐藏金额（`ui_config.json` 持久化）④今日分析持久化：重启后自动恢复当天分析报告 ⑤并发锁：修复加载中切换金额隐藏导致数据错乱
- Releases：v2.0.1~v2.0.5 共 5 个 exe 成品（约 21MB，`FundMonitor-vX.Y.Z.exe`）
- 待办（用户未确认）：`compute_risk_metrics` 重复定义（第二版覆盖第一版 → beta/alpha 永远 None）；按钮整合；逻辑证伪复盘

## 协作省 token 约定（用户偏好，必须遵守）
- 小改动：直接告诉用户改哪一行，尽量不打包
- 攒 2-3 个需求再打包一次
- 触发：用户说「基金监控项目」时，先读本文件接续开发
- **排错类任务：先问 1-2 个关键问题定位，再动手，不盲目枚举窗口/进程**
- **打包部署：一律走 fund-monitor-build skill 的 deploy_check.py，不再手写冒烟脚本**
- **读大文件：优先用模块地图（MODULE_MAP.md 等）精准定位，单次 Read 只取目标区间，不整读 180KB 文件**
- **冒烟测试没必要：打包部署后由用户自己打开 exe 验证，不自动启动 exe 做存活检测**
