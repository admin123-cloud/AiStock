"""Audit a copied WStock wsCSV subset without touching ClickHouse."""
from __future__ import annotations
import argparse, csv, hashlib, json, os, re, shutil
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

SLOTS = [f"{t//60:02d}:{t%60:02d}" for t in range(9*60+35, 11*60+30+1, 5)] + [f"{t//60:02d}:{t%60:02d}" for t in range(13*60+5, 15*60+1, 5)]
PKG_RE = re.compile(r"wstock_SHSZ_(\d{4})(?:-(\d{4}))?_5Min\.wdz$", re.I)

def sha256(path: Path, chunk: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while b := f.read(chunk): h.update(b)
    return h.hexdigest()

def package_meta(root: Path) -> list[dict]:
    out=[]
    for p in sorted(root.glob("wstock_SHSZ_*_5Min.wdz")):
        m=PKG_RE.match(p.name)
        if not m: continue
        a=int(m.group(1)); b=int(m.group(2) or a)
        st=p.stat()
        out.append({"path":str(p),"name":p.name,"years":[a,b],"size_bytes":st.st_size,"sha256":sha256(p)})
    return out

def main() -> int:
    ap=argparse.ArgumentParser(); ap.add_argument("--source",required=True); ap.add_argument("--dest",required=True); ap.add_argument("--run-id",required=True); ap.add_argument("--out",required=True)
    a=ap.parse_args(); src=Path(a.source); dst=Path(a.dest)
    packages=package_meta(src.parent)
    files=[]; totals=Counter(); all_dates=set(); package_for=defaultdict(list)
    seen=set()
    for p in sorted([* (dst/"SH").glob("*.csv"), * (dst/"SZ").glob("*.csv")]):
        if not p.is_file() or str(p) in seen: continue
        seen.add(str(p)); name=p.name; market=name[:2].upper(); symbol=name[2:8]
        rows=0; invalid=0; dup=0; byday=Counter(); day_slots=defaultdict(set); min_dt=None; max_dt=None
        h=hashlib.sha256()
        with p.open("rb") as fb:
            for raw in fb:
                h.update(raw)
        with p.open("r", encoding="utf-8-sig", newline="") as f:
            rd=csv.reader(f); header=next(rd,None)
            if header != ["Date","Open","Low","High","Close","Volume","Amount"]: invalid += 1
            keys=set()
            for c in rd:
                rows += 1
                try:
                    dt=datetime.strptime(c[0].strip(), "%Y-%m-%d %H:%M:%S"); vals=[float(x) for x in c[1:7]]
                    if len(c)<7 or any(v != v or v in (float("inf"),float("-inf")) for v in vals): raise ValueError
                    d=dt.date().isoformat(); hm=dt.strftime("%H:%M"); key=(d,hm)
                    if key in keys: dup += 1
                    keys.add(key); byday[d]+=1; day_slots[d].add(hm); all_dates.add(d)
                    min_dt=min_dt or dt; max_dt=dt if max_dt is None or dt>max_dt else max_dt
                except Exception: invalid += 1
        expected_days=sum(1 for d in byday if len(day_slots[d])==48 and set(day_slots[d])==set(SLOTS))
        review_days=sum(1 for d in byday if not (len(day_slots[d])==48 and set(day_slots[d])==set(SLOTS)))
        years=sorted({int(d[:4]) for d in byday})
        archives=[x for x in packages if any(x["years"][0] <= y <= x["years"][1] for y in years)]
        rec={"source_path":str(src/market/name),"backup_path":str(p),"member":name,"market":market,"code":f"{symbol}.{market}","sha256":h.hexdigest(),"size_bytes":p.stat().st_size,"rows":rows,"invalid_rows":invalid,"duplicate_datetime_keys":dup,"date_min":min_dt.date().isoformat() if min_dt else None,"date_max":max_dt.date().isoformat() if max_dt else None,"years":years,"complete_48_slot_days":expected_days,"review_days":review_days,"source_archives":archives}
        files.append(rec); totals.update(files=1,rows=rows,invalid_rows=invalid,duplicate_datetime_keys=dup,complete_48_slot_days=expected_days,review_days=review_days)
    payload={"schema":"wstock-isolated-backup-v1","run_id":a.run_id,"created_at":datetime.now().astimezone().isoformat(),"source_root":str(src),"backup_root":str(dst),"formal_authorized":False,"clickhouse_touched":False,"source_archives":packages,"file_count":len(files),"totals":dict(totals),"files":files,"date_min":min(all_dates) if all_dates else None,"date_max":max(all_dates) if all_dates else None,"notes":["Copied from existing wsCSV output; source .wdz and prior stage untouched.","Structure checks are local evidence only; no formal promotion authorization."]}
    out=Path(a.out); out.parent.mkdir(parents=True,exist_ok=True); tmp=out.with_suffix(out.suffix+".tmp"); tmp.write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding="utf-8"); os.replace(tmp,out)
    print(json.dumps({"run_id":a.run_id,"manifest":str(out),"files":len(files),"totals":dict(totals)},ensure_ascii=False))
    return 0
if __name__ == "__main__": raise SystemExit(main())
