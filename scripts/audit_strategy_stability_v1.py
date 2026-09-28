from __future__ import annotations

"""研究用途：对 G3 已回放交易做稳定性、成本与仓位候选审计。"""
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))
import numpy as np
import pandas as pd
from utils.paths import report_path

SOURCE = report_path("g3_recalled_mainwave_contract_v1", "two_slot_closed_trades.csv")
OUT = report_path("g3_strategy_stability_audit_v2")
SEED = 20260926
COSTS = {"zero": 0.0, "low": 0.002, "stress": 0.005}

@dataclass(frozen=True)
class Variant:
    name: str
    description: str
    def weight(self, df: pd.DataFrame) -> pd.Series:
        m = pd.to_numeric(df["index_mom60"], errors="coerce").fillna(0.0)
        if self.name == "base":
            return pd.Series(1.0, index=df.index)
        if self.name == "mom_negative_half":
            return pd.Series(np.where(m < 0, 0.5, 1.0), index=df.index)
        if self.name == "mom_negative_quarter":
            return pd.Series(np.where(m < -0.05, 0.25, np.where(m < 0, 0.5, 1.0)), index=df.index)
        raise ValueError(self.name)

VARIANTS = [
    Variant("base", "现行两槽等权研究口径"),
    Variant("mom_negative_half", "指数60日动量为负时仓位减半"),
    Variant("mom_negative_quarter", "指数60日动量<-5%时四分之一仓位，负动量时半仓"),
]

def _max_dd(x: pd.Series) -> float:
    curve = x.fillna(0).cumsum()
    return float((curve - curve.cummax()).min())

def _provenance(df: pd.DataFrame) -> dict:
    same_day = pd.to_datetime(df["exit_date"]).dt.normalize().eq(pd.to_datetime(df["entry_datetime"]).dt.normalize())
    late = pd.to_datetime(df["entry_datetime"]).dt.strftime("%H:%M:%S").ne("10:00:00")
    return {"source_rows": int(len(df)), "same_day_exit_rows": int(same_day.sum()), "non_first_bar_rows": int(late.sum()), "blockers": ["旧样本使用MA20承接，不符合当前首根10:00放量突破合同", "缺少同业共振>=2逐票据证据", "存在T+0样本，T+1未完整建模", "退出为日线OHLC代理，未证明真实可成交", "2025/2026已被使用，不能称独立盲测"]}

def _metrics(x: pd.Series) -> dict:
    x = x.dropna().astype(float)
    return {"trades": int(len(x)), "total_return": float(x.sum()), "mean_trade": float(x.mean()) if len(x) else None,
            "win_rate": float((x > 0).mean()) if len(x) else None, "max_drawdown": _max_dd(x),
            "worst_trade": float(x.min()) if len(x) else None, "p05_trade": float(x.quantile(.05)) if len(x) else None}

def _bootstrap_mean(x: pd.Series, n: int = 3000) -> dict:
    a = x.dropna().to_numpy(dtype=float)
    if not len(a): return {"mean_p05": None, "mean_p50": None, "mean_p95": None}
    rng = np.random.default_rng(SEED)
    means = rng.choice(a, size=(n, len(a)), replace=True).mean(axis=1)
    return {"mean_p05": float(np.quantile(means,.05)), "mean_p50": float(np.quantile(means,.50)), "mean_p95": float(np.quantile(means,.95))}

