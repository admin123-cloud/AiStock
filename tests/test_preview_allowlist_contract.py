"""只读安全栈（api.operations_preview）白名单契约守卫测试。

确保：
1. _ALLOWED_PREVIEW_PATHS 与 _SCOPED_WRITE_PREVIEW_PATHS 不含任何已删除路由的幽灵路径。
2. 除 _SCOPED_WRITE_PREVIEW_PATHS 明确放行的 POST 外，其余暴露路由全部严格限制为 GET/HEAD。
"""
from __future__ import annotations


def test_preview_allowlist_has_no_ghost_paths():
    from api.operations_preview import (
        app,
        _ALLOWED_PREVIEW_PATHS,
        _SCOPED_WRITE_PREVIEW_PATHS,
    )

    registered = {getattr(r, "path", "") for r in app.routes}
    expected = _ALLOWED_PREVIEW_PATHS | _SCOPED_WRITE_PREVIEW_PATHS
    ghosts = sorted(expected - registered)
    assert not ghosts, f"发现幽灵白名单路径（已删除但仍滞留 allowlist）：{ghosts}"
    assert registered == expected


def test_preview_methods_strictly_readonly_except_scoped_writes():
    from api.operations_preview import app, _SCOPED_WRITE_PREVIEW_PATHS

    for route in app.routes:
        path = getattr(route, "path", None)
        methods = set(getattr(route, "methods", None) or set())
        if path in _SCOPED_WRITE_PREVIEW_PATHS:
            assert methods == {"POST"}, f"scoped-write 路由方法异常: {path} -> {methods}"
        else:
            assert methods <= {"GET", "HEAD"}, f"非写白名单路由暴露了写方法: {path} -> {methods}"
