# -*- coding: utf-8 -*-
"""THS 历史逐笔成交证据链：独立复审 FAIL 后的隔离合成反例（返修切片1）。

任务书 2026-11-15（scratch: ths_evidence_red_brief_20261115.md）：上一任务
未通过两位独立 reviewer 复审；本文件只新增合成反例与正对照，不改生产脚本、
不运行真实入口。反例全部为合成内存数据，绝不伪造真实成交。

隔离方式（任务书要求，绝 import 项目模块）：读取精确源码
scripts/ths_client.py 的文本 + AST，只摘出 allowlist 内的顶层纯函数与
字面量常量，注入 stdlib re / datetime.date / typing.Any/Optional 与安全
内建后在受控命名空间编译执行；任何 allowlist 外语法、未审 import/I/O、
decorator、调用型默认参数都在加载期断言失败。与
tests/test_ths_single_snapshot_contract.py 的受限 AST 技法同源。sha256
硬门禁由 scratch runner 在 RED 执行前负责，本文件只记录冻结基线供追溯。

覆盖（编号对应任务书"需要新增的合成反例"1-6）：
  CE1 date_range 仅由页面令牌/行内日期 min-max 推得 ≠ 查询范围已证明；
      复刻采集器 scripts/ths_client.py L4034-4037 的推导（仅令牌排序取
      首尾），页面上没有任何查询日期控件读回令牌 → date_range_proven
      必须为 False 且不得 status=ok；
  CE2 count_total=None 且分页未知（page_total>1 或存在"下一页"）时，
      即便"汇总"行对账吻合也不得 coverage_complete——未知分页不可默认
      为一页（现 verdict L3390-3396 分支完全绕过分页检查）；
  CE3 汇总数量与金额都吻合、但另一已读汇总数值与行集合计矛盾 →
      summary_reconciled 必须 False（现 L3349-3353 首个可对账字段吻合即
      return True）；
  CE4 行内成交时间缺失/非法、side 或关键价量字段缺失 → 不得 status=ok
      （行原样保留，row_count 不缩水；完全重复的逐笔记录绝不按字段相同
      去重，见 PC4）；
  CE5 Unicode 负号（U+2212）与"负号+空白"形态：负数必须保留负号或显式
      解析失败，绝不转正（现 _parse_number L838 的 `-?\\d+` 正则会丢弃
      这两种负号形态取到正数；全局 _parse_number 的修复不得破坏旧资金
      路径，PC3 为回归锚）；
  CE6 必要表头未确认（横向字段未覆盖）→ 即便计数吻合也不得宣称完整；
      reader=vision_model 本身不构成覆盖证据。

尚缺证据字段的最小输入契约（生产 fev 目前没有，属本测试提出、待修复
任务采纳的扩展；不硬造内部结构，只加 fev 级新键）：
  fev["date_range_source"] ∈ {"query_controls_readback", "page_tokens"}
    —— "query_controls_readback"=查询日期控件已读回（唯一可使
    date_range_proven=True 的证据）；"page_tokens"=仅页面令牌推断（含行
    内日期 min/max）；键缺席视同 page_tokens（未证明）。
  fev["header_columns"] —— 采集器已写入的真实字段（L4031-4032）。必要
    集合 = _YH_FILLS_TRADE_TOKENS 六个成交页表头经 YH_FILLS_COLUMN_ALIASES
    映射的规范键 {date, time, side_raw, quantity, price, amount}；覆盖
    完整声明要求该集合全部被确认。
  行有效性最小契约：time 可解析为合法 HH:MM:SS、side ∈ {buy, sell}、
    price/quantity 为数值非 None；不满足的行保留原样但不得支撑 status=ok。

SAFETY：只读 scripts/ths_client.py 与本文件；全部输入合成；不触
runtime/DB/网络/GUI/凭证；不写任何文件。本切片不运行；RED 执行须经
PM 批准后由 scratch runner（sha 门禁）执行。
"""
import ast
import copy
import re
import typing
import unittest
from datetime import date
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "scripts" / "ths_client.py"
FROZEN_SOURCE_SHA256 = ("3337eaae85c0fdfbeabfc00e7cf8e9b2d538badb"
                        "46739f5a0ddf41ba2cdcfbe7")   # 追溯基线，不做硬门禁

