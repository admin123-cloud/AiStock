"""wstock 免费轮换日自动导出(csv 模式,无年份限制)+ 补数管线。

用法: python wstock_rotation_day.py --tail N
流程: 杀旧实例 → 启动 → 登录 wstock{tail}/{tail*4}(tail6 用 wstock.net/1111) →
      菜单 格式选择→csv格式 → 设源/目标目录 → 扫描 → 全部选中 → 输出数据 →
      自动应答 TDXW 警告框,轮询至完毕。
之后由 rotation_day.bat 调 stage/apply。
"""
from __future__ import annotations
import argparse, ctypes, shutil, subprocess, sys, time
from ctypes import wintypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from winutil import dump, get_class, get_text

user32 = ctypes.windll.user32
WM_SETTEXT = 0x000C
WM_COMMAND = 0x0111
BM_CLICK = 0x00F5
LB_GETCOUNT = 0x018B

EXE = r'E:\AStockHistory\wstock\解压工具\wsWDZ\wdz_app\wsWDZ.exe'
SRC_DIR = r'E:\AStockHistory\wstock\原始压缩包\WDZ'
DST_DIR = r'E:\AStockHistory\wstock\输出\轮转暂存'
NEEDLE = '可能不是正确的通达信文件夹'
CSV_TITLE = 'csv格式'

def prepare_output_dir(output_dir: Path) -> None:
    """Keep prior/partial output as evidence and start this run clean."""
    if not output_dir.exists():
        output_dir.mkdir(parents=True, exist_ok=True)
        return
    if any(output_dir.rglob('*')):
        stamp = time.strftime('%Y%m%d_%H%M%S')
        archive = output_dir.with_name(output_dir.name + '_archive_' + stamp)
        shutil.move(str(output_dir), str(archive))
        print(f'archived previous output: {archive}', flush=True)
    output_dir.mkdir(parents=True, exist_ok=True)

def click(h):
    user32.PostMessageW(h, BM_CLICK, 0, 0)

def set_text(h, text):
    user32.SendMessageW(h, WM_SETTEXT, 0, text)

def find_wins(title_part=''):
    res = []
    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)

    def cb(h, l):
        t = get_text(h)
        if title_part in t:
            res.append((h, t, get_class(h)))
        return True
    user32.EnumWindows(WNDENUMPROC(cb), 0)
    return res

