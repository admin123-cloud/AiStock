"""Daily return fields use the provider's previous-close reference when present."""
import numpy as np
import pandas as pd


def daily_price_metrics(frame: pd.DataFrame) -> pd.DataFrame:
    close = pd.to_numeric(frame['close'], errors='coerce')
    previous = close.where(np.isfinite(close) & close.gt(0)).ffill().shift(1)
    native = pd.to_numeric(frame.get('preClose', pd.Series(index=frame.index, dtype=float)), errors='coerce')
    native = native.where(np.isfinite(native) & native.gt(0))
    reference = native.combine_first(previous)
    valid = np.isfinite(reference) & reference.gt(0)
    change = (close-reference).where(valid, 0).fillna(0)
    return pd.DataFrame({
        'amplitude': ((pd.to_numeric(frame['high'], errors='coerce')-pd.to_numeric(frame['low'], errors='coerce'))/reference*100).where(valid, 0).fillna(0),
        'change_pct': (change/reference*100).where(valid, 0).fillna(0),
        'change_amount': change,
        'reference_close': native,
    }, index=frame.index)
