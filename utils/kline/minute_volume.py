"""Exact, additive 5m quantity storage; legacy Int64 values stay untouched."""
from decimal import Decimal, localcontext, InvalidOperation
import math

PRECISE_MIN5_VIEW = 'stock.kline_minute_5_precise'
VOLUME_SCALE = 45


def normalize_min5_frame(frame):
    """Present UTC storage in business time; never shift persisted records."""
    import pandas as pd
    result = frame.copy()
    for column in ('datetime', 'date_value'):
        if column in result:
            result[column] = pd.to_datetime(result[column], errors='coerce', utc=True).dt.tz_convert('Asia/Shanghai')
    return result


def split_volume(value):
    """Return legacy integer/exact decimal without rounding a source number."""
    if value is None:
        return None, None
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError('5m volume must be finite')
        number = Decimal.from_float(value)
    else:
        number = Decimal(value.item() if hasattr(value, 'item') else value)
    if not number.is_finite() or number < 0:
        raise ValueError('5m volume must be finite and nonnegative')
    if number == number.to_integral_value() and number <= 2**63-1:
        return int(number), None
    with localcontext() as ctx:
        ctx.prec = 200
        try:
            exact = number.quantize(Decimal('1e-45'))
        except InvalidOperation as exc:
            raise ValueError('5m volume exceeds exact decimal storage') from exc
        if exact != number or abs(exact) >= Decimal(10)**31:
            raise ValueError('5m volume cannot be represented losslessly at Decimal256(45)')
    return None, exact


def prepare_min5_volumes(frame):
    """Convert source quantities to the two physical columns, keeping nulls."""
    if 'volume' not in frame:
        return frame
    import pandas as pd
    result = frame.copy()
    values = []
    # iterrows can coerce an Int64 to Float64 before validation.
    precise_values = result['volume_exact'].tolist() if 'volume_exact' in result else [None]*len(result)
    for original, precise in zip(result['volume'].tolist(), precise_values):
        if precise is not None and not pd.isna(precise):
            if original is not None and not pd.isna(original) and Decimal(str(original)) != precise:
                raise ValueError('conflicting legacy and precise 5m quantities')
            original = precise
        if original is None or pd.isna(original):
            original = None
        values.append(split_volume(original))
    result['volume'] = pd.Series([v[0] for v in values], index=result.index, dtype=object)
    result['volume_exact'] = pd.Series([v[1] for v in values], index=result.index, dtype=object)
    return result


def quantity_json(value):
    """Preserve integers (including >2**53); binary fractions stay exact floats."""
    if value is None:
        return None
    if isinstance(value, Decimal):
        if not value.is_finite():
            return None
        if value == value.to_integral_value():
            return int(value)
        converted = float(value)
        if Decimal.from_float(converted) != value:
            raise ValueError('quantity cannot be serialized losslessly as a JSON number')
        return converted
    if isinstance(value, int):
        return value
    number = float(value)
    return number if math.isfinite(number) else None


def precise_minute_read_sql(sql):
    # Share the tested lexer, preserving literals/comments and mutation targets.
    import re
    from utils.daily_quality_read import _TOKEN, _RESERVED
    tokens = _TOKEN.findall(sql)
    significant = [i for i,t in enumerate(tokens) if t.strip() and not t.startswith(('--','/*'))]
    if not significant or tokens[significant[0]].lower() not in {'select','with'}:
        return sql
    if any(tokens[i].lower() in {'insert','alter','update','delete','create','drop','truncate','optimize','exchange'} for i in significant):
        return sql
    for pos,i in enumerate(significant[:-1]):
        if tokens[i].lower() not in {'from','join'}:
            continue
        j=significant[pos+1]
        if tokens[j].strip('`"').lower() not in {'kline_minute_5','stock.kline_minute_5'}:
            continue
        nxt=pos+2
        if nxt<len(significant) and tokens[significant[nxt]].lower()=='final':
            tokens[significant[nxt]]='';nxt+=1
        alias=nxt<len(significant) and (tokens[significant[nxt]].lower()=='as' or (re.fullmatch(r'[A-Za-z_]\w*',tokens[significant[nxt]]) and tokens[significant[nxt]].lower() not in _RESERVED))
        tokens[j]=f'(SELECT * FROM {PRECISE_MIN5_VIEW})'+('' if alias else ' AS kline_minute_5')
        if alias:
            after=nxt+2 if tokens[significant[nxt]].lower()=='as' else nxt+1
            if after<len(significant) and tokens[significant[after]].lower()=='final':tokens[significant[after]]=''
    return ''.join(tokens)
