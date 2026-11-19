import re

h = open(r'F:\AiStock\app\scripts\research\wsmin2.html', 'rb').read()
try:
    t = h.decode('utf-8')
except UnicodeDecodeError:
    t = h.decode('gb2312', errors='replace')
txt = re.sub(r'<script.*?</script>', ' ', t, flags=re.S)
txt = re.sub(r'<style.*?</style>', ' ', txt, flags=re.S)
txt = re.sub(r'<[^>]+>', ' ', txt)
txt = re.sub(r'\s+', ' ', txt)
i = txt.find('免费用户名wstock.net')
print(txt[i:i+2200])
