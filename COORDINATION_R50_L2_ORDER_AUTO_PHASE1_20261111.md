# COORDINATION R50：L2 下单自动化 Phase1——风控门+勘察工具已交付；实机 UIA 测绘因面板未开顺延，Owner 开面板后一条命令完成（2026-11-11 23:1x，aistock-engineer → 微信PM）

## 一句话
任务书 3/4 项完成（l2_order_gate 风控门骨架 65 单测全过 + l2_panel_survey 勘察
工具就绪且管线实测验证）；第 1/2 项（实机 UIA 测绘+提交类按钮清单）因**今晚
交易面板未开**（Owner 15:11 关闭后未再开，EnumWindows/tasklist 双重取证）无法
执行——零输入铁律下不代开客户端（需登录凭证）。

## 需 PM 转 Owner（明早 Phase 2 前置）
1. **打开交易客户端并登录**（acct1 曾* 或 acct2 王*美皆可），然后一条命令完成测绘：
   `F:/python/python3.10/python.exe F:/Aistock/app/scripts/l2_panel_survey.py`
   （面板在王*美时加 `--allow-hotswitch` 自动 Alt+1 切曾*再测，OCR 自证）
   产出 runtime/l2_r50/panel_map.json（全控件树+可读写性+坐标）+ 截图 + 提交类
   按钮清单（今晚/勘察轮绝不 Invoke 已内建硬标注）。
2. 脚本零输入零点击零 Invoke 已做源码级自审计（含单测注毒验证）；前台有人自动让位；
   身份未知零触碰；勘察毕自动恢复最小化。

## 风控门（已可用）
- services/l2_order_gate.py：查 l2_risk_config（t_tracker），acct2 300股/6500元/
  日亏800、acct1 min(25000/价,底仓1/3整百)、偏离0.5%、同码日2单、120s新鲜度、
  09:30-11:30/13:00-15:00 时段、14:30 禁开新仓（平今卖腿豁免）、kill_switch、
  停牌/涨跌停/断流——全部 fail-closed（输入缺失=拒绝）。65 单测全过（含真实配置
  集成），相邻族 99 例零回归。纯逻辑零 UI（AST 守卫）。
- PM_REVIEW 开放点见 journal：日亏口径（现=day_pnl 含浮动）、次数口径（现=双向
  合计）、集合竞价窗口是否放行。

## 边界
零交易零 formal 表写；qmtmini 禁令未触碰；实测仅在既有非交易窗口上做只读 UIA
管线验证（通达信最小化窗、hexin_UserEnum 离屏调试框、设置/微信）。

证据：runtime/l2_r50/journal_r50_phase1.md + panel_map.json(status=panel_absent, rc=2)。
