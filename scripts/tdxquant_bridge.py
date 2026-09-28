"""
Stable external-strategy identity for TdxQuant/tqcenter.py.

TdxQuant uses the Python file path passed to tq.initialize(...) as the
connection identity. Keep this file small and dedicated so Gateway restarts do
not reuse a large business module path or collide with unrelated API code.
"""
