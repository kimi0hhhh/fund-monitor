# -*- coding: utf-8 -*-
"""
基金监控 · 一键构建脚本（装依赖 + 打包 exe）

用法（在项目根目录打开命令行）：
    python build.py                          # 一条命令：建 venv → 装依赖 → 打包 exe
    python build.py --copy-to <目录>          # 打包后把 exe 复制到指定目录（如桌面）
    python build.py --venv <已有venv目录>      # 复用已有虚拟环境，跳过创建
    python build.py --skip-install            # 跳过依赖安装（复用环境时加速）

产物：dist\\基金监控.exe（单文件、双击即用，无需安装 Python/依赖）

注意：
    - 首次运行会自动创建 .venv 虚拟环境，并按 requirements.txt 安装依赖
    - 打包使用仓库自带的 基金监控.spec（PyInstaller 单文件 + 无控制台窗口）
"""
import argparse
import os
import platform
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
SPEC = os.path.join(ROOT, "基金监控.spec")
MAIN = os.path.join(ROOT, "fund_monitor.py")
REQ = os.path.join(ROOT, "requirements.txt")
DIST_EXE = os.path.join(ROOT, "dist", "基金监控.exe")
DEFAULT_VENV = os.path.join(ROOT, ".venv")
EXE_NAME = "基金监控.exe"


def venv_python(venv):
    if platform.system() == "Windows":
        return os.path.join(venv, "Scripts", "python.exe")
    return os.path.join(venv, "bin", "python")


def clean_env():
    """去掉 PYTHONPATH：沙箱环境的 sitecustomize 会把删除操作改造成回收站删除，
    导致 pip / PyInstaller 崩溃（OSError: SAFE_DELETE_FAIL_CLOSED），清掉即可。"""
    return {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}


def run(cmd, env, **kw):
    print(">>", " ".join(cmd))
    return subprocess.run(cmd, env=env, **kw)


def main():
    ap = argparse.ArgumentParser(description="基金监控一键构建：装依赖 + 打包 exe")
    ap.add_argument("--copy-to", help="打包完成后把 exe 复制到这个目录（可省略）")
    ap.add_argument("--venv", help="复用已有的虚拟环境目录，跳过创建（可省略）")
    ap.add_argument("--skip-install", action="store_true", help="跳过依赖安装，只打包")
    args = ap.parse_args()

    if sys.version_info < (3, 10):
        print("[错误] 需要 Python 3.10+，当前：%s" % sys.version.split()[0])
        return 1

    # 检查必备文件
    for f, tag in ((MAIN, "主程序 fund_monitor.py"), (SPEC, "打包配置 基金监控.spec")):
        if not os.path.exists(f):
            print("[错误] 缺少 %s：%s" % (tag, f))
            return 1
    if not os.path.exists(REQ):
        print("[警告] 缺少 requirements.txt，依赖将不会自动安装")

    env = clean_env()

    # 1/4 虚拟环境
    venv = args.venv or DEFAULT_VENV
    py = venv_python(venv)
    if not os.path.exists(py):
        print("[1/4] 创建虚拟环境 %s ..." % venv)
        r = run([sys.executable, "-m", "venv", venv], env, cwd=ROOT)
        if r.returncode != 0:
            print("[错误] 创建虚拟环境失败。请先安装 Python 3.10+（勾选 Add to PATH）后重试。")
            return 1
    else:
        print("[1/4] 复用虚拟环境：%s" % venv)

    # 2/4 依赖
    if not args.skip_install and os.path.exists(REQ):
        print("[2/4] 安装依赖（首次较慢，网络不佳可重试）...")
        r = run([py, "-m", "pip", "install", "--disable-pip-version-check", "-r", REQ],
                env, cwd=ROOT)
        if r.returncode != 0:
            print("[错误] 依赖安装失败，请检查网络后重新运行本脚本。")
            return 1
    else:
        print("[2/4] 跳过依赖安装")

    # 3/4 打包
    print("[3/4] PyInstaller 打包（约 1-2 分钟）...")
    r = run([py, "-m", "PyInstaller", SPEC, "--clean", "--noconfirm"], env, cwd=ROOT)
    if r.returncode != 0 or not os.path.exists(DIST_EXE):
        print("[错误] 打包失败，请查看上方日志。")
        return 1
    size_mb = os.path.getsize(DIST_EXE) // 1024 // 1024
    print("[3/4] 打包完成：%s（%d MB）" % (DIST_EXE, size_mb))

    # 4/4 复制
    if args.copy_to:
        try:
            os.makedirs(args.copy_to, exist_ok=True)
            dst = os.path.join(args.copy_to, EXE_NAME)
            # 目标 exe 若正在运行会复制失败，先尝试结束进程
            try:
                subprocess.run(["taskkill", "/F", "/IM", EXE_NAME],
                               capture_output=True, env=env)
            except Exception:
                pass
            shutil.copy2(DIST_EXE, dst)
            print("[4/4] 已复制到：%s" % dst)
        except Exception as e:
            print("[警告] 复制失败（exe 可能正在运行，请关闭后重试）：%s" % e)
    else:
        print("[4/4] 全部完成。可加 --copy-to <目录> 自动把 exe 复制到指定位置。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
