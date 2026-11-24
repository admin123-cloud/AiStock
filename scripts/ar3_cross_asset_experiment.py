# -*- coding: utf-8 -*-
"""AR3 跨资产全天候动量轮动（基于 ClickHouse 独立表 stock.etf_kline_daily）严格单账户零杠杆实证实验。

防未来函数与防幻觉铁律：
1. 100% 场内真实前复权高流动性 ETF（5大类14只，零个股生存者偏差/零暴雷）；
2. 严格 T-1 收盘数据计算信号与权重 -> T 日开盘价 open 撮合，精确分离隔夜跳空与日内收益，扣双边 ETF 成本 0.12%；
3. 单账户现金池硬锁死总仓位 <= 100%（零隐形杠杆）；
4. 严格 IS / Val / OOS 三段独立验收，对比沪深300与 AR1 v2.1。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ar1_alpha_experiment import _build_ar1_regimes, _ch_query_df, _eval_nav  # noqa: E402
from services.mining.backtest_engine import split_trade_dates  # noqa: E402
from services.mining.feature_store import build_or_load_feature_panel  # noqa: E402
from utils.common.paths import runtime_path  # noqa: E402

ETF_ONE_WAY_COST = 0.0006  # 场内 ETF 单边佣金+滑点（免印花税，往返 0.12%）
SAFE_BOND_CODE = "511260.SH"


CORE_CROSS_ASSET_CLASSES = ("cn_broad", "cn_dividend", "gold", "overseas", "bond")


def _load_etf_panels(*, include_sectors: bool = False) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, str], dict[str, str]]:
    cls_filter = ""
    if not include_sectors:
        cls_sql = ",".join(f"'{c}'" for c in CORE_CROSS_ASSET_CLASSES)
        cls_filter = f" AND asset_class IN ({cls_sql})"
    raw = _ch_query_df(
        f"""
        SELECT code, name, asset_class, toString(trade_date) AS trade_date, open, close
        FROM stock.etf_kline_daily FINAL
        WHERE trade_date >= '2022-06-01' AND trade_date <= '2026-11-30'{cls_filter}
        ORDER BY code, trade_date
        """
    )
    meta_df = raw.drop_duplicates("code")[["code", "name", "asset_class"]]
    code_to_class = dict(zip(meta_df["code"], meta_df["asset_class"]))
    code_to_name = dict(zip(meta_df["code"], meta_df["name"]))
    piv_o = raw.pivot(index="trade_date", columns="code", values="open").sort_index().ffill()
    piv_c = raw.pivot(index="trade_date", columns="code", values="close").sort_index().ffill()
    return piv_o, piv_c, code_to_class, code_to_name


def simulate_cross_asset_strategy(
    day_df: pd.DataFrame,
    piv_o: pd.DataFrame,
    piv_c: pd.DataFrame,
    code_to_class: dict[str, str],
    *,
    mode: str,
    top_k: int = 2,
    rebal_every: int = 5,
    one_per_class: bool = True,
    use_panic_overlay: bool = False,
) -> tuple[pd.Series, dict[str, Any]]:
    """严格 T-1 -> T 开盘撮合、单账户现金池总仓位 <= 100% 的跨资产 ETF 仿真器。"""
    dates = day_df["trade_date"].astype(str).tolist()
    reg_map = dict(zip(dates, day_df["ar1_regime"].tolist()))
    breadth_map = dict(zip(dates, day_df["market_above_ma20_pct"].fillna(50.0).tolist()))

    # 预计算严格 T 日收盘已知的动量与波动率矩阵
    ret10 = piv_c.pct_change(10)
    ret20 = piv_c.pct_change(20)
    ma20 = piv_c.rolling(20).mean()
    above_ma20 = piv_c > ma20
    vol20 = piv_c.pct_change().rolling(20).std() * np.sqrt(242.0)
    risk_adj_mom = ret20 / (vol20 + 0.05)
    dual_mom = 0.5 * ret10 + 0.5 * ret20

    # 冰点企稳反抽监测（用真实场内 ETF：159915.SZ + 512100.SH）
    panic_pair_cc = piv_c[["159915.SZ", "512100.SH"]].pct_change().mean(axis=1)

    all_codes = list(piv_c.columns)
    non_cn_broad = [c for c in all_codes if code_to_class.get(c) != "cn_broad"]
    cn_broad_codes = [c for c in all_codes if code_to_class.get(c) == "cn_broad"]

    def _pick_top(prev_dt: str, pool: list[str], score_df: pd.DataFrame, k: int, per_class: bool) -> list[str]:
        if prev_dt not in score_df.index:
            return []
        s_row = score_df.loc[prev_dt]
        a_row = above_ma20.loc[prev_dt]
        r20_row = ret20.loc[prev_dt]
        cands = []
        for c in pool:
            if c not in s_row.index or pd.isna(s_row[c]) or pd.isna(r20_row.get(c)):
                continue
            if bool(a_row.get(c, False)) and float(r20_row[c]) > 0.0:
                cands.append((c, float(s_row[c])))
        cands.sort(key=lambda x: x[1], reverse=True)
        chosen: list[str] = []
        seen_cls: set[str] = set()
        for c, _ in cands:
            cls = code_to_class.get(c, "other")
            if per_class and cls in seen_cls:
                continue
            chosen.append(c)
            seen_cls.add(cls)
            if len(chosen) >= k:
                break
        return chosen

    cur_w: dict[str, float] = {}
    nav = 1.0
    nav_list = [1.0]
    days_since_rebal = rebal_every
    rebal_cnt = 0
    max_gross = 0.0
    panic_active = False
    panic_days = 0
    panic_cum = 1.0
    panic_cd = 0
    panic_hits = 0
    hold_counts: dict[str, int] = {}

    for i in range(1, len(dates)):
        prev_dt = dates[i - 1]
        dt = dates[i]
        reg_prev = reg_map.get(prev_dt, "balanced")
        b_prev = breadth_map.get(prev_dt, 50.0)
        pair_r_prev = float(panic_pair_cc.get(prev_dt, 0.0)) if prev_dt in panic_pair_cc.index else 0.0

        # 1. 冰点企稳右侧触发状态机
        panic_changed = False
        if use_panic_overlay:
            if panic_active:
                if panic_days >= 5 or (panic_cum - 1.0) <= -0.06 or (panic_cum - 1.0) >= 0.12 or b_prev >= 38.0:
                    panic_active = False
                    panic_days = 0
                    panic_cd = 2
                    panic_changed = True
            else:
                if panic_cd > 0:
                    panic_cd -= 1
                elif b_prev <= 18.0 and pair_r_prev >= 0.005:
                    panic_active = True
                    panic_days = 0
                    panic_cum = 1.0
                    panic_hits += 1
                    panic_changed = True

        days_since_rebal += 1
        need_eval = panic_changed or (not panic_active and (days_since_rebal >= rebal_every or not cur_w))
        if mode.startswith("regime_") and i >= 2 and reg_prev != reg_map.get(dates[i - 2], reg_prev):
            need_eval = True

        target_w: dict[str, float] = dict(cur_w)
        if need_eval:
            target_w = {}
            if panic_active:
                # 60% 切入创业板+中证1000 冰点反抽，40% 放跨资产避险最强（否则十年国债）
                target_w["159915.SZ"] = 0.30
                target_w["512100.SH"] = 0.30
                safe_top = _pick_top(prev_dt, non_cn_broad, risk_adj_mom, 1, False)
                safe_c = safe_top[0] if safe_top else SAFE_BOND_CODE
                target_w[safe_c] = target_w.get(safe_c, 0.0) + 0.40
            elif mode == "pure_mom20":
                chosen = _pick_top(prev_dt, all_codes, ret20, top_k, one_per_class)
                if chosen:
                    for c in chosen:
                        target_w[c] = 1.0 / len(chosen)
                else:
                    target_w[SAFE_BOND_CODE] = 1.0
            elif mode == "risk_adj_mom":
                chosen = _pick_top(prev_dt, all_codes, risk_adj_mom, top_k, one_per_class)
                if chosen:
                    for c in chosen:
                        target_w[c] = 1.0 / len(chosen)
                else:
                    target_w[SAFE_BOND_CODE] = 1.0
            elif mode == "dual_mom":
                chosen = _pick_top(prev_dt, all_codes, dual_mom, top_k, one_per_class)
                if chosen:
                    for c in chosen:
                        target_w[c] = 1.0 / len(chosen)
                else:
                    target_w[SAFE_BOND_CODE] = 1.0
            elif mode == "regime_hybrid":
                if reg_prev in ("offensive", "extreme_offensive"):
                    cn_top = _pick_top(prev_dt, cn_broad_codes, ret20, 2, False)
                    cross_top = _pick_top(prev_dt, non_cn_broad, risk_adj_mom, 1, False)
                    if cn_top:
                        for c in cn_top:
                            target_w[c] = 0.60 / len(cn_top)
                    if cross_top:
                        target_w[cross_top[0]] = target_w.get(cross_top[0], 0.0) + 0.40
                    rem = 1.0 - sum(target_w.values())
                    if rem > 0.01:
                        target_w[SAFE_BOND_CODE] = target_w.get(SAFE_BOND_CODE, 0.0) + rem
                elif reg_prev == "balanced":
                    chosen = _pick_top(prev_dt, all_codes, risk_adj_mom, 2, True)
                    if chosen:
                        for c in chosen:
                            target_w[c] = 1.0 / len(chosen)
                    else:
                        target_w[SAFE_BOND_CODE] = 1.0
                else:
                    # A 股防守期：100% 切入非 A 股宽基的跨资产（黄金/纳指/标普/红利/国债）
                    chosen = _pick_top(prev_dt, non_cn_broad, risk_adj_mom, top_k, True)
                    if chosen:
                        for c in chosen:
                            target_w[c] = 1.0 / len(chosen)
                    else:
                        target_w[SAFE_BOND_CODE] = 1.0
            elif mode == "regime_focus":
                if reg_prev in ("offensive", "extreme_offensive"):
                    cn_top = _pick_top(prev_dt, cn_broad_codes, ret20, 2, False)
                    if cn_top:
                        for c in cn_top:
                            target_w[c] = 1.0 / len(cn_top)
                    else:
                        chosen = _pick_top(prev_dt, non_cn_broad, ret20, 1, False)
                        target_w[chosen[0] if chosen else SAFE_BOND_CODE] = 1.0
                else:
                    chosen = _pick_top(prev_dt, non_cn_broad, ret20, 1, False)
                    target_w[chosen[0] if chosen else SAFE_BOND_CODE] = 1.0

            days_since_rebal = 0

        # 硬锁死总仓位 <= 1.0（零杠杆）
        gross = sum(target_w.values())
        if gross > 1.0:
            target_w = {k: v / gross for k, v in target_w.items()}
            gross = 1.0
        max_gross = max(max_gross, gross)

        # 2. 今日开盘严格撮合（分离隔夜跳空 + 调仓成本 + 日内收益）
        day_r = 0.0
        if dt in piv_o.index and dt in piv_c.index and prev_dt in piv_c.index:
            o_row = piv_o.loc[dt]
            c_row = piv_c.loc[dt]
            pc_row = piv_c.loc[prev_dt]

            for c, cw in cur_w.items():
                if pd.notna(o_row.get(c)) and pd.notna(pc_row.get(c)) and float(pc_row[c]) > 0:
                    day_r += cw * (float(o_row[c]) / float(pc_row[c]) - 1.0)

            turnover = sum(abs(target_w.get(c, 0.0) - cur_w.get(c, 0.0)) for c in set(target_w) | set(cur_w))
            if turnover > 0.02:
                day_r -= turnover * ETF_ONE_WAY_COST
                cur_w = dict(target_w)
                rebal_cnt += 1

            pair_intra = 0.0
            for c, tw in cur_w.items():
                hold_counts[c] = hold_counts.get(c, 0) + 1
                if pd.notna(o_row.get(c)) and pd.notna(c_row.get(c)) and float(o_row[c]) > 0:
                    r_oc = float(c_row[c]) / float(o_row[c]) - 1.0
                    day_r += tw * r_oc
                    if panic_active and c in ("159915.SZ", "512100.SH"):
                        pair_intra += r_oc * 0.5

            if panic_active:
                panic_days += 1
                if panic_days == 1:
                    panic_cum *= 1.0 + pair_intra
                else:
                    panic_cum *= 1.0 + float(panic_pair_cc.get(dt, 0.0))

        nav *= 1.0 + day_r
        nav_list.append(nav)

    top_held = sorted(hold_counts.items(), key=lambda x: x[1], reverse=True)[:4]
    return pd.Series(nav_list, index=dates, dtype=np.float64), {
        "rebal_cnt": rebal_cnt,
        "max_gross_pct": round(max_gross * 100.0, 1),
        "panic_hits": panic_hits,
        "top_held": top_held,
    }


def main() -> int:
    panel_df, bench_df = build_or_load_feature_panel(start_date="2023-01-01", min_amt_20d=50_000_000.0)
    day_df = _build_ar1_regimes(panel_df, bench_df)
    sp = split_trade_dates(day_df["trade_date"].astype(str).tolist())
    dates = day_df["trade_date"].astype(str).tolist()

    piv_o, piv_c, code_to_class, code_to_name = _load_etf_panels()

    hs_nav = pd.Series(
        piv_c.loc[dates, "510300.SH"].astype(float).values / float(piv_c.loc[dates[0], "510300.SH"]),
        index=dates,
    )

    configs = [
        ("AR3_1_PureCrossMom20_Top2", dict(mode="pure_mom20", top_k=2, rebal_every=5, one_per_class=True, use_panic_overlay=False)),
        ("AR3_2_RiskAdjMom_Top2", dict(mode="risk_adj_mom", top_k=2, rebal_every=5, one_per_class=True, use_panic_overlay=False)),
        ("AR3_3_DualMom_10_20_Top2", dict(mode="dual_mom", top_k=2, rebal_every=5, one_per_class=True, use_panic_overlay=False)),
        ("AR3_4_RiskAdjMom_Top1_Focus", dict(mode="risk_adj_mom", top_k=1, rebal_every=5, one_per_class=True, use_panic_overlay=False)),
        ("AR3_5_AR1Regime_CrossAsset", dict(mode="regime_hybrid", top_k=2, rebal_every=5, one_per_class=True, use_panic_overlay=True)),
        ("AR3_6_RiskAdjMom_Top2+Panic", dict(mode="risk_adj_mom", top_k=2, rebal_every=5, one_per_class=True, use_panic_overlay=True)),
        ("AR3_7_DualMom_Top2+Panic", dict(mode="dual_mom", top_k=2, rebal_every=5, one_per_class=True, use_panic_overlay=True)),
        ("AR3_8_PureMom20_Top1+Panic", dict(mode="pure_mom20", top_k=1, rebal_every=5, one_per_class=False, use_panic_overlay=True)),
        ("AR3_9_RegimeFocus_Top1+Panic", dict(mode="regime_focus", top_k=1, rebal_every=5, one_per_class=False, use_panic_overlay=True)),
    ]

    def pack(label: str, nav: pd.Series, meta: dict[str, Any]) -> dict[str, Any]:
        segs = {sk: _eval_nav(nav[(nav.index >= d0) & (nav.index <= d1)]) for sk, (d0, d1) in sp.items()}
        return {"label": label, "full": _eval_nav(nav), "segments": segs, "meta": meta}

    results = [pack("B0_510300_HS300ETF_BuyHold", hs_nav, {})]
    for lbl, kw in configs:
        nav, m = simulate_cross_asset_strategy(day_df, piv_o, piv_c, code_to_class, **kw)
        # 翻译 top_held 名称
        m["top_held_names"] = [f"{code_to_name.get(c, c)}({cnt}d)" for c, cnt in m.pop("top_held")]
        results.append(pack(lbl, nav, m))

    results.sort(key=lambda x: x["full"]["ann_ret_pct"], reverse=True)
    out_path = runtime_path("mining_runs") / "ar3_cross_asset_experiment_v1.json"
    out_path.write_text(json.dumps({"splits": sp, "results": results}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print("=" * 124)
    print(f"AR3 跨资产全天候动量轮动实证排行榜（100%真实场内ETF · 单账户零杠杆 | IS[{sp['is'][0]}~{sp['is'][1]}] | Val[{sp['val'][0]}~{sp['val'][1]}] | OOS[{sp['oos'][0]}~{sp['oos'][1]}]）")
    print("=" * 124)
    for idx, r in enumerate(results, 1):
        f = r["full"]
        s_is, s_val, s_oos = r["segments"]["is"], r["segments"]["val"], r["segments"]["oos"]
        print(
            f"#{idx:02d} {r['label']:32s} | FULL: 累计{f['cum_ret_pct']:+6.1f}% 年化{f['ann_ret_pct']:+5.1f}% 回撤{f['max_dd_pct']:4.1f}% 夏普{f['sharpe']:4.2f} 卡玛{f['calmar']:4.2f}"
        )
        print(
            f"    IS={s_is['cum_ret_pct']:+6.1f}%(DD{s_is['max_dd_pct']:4.1f}%) | "
            f"Val={s_val['cum_ret_pct']:+6.1f}%(DD{s_val['max_dd_pct']:4.1f}%) | "
            f"OOS={s_oos['cum_ret_pct']:+6.1f}%(DD{s_oos['max_dd_pct']:4.1f}%) | {r['meta']}"
        )
    print("=" * 124)
    print(f"报告已落盘：{out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
