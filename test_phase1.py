# -*- coding: utf-8 -*-
"""
test_phase1.py — Phase 1 回测验证（次日方向预测）

用东财历史净值（fetch_history）拉 22 只持仓基金的历史，逐只 walk-forward 回测，
对比「弹性网络模型」vs「动量基线 / 均值回归基线 / 随机 50%」。

依赖：fundapp venv（含 requests）；factor_engine.py 纯标准库。
"""
import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fund_analysis as fa
import factor_engine as fe


def main(days=180, train_init=30):
    base = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(base, "funds_data.json"), encoding="utf-8") as f:
        data = json.load(f)

    codes = list(data.keys())
    print("共 %d 只持仓基金，拉取 %d 天历史回测...\n" % (len(codes), days))

    rows = []
    for code in codes:
        name = data[code].get("name", code)
        hist = fa.fetch_history(code, days=days)
        navs = [h["nav"] for h in hist if h.get("nav")]
        if len(navs) < train_init + 15:
            print("[skip] %s %s 历史不足(%d天)" % (code, name, len(navs)))
            continue
        preds, actuals, probs = fe.walk_forward(navs, train_init=train_init)
        if not preds:
            print("[skip] %s %s 样本不足" % (code, name))
            continue
        acc = fe.direction_accuracy(preds, actuals)
        mom = fe.momentum_baseline(navs)
        rev = fe.reversal_baseline(navs)
        rows.append({
            "code": code, "name": name, "n": len(preds),
            "model": acc, "mom": mom, "rev": rev,
        })
        print("%s %-20s 样本=%3d  模型=%5.1f%%  动量=%5.1f%%  反转=%5.1f%%"
              % (code, name[:20], len(preds), acc * 100, mom * 100, rev * 100))

    if not rows:
        print("\n无有效回测结果")
        return

    # 汇总（按样本加权）
    tot_n = sum(r["n"] for r in rows)
    tot_model = sum(r["model"] * r["n"] for r in rows) / tot_n
    tot_mom = sum(r["mom"] * r["n"] for r in rows) / tot_n
    tot_rev = sum(r["rev"] * r["n"] for r in rows) / tot_n

    print("\n" + "=" * 60)
    print("汇总（%d 只基金，共 %d 个预测样本）" % (len(rows), tot_n))
    print("  弹性网络模型      : %5.1f%%" % (tot_model * 100))
    print("  动量基线(跟涨)    : %5.1f%%" % (tot_mom * 100))
    print("  均值回归基线(反向): %5.1f%%" % (tot_rev * 100))
    print("  随机基线          : %5.1f%%" % 50.0)
    print("  模型 vs 最优基线  : %+.1fpp" % ((tot_model - max(tot_mom, tot_rev)) * 100))
    print("=" * 60)


if __name__ == "__main__":
    main()
