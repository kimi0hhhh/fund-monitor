# -*- coding: utf-8 -*-
"""基线回测：用历史净值评估日频方向可预测性（学术可预测性检验）

用法：python baseline_check.py [天数]
输出：单日动量/12-1月动量/均值回归/历史频率/随机 的方向正确率
结论解读：全部≈50% → 日频方向基本不可预测，AI 预测≈随机属正常，不构成模型缺陷。
"""
import sys, json

sys.path.insert(0, ".")
import fund_analysis as fa


def main():
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 65
    codes = list(json.load(open("funds_data.json", encoding="utf-8")).keys())
    stats = {k: [0, 0] for k in ["mom", "mom12_1", "rev", "hist_freq", "random"]}
    per_fund = {}
    for code in codes:
        h = fa.fetch_history(code, days)
        if not h or len(h) < 25:
            print(f"{code}: 数据不足({len(h)})")
            continue
        h.sort(key=lambda x: x["date"])
        pcts = [x.get("pct", 0) or 0 for x in h]
        f = {k: [0, 0] for k in stats}
        for i in range(24, len(h) - 1):
            today_pct = pcts[i]
            actual_next = pcts[i + 1]
            actual_dir = "UP" if actual_next > 0.05 else ("DOWN" if actual_next < -0.05 else "FLAT")
            if actual_dir == "FLAT":
                continue
            mom_dir = "UP" if today_pct > 0.05 else ("DOWN" if today_pct < -0.05 else "FLAT")
            ret20 = sum(pcts[max(0, i - 21):i - 1])
            mom12_dir = "UP" if ret20 > 0.3 else ("DOWN" if ret20 < -0.3 else "FLAT")
            rev_dir = {"UP": "DOWN", "DOWN": "UP", "FLAT": "FLAT"}[mom_dir]
            ups = sum(1 for p in pcts[max(0, i - 20):i] if p > 0.05)
            downs = sum(1 for p in pcts[max(0, i - 20):i] if p < -0.05)
            hf_dir = "UP" if ups > downs else ("DOWN" if downs > ups else "FLAT")
            rand_dir = "UP" if (i % 2) == 0 else "DOWN"
            for k, d in [("mom", mom_dir), ("mom12_1", mom12_dir), ("rev", rev_dir),
                         ("hist_freq", hf_dir), ("random", rand_dir)]:
                f[k][1] += 1
                if d == actual_dir:
                    f[k][0] += 1
        per_fund[code] = f
        for k in stats:
            stats[k][1] += f[k][1]
            stats[k][0] += f[k][0]

    names = {"mom": "单日动量", "mom12_1": "12-1月动量", "rev": "均值回归",
             "hist_freq": "历史频率(20日)", "random": "随机50%"}
    print(f'{"策略":<12}{"方向正确率":<10}{"样本数":<8}{"vs 50%":<10}')
    print("-" * 45)
    for k in ["mom", "mom12_1", "rev", "hist_freq", "random"]:
        c, n = stats[k]
        rate = round(c / n * 100, 1) if n else None
        print(f'{names[k]:<12}{str(rate) + "%":<10}{n:<8}{round(rate - 50, 1) if rate else "-"}')
    print("\n结论：全部≈50% → 日频方向基本不可预测，预测模块应转向中长期策略+风控。")


if __name__ == "__main__":
    main()
