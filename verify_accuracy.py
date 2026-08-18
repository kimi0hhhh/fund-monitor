# -*- coding: utf-8 -*-
"""方向预测择优覆盖离线验证：walk-forward 模拟「AI 方向 vs 滚动最优基线」择优策略
用法：python verify_accuracy.py
输出：AI 原始方向正确率 / 各基线正确率 / 择优覆盖后正确率（对比能否上 50%）
"""
import sys, json
sys.path.insert(0, ".")
import fund_analysis as fa


def _dir_of(pct, th=0.05):
    if pct is None:
        return None
    return "UP" if pct > th else ("DOWN" if pct < -th else "FLAT")


def main():
    h = json.load(open("analysis_history.json", encoding="utf-8"))
    # 收集样本: (dstr, code, fd, ai_dir, has_calib_override)
    samples = []
    for dstr, day in h.items():
        for code, pred in (day.get("predictions") or {}).items():
            tf = pred.get("tomorrow_forecast") or {}
            ai_dir = str(tf.get("direction", "")).upper()
            if ai_dir not in ("UP", "DOWN", "FLAT"):
                continue
            calib = pred.get("_calibration") or {}
            raw_dir = calib.get("direction_raw")
            fd = pred.get("forecast_date")
            if not fd:
                continue
            samples.append({"dstr": dstr, "code": code, "fd": fd,
                            "ai_dir": ai_dir, "raw_dir": raw_dir})

    # 按分析日分组，拉历史净值，算实际与基线方向
    groups = {}
    hist_cache = {}
    for s in samples:
        code = s["code"]
        if code not in hist_cache:
            try:
                hist = fa.fetch_history(code, 90)
                hist.sort(key=lambda x: x["date"])
            except Exception as e:
                hist = None
            hist_cache[code] = hist or []
        hist = hist_cache[code]
        actual_pct = next((x.get("pct") for x in hist if x.get("date") == s["fd"]), None)
        if actual_pct is None:
            continue  # 净值未更新/网络拿不到
        s["actual_dir"] = _dir_of(actual_pct)
        # 基线方向：只用 dstr 当天及以前的数据（防泄漏）
        hist_before = [x for x in hist if x.get("date") <= s["dstr"]]
        if not hist_before:
            continue
        last_pct = hist_before[-1].get("pct", 0)
        last_dir = _dir_of(last_pct)
        mom_ret = sum(x.get("pct", 0) for x in hist_before[-61:-1]) if len(hist_before) > 1 else last_pct
        mom_dir = "UP" if mom_ret > 0.3 else ("DOWN" if mom_ret < -0.3 else "FLAT")
        rev_dir = {"UP": "DOWN", "DOWN": "UP", "FLAT": "FLAT"}.get(last_dir)
        recent = [x.get("pct", 0) for x in hist_before][-20:]
        ups = sum(1 for p in recent if p > 0.05)
        downs = sum(1 for p in recent if p < -0.05)
        hf_dir = "UP" if ups > downs else ("DOWN" if downs > ups else "FLAT")
        s["mom_dir"], s["rev_dir"], s["hf_dir"] = mom_dir, rev_dir, hf_dir
        groups.setdefault(s["dstr"], []).append(s)

    # walk-forward：每个分析日组的决策用「该组之前全部样本」的累计统计
    order = sorted(groups.keys())
    acc = {"ai": [0, 0], "mom": [0, 0], "rev": [0, 0], "hf": [0, 0]}
    stat = {"total": 0, "correct": 0, "covered": 0, "covered_correct": 0, "ai_raw_correct": 0, "ai_raw_total": 0}
    best_names = {"mom": "动量", "rev": "均值回归", "hf": "历史频率"}
    for i, dstr in enumerate(order):
        # 决策依据：之前所有组的累计正确率（当前组样本不能参与自身决策）
        ai_rate = acc["ai"][0] / acc["ai"][1] if acc["ai"][1] else None
        rates = {}
        for k in ("mom", "rev", "hf"):
            c, n = acc[k]
            rates[k] = c / n if n else None
        best_k, best_r = max(rates.items(), key=lambda kv: (kv[1] if kv[1] is not None else -1))
        cold = ai_rate is None or best_r is None   # 冷启动：无任何历史统计
        for s in groups[dstr]:
            actual = s["actual_dir"]
            if actual is None:
                continue
            # AI 原始方向（校准覆盖前的方向；无 direction_raw 就用已存方向）
            ai_dir = s.get("raw_dir") or s["ai_dir"]
            if ai_dir not in ("UP", "DOWN", "FLAT"):
                continue
            stat["ai_raw_total"] += 1
            if ai_dir == actual:
                stat["ai_raw_correct"] += 1
                acc["ai"][0] += 1
            acc["ai"][1] += 1
            for k in ("mom", "rev", "hf"):
                acc[k][1] += 1
                if s[k + "_dir"] == actual:
                    acc[k][0] += 1
            # 覆盖决策
            use_baseline = False
            if cold:
                # 冷启动：兜底均值回归（回测最强基线）
                best_k = "rev"
                use_baseline = True
            elif best_r is not None and ai_rate is not None and ai_rate < best_r:
                use_baseline = True
            if use_baseline and best_k:
                bdir = s[best_k + "_dir"]
                if bdir not in ("UP", "DOWN"):
                    use_baseline = False  # FLAT 基线不覆盖
            final_dir = s[best_k + "_dir"] if (use_baseline and best_k) else ai_dir
            stat["total"] += 1
            ok = final_dir == actual
            if ok:
                stat["correct"] += 1
            if use_baseline:
                stat["covered"] += 1
                if ok:
                    stat["covered_correct"] += 1

    print(f'{"策略":<16}{"正确率":<10}{"样本":<6}')
    print("-" * 36)
    n = stat["ai_raw_total"]
    if n:
        print(f'{"AI 原始方向":<14}{round(stat["ai_raw_correct"]/n*100,1)}%{n}')
        for k in ("mom", "rev", "hf"):
            c, nn = acc[k]
            if nn:
                print(f'{best_names[k]:<14}{round(c/nn*100,1)}%{nn}')
    if stat["total"]:
        print("-" * 36)
        print(f'{"择优覆盖后":<14}{round(stat["correct"]/stat["total"]*100,1)}%{stat["total"]}'
              f'（覆盖 {stat["covered"]} 次，覆盖中正确 {stat["covered_correct"]}）')
    # 兜底策略：全部用均值回归
    rev_c, rev_n = acc["rev"]
    if rev_n:
        print(f'{"纯均值回归":<14}{round(rev_c/rev_n*100,1)}%{rev_n}')


if __name__ == "__main__":
    main()
