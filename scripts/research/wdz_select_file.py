import ctypes, sys, time
sys.path.insert(0, r'F:\AiStock\app\scripts\research')
from winutil import dump

user32 = ctypes.windll.user32
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    pass

CSV_WIN = 400130
FILE_LIST = 334768
LB_SETCURSEL = 0x0186
LB_GETTEXTLEN = 0x018A
WM_COMMAND = 0x0111
LB_GETCOUNT = 0x018B


def dlg_ctrl_id(h):
    return user32.GetDlgCtrlID(h)


def fire_selchange(list_h, parent_h):
    cid = dlg_ctrl_id(list_h)
    wp = (1 << 16) | cid  # LBN_SELCHANGE=1
    user32.SendMessageW(parent_h, WM_COMMAND, wp, list_h)


if __name__ == '__main__':
    idx = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    n = user32.SendMessageW(FILE_LIST, LB_GETCOUNT, 0, 0)
    print('items:', n)
    for i in range(n):
        ln = user32.SendMessageW(FILE_LIST, LB_GETTEXTLEN, i, 0)
        print(i, 'len', ln)
    user32.SendMessageW(FILE_LIST, LB_SETCURSEL, idx, 0)
    fire_selchange(FILE_LIST, CSV_WIN)
    print('selected index', idx)
    time.sleep(1)
    for ch, cl, tx, rc in dump(CSV_WIN):
        if ch in (1383384, 269280, 269100, 334766):
            print(ch, cl, repr(tx[:90]))
