# -*- coding: utf-8 -*-
"""
factor_engine.py — 次日方向预测的确定性因子引擎（Phase 1 MVP）

方法论（源自 Gu-Kelly-Xiu 2020 RFS + APT 多因子，纯 Python 零新依赖）：

  预测目标   E_t[r_{t+1}] = g*(x_t)          （次日收益 = 当日因子向量 x_t 的函数）
  方向       y_hat = sign(g*(x_t))          （+1 涨 / -1 跌）
  合成模型   弹性网络逻辑回归（L1+L2 正则），近端梯度下降求解
  验证       walk-forward 回测 + 方向正确率，对比动量/随机基线

设计约束：
  - 纯 Python，不依赖 numpy/sklearn/requests，可打包进 exe
  - 全程确定性：固定初始化、固定迭代、无随机采样、无 set 遍历
  - LLM 不参与方向判断；本模块只做「因子 + 模型」，方向/概率全由确定性计算产出

用法（独立验证）：
  python factor_engine.py           # 用 funds_data.json 的 navmap 做冒烟自测
"""

import math

# ==================== 因子定义 ====================
# 因子顺序即特征向量列顺序，改动需同步 FACTOR_NAMES 与 build_features
FACTOR_NAMES = ["mom1", "mom5", "mom12_1", "vol20", "ma_ratio", "zscore", "rsi14"]


def to_returns(navs):
    """净值序列 → 日收益率(%)列表，长度比 navs 少 1"""
    out = []
    for i in range(1, len(navs)):
        prev, cur = navs[i - 1], navs[i]
        if prev and cur and prev > 0:
            out.append((cur - prev) / prev * 100.0)
    return out


def _mom(rets, n):
    """n 日累计收益率（最近 n 日）"""
    if len(rets) < n:
        return None
    return sum(rets[-n:])


def _mom_12_1(rets):
    """12-1 动量：过去 12 日累计、跳过最近 1 日（经典动量因子）"""
    if len(rets) < 12:
        return None
    return sum(rets[-12:-1])


def _vol(rets, n=20):
    """n 日波动率：收益率样本标准差"""
    if len(rets) < n:
        return None
    r = rets[-n:]
    m = sum(r) / n
    var = sum((x - m) ** 2 for x in r) / (n - 1)
    return math.sqrt(var)


def _ma_ratio(navs, short=5, long=20):
    """趋势：MA5 / MA20（>1 短期强于长期 = 上升趋势）"""
    if len(navs) < long:
        return None
    ma_s = sum(navs[-short:]) / short
    ma_l = sum(navs[-long:]) / long
    if ma_l <= 0:
        return None
    return ma_s / ma_l


def _zscore(navs, n=20):
    """均值回归：当前净值相对 MA20 的偏离（z 分数）"""
    if len(navs) < n:
        return None
    w = navs[-n:]
    m = sum(w) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in w) / n)
    if sd <= 0:
        return 0.0
    return (navs[-1] - m) / sd


def _rsi(navs, n=14):
    """RSI 相对强弱指标（简化版，区间 0~100）"""
    if len(navs) < n + 1:
        return None
    gains = losses = 0.0
    for i in range(-n, 0):
        d = navs[i] - navs[i - 1]
        if d >= 0:
            gains += d
        else:
            losses -= d
    if losses == 0:
        return 100.0
    rs = gains / losses
    return 100.0 - 100.0 / (1.0 + rs)


def build_features(navs):
    """给定「截至 T 日」的净值序列，返回该时点的因子向量（dict）。

    navs 必须是按日期正序的净值列表（含当日）。
    任一因子数据不足时该因子为 None。
    """
    rets = to_returns(navs)
    return {
        "mom1": _mom(rets, 1),
        "mom5": _mom(rets, 5),
        "mom12_1": _mom_12_1(rets),
        "vol20": _vol(rets, 20),
        "ma_ratio": _ma_ratio(navs, 5, 20),
        "zscore": _zscore(navs, 20),
        "rsi14": _rsi(navs, 14),
    }


def feature_row(feats):
    """dict → 按 FACTOR_NAMES 顺序的数值列表；含 None 则返回 None"""
    vals = [feats.get(k) for k in FACTOR_NAMES]
    if any(v is None for v in vals):
        return None
    return [float(v) for v in vals]


# ==================== 样本构造 ====================

def build_sample_series(navs, min_len=21):
    """把净值序列转成 (X, y, t_idx) 时序样本。

    X[t] 由 navs[:t+1]（截至 t 日）算因子得到；
    y[t] = +1（t+1 日相对 t 日上涨）或 -1（下跌）。
    min_len：因子所需最少历史长度（vol20/zscore/ma_ratio 需要 20 日，+1）。
    返回：X(list[list[float]]), y(list[int]), t_idx(list[int])
    """
    X, y, t_idx = [], [], []
    for t in range(min_len - 1, len(navs) - 1):
        feats = build_features(navs[: t + 1])
        row = feature_row(feats)
        if row is None:
            continue
        X.append(row)
        y.append(1 if navs[t + 1] > navs[t] else -1)
        t_idx.append(t)
    return X, y, t_idx


# ==================== 标准化 ====================

def _mean_std(rows):
    """按列算 mean / std（用于 z-score 标准化）。std=0 时记 1 避免除零。"""
    n = len(rows)
    k = len(rows[0])
    mean = [sum(r[j] for r in rows) / n for j in range(k)]
    std = [1.0] * k
    for j in range(k):
        var = sum((r[j] - mean[j]) ** 2 for r in rows) / n
        std[j] = math.sqrt(var) if var > 1e-12 else 1.0
    return mean, std