# ── allowlist：仅这些顶层纯函数/常量可被摘出执行 ────────────────────
ALLOWED_FUNCTIONS = (
    "_parse_number",
    "_yh_norm_colkey",
    "yh_group_rows",
    "yh_normalize_fill_record",
    "yh_fills_rows_from_vision",
    "_yh_fills_header_map",
    "yh_fills_from_items",
    "yh_fills_date_tokens",
    "yh_fills_page_evidence",
    "yh_fills_summary_evidence",
    "yh_fills_summary_reconciled",
    "yh_fills_verdict",
)
ALLOWED_ASSIGNMENTS = (
    "YH_FILLS_COLUMN_ALIASES",
    "_YH_FILLS_NUMERIC_FIELDS",
    "_YH_FILLS_CANON_FIELDS",
    "YH_HOLDINGS_MIN_CODE_X",
    "_YH_DATE8_RE",
    "_YH_DATE_SEP_RE",
    "_YH_FILLS_COUNT_RE",
    "_YH_FILLS_PAGE_RE",
    "_YH_FILLS_NEXT_TOKENS",
    "_YH_FILLS_ORDER_TOKENS",
    "_YH_FILLS_TRADE_TOKENS",
)

_INJECTED_BUILTINS = {
    "str": str, "int": int, "float": float, "bool": bool, "len": len,
    "abs": abs, "min": min, "max": max, "sum": sum, "any": any, "all": all,
    "sorted": sorted, "reversed": reversed, "set": set,
    "frozenset": frozenset, "list": list, "dict": dict, "tuple": tuple,
    "zip": zip, "isinstance": isinstance, "issubclass": issubclass,
    "enumerate": enumerate, "next": next, "iter": iter, "range": range,
    "round": round, "ValueError": ValueError, "TypeError": TypeError,
    "KeyError": KeyError, "StopIteration": StopIteration,
}
_FORBIDDEN_NAMES = frozenset({
    "eval", "exec", "compile", "open", "input", "breakpoint", "globals",
    "locals", "vars", "getattr", "setattr", "delattr", "__import__",
})
_ATTR_CALLS = frozenset({
    "append", "extend", "insert", "pop", "update", "setdefault", "get",
    "items", "keys", "values", "add", "group", "groups", "isoformat",
    "strip", "lstrip", "rstrip", "lower", "upper", "replace", "join",
    "startswith", "endswith", "sort", "copy", "find", "split",
    "compile", "sub", "subn", "search", "match", "fullmatch", "finditer",
})
_ALLOWED_NODES = frozenset({
    ast.FunctionDef, ast.arguments, ast.arg, ast.Return, ast.Assign,
    ast.AnnAssign, ast.AugAssign, ast.If, ast.For, ast.While, ast.Break,
    ast.Continue, ast.Pass, ast.Expr, ast.Name, ast.Load, ast.Store,
    ast.Constant, ast.Dict, ast.List, ast.Tuple, ast.Set, ast.Subscript,
    ast.Slice, ast.IfExp, ast.BoolOp, ast.And, ast.Or, ast.UnaryOp,
    ast.Not, ast.USub, ast.UAdd, ast.Invert, ast.BinOp, ast.Add, ast.Sub,
    ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow, ast.Compare, ast.Eq,
    ast.NotEq, ast.Is, ast.IsNot, ast.In, ast.NotIn, ast.Lt, ast.LtE,
    ast.Gt, ast.GtE, ast.Call, ast.keyword, ast.Attribute, ast.Lambda,
    ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp,
    ast.comprehension, ast.Try, ast.ExceptHandler, ast.Raise,
})


