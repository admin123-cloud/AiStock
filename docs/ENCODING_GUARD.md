# 编码防护手册

本手册用于避免仓库和全局记忆文件出现乱码回归。

## 提交钩子

在本仓库建议开启 hooks：

```bash
git config core.hooksPath .githooks
```

`.githooks/pre-commit` 已包含两层校验：

```bash
python scripts/check_text_health.py --staged
```

## 日常检查命令

检查已修改但未提交的文本文件：

```bash
python scripts/check_encoding_guard.py --changed
```

或一条命令统一执行两项检查：

```bash
python scripts/check_text_health.py
```

校验仓库全部文本文件（可能较慢）：

```bash
python scripts/check_encoding_guard.py --all
```

校验全局记忆文件（`C:\Users\Administrator\.codex\memories`）：

```bash
python scripts/check_global_memory_encoding.py
```

## 推荐修复方式

建议将文本文件统一成 UTF-8 无 BOM、LF 行尾：

```bash
python scripts/normalize_text_encoding.py
```

校验模式（不改文件）：

```bash
python scripts/normalize_text_encoding.py --check
```

## 规则说明

`check_encoding_guard.py` 重点检测：

- 非 UTF-8 字节
- UTF-8 BOM
- CRLF / CR 行尾
- 常见 mojibake / 乱码片段

全局记忆检查同样覆盖：

- `PROFILE.md`
- `ACTIVE.md`
- `LEARNINGS.md`
- `ERRORS.md`
- `FEATURE_REQUESTS.md`

## 为什么会乱码

大多数乱码来自混用编码工具导致的编码转换链路（例如 PowerShell 某些写入方式、默认系统编码）。

## Windows 建议写法

- 直接编辑 `.md/.py/.js/.py` 等文本文件时，优先使用 `apply_patch`。
- Python/Node 写文件时显式指定 UTF-8 无 BOM。
- 避免在中文文本场景下用 `Set-Content`、`Out-File`、`Add-Content`。
