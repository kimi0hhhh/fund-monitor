# -*- coding: utf-8 -*-
"""
test_portfolio_backtest.py — 组合加权投票方向回测验证

验证「按持仓金额加权的 12-1 动量方向」在组合层面的次日方向正确率，
对比组合动量跟涨基线 / 随机 50%。
"""
import json
import sys
import os
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fund_analysis as fa


def mom12_dir_from_navs(navs):
    """navs 截至 t 日，返回 12-1 动量方向（跳过最近 1 日）"""
    if len(navs) < 13:
        return None
    rets = [(navs[i] - navs[i - 1]) / navs[i - 1] * 100 for i in range(1, len(navs))]
    mom = sum(rets[-12:-1])
    if mom > 0:
        return "UP"
    if mom < 0:
        return "DOWN"
    return "FLAT"


def main(days=180):
    data = json.load(open("funds_data.json", encoding="utf-8"))
    # {code: {dates: [...], navs: [...], shares}}
    series = {}
    for code, d in data.items():
        hist = fa.fetch_history(code, days)
        s = [(h["date"], h["nav"]) for h in hist if h.get("nav")]
        if len(s) >= 50:
            series[code] = {"dates": [x[0] for x in s],
                            "navs": [x[1] for x in s],
                            "shares": d.get("shares", 0) or 0}

    # 每只基金每天：方向 + 次日实际涨跌（用各自时间轴）
    # 收集 (date, code, dir, value, next_ret)
    daily = defaultdict(list)  # date -> [ (code, dir, value, next_ret) ]
    for code, s in series.items():
        navs = s["navs"]
        dates = s["dates"]
        shares = s["shares"]
        for t in range(13, len(navs) - 1):
            d = mom12_dir_from_navs(navs[: t + 1])
            if d in ("UP", "DOWN"):
                value = shares * navs[t]
                next_ret = (navs[t + 1] - navs[t]) / navs[t] * 100
                daily[dates[t]].append((code, d, value, next_ret))

    # 按日期组合：加权方向 vs 加权次日涨跌
    dates = sorted(daily.keys())
    model_hit = mom_hit = tot = 0
    up_cnt = down_cnt = 0
    for i in range(len(dates) - 1):
        rows = daily[dates[i]]
        up_w = down_w = 0.0
        ret_sum = 0.0
        w_sum = 0.0
        prev_ret_sum = 0.0
        for code, d, value, next_ret in rows:
            if d == "UP":
                up_w += value
            else:
                down_w += value
            ret_sum += value * next_ret
            w_sum += value
        if w_sum <= 0:
            continue
        comb_dir = "UP" if up_w > down_w else "DOWN"
        if up_w > down_w:
            up_cnt += 1
        else:
            down_cnt += 1
        comb_ret = ret_sum / w_sum  # 组合次日加权涨跌
        actual = "UP" if comb_ret > 0 else "DOWN"
        model_hit += (comb_dir == actual)
        # 组合动量跟涨基线：用「上一日组合方向」押「今日」（近似：用今日方向押次日=跟涨）
        # 简化：用「组合自身动量方向」已经是 12-1 动量；这里跟涨基线=用最近1日方向
        tot += 1

    print("=" * 60)
    print("组合加权投票回测（%d 个交易日）" % tot)
    print("  组合加权 12-1 动量方向正确率 : %5.1f%%" % (model_hit / tot * 100))
    print("  方向分布：UP %d 天 / DOWN %d 天" % (up_cnt, down_cnt))
    print("  随机基线                        : %5.1f%%" % 50.0)
    print("=" * 60)


if __name__ == "__main__":
    main()
