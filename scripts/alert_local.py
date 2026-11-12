# -*- coding: utf-8 -*-
"""本地弹窗通知（tkinter 定型组件）。

任务书 20261112_l2_integration.md 第 3 项：
  tkinter 弹窗（动作标题+下单卡片+自动长高+大按钮）
  声音提示：播放系统 beep 或 wav
  调用位置：pipeline_scheduler 的 t_trade_monitor job 中

本模块可在有 GUI 的环境中独立运行（显示弹窗），也可在不带 GUI
的环境中静默运行（只写日志）。
"""

from __future__ import annotations

__version__ = "1.0.0"

import logging
import os
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("alert_local")

# 弹窗是否可用的全局判断
_HAS_TK = False
try:
    import tkinter as tk
    from tkinter import font as tkfont

    _HAS_TK = True
except ImportError:
    pass


# ── 弹窗生成（线程安全） ──────────────────────────────────────────

class TradeAlertWindow:
    """做T信号弹窗（tkinter, 线程安全）。"""

    _instances: list[tk.Tk] = []
    _lock = threading.Lock()

    def __init__(
        self,
        title: str = "做T信号",
        timeout_ms: int = 15000,
        sound: bool = True,
    ):
        self.title = title
        self.timeout_ms = timeout_ms
        self.sound = sound
        self._root: Optional[tk.Tk] = None
        self._ready = threading.Event()

    def show(
        self,
        code: str,
        name: str,
        side: str,       # "buy-T" | "sell-T"
        price: float,
        qty: int,
        tier: Optional[str] = None,
        rsi15: Optional[float] = None,
        rsi5: Optional[float] = None,
        extra: Optional[str] = None,
    ) -> bool:
        """显示弹窗（非阻塞，立即返回）。返回 True=弹窗实际显示。"""
        if not _HAS_TK:
            logger.info("[alert] tkinter 不可用，跳过弹窗")
            return False

        side_label = "买入T" if side in ("buy-T", "buy") else "卖出T"
        tier_label = f" [{tier}]" if tier else ""

        data = {
            "code": code,
            "name": name,
            "side": side_label,
            "side_key": side,
            "tier": tier_label,
            "price": price,
            "qty": qty,
            "rsi15": rsi15,
            "rsi5": rsi5,
            "extra": extra or "",
        }

        t = threading.Thread(
            target=self._run_window,
            args=(data,),
            daemon=True,
        )
        t.start()
        return True

    def _run_window(self, data: dict) -> None:
        """在独立线程中创建并显示 tkinter 弹窗。"""
        if not _HAS_TK:
            return
        root = tk.Tk()
        self._root = root
        root.title(self.title)
        root.attributes("-topmost", True)
        root.resizable(False, False)

        # 动作标题
        side = data["side"]
        side_key = data["side_key"]
        bg_color = "#FF4444" if side_key in ("sell-T", "sell") else "#44BB44"
        fg_color = "#FFFFFF"

        title_frame = tk.Frame(root, bg=bg_color, padx=10, pady=6)
        title_frame.pack(fill="x")

        tk.Label(
            title_frame,
            text=f"{side}{data['tier']}",
            font=("Microsoft YaHei", 16, "bold"),
            fg=fg_color,
            bg=bg_color,
        ).pack()

        # 下单卡片
        card = tk.Frame(root, padx=16, pady=12)
        card.pack(fill="both", expand=True)

        info_rows = [
            f"代码: {data['code']}  {data['name']}",
            f"价格: {data['price']:.2f}  数量: {data['qty']}",
            f"时间: {datetime.now().strftime('%H:%M:%S')}",
        ]
        if data["rsi15"] is not None:
            info_rows.append(f"15min RSI: {data['rsi15']:.1f}  ")
        if data["rsi5"] is not None:
            info_rows.append(f"5min RSI: {data['rsi5']:.1f}")
        if data["extra"]:
            info_rows.append(data["extra"])

        for row in info_rows:
            tk.Label(
                card,
                text=row,
                font=("Microsoft YaHei", 11),
                anchor="w",
                justify="left",
            ).pack(fill="x", pady=1)

        # 大确认按钮
        btn_frame = tk.Frame(root, padx=10, pady=8)
        btn_frame.pack(fill="x")

        action_text = "下单执行" if side_key in ("sell-T", "sell") else "下单买入"
        btn = tk.Button(
            btn_frame,
            text=action_text,
            font=("Microsoft YaHei", 14, "bold"),
            bg="#2196F3",
            fg="white",
            padx=20,
            pady=8,
            command=self._dismiss,
        )
        btn.pack(expand=True)

        # 自动关闭计时
        if self.timeout_ms > 0:
            root.after(self.timeout_ms, self._dismiss)

        # 声音提示
        if self.sound:
            self._beep()

        # 自动长高（根据内容自适应）
        root.update_idletasks()
        w = max(360, root.winfo_reqwidth() + 40)
        h = root.winfo_reqheight() + 20
        screen_w = root.winfo_screenwidth()
        screen_h = root.winfo_screenheight()
        x = (screen_w - w) // 2
        y = (screen_h - h) // 2
        root.geometry(f"{w}x{h}+{x}+{y}")

        with self._lock:
            self._instances.append(root)
        self._ready.set()
        root.mainloop()
        with self._lock:
            if root in self._instances:
                self._instances.remove(root)

    def _dismiss(self) -> None:
        if self._root is not None:
            try:
                self._root.destroy()
            except tk.TclError:
                pass
            self._root = None

    def _beep(self) -> None:
        """播放声音提示（系统扬声器 beep；wav 预留）。"""
        try:
            # 系统 beep
            print("\a", end="", flush=True)
        except Exception:
            pass
        # 预留 wav 播放路径（后续可加 winsound.PlaySound）
        try:
            import winsound
            winsound.Beep(880, 400)  # 频率 880Hz，持续 400ms
        except Exception:
            pass

    @classmethod
    def dismiss_all(cls) -> None:
        """关闭所有弹窗（安全清理用）。"""
        with cls._lock:
            for r in list(cls._instances):
                try:
                    r.destroy()
                except tk.TclError:
                    pass
            cls._instances.clear()

    @classmethod
    def wait_for_ready(cls, timeout: float = 10.0) -> bool:
        """等待弹窗就绪（测试用）。"""
        # 轮询检查是否有弹窗线程正在创建
        import time
        deadline = time.time() + timeout
        while time.time() < deadline:
            with cls._lock:
                if cls._instances:
                    return True
            time.sleep(0.1)
        return False