def zscore_normalize(rows, mean, std):
    """用给定 mean/std 标准化一组行（训练统计应用到训练/测试，防泄漏）"""
    out = []
    for r in rows:
        out.append([(r[j] - mean[j]) / std[j] for j in range(len(r))])
    return out


# ==================== 弹性网络逻辑回归 ====================

def _sigmoid(z):
    """数值稳定的 sigmoid"""
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    e = math.exp(z)
    return e / (1.0 + e)


def _soft_threshold(v, lam):
    """L1 近端算子：soft-thresholding"""
    if v > lam:
        return v - lam
    if v < -lam:
        return v + lam
    return 0.0


def elastic_net_logistic(X, y, l1=0.05, l2=0.05, lr=0.05, iters=400):
    """弹性网络正则逻辑回归（近端梯度下降，全确定性）。

    X: list[list[float]]（已标准化），y: list[int]（+1/-1）
    损失 = (1/N)Σ log(1+exp(-y·x·θ)) + l1·||θ||₁ + 0.5·l2·||θ||₂²
    返回 theta(list[float])，不含截距（X 已标准化、样本对称，截距近似 0）。
    """
    n = len(X)
    k = len(X[0])
    theta = [0.0] * k
    for _ in range(iters):
        # 逻辑损失梯度
        grad = [0.0] * k
        for i in range(n):
            margin = y[i] * sum(X[i][j] * theta[j] for j in range(k))
            w = _sigmoid(-margin)  # 权重 = sigmoid(-y·x·θ)
            for j in range(k):
                grad[j] += -y[i] * X[i][j] * w
        for j in range(k):
            grad[j] = grad[j] / n + l2 * theta[j]
        # 近端梯度下降：先梯度步，再 L1 soft-threshold
        for j in range(k):
            v = theta[j] - lr * grad[j]
            theta[j] = _soft_threshold(v, lr * l1)
    return theta


def predict_prob(x, theta):
    """sigmoid(x·θ) → P(y=+1)"""
    z = sum(x[j] * theta[j] for j in range(len(theta)))
    return _sigmoid(z)


# ==================== walk-forward 回测 ====================

def walk_forward(navs, train_init=25, min_len=21, l1=0.05, l2=0.05, lr=0.05, iters=400):
    """单只基金 walk-forward 回测。

    用前 train_init 个样本训练第一个模型，之后逐样本滚动预测。
    返回 (preds, actuals, probs)：
      preds   预测方向 +1/-1（按概率>0.5 判涨）
      actuals 实际方向 +1/-1
      probs   预测概率 P(涨)
    """
    X, y, t_idx = build_sample_series(navs, min_len)
    if len(X) < train_init + 1:
        return [], [], []
    preds, actuals, probs = [], [], []
    for i in range(train_init, len(X)):
        train_X = X[:i]
        train_y = y[:i]
        mean, std = _mean_std(train_X)
        tr = zscore_normalize(train_X, mean, std)
        te = zscore_normalize([X[i]], mean, std)[0]
        theta = elastic_net_logistic(tr, train_y, l1, l2, lr, iters)
        p = predict_prob(te, theta)
        preds.append(1 if p >= 0.5 else -1)
        actuals.append(y[i])
        probs.append(p)
    return preds, actuals, probs


def direction_accuracy(preds, actuals):
    """方向正确率"""
    if not preds:
        return 0.0
    hit = sum(1 for p, a in zip(preds, actuals) if p == a)
    return hit / len(preds)


# ==================== 基线 ====================

def momentum_baseline(navs, min_len=21):
    """动量基线：用今日方向押明日（今天涨→预测明天涨），返回正确率"""
    correct = total = 0
    for t in range(min_len - 1, len(navs) - 1):
        if navs[t] > navs[t - 1]:
            pred = 1
        else:
            pred = -1
        actual = 1 if navs[t + 1] > navs[t] else -1
        correct += (pred == actual)
        total += 1
    return (correct / total) if total else 0.0


def reversal_baseline(navs, min_len=21):
    """均值回归基线：与今日方向相反，返回正确率"""
    correct = total = 0
    for t in range(min_len - 1, len(navs) - 1):
        if navs[t] > navs[t - 1]:
            pred = -1
        else:
            pred = 1
        actual = 1 if navs[t + 1] > navs[t] else -1
        correct += (pred == actual)
        total += 1
    return (correct / total) if total else 0.0


# ==================== 冒烟自测 ====================

def _smoke_test():
    """用 funds_data.json 的 navmap 做最小自测：验证因子/模型/回测代码正确跑通"""
    import json
    import os
    base = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(base, "funds_data.json"), encoding="utf-8") as f:
        data = json.load(f)
    # 取一只基金，用 navmap（按日期排序）构造净值序列
    code = "000217"
    navmap = data[code]["navmap"]
    navs = [navmap[d] for d in sorted(navmap.keys())]
    print("[smoke] %s 净值序列长度=%d" % (code, len(navs)))
    feats = build_features(navs)
    print("[smoke] 因子:", {k: (round(v, 4) if v is not None else None) for k, v in feats.items()})
    X, y, t = build_sample_series(navs, min_len=21)
    print("[smoke] 样本数=%d" % len(X))
    if len(X) >= 3:
        mean, std = _mean_std(X)
        zx = zscore_normalize(X, mean, std)
        theta = elastic_net_logistic(zx, y, iters=200)
        print("[smoke] theta 非零系数=%d/%d" % (sum(1 for v in theta if abs(v) > 1e-9), len(theta)))
    print("[smoke] OK")


if __name__ == "__main__":
    _smoke_test()
