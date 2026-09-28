"""Current QMT membership scope, preserving exclusions as source evidence."""
import re

QMT_CURRENT_STOCKS = '沪深京A股'


def canonical_members(raw):
    values = {str(c.get('Code') if isinstance(c, dict) else c).strip().upper() for c in raw or [] if c}
    if any(not re.fullmatch(r'\d{6}\.(SH|SZ|BJ)', code) for code in values):
        raise RuntimeError('Unsupported QMT member identity')
    return values


def current_stock_universe(client):
    values = canonical_members(client.get_stock_list_in_sector(QMT_CURRENT_STOCKS))
    if len(values) < 1000:
        raise RuntimeError('Current QMT A-share universe is empty or suspiciously small')
    return values


def scoped_members(raw, current_codes):
    values = canonical_members(raw)
    included = sorted(values & current_codes)
    excluded = sorted(values - current_codes)
    if not included:
        raise RuntimeError('Empty current QMT sector membership')
    return included, excluded
