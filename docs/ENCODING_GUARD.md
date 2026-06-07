# Encoding Guard

本项目统一使用 UTF-8 保存源码、配置、脚本、文档和可提交文本文件。

## 日常提交前检查

只检查已暂存文件，适合放到 git pre-commit hook 里：

```powershell
python scripts/check_encoding_guard.py --staged
```

本仓库已提供 `.githooks/pre-commit` 模板。启用方式：

```powershell
git config core.hooksPath .githooks
```

## 检查当前改动

检查未暂存改动和未跟踪文件：

```powershell
python scripts/check_encoding_guard.py --changed
```

不传参数时默认等同于 `--changed`。

## 统一文本格式

将可识别文本文件统一为 UTF-8 无 BOM 和 LF 换行：

```powershell
python scripts/normalize_text_encoding.py
```

只检查不写入：

```powershell
python scripts/normalize_text_encoding.py --check
```

## 全仓审计

用于拉出历史存量问题清单：

```powershell
python scripts/check_encoding_guard.py --all
```

当前仓库仍有历史乱码债务，所以 `--all` 失败是预期结果。治理节奏建议是先用 `--staged`
拦住新增问题，再按模块逐步修复历史文件。

## 检测范围

脚本会检查常见源码和文本扩展名，并默认跳过这些生成或重型目录：

- `.git`
- `.idea`
- `.pytest_cache`
- `__pycache__`
- `artifacts`
- `data`
- `dist`
- `logs`
- `node_modules`

## 修复原则

- 能从 git 历史恢复的，优先从历史版本恢复原文。
- 已经变成问号占位符的文本通常不可逆，需要按业务语义人工补回。
- 带替换字符的文本通常已经部分丢失，优先确认是否影响页面、API message、导出文件或日志。
- 可逆 mojibake 可以脚本化修复，但修复后必须逐行抽查关键页面文案。
