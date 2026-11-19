import ctypes, sys, time
sys.path.insert(0, r'F:\AiStock\app\scripts\research')
from winutil import dump, get_text, get_class

user32 = ctypes.windll.user32
WM_SETTEXT = 0x000C
BM_CLICK = 0x00F5
LB_GETCOUNT = 0x018B

CSV_WIN = 400130
SRC_EDIT = 269280      # 源 wdz 路径
SCAN_BTN = 269282      # 扫描
SELALL_BTN = 203766    # 全部选中
DST_EDIT = 269100      # 输出目录
OUT_BTN = 531172       # 输出数据
LISTBOX = 334768

def click(h):
    user32.PostMessageW(h, BM_CLICK, 0, 0)

def set_text(h, text):
    user32.SendMessageW(h, WM_SETTEXT, 0, text)

def lb_count(h):
    return user32.SendMessageW(h, LB_GETCOUNT, 0, 0)

def win_exists(h):
    return user32.IsWindow(h)

if __name__ == '__main__':
    year = sys.argv[1]
    src = rf'E:\AStockHistory\wstock\原始压缩包\WDZ\wstock_SHSZ_{year}_5Min.wdz'
    dst = rf'E:\AStockHistory\wstock\输出\转换暂存\{year}'
    assert win_exists(CSV_WIN), 'csv window gone'
    set_text(SRC_EDIT, src)
    set_text(DST_EDIT, dst)
    time.sleep(0.5)
    click(SCAN_BTN)
    print('scan clicked', src)
