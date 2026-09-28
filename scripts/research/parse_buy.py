import re, sys

def clean(fp):
    h = open(fp, 'rb').read()
    try:
        t = h.decode('utf-8')
    except UnicodeDecodeError:
        t = h.decode('gb2312', errors='replace')
    txt = re.sub(r'<script.*?</script>', ' ', t, flags=re.S)
    txt = re.sub(r'<style.*?</style>', ' ', txt, flags=re.S)
    txt = re.sub(r'<[^>]+>', ' ', txt)
    return re.sub(r'\s+', ' ', txt)

txt = clean(r'F:\AiStock\app\scripts\research\buy_shsz.html')
i = txt.find('价格')
print('== buy_shsz ==')
print(txt[:2500])
print()
txt2 = clean(r'F:\AiStock\app\scripts\research\wsmin2.html')
print('== wsmin ==')
print(txt2[:3000])