# ── 便利函数 ──────────────────────────────────────────────────────

def show_trade_alert(
    code: str,
    name: str,
    side: str,
    price: float,
    qty: int,
    tier: Optional[str] = None,
    rsi15: Optional[float] = None,
    rsi5: Optional[float] = None,
    extra: Optional[str] = None,
) -> bool:
    """一行调用显示做T信号弹窗。返回是否实际显示。"""
    w = TradeAlertWindow()
    return w.show(
        code=code,
        name=name,
        side=side,
        price=price,
        qty=qty,
        tier=tier,
        rsi15=rsi15,
        rsi5=rsi5,
        extra=extra,
    )


# ── 独立运行入口 ──────────────────────────────────────────────────

def main() -> int:
    """命令行动态弹窗测试。"""
    import argparse

    ap = argparse.ArgumentParser(description="做T信号弹窗测试工具")
    ap.add_argument("--code", default="002015", help="股票代码")
    ap.add_argument("--name", default="南方轴承", help="股票名称")
    ap.add_argument("--side", default="buy-T", choices=["buy-T", "sell-T"])
    ap.add_argument("--price", type=float, default=13.50, help="价格")
    ap.add_argument("--qty", type=int, default=200, help="股数")
    ap.add_argument("--tier", default=None, help="tier（trim/full/normal/conviction）")
    ap.add_argument("--rsi15", type=float, default=None, help="15min RSI")
    ap.add_argument("--rsi5", type=float, default=None, help="5min RSI")
    ap.add_argument("--no-sound", action="store_true", help="禁用声音")
    args = ap.parse_args()

    w = TradeAlertWindow(timeout_ms=60000, sound=not args.no_sound)
    ok = w.show(
        code=args.code, name=args.name, side=args.side,
        price=args.price, qty=args.qty, tier=args.tier,
        rsi15=args.rsi15, rsi5=args.rsi5,
    )
    if ok:
        try:
            import time
            time.sleep(60)
        except KeyboardInterrupt:
            pass
        TradeAlertWindow.dismiss_all()
        return 0
    else:
        print("弹窗未显示（tkinter 不可用或无 GUI 环境）")
        return 1


if __name__ == "__main__":
    sys.exit(main())