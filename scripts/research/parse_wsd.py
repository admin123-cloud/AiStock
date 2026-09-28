import re

h = open(r'F:\AiStock\app\scripts\research\wsd2.html', 'rb').read()
try:
    t = h.decode('utf-8')
except UnicodeDecodeError:
    t = h.decode('gb2312', errors='replace')
print('len', len(t))
txt = re.sub(r'<script.*?</script>', '', t, flags=re.S)
txt = re.sub(r'<style.*?</style>', '', txt, flags=re.S)
txt = re.sub(r'<[^>]+>', ' ', txt)
txt = re.sub(r'\s+', ' ', txt)
print(txt[:2000])
links = re.findall(r'href="([^"]+)"', t)
print([l for l in links if any(k in l.lower() for k in ('min', 'wdz', 'down', 'buy', 'price'))][:15])
