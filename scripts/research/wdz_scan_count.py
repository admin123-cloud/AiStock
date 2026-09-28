import ctypes, sys, time
sys.path.insert(0, r'F:\AiStock\app\scripts\research')
from winutil import dump, get_text, get_class

user32 = ctypes.windll.user32
LB_GETCOUNT = 0x018B
LB_SETCURSEL = 0x0186
WM_COMMAND = 0x0111
BM_CLICK = 0x00F5

CSV_WIN = 400130
FILE_LIST = 334768
SCAN = 269282

def fire_selchange(h, parent):
    cid = user32.GetDlgCtrlID(h)
    user32.SendMessageW(parent, WM_COMMAND, (1 << 16) | cid, h)

def count(h):
    return user32.SendMessageW(h, LB_GETCOUNT, 0, 0)

print('list before:', count(FILE_LIST))
# 选中 2003(序号3)并触发
user32.SendMessageW(FILE_LIST, LB_SETCURSEL, 3, 0)
fire_selchange(FILE_LIST, CSV_WIN)
time.sleep(1)
click_scan = SCAN
user32.PostMessageW(SCAN, BM_CLICK, 0, 0)
print('scan clicked, waiting...')
for i in range(30):
    time.sleep(5)
    print(f't={i*5}s list count:', count(FILE_LIST), flush=True)
    n = count(FILE_LIST)
    if n > 8:
        break
print('final count:', count(FILE_LIST))