def _validate(node, what):
    """allowlist 外语法/调用/属性/名称一律断言拒绝（加载期、非运行期）。"""
    local_defs = {sub.name for sub in ast.walk(node)
                  if isinstance(sub, ast.FunctionDef)}
    for sub in ast.walk(node):
        kind = type(sub)
        assert kind in _ALLOWED_NODES, (
            f"{what}: 出现未审 AST 节点 {kind.__name__}，拒绝加载")
        assert not isinstance(sub, (ast.Import, ast.ImportFrom)), (
            f"{what}: 提取物内禁止 import")
        if isinstance(sub, ast.Attribute):
            assert not sub.attr.startswith("__"), f"{what}: 禁止双下划线属性"
            assert sub.attr in _ATTR_CALLS, (
                f"{what}: 未审属性 .{sub.attr}()")
        elif isinstance(sub, ast.Name):
            assert not sub.id.startswith("__"), f"{what}: 禁止双下划线名称"
            assert sub.id not in _FORBIDDEN_NAMES, (
                f"{what}: 禁止名称 {sub.id}")
        elif isinstance(sub, ast.Call):
            assert all(k.arg is not None for k in sub.keywords), (
                f"{what}: 禁止 ** 展开")
            func = sub.func
            if isinstance(func, ast.Name):
                allowed = set(ALLOWED_FUNCTIONS) | set(_INJECTED_BUILTINS) \
                    | {"date", "re"} | local_defs
                assert func.id in allowed, (
                    f"{what}: 未审调用 {func.id}()")
        if isinstance(sub, ast.FunctionDef):
            assert not sub.decorator_list, f"{what}: 禁止 decorator"
            for default in list(sub.args.defaults) \
                    + [d for d in sub.args.kw_defaults if d is not None]:
                ok = isinstance(default, ast.Constant) or (
                    isinstance(default, ast.Name)
                    and default.id in ALLOWED_ASSIGNMENTS)
                assert ok, (
                    f"{what}: 默认参数仅允许字面量或已审常量名")


def _const_ok(value):
    """常量赋值仅接受字面量容器/已审常量名/re.compile(字面量)。"""
    if isinstance(value, ast.Constant):
        return True
    if isinstance(value, ast.Name):
        return value.id in ALLOWED_ASSIGNMENTS
    if isinstance(value, (ast.List, ast.Tuple, ast.Set)):
        return all(_const_ok(e) for e in value.elts)
    if isinstance(value, ast.Dict):
        keys_ok = all(k is None or _const_ok(k) for k in value.keys)
        return keys_ok and all(_const_ok(v) for v in value.values)
    if isinstance(value, ast.UnaryOp) and isinstance(
            value.op, (ast.USub, ast.UAdd)):
        return _const_ok(value.operand)
    if (isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute)
            and isinstance(value.func.value, ast.Name)
            and value.func.value.id == "re"
            and value.func.attr == "compile" and not value.keywords):
        return all(isinstance(a, ast.Constant) for a in value.args)
    return False


