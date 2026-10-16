"""Quality-aware daily reads. Storage and maintenance writes keep canonical tables."""
import re
import math

QUALITY_VIEW = 'stock.kline_daily_quality_read_20261016_v1'

def finite_number(value):
    if value is None:return None
    try:number=float(value)
    except (TypeError,ValueError):return None
    return number if math.isfinite(number) else None
# Preserve literals/comments verbatim; never replace names inside them.
_TOKEN = re.compile(r"--[^\n]*|/\*[\s\S]*?\*/|'(?:''|\\.|[^'\\])*'|\"[^\"]*\"|`[^`]*`|[A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*)*|\s+|.")
_RESERVED = {'where','join','left','right','inner','outer','full','cross','group','order','limit','settings','union','having','on','prewhere','sample','array','final','offset','format'}


def quality_read_sql(sql):
    from utils.minute_volume import precise_minute_read_sql
    sql = precise_minute_read_sql(sql)
    tokens = _TOKEN.findall(sql)
    significant = [i for i,t in enumerate(tokens) if t.strip() and not t.startswith(('--','/*'))]
    if not significant or tokens[significant[0]].lower() not in {'select','with'}:
        return sql
    # A mutating WITH/INSERT statement must never target the read projection.
    if any(tokens[i].lower() in {'insert','alter','update','delete','create','drop','truncate','optimize','exchange'} for i in significant):
        return sql
    for pos,i in enumerate(significant[:-1]):
        if tokens[i].lower() not in {'from','join'}:
            continue
        j = significant[pos+1]
        name = tokens[j].strip('`"').lower()
        if name not in {'kline_daily','stock.kline_daily'}:
            continue
        nxt = pos+2
        if nxt < len(significant) and tokens[significant[nxt]].lower()=='final':
            tokens[significant[nxt]] = ''
            nxt += 1
        alias = nxt < len(significant) and (tokens[significant[nxt]].lower()=='as' or (re.fullmatch(r'[A-Za-z_]\w*',tokens[significant[nxt]]) and tokens[significant[nxt]].lower() not in _RESERVED))
        tokens[j] = f'(SELECT * FROM {QUALITY_VIEW})' + ('' if alias else ' AS kline_daily')
        # ClickHouse also accepts "table AS alias FINAL".
        if alias:
            after = nxt+2 if tokens[significant[nxt]].lower()=='as' else nxt+1
            if after<len(significant) and tokens[significant[after]].lower()=='final':tokens[significant[after]]=''
    if any(QUALITY_VIEW in token for token in tokens):
        for pos,i in enumerate(significant[:-1]):
            if tokens[i].lower() in {'sum','avg'} and tokens[significant[pos+1]]=='(':
                tokens[i] += 'OrNull'
    return ''.join(tokens)


class QualityReadClient:
    def __init__(self, client):self._client=client
    def query(self, sql, *args, **kwargs):return self._client.query(quality_read_sql(sql), *args, **kwargs)
    def query_df(self, sql, *args, **kwargs):return self._client.query_df(quality_read_sql(sql), *args, **kwargs)
    def __getattr__(self, name):return getattr(self._client,name)
