"""Discover and run a named historical experiment without eagerly importing it."""
import argparse
import json
from pathlib import Path
import runpy
import sys


def main(argv=None):
    parser = argparse.ArgumentParser(description='AiStock离线研究入口；执行实验可能查询数据并写研究报告。')
    parser.add_argument('--list', action='store_true', help='只列出迁移后的研究命令')
    parser.add_argument('--resolve', metavar='NAME', help='只解析旧脚本名称，不执行')
    parser.add_argument('experiment', nargs='?', help='脚本名，可省略.py')
    parser.add_argument('arguments', nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    catalog = json.loads(Path(__file__).with_name('catalog.json').read_text(encoding='utf-8'))
    entries = {Path(x['old']).stem:x for x in catalog['research_moves']}
    if args.list:
        for name, item in sorted(entries.items()):
            print(f"{name}\t{item['new']}")
        return 0
    name = Path(args.resolve or args.experiment or '').stem
    if name not in entries:
        parser.error('请使用--list查询有效实验名')
    target = entries[name]['new']
    if args.resolve:
        print(target)
        return 0
    arguments = args.arguments[1:] if args.arguments[:1] == ['--'] else args.arguments
    sys.argv = [target, *arguments]
    runpy.run_module(target[:-3].replace('/', '.'), run_name='__main__')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