def _load_isolated_namespace():
    text = SOURCE.read_text(encoding="utf-8")
    tree = ast.parse(text, filename=str(SOURCE))
    funcs: dict = {}
    consts: dict = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in ALLOWED_FUNCTIONS:
            assert node.name not in funcs, f"顶层重复定义 {node.name}"
            funcs[node.name] = node
        elif isinstance(node, ast.Assign):
            hit = [t.id for t in node.targets
                   if isinstance(t, ast.Name) and t.id in ALLOWED_ASSIGNMENTS]
            if hit:
                assert len(node.targets) == 1 and len(hit) == 1, (
                    f"常量 {hit[0]} 必须单一目标单一命中")
                assert hit[0] not in consts, f"常量重复赋值 {hit[0]}"
                consts[hit[0]] = node
    missing = ([n for n in ALLOWED_FUNCTIONS if n not in funcs]
               + [n for n in ALLOWED_ASSIGNMENTS if n not in consts])
    assert not missing, f"源码缺少 allowlist 定义（源码漂移？）: {missing}"
    for name in ALLOWED_ASSIGNMENTS:
        node = consts[name]
        _validate(node, f"常量 {name}")
        assert _const_ok(node.value), (
            f"常量 {name} 的值超出 字面量/re.compile 形态")
    prepared = [copy.deepcopy(consts[n]) for n in ALLOWED_ASSIGNMENTS]
    for name in ALLOWED_FUNCTIONS:
        fn = copy.deepcopy(funcs[name])
        _validate(fn, f"函数 {name}")
        prepared.append(fn)
    module = ast.Module(body=prepared, type_ignores=[])
    ast.fix_missing_locations(module)
    code = compile(module, "<ths_client_isolated_fills_evidence>", "exec")
    # 注解在 def/AnnAssign 时会被求值：注入 typing.Any/Optional 使其可求值；
    # 其余注入面仅 stdlib re、datetime.date 与安全内建。
    env = {"__builtins__": dict(_INJECTED_BUILTINS), "re": re, "date": date,
           "Any": typing.Any, "Optional": typing.Optional}
    exec(code, env)
    return env


_NAMESPACE = None


def _ns():
    global _NAMESPACE
    if _NAMESPACE is None:
        _NAMESPACE = _load_isolated_namespace()
    return _NAMESPACE


# ── 合成数据构造（全部虚构；坐标形态仿既有契约测试，值为编造） ──────
def _required_header_columns():
    """必要表头规范键 = 生产常量 _YH_FILLS_TRADE_TOKENS 经别名映射。"""
    ns = _ns()
    return {ns["YH_FILLS_COLUMN_ALIASES"][tok]
            for tok in ns["_YH_FILLS_TRADE_TOKENS"]}


def _valid_row(**over):
    """合法规范行（合成；协鑫能科/002015 等均为虚构示例，非真实成交）。"""
    row = {"date": "20261112", "time": "09:42:29", "side": "buy",
           "code": "002015", "name": "协鑫能科", "quantity": 100.0,
           "price": 13.37, "amount": 1337.0, "fee": 5.0,
           "stamp_tax": 0.0, "net_amount": -1342.0,
           "contract_no": "14499", "fill_no": "010400",
           "shareholder_code": None, "balance_after": 4100.0}
    row.update(over)
    return row


def _full_fev(rows, **over):
    """除被测维度外证据齐备的 fev（正对照基准形态；含提出的扩展键）。"""
    fev = {
        "attempted": True, "reason": None, "wrong_page": False,
        "rows": rows, "row_count": len(rows),
        "count_total": len(rows), "page_current": 1, "page_total": 1,
        "next_page_present": False,
        "date_tokens": [{"token": "20261111", "iso": "2026-11-11"},
                        {"token": "20261112", "iso": "2026-11-12"}],
        "date_range": {"start": "2026-11-11", "end": "2026-11-12"},
        "date_range_source": "query_controls_readback",
        "header_columns": sorted(_required_header_columns()
                                 | {"code", "name", "fee"}),
        "reader": "vision_model", "summary": {"present": False},
        "orders_like_page": False, "trade_like_page": True,
    }
    fev.update(over)
    return fev


def _three_rows():
    """三行合成成交（数量/金额/印花税/发生金额合计见 CE2 注释）。"""
    return [
        _valid_row(),
        _valid_row(date="20261111", time="11:12:29", side="sell",
                   quantity=-2000.0, price=13.30, amount=26600.0,
                   fee=4.0, stamp_tax=13.3, net_amount=26581.0,
                   contract_no="37671", fill_no="010401",
                   balance_after=2100.0),
        _valid_row(date="20261111", time="10:04:30", side="buy",
                   quantity=2000.0, price=13.33, amount=26660.0,
                   fee=4.0, stamp_tax=0.0, net_amount=-26673.0,
                   contract_no="37672", fill_no="010402",
                   balance_after=4100.0),
    ]


