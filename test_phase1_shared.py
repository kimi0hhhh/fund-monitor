# -*- coding: utf-8 -*-
"""
test_phase1_shared.py — 共享模型（横截面面板）walk-forward 回测

按日期滚动：用 t 日之前所有基金样本训练一个共享弹性网络，预测 t 日所有基金的次日方向。
（对齐 Gu-Kelly-Xiu 的横截面做法，而非每只基金独立建模）
"""
import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fund_analysis as fa
import factor_engine as fe


def build_panel(all_series, min_len=21):
    """all_series: {code: [(date, nav), ...]} 正序
    返回按 (date, code) 排序的样本列表 [(date, code, x_row, y)]"""
    samples = []
    for code, series in all_series.items():
        navs = [nav for d, nav in series]
        dates = [d for d, nav in series]
        for t in range(min_len - 1, len(navs) - 1):
            feats = fe.build_features(navs[: t + 1])
            row = fe.feature_row(feats)
            if row is None:
                continue
            y = 1 if navs[t + 1] > navs[t] else -1
            samples.append((dates[t], code, row, y))
    samples.sort(key=lambda s: (s[0], s[1]))
    return samples


def panel_walk_forward(samples, l1=0.05, l2=0.05, lr=0.05, iters=400):
    """按日期滚动：每个日期用「之前」样本训练，预测「当前日期」样本"""
    preds, actuals, probs = [], [], []
    train_rows, train_y = [], []
    cur_date = None
    cur_batch = []

    def train_predict(batch):
        nonlocal train_rows, train_y
        if len(train_rows) < 30:
            return
        mean, std = fe._mean_std(train_rows)
        tr = fe.zscore_normalize(train_rows, mean, std)
        theta = fe.elastic_net_logistic(tr, train_y, l1, l2, lr, iters)
        for d, c, row, y in batch:
            te = fe.zscore_normalize([row], mean, std)[0]
            p = fe.predict_prob(te, theta)
            preds.append(1 if p >= 0.5 else -1)
            actuals.append(y)
            probs.append(p)

    for d, c, row, y in samples:
        if cur_date is None:
            cur_date = d
        if d != cur_date:
            # 结束上一个日期批次：先用旧训练集预测上一批，再把上一批并入训练
            train_predict(cur_batch)
            for _d, _c, _row, _y in cur_batch:
                train_rows.append(_row)
                train_y.append(_y)
            cur_date = d
            cur_batch = [(d, c, row, y)]
        else:
            cur_batch.append((d, c, row, y))
    if cur_batch:
        train_predict(cur_batch)
    return preds, actuals, probs


def main(days=180):
    base = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(base, "funds_data.json"), encoding="utf-8") as f:
        data = json.load(f)

    series = {}
    for code in data:
        hist = fa.fetch_history(code, days=days)
        s = [(h["date"], h["nav"]) for h in hist if h.get("nav")]
        if len(s) >= 50:
            series[code] = s

    samples = build_panel(series)
    print("面板样本总数=%d，覆盖 %d 只基金" % (len(samples), len(series)))

    # 模型回测
    preds, actuals, probs = panel_walk_forward(samples)
    model_acc = fe.direction_accuracy(preds, actuals)

    # 动量 / 反转基线（同样按日期滚动的横截面口径）
    mom_hit = rev_hit = tot = 0
    for code, s in series.items():
        navs = [nav for d, nav in s]
        for t in range(20, len(navs) - 1):
            mom_pred = 1 if navs[t] > navs[t - 1] else -1
            actual = 1 if navs[t + 1] > navs[t] else -1
            mom_hit += (mom_pred == actual)
            rev_hit += (mom_pred != actual)
            tot += 1

    print("\n" + "=" * 60)
    print("共享模型（横截面面板）walk-forward")
    print("  预测样本数        : %d" % len(preds))
    print("  弹性网络模型      : %5.1f%%" % (model_acc * 100))
    print("  动量基线          : %5.1f%%" % (mom_hit / tot * 100))
    print("  均值回归基线      : %5.1f%%" % (rev_hit / tot * 100))
    print("  随机基线          : %5.1f%%" % 50.0)
    print("  模型 vs 最优基线  : %+.1fpp" % ((model_acc - max(mom_hit / tot, rev_hit / tot)) * 100))
    print("=" * 60)


if __name__ == "__main__":
    main()
