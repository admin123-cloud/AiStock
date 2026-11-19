import urllib.request, urllib.parse, json

for q in ['wstock wdz', 'wdz sh000001', 'SHSZ_5Min', 'wstock.net']:
    url = 'https://grep.app/api/search?q=' + urllib.parse.quote(q)
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    try:
        d = json.load(urllib.request.urlopen(req, timeout=15))
        hits = d.get('hits', {}).get('hits', [])
        print(q, '->', len(hits))
        for h in hits[:5]:
            print('  ', h.get('repo', {}).get('raw'), h.get('path', {}).get('raw'))
    except Exception as e:
        print(q, 'ERR', str(e)[:100])