def _page_items(rows_cells, count_text="共2条"):
    """合成页面文本项：表头行 + 数据行 + 计数（x≥200，全部虚构坐标）。"""
    def it(text, x, y):
        return {"text": text, "x": x, "y": y, "score": 0.9}

    header = [("成交日期", 260.0), ("业务名称", 330.0), ("证券代码", 400.0),
              ("证券名称", 470.0), ("成交数量", 540.0), ("成交价格", 610.0),
              ("成交金额", 680.0)]
    items = [it(t, x, 350.0) for t, x in header]
    y = 380.0
    for cells in rows_cells:
        items += [it(t, x, y) for t, x in cells]
        y += 24.0
    items.append(it(count_text, 700.0, 590.0))
    return items


# ── 正对照（正常合成数据；当前冻结脚本与修复后都必须通过） ──────────
class FillsEvidencePositiveControls(unittest.TestCase):

    def test_pc1_normal_record_alias_and_number_normalize(self):
        ns = _ns()
        rec = ns["yh_normalize_fill_record"]({
            "成交日期": "20261112", "成交时间": "09 : 42:29",
            "业务名称": "证券买入", "证券代码": "002015",
            "证券名称": "协鑫能科", "成交数量": "100",
            "成交价格": "13. 370", "成交金额": "1,337.00",
            "手续费": "5.00"})
        self.assertEqual(rec["side"], "buy")
        self.assertEqual(rec["code"], "002015")
        self.assertEqual(rec["time"], "09:42:29")   # OCR 冒号空格清洗
        self.assertEqual(rec["quantity"], 100.0)
        self.assertEqual(rec["price"], 13.37)
        self.assertEqual(rec["amount"], 1337.0)
        self.assertEqual(rec["fee"], 5.0)

    def test_pc2_full_evidence_verdict_ok(self):
        """证据齐备（含 date_range_source 读回 + 必要表头确认 + 行有效）
        的正对照：必须 ok / coverage_complete=True。"""
        ns = _ns()
        rows = [_valid_row(),
                _valid_row(side="sell", quantity=-200.0, price=13.30,
                           amount=2660.0, fee=4.0)]
        v = ns["yh_fills_verdict"](_full_fev(rows, count_total=2))
        self.assertEqual(v["status"], "ok")
        self.assertTrue(v["coverage_complete"])
        self.assertEqual(v["row_count"], 2)

    def test_pc3_clean_negative_and_plain_numbers_unchanged(self):
        """旧资金路径回归锚：常规负数/千分位/占位符解析行为不得改变。"""
        ns = _ns()
        parse = ns["_parse_number"]
        self.assertEqual(parse("-1,234.56"), -1234.56)
        self.assertEqual(parse("1,234.56"), 1234.56)
        self.assertIsNone(parse("--"))
        self.assertIsNone(parse(""))
        rec = ns["yh_normalize_fill_record"](
            {"发生金额": "-1,337.00", "证券代码": "002015"})
        self.assertEqual(rec["net_amount"], -1337.0)

    def test_pc4_identical_duplicate_rows_preserved(self):
        """完全重复的逐笔记录必须原样保留，绝不按字段相同去重。"""
        ns = _ns()
        cells = [("20261112", 260.0), ("证券买入", 330.0), ("002015", 400.0),
                 ("协鑫能科", 470.0), ("100", 540.0), ("13.37", 610.0),
                 ("1337.00", 680.0)]
        rows = ns["yh_fills_from_items"](_page_items([cells, cells]))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0], rows[1])


