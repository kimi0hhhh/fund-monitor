# build.py / make_icon.py · 说明（小工具）

> 两个文件都很小（124 / 70 行），无需单独模块地图，此处简要说明用途与关键点。

---

## build.py（124 行）· 一键构建脚本

### 职责
建 venv → 装依赖 → PyInstaller 打包 exe →（可选）复制到指定目录。

### 关键逻辑
| 行号 | 内容 |
|---|---|
| 24-30 | ROOT / SPEC / MAIN / REQ / DIST_EXE / DEFAULT_VENV 路径常量 |
| 39-43 | `clean_env()`：**去掉 PYTHONPATH**（沙箱 sitecustomize 把删除操作改成回收站删除 → PyInstaller 崩溃） |
| 45-47 | `run(cmd, env)`：subprocess 封装 |
| 50-120 | `main()`：4 步流程——建/复用 venv → 装依赖 → PyInstaller（`基金监控.spec --clean --noconfirm`）→ copy-to |

### 用法（打包必读）
```bash
PYTHONPATH= python build.py --venv C:\Users\10719\.workbuddy\binaries\python\envs\fundapp --skip-install
PYTHONPATH= python build.py --copy-to C:\Users\10719\Desktop   # 打包后自动复制
```
- `--venv`：复用已有环境（跳过创建）
- `--skip-install`：跳过依赖安装（venv 就绪时加速）
- ⚠️ 复制步骤对中文进程名 taskkill 可能失败（WinError 32），失败时用 Python shutil 兜底（见 fund-monitor-build skill）

---

## make_icon.py（70 行）· 图标生成脚本

### 职责
用 PIL 从文字/图形生成 app.ico（应用图标 + 悬浮窗/托盘图标共用）。

### 说明
- 仅在需要重新生成图标时运行，正常开发不涉及
- 输出：`app.ico`（打包时经 spec `datas=[('app.ico', '.')]` 打进 exe）

---

## 相关文件一览

| 文件 | 职责 | 文档 |
|---|---|---|
| fund_monitor.py | 主程序（窗口/持仓/UI/托盘） | `MODULE_MAP.md` |
| fund_analysis.py | AI 分析/复盘/信号/量化 | `fund_analysis_MODULE_MAP.md` |
| build.py | 一键打包 | 本文档 |
| make_icon.py | 图标生成 | 本文档 |
| 基金监控.spec | PyInstaller 配置（hiddenimports 含 pystray._win32） | 见 fund-monitor-build skill |
| requirements.txt | 依赖清单（pywebview5.4/pystray/pillow 等） | — |
