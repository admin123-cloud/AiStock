# 检查通达信 lc5(5分钟线)文件的覆盖范围
# 用法: python tdx_lc5_probe.py D:\TDX\vipdoc  (可选指定代码列表)
import struct, sys, os, glob, datetime

def probe(fp):
    size = os.path.getsize(fp)
    n = size // 32
    if n <= 0:
        return None
    with open(fp, 'rb') as f:
        first = f.read(32)
        f.seek((n - 1) * 32)
        last = f.read(32)
    def parse(rec):
        d, t, o, h, l, c, amt, vol = struct.unpack('<HHffffII', rec[:28])
        try:
            r = d % 2048
            dt = datetime.date(d // 2048 + 2004, r // 100, r % 100)
        except ValueError:
            return None, None
        return dt, t
    fd, ft = parse(first)
    if fd is None:
        return None
    ld, lt = parse(last)
    return n, fd, ft, ld, lt

vip = sys.argv[1] if len(sys.argv) > 1 else r'D:\TDX\vipdoc'
codes = sys.argv[2:] or ['sh000001', 'sz000001', 'sh600000', 'sz000002', 'sh600519']
for code in codes:
    fp = os.path.join(vip, code[:2], 'fzline', code + '.lc5')
    r = probe(fp) if os.path.exists(fp) else None
    print(code, r if r else 'NOT FOUND')

# 全市场最早日期统计(抽样每隔20个文件)
earliest = []
for mkt in ('sh', 'sz'):
    for fp in glob.glob(os.path.join(vip, mkt, 'fzline', '*.lc5'))[::20]:
        r = probe(fp)
        if r:
            earliest.append((r[1], os.path.basename(fp)))
earliest.sort()
print('\n抽样最早的10个文件起始日期:')
for d, n in earliest[:10]:
    print(d, n)
