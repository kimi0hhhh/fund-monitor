# 基金监控小工具 (Fund Monitor)

一个本地运行的基金持仓监控 + AI 投研分析桌面工具。基于 **Python + pywebview (WebView2)**，双击 exe 即可使用，持仓数据完全保存在本机。

## ✨ 功能特性

### 持仓监控（免费，无需 AI）
- 录入 / 买入 / 卖出基金，支持每只基金独立的 **T+N 确认规则**（T+0 / T+1 / T+2）
- 待确认交易管理：按 15:00 截止规则自动确认净值日，支持撤单
- 实时盘中估值（腾讯财经 + 蛋卷基金数据源），休市显示最新官方净值
- 顶部汇总卡片：总资产 / 今日收益 / 收益率 / 累计收益 / 闲钱
- 收益率折线图（含沪深 300 基准对比线）
- 独立置顶悬浮窗：总资产 / 今日收益 / 各基金涨跌，可拖可缩
- 风险指标：Sharpe、VaR95、下行风险、最大连跌
- iOS 浅色主题，A 股配色（红涨绿跌）

### AI 投研分析（需 DeepSeek API key）
- **投研团队流水线**：技术分析师 / 基本面 / 新闻 / 情绪 → 多空辩论 → 主管 → 交易员 → 风控三方，输出结构化报告
- 报告含：核心结论、今日分析、**明日预测**（方向/幅度/信心）、**加减仓建议**、关键风险、技术指标
- 持仓穿透：穿透查看各基金前十大持仓与行业分布
- 组合分析：整体预测、板块调整建议、闲钱配置、量化风险平价配置
- **预测复盘**：按日期批量复盘，方向正确率 + 幅度准确率 + AI 偏差原因分析
- **加减仓复盘闭环**：AI 加减仓建议自动收集 → 开市日收盘后（23:00，等净值更新完）自动复盘盈亏 → 标注偏差类型 → 提炼经验教训 + 盈利占比，**自动喂回下次分析**，形成持续改进闭环
- 信号追踪：AI 建议转为信号，自动审核（兑现/证伪/强化），统计胜率

## 🚀 快速开始

1. **下载**：从 Releases 下载 `基金监控.exe`（单文件，双击即用），或自行打包（见下）
2. **配置 LLM**（仅分析模式需要）：
   - 打开程序 → 「分析」tab → 右上角「⚙ 设置」
   - 填入 DeepSeek API key（https://platform.deepseek.com 申请）
   - 默认 model=`deepseek-v4-pro`，可切 `deepseek-v4-flash` 省成本
3. 持仓监控功能完全免费，不依赖 LLM

> 依赖 WebView2 运行时（Windows 11 和绝大多数 Win10 已自带，缺失可到微软官网下载）

## 🔧 自行打包

```bash
# 环境：Python 3.13 + pyinstaller + pywebview 5.4（6.x 有 window.native 递归 bug，勿升）
PYTHONPATH= pyinstaller --onefile --noconsole --name 基金监控 \
  --hidden-import=clr --hidden-import=openai \
  --icon app.ico --add-data "app.ico;." --clean fund_monitor.py
# 产物：dist/基金监控.exe
```

## 📁 数据文件（exe 同目录，已 gitignore）

| 文件 | 内容 |
|---|---|
| `funds_data.json` | 持仓份额与交易记录 |
| `idle_cash.json` | 闲钱 |
| `rate_history.json` | 收益率采样（交易日盘中） |
| `analysis_config.json` | LLM key / model（仅本机） |
| `analysis_history.json` | 每日预测（用于复盘） |
| `signals.json` | 信号库（胜率统计） |
| `trade_review.json` / `trade_lessons.json` | 加减仓复盘记录与经验教训 |

换电脑时**把 exe 同目录的所有 json 一起拷贝**即可无缝迁移。

## 📡 数据源（免费公开接口）

- 实时行情：腾讯财经 `qt.gtimg.cn`（GBK）
- 盘中估值：蛋卷基金 `danjuanapp.com/djapi/fund/estimate-nav/`
- 历史净值：天天基金 `api.fund.eastmoney.com/f10/lsjz`
- 前十大持仓：东财移动端 `fundmobapi.eastmoney.com`

## ⚠️ 免责声明

本工具仅用于个人学习与辅助决策，盘中估值为估算数据（以基金公司结算为准），所有 AI 分析与预测不构成任何投资建议。市场有风险，投资需谨慎。

## License

MIT