# ── 反例（当前冻结脚本必须暴露的六类证据缺口；RED 预期） ────────────
class FillsEvidenceCounterexamples(unittest.TestCase):

    def test_ce1_date_range_from_page_tokens_only_is_not_proven(self):
        """CE1：全页记录日期的 min/max ≠ 查询范围已证明。

        合成页面上除两行行内成交日期外没有任何查询日期控件读回令牌；
        date_range 完全由页面令牌排序取首尾推得（复刻采集器 L4034-4037
        的推导）。此时 date_range_proven 必须 False、不得 status=ok。
        date_range_source 缺席或 = "page_tokens" 都视为未证明（最小输入
        契约，见模块 docstring）；绝不臆造"日期控件已读回"的事实。"""
        ns = _ns()
        r1 = [("20261103", 260.0), ("证券买入", 330.0), ("002015", 400.0),
              ("协鑫能科", 470.0), ("100", 540.0), ("13.37", 610.0),
              ("1337.00", 680.0)]
        r2 = [("20261112", 260.0), ("证券卖出", 330.0), ("002015", 400.0),
              ("协鑫能科", 470.0), ("-200", 540.0), ("13.30", 610.0),
              ("2660.00", 680.0)]
        items = _page_items([r1, r2])
        tokens = ns["yh_fills_date_tokens"](items)
        isos = sorted({d["iso"] for d in tokens})
        self.assertEqual(isos, ["2026-11-03", "2026-11-12"])  # 仅行内日期
        derived = ({"start": isos[0], "end": isos[-1]}
                   if len(isos) >= 2 else None)
        rows = [_valid_row(date="20261103"),
                _valid_row(side="sell", quantity=-200.0, price=13.30,
                           amount=2660.0, fee=4.0)]
        for source in (None, "page_tokens"):
            with self.subTest(date_range_source=source):
                fev = _full_fev(rows, count_total=2, date_tokens=tokens,
                                date_range=derived,
                                date_range_source=source)
                v = ns["yh_fills_verdict"](fev)
                self.assertFalse(
                    v["date_range_proven"],
                    "行日期 min/max 不得当作查询范围已证明")
                self.assertNotEqual(v["status"], "ok")
                self.assertFalse(v["coverage_complete"])

    def test_ce2_unknown_pagination_blocks_complete_despite_summary(self):
        """CE2：count_total=None 且分页未知时，汇总对账吻合也不得完整。

        页面无"共N条"（count_total=None）时，page_total>1 或存在"下一页"
        都意味着覆盖未证——未知分页不可默认为一页。先锚定所用汇总确实与
        行集对账吻合（数量 100-2000+2000=100，金额 1337+26600+26660=
        54597），确保本反例唯一变量是分页未知。"""
        ns = _ns()
        rows = _three_rows()
        summary = {"present": True, "quantity": 100.0, "amount": 54597.0,
                   "stamp_tax": 13.3, "net_amount": -1434.0}
        self.assertTrue(
            ns["yh_fills_summary_reconciled"]({"rows": rows,
                                               "summary": summary}),
            "锚定：该合成汇总本身对账吻合")
        for over in ({"page_total": 3, "page_current": 1},
                     {"next_page_present": True}):
            with self.subTest(pagination=over):
                fev = _full_fev(rows, count_total=None, summary=summary,
                                **over)
                v = ns["yh_fills_verdict"](fev)
                self.assertFalse(
                    v["coverage_complete"],
                    "未知分页不可默认一页，不得宣称覆盖完整")
                self.assertNotEqual(v["status"], "ok")

    def test_ce3_summary_contradiction_on_any_read_field_is_not_reconciled(
            self):
        """CE3：数量+金额吻合但另一已读汇总值矛盾 → 必须 False。

        汇总行每个"行集可合计且汇总已读出"的字段都必须吻合；不得首个
        吻合字段（amount）即 return True。行集合计：stamp_tax 13.3、
        net_amount -1434（见 _three_rows）。"""
        ns = _ns()
        rows = _three_rows()
        base = {"present": True, "quantity": 100.0, "amount": 54597.0,
                "stamp_tax": 13.3, "net_amount": -1434.0}
        self.assertTrue(
            ns["yh_fills_summary_reconciled"]({"rows": rows,
                                               "summary": dict(base)}),
            "锚定：全部吻合时对账为 True")
        for field, bad in (("stamp_tax", 999.0), ("net_amount", 999.0)):
            with self.subTest(field=field):
                summary = dict(base)
                summary[field] = bad
                self.assertFalse(
                    ns["yh_fills_summary_reconciled"](
                        {"rows": rows, "summary": summary}),
                    f"{field} 矛盾时不得因数量/金额吻合而判对账通过")

    def test_ce4_rows_missing_key_fields_cannot_yield_ok(self):
        """CE4：行内关键字段缺失/非法 → 不得 status=ok（行原样保留）。

        行有效性最小契约（见模块 docstring）：time 合法 HH:MM:SS、side ∈
        {buy,sell}、price/quantity 数值非 None。违规行必须保留在 rows 中
        （row_count 不缩水），但不得支撑 ok——证据不足按 partial 如实
        降级。"""
        ns = _ns()
        for over in ({"time": None}, {"time": "99:99:99"},
                     {"side": None}, {"price": None}, {"quantity": None}):
            with self.subTest(row_override=over):
                rows = [_valid_row(**over)]
                v = ns["yh_fills_verdict"](_full_fev(rows, count_total=1))
                self.assertNotEqual(
                    v["status"], "ok",
                    f"行缺失 {over} 时不得宣称 ok")
                self.assertFalse(v["coverage_complete"])
                self.assertEqual(
                    v["row_count"], len(rows),
                    "违规行必须原样保留，不得静默丢弃")

    def test_ce5_unicode_minus_and_minus_space_never_flip_sign(self):
        """CE5：Unicode 负号(U+2212)/负号后空白绝不能把负数转正。

        现行为：_parse_number 的 `-?\\d+` 在 "−1,234.56"（U+2212）与
        "- 1,234.56"（负号+空白）上都会跳过负号形态取到 +1234.56——
        负号丢失=金额符号翻转=对账灾难。要求：解析为负数或显式 None，
        绝不为正。规范层与 _parse_number 层同口径断言；常规负数不变由
        PC3 锚定（修复不得改坏旧资金路径）。"""
        ns = _ns()
        normalize = ns["yh_normalize_fill_record"]
        parse = ns["_parse_number"]
        minus_forms = ["−1,234.56", "- 1,234.56", "− 500"]
        for text in minus_forms:
            for column, field in (("发生金额", "net_amount"),
                                  ("成交数量", "quantity")):
                with self.subTest(column=column, text=text):
                    rec = normalize({column: text,
                                     "证券代码": "002015"})
                    value = rec[field]
                    self.assertTrue(
                        value is None or value < 0,
                        f"{column}={text!r} → {field}={value!r}："
                        "负号丢失/符号翻转")
            with self.subTest(layer="_parse_number", text=text):
                value = parse(text)
                self.assertTrue(
                    value is None or value < 0,
                    f"_parse_number({text!r}) = {value!r}：负数被转正")

    def test_ce6_unconfirmed_headers_block_complete_even_vision_model(self):
        """CE6：必要表头未确认 → 计数吻合也不得完整；reader 非证据。

        header_columns 是采集器已写字段（L4031-4032）；必要集合由生产
        常量 _YH_FILLS_TRADE_TOKENS 推导（见 _required_header_columns）。
        即便 reader=vision_model、count_total==行数、单页无下一页，必要
        表头缺席/不齐也不得 coverage_complete——视觉模型种类不是覆盖
        证据。"""
        ns = _ns()
        required = _required_header_columns()
        self.assertEqual(required, {"date", "time", "side_raw",
                                    "quantity", "price", "amount"})
        for cols in ([], sorted(required - {"time"})):
            with self.subTest(header_columns=cols):
                fev = _full_fev([_valid_row()], count_total=1,
                                header_columns=cols,
                                reader="vision_model")
                v = ns["yh_fills_verdict"](fev)
                self.assertFalse(
                    v["coverage_complete"],
                    "必要表头未确认不得宣称横向覆盖完整")
                self.assertNotEqual(v["status"], "ok")


if __name__ == "__main__":  # 本切片不运行；RED 执行走 scratch runner
    unittest.main(verbosity=2)