def wait_for(pred, timeout=60, step=2):
    for _ in range(max(1, timeout // step)):
        r = pred()
        if r:
            return r
        time.sleep(step)
    return None

def children(hwnd):
    return dump(hwnd)

def visible_forms():
    """Find VB6 forms without depending on localized/garbled captions."""
    return [(h, t, c) for h, t, c in find_wins()
            if user32.IsWindowVisible(h) and c == 'ThunderRT6FormDC']

def form_rect(hwnd):
    r = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return r.left, r.top, r.right, r.bottom

def controls_of(hwnd, cls):
    return [(h, r) for h, c, _t, r in children(hwnd) if c == cls]

def find_login_form():
    for h, _t, _c in visible_forms():
        if len(controls_of(h, 'ThunderRT6TextBox')) >= 2 and controls_of(h, 'ThunderRT6CommandButton') and not controls_of(h, 'ThunderRT6ListBox'):
            return h
    return None

def find_csv_form():
    for h, _t, _c in visible_forms():
        if len(controls_of(h, 'Edit')) >= 2 and len(controls_of(h, 'ThunderRT6CommandButton')) >= 8 and controls_of(h, 'ThunderRT6ListBox'):
            return h
    return None

def button_by_geometry(hwnd, y0, y1, x0=0, x1=10_000):
    left, top, _right, _bottom = form_rect(hwnd)
    candidates = []
    for h, c, _t, r in children(hwnd):
        if c != 'ThunderRT6CommandButton':
            continue
        rx, ry = r[0] - left, r[1] - top
        if y0 <= ry <= y1 and x0 <= rx <= x1:
            candidates.append((rx, ry, h))
    candidates.sort()
    return candidates[0][2] if candidates else None

def probe_user_info(hwnd):
    """Return the vendor permission text, or None when the account is not active today."""
    user32.PostMessageW(hwnd, WM_COMMAND, 11, 0)  # 查看用户信息
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        for h, _t, c in find_wins():
            if c != '#32770' or not user32.IsWindowVisible(h):
                continue
            child_rows = children(h)
            texts = [t for _ch, cls, t, _r in child_rows if cls == 'Static' and t and t != 'for debug']
            has_static = any(cls == 'Static' and t != 'for debug' for _ch, cls, t, _r in child_rows)
            has_button = any(cls == 'Button' for _ch, cls, _t, _r in child_rows)
            text = ' '.join(texts).strip()
            if has_static and has_button:
                for ch, cls, _t, _r in child_rows:
                    if cls == 'Button':
                        click(ch)
                        break
                return text or 'permission_dialog'
        time.sleep(0.2)
    return None

def auto_answer_tdxw_dialog(timeout_hours=4):
    import threading

    def worker():
        deadline = time.time() + timeout_hours * 3600
        while time.time() < deadline:
            answered = False
            for h, t, c in find_wins('#32770'):
                if c != '#32770' or not user32.IsWindowVisible(h):
                    continue
                kids = children(h)
                texts = [t2 for ch, c2, t2, r in kids if 'Static' in c2 and t2]
                if any(NEEDLE in x for x in texts):
                    for ch, c2, t2, r in kids:
                        if c2 == 'Button' and t2.replace('&', '').startswith('是'):
                            click(ch)
                            answered = True
                            break
                if answered:
                    break
            time.sleep(3 if answered else 5)

    threading.Thread(target=worker, daemon=True).start()

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--tail', type=int, required=True)
    ap.add_argument('--only-login', action='store_true')
    ap.add_argument('--source-dir', type=Path, default=Path(SRC_DIR))
    ap.add_argument('--output-dir', type=Path, default=None)
    ap.add_argument('--export-timeout', type=int, default=1800)
    a = ap.parse_args()
    tail = str(a.tail)
    credentials = {
        '3': ('wstock3', '3333'),
        '4': ('wstock4', '4443'),
        '5': ('wstock5', '5555'),
        '6': ('wstock.net', '1111'),
        '7': ('wstock7', '7777'),
    }
    if tail not in credentials:
        print(f'ERROR: unsupported free account tail {tail}; configured tails={sorted(credentials)}')
        sys.exit(2)
    user, pwd = credentials[tail]

    subprocess.run(['taskkill', '/F', '/T', '/IM', 'wsWDZ.exe'], capture_output=True)
    time.sleep(2)
    wait_for(lambda: 'wswdz.exe' not in subprocess.run(
        ['tasklist', '/FI', 'IMAGENAME eq wsWDZ.exe'], capture_output=True,
        text=True, encoding='mbcs', errors='replace').stdout.lower(), 20, 1)
    subprocess.Popen([EXE], cwd=str(Path(EXE).parent))
    time.sleep(6)

    lh = wait_for(find_login_form, 40)
    if not lh:
        print('ERROR: no login window'); sys.exit(2)
    boxes = [h for h, _r in controls_of(lh, 'ThunderRT6TextBox')]
    ok = button_by_geometry(lh, 75, 125, 0, 150)
    if len(boxes) < 2 or not ok:
        print('ERROR: login controls missing'); sys.exit(2)
    set_text(boxes[0], user)
    set_text(boxes[1], pwd)
    time.sleep(0.5)
    click(ok)
    time.sleep(3)

    # 任意可见的微盛格式窗口(通达信/大智慧 等模式窗) → 菜单切到 csv 格式
    wh = wait_for(lambda: next((h for h, _t, _c in visible_forms() if user32.GetMenu(h)), None), 60)
    if not wh:
        print('ERROR: format window missing'); sys.exit(2)
    m = user32.GetMenu(wh)
    if not m:
        print('ERROR: no menu'); sys.exit(2)
    # 菜单: 0=格式选择; 子项 id 48 = csv格式(wsCSV)
    sub = user32.GetSubMenu(m, 0)
    user32.PostMessageW(wh, WM_COMMAND, 48, 0)
    time.sleep(4)
    wh2 = wait_for(find_csv_form, 60)
    if not wh2:
        print('ERROR: csv window missing'); sys.exit(2)
    print('csv window ready:', wh2, flush=True)
    permission_text = probe_user_info(wh2)
    if permission_text:
        print('permission info:', permission_text, flush=True)
    else:
        print('ERROR: no active permission info; account/date gate likely not satisfied', flush=True)
        raise SystemExit(4)
    if a.only_login:
        print('LOGIN PHASE OK'); return

    kids = children(wh2)
    ctrl = {t: h for h, c, t, r in kids if get_class(h) == 'ThunderRT6CommandButton'}
    command_buttons = [h for h, c, _t, _r in kids if c == 'ThunderRT6CommandButton']
    edits = sorted([(h, r) for h, c, t, r in kids if c == 'Edit'], key=lambda x: x[1][1])
    # VB6 text retrieval may be empty across the 32/64-bit boundary. The
    # wsCSV child order is stable: output, filter, select-all, deselect-all,
    # ..., scan. Prefer that order over localized captions or screen geometry.
    scan_btn = ctrl.get('扫描') or (command_buttons[-1] if command_buttons else None)
    output_btn = ctrl.get('输出数据') or (command_buttons[0] if command_buttons else None)
    select_all = next((h for h, c, t, r in kids
                       if c == 'ThunderRT6CommandButton' and '全部' in t), None)
    select_all = select_all or (command_buttons[2] if len(command_buttons) >= 3 else None)
    if len(edits) < 2 or not scan_btn or not output_btn:
        print('ERROR: csv controls missing'); sys.exit(2)
    source_dir = str(a.source_dir)
    output_dir = a.output_dir or (Path(DST_DIR) / f'tail{tail}')
    prepare_output_dir(output_dir)
    set_text(edits[0][0], source_dir)
    set_text(edits[1][0], str(output_dir))
    time.sleep(0.5)
    click(scan_btn)
    print('scan clicked', flush=True)
    listbox = [h for h, c, t, r in children(wh2) if get_class(h) == 'ThunderRT6ListBox']
    for _ in range(30):
        time.sleep(2)
        if listbox and user32.SendMessageW(listbox[0], LB_GETCOUNT, 0, 0) >= 8:
            break
    print('file list count:', user32.SendMessageW(listbox[0], LB_GETCOUNT, 0, 0), flush=True)
    if select_all:
        click(select_all); time.sleep(0.5)
    auto_answer_tdxw_dialog()
    click(output_btn)
    print('export clicked, polling...', flush=True)
    stable = 0
    last_snapshot = None
    started = time.monotonic()
    for i in range(max(1, a.export_timeout // 10)):
        time.sleep(10)
        texts = ' '.join(t for h, c, t, r in children(wh2) if t)
        files = [p for p in output_dir.rglob('*') if p.is_file()]
        snapshot = (len(files), sum(p.stat().st_size for p in files))
        if snapshot[0] and snapshot == last_snapshot:
            stable += 1
        elif '完毕' in texts:
            stable += 1
        else:
            stable = 0
        last_snapshot = snapshot
        if stable >= 3:
            print(f'export done t={int(time.monotonic() - started)}s files={snapshot[0]} bytes={snapshot[1]}', flush=True)
            break
    else:
        print(f'export timeout t={a.export_timeout}s files={last_snapshot[0] if last_snapshot else 0} bytes={last_snapshot[1] if last_snapshot else 0}', flush=True)
        raise SystemExit(3)
    if not last_snapshot or not last_snapshot[0]:
        print('ERROR: export produced no files; stop before stage/apply', flush=True)
        raise SystemExit(3)
    print('PHASE1 DONE')

if __name__ == '__main__':
    main()

CSV_TITLE = 'csv格式'
