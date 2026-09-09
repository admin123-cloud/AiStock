"""Run on an independent machine: persist outages; no notification unless --notify."""
import argparse
import json
import sys
import urllib.request
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0,str(ROOT))
from services.operations.incidents import reconcile,dispatch,send_digest


def poll(url, path, *, fetch=None, sender=None, now=None, grace_minutes=10):
    def request(url):
        with urllib.request.urlopen(url,timeout=10) as response:
            if response.status != 200: raise RuntimeError('http_unhealthy')
            return json.load(response)
    try:
        payload=(fetch or request)(url)
        ok=payload.get('ready') is True or payload.get('status')=='healthy'
        reason='remote_ready' if ok else 'remote_not_ready'
    except Exception as exc:
        ok,reason=False,type(exc).__name__
    events=reconcile(path,[{'name':'external_host_readiness','ok':ok,'reason':reason,
                           'message':'独立设备无法确认AiStock服务就绪；需核对主机/网络/进程'}],
                     now=now,grace_minutes=grace_minutes)
    notification=dispatch(path,sender,now=now) if sender else {'status':'not_requested'}
    return {'reachable_and_ready':ok,'events':events,'notification':notification}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--config',type=Path)
    parser.add_argument('--url',help='Independent-machine reachable readiness URL')
    parser.add_argument('--state',type=Path)
    parser.add_argument('--notify',action='store_true')
    args=parser.parse_args()
    cfg=json.loads(args.config.read_text(encoding='utf-8')) if args.config else {}
    url=args.url or cfg.get('url')
    state=args.state or (Path(cfg['state']) if cfg.get('state') else None)
    if not url or not state: parser.error('--url/--state or --config required')
    print(json.dumps(poll(url,state,sender=send_digest if args.notify else None,grace_minutes=int(cfg.get('grace_minutes',10))),ensure_ascii=False))

if __name__=='__main__': main()
