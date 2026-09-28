import ctypes
from ctypes import wintypes

u32 = ctypes.windll.user32

def get_menu_text(hmenu, idx, buflen=200):
    buf = ctypes.create_unicode_buffer(buflen)
    n = u32.GetMenuStringW(hmenu, idx, buf, buflen, 0x400)  # MF_BYPOSITION
    return buf.value if n else ''

def walk(menu, depth=0, path=''):
    n = u32.GetMenuItemCount(menu)
    for i in range(n):
        mid = u32.GetMenuItemID(menu, i)
        txt = get_menu_text(menu, i)
        sub = u32.GetSubMenu(menu, i)
        print('  ' * depth, i, repr(txt), 'id=', mid, 'sub=', bool(sub))
        if sub and depth < 3:
            walk(sub, depth + 1, path + '/' + txt)

hwnd = 793574
m = u32.GetMenu(hwnd)
print('menu:', m)
if m:
    walk(m)
