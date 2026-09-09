"""Read-only, disposable SDK worker; never downloads history or writes market tables."""
import argparse
import json
from pathlib import Path
import os


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--request',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    request=json.loads(args.request.read_text(encoding='utf-8'))
    from xtquant import xtdata
    xtdata.enable_hello=False
    xtdata.connect(os.getenv('AISTOCK_QMT_QUOTE_HOST','127.0.0.1'),int(os.getenv('AISTOCK_QMT_QUOTE_PORT','58610')))
    ticks={}
    failures=[]
    codes=request['codes']
    size=max(1,int(request['batch_size']))
    for offset in range(0,len(codes),size):
        try:
            result=xtdata.get_full_tick(codes[offset:offset+size])
            if isinstance(result,dict):ticks.update(result)
            else:failures.append({'offset':offset,'reason':'invalid_response'})
        except Exception as exc:failures.append({'offset':offset,'reason':str(exc)})
    args.output.write_bytes(json.dumps({'ticks':ticks,'failed_batches':failures},ensure_ascii=False,default=str).encode('utf-8'))
    return 0


if __name__=='__main__':raise SystemExit(main())