def main() -> int:
    if not SOURCE.exists(): raise FileNotFoundError(SOURCE)
    df = pd.read_csv(SOURCE, low_memory=False)
    provenance = _provenance(df)
    df["entry_date"] = pd.to_datetime(df["entry_date"], errors="coerce")
    df["net_ret"] = pd.to_numeric(df["net_ret"], errors="coerce")
    df = df.dropna(subset=["entry_date", "net_ret"]).sort_values(["entry_date", "code"])
    rows=[]; annual=[]; concentration=[]
    for v in VARIANTS:
        w=v.weight(df)
        for cost_name,cost in COSTS.items():
            x=df["net_ret"]*w-cost*w
            met=_metrics(x); boot=_bootstrap_mean(x)
            rows.append({"variant":v.name,"description":v.description,"cost_scenario":cost_name,"cost_per_unit":cost,**met,**boot})
            tmp=pd.DataFrame({"year":df.entry_date.dt.year,"ret":x})
            for year,g in tmp.groupby("year"):
                annual.append({"variant":v.name,"cost_scenario":cost_name,"year":int(year),**_metrics(g.ret)})
        x=df["net_ret"]*w-COSTS["low"]*w
        pos=x[x>0].sort_values(ascending=False)
        total=x.sum()
        concentration.append({"variant":v.name,"top1_share":float(pos.head(1).sum()/total) if total else None,"top5_share":float(pos.head(5).sum()/total) if total else None,"positive_pnl_share":float(pos.sum()/total) if total else None})
    metrics=pd.DataFrame(rows); annual_df=pd.DataFrame(annual); conc=pd.DataFrame(concentration)
    # Conservative research gate: low-cost blind 2025/2026 both non-negative mean and no worse than base stress drawdown.
    blind=annual_df[(annual_df.year>=2025)&(annual_df.cost_scenario=="stress")].groupby("variant").agg(blind_return=("total_return","sum"), blind_years=("year","nunique")).reset_index()
    stress=metrics[metrics.cost_scenario.eq("stress")][["variant","max_drawdown","total_return"]].rename(columns={"max_drawdown":"stress_dd","total_return":"stress_total"})
    decision=blind.merge(stress,on="variant",how="left")
    base_dd=float(decision.loc[decision.variant.eq("base"),"stress_dd"].iloc[0])
    decision["status"]=np.where((decision.blind_return>=0)&(decision.stress_dd>=base_dd),"research_candidate","reject")
    OUT.mkdir(parents=True, exist_ok=True)
    metrics.to_csv(OUT/"variant_metrics.csv",index=False,encoding="utf-8-sig")
    annual_df.to_csv(OUT/"annual_metrics.csv",index=False,encoding="utf-8-sig")
    conc.to_csv(OUT/"concentration.csv",index=False,encoding="utf-8-sig")
    decision.to_csv(OUT/"decision.csv",index=False,encoding="utf-8-sig")
    summary={"status":"diagnostic_completed_promotion_blocked","research_only":True,"source":str(SOURCE),"generated_at":datetime.now().isoformat(timespec="seconds"),"provenance": provenance,"promotion_blocked": True,"variants":[v.__dict__ for v in VARIANTS],"decision":decision.to_dict("records"),"limitations":["历史成交为OHLC代理，未建模涨跌停、滑点、排队与真实成交","交易按逐笔汇总，未替代组合级逐日MTM","研究候选不等于可下单合同"]}
    (OUT/"summary.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding="utf-8")
    report=['# G3策略稳定性与仓位候选审计 v1','', '- 仅研究，不改变正式合同和自动下单。','- 成本场景：0、0.2%、0.5%；盲测窗口使用2025—2026年。','', '## 候选判定', '', decision.to_markdown(index=False), '', '## 说明', '', '- `research_candidate` 仅表示在当前样本和压力成本下通过机械门槛，仍需独立前向纸面交易。', '- 任何缺失、未来函数或真实成交约束未覆盖的部分均不能解释为稳定盈利。']
    (OUT/"REPORT_CN.md").write_text("\n".join(report)+"\n",encoding="utf-8")
    report.insert(3, "- 来源阻断：" + "；".join(provenance["blockers"]))
    (OUT/"REPORT_CN.md").write_text("\n".join(report)+"\n",encoding="utf-8")
    print(json.dumps(summary,ensure_ascii=False,indent=2))
    return 0
if __name__ == "__main__": raise SystemExit(main())


