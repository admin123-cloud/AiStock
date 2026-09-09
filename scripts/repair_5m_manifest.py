"""Conservative manifest-driven 5m repair.  Default mode is read-only dry-run."""
import argparse, json, sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0,str(ROOT))
from utils.market_warehouse import clickhouse_client
from utils.paths import runtime_path

def main():
 p=argparse.ArgumentParser();p.add_argument('--manifest',required=True);p.add_argument('--apply',action='store_true');p.add_argument('--backup-confirmation',default='');a=p.parse_args()
 if a.apply and not a.backup_confirmation: raise SystemExit('--apply requires explicit backup confirmation')
 m=json.loads(Path(a.manifest).read_text(encoding='utf8'))
 if m.get('scope')!='partial_bucket_only; does not enumerate wholly absent code-days or buckets': raise SystemExit('manifest scope not accepted')
 # Each incomplete derived bucket maps to source 5m times.  A real apply is deliberately blocked
 # until the protected source backup is confirmed and a staged evidence file is reviewed.
 rows={(x['code'],x['trade_date']) for x in m['gaps']}
 out={'manifest':a.manifest,'mode':'dry_run' if not a.apply else 'blocked_pending_stage_review',
      'code_days':len(rows),'bucket_rows':len(m['gaps']),'guarantees':['no overwrite of existing 5m keys','QMT cache read only','stage evidence required before insert']}
 path=runtime_path('operations','derived_recovery','repair_plans','5m_manifest_plan.json');path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(out,ensure_ascii=False,indent=2),encoding='utf8');print(json.dumps(out,ensure_ascii=False))
if __name__=='__main__': main()
