import re

data = open(r'E:\AStockHistory\wstock\解压工具\wsWDZ\wdz_app\wsWDZ.exe', 'rb').read()
# 提取 ASCII 与 GBK 可读字符串,找命令行/自动化线索
strs = re.findall(rb'[\x20-\x7e]{5,}', data)
keys = (b'command', b'batch', b'auto', b'convert', b'exe', b'.wdz', b'param', b'silent', b'/', b'inifile', b'ini')
seen = set()
for s in strs:
    low = s.lower()
    if any(k in low for k in (b'commandline', b'cmdline', b'batch', b'autoexit', b'/auto', b'-auto', b'silent')):
        t = s.decode('ascii', 'replace')
        if t not in seen:
            seen.add(t)
            print(t[:120])
print('---ini/exe refs---')
n = 0
for s in strs:
    low = s.lower()
    if (b'.ini' in low or b'wszex' in low or b'getcommandline' in low) and n < 25:
        t = s.decode('ascii', 'replace')
        if t not in seen:
            seen.add(t); n += 1
            print(t[:120])
