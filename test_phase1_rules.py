# -*- coding: utf-8 -*-
"""
test_phase1_rules.py — 对比多种合成方式在 walk-forward 下的方向正确率

A. 弹性网络（基准，已知 ~48%）
B. 单因子符号（mom12_1）
C. 简单多数投票（7 因子）
D. 滚动加权打分卡（权重=训练窗口内各因子方向正确率，逐日滚动更新，无前视）
"""
import json
import sys
import os
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fund_analysis as fa
import factor_engine as fe


def main(days=180):
    data = json.load(open("funds_data.json", encoding="utf-8"))
    series = {}
    for code in data:
        hist = fa.fetch_history(code, days=days)
        s = [(h["date"], h["nav"]) for h in hist if h.get("nav")]
        if len(s) >= 50:
            series[code] = s

    # 构造面板样本（按日期排序）
    samples = []
    for code, s in series.items():
        navs = [nav for d, nav in s]
        dates = [d for d, nav in s]
        for t in range(20, len(navs) - 1):
            feats = fe.build_features(navs[: t + 1])
            row = fe.feature_row(feats)
            if row is None:
                continue
            y = 1 if navs[t + 1] > navs[t] else -1
            samples.append((dates[t], code, feats, y))
    samples.sort(key=lambda s: (s[0], s[1]))

    # 初始化统计
    res = {k: {"hit": 0, "tot": 0} for k in ["mom12_1", "vote", "weighted"]}
    # 滚动因子正确率（用于加权打分卡）
    factor_hit = defaultdict(int)
    factor_tot = defaultdict(int)

    cur_date = None
    cur_batch = []
    history = []  # 已完成的历史样本（用于计算滚动因子正确率）

    def predict_batch(batch):
        for d, c, feats, y in batch:
            # B: mom12_1 单因子
            b = feats["mom12_1"]
            pred_b = 1 if b > 0 else -1
            res["mom12_1"]["hit"] += (pred_b == y)
            res["mom12_1"]["tot"] += 1

            # C: 多数投票（7 因子符号）
            votes = 0
            for k in fe.FACTOR_NAMES:
                v = feats.get(k)
                if v is None:
                    continue
                votes += 1 if v > 0 else -1
            pred_v = 1 if votes > 0 else -1
            res["vote"]["hit"] += (pred_v == y)
            res["vote"]["tot"] += 1

            # D: 加权打分卡（滚动因子正确率做权重）
            score = 0.0
            for k in fe.FACTOR_NAMES:
                v = feats.get(k)
                if v is None or factor_tot[k] == 0:
                    continue
                acc = factor_hit[k] / factor_tot[k]
                w = acc - 0.5  # 偏离 0.5 的部分作为权重方向
                score += w * (1 if v > 0 else -1)
            pred_w = 1 if score > 0 else -1
            res["weighted"]["hit"] += (pred_w == y)
            res["weighted"]["tot"] += 1

    for d, c, feats, y in samples:
        if cur_date is None:
            cur_date = d
        if d != cur_date:
            predict_batch(cur_batch)
            # 把上一批并入历史，更新滚动因子正确率
            for _d, _c, _feats, _y in cur_batch:
                history.append((_d, _c, _feats, _y))
                for k in fe.FACTOR_NAMES:
                    v = _feats.get(k)
                    if v is None:
                        continue
                    pred = 1 if v > 0 else -1
                    factor_hit[k] += (pred == _y)
                    factor_tot[k] += 1
            cur_date = d
            cur_batch = [(d, c, feats, y)]
        else:
            cur_batch.append((d, c, feats, y))
    if cur_batch:
        predict_batch(cur_batch)

    print("=" * 60)
    print("各合成方式 walk-forward 方向正确率（%d 个预测样本）" % res["mom12_1"]["tot"])
    print("=" * 60)
    for k, label in [("mom12_1", "单因子 mom12_1 符号"),
                     ("vote", "7因子多数投票"),
                     ("weighted", "滚动加权打分卡")]:
        r = res[k]
        print("  %-22s %5.1f%%" % (label, r["hit"] / r["tot"] * 100))
    print("  随机基线               %5.1f%%" % 50.0)
    print("=" * 60)


if __name__ == "__main__":
    main()
