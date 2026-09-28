# ctypes 枚举窗口控件:类名+文本(VB6 标准控件可用 WM_GETTEXT 跨位数读取)
import ctypes
from ctypes import wintypes

user32 = ctypes.windll.user32
EnumChildWindows = user32.EnumChildWindows
WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

SMTO_ABORTIFHUNG = 0x0002
SMTO_BLOCK = 0x0001

def _send_text(h, msg, w, text_ptr, timeout_ms=500):
    res = ctypes.c_size_t(0)
    ok = user32.SendMessageTimeoutW(h, msg, w, text_ptr,
                                    SMTO_ABORTIFHUNG | SMTO_BLOCK, timeout_ms,
                                    ctypes.byref(res))
    return bool(ok), res.value

def get_text(h):
    """跨进程安全的文本读取:繁忙窗口 500ms 超时,挂起进程返回 ''。"""
    buf = ctypes.create_unicode_buffer(512)
    res = ctypes.c_size_t(0)
    ok = user32.SendMessageTimeoutW(h, 0x000E, 512, buf,  # WM_GETTEXT
                                    SMTO_ABORTIFHUNG | SMTO_BLOCK, 500,
                                    ctypes.byref(res))
    if ok and buf.value:
        return buf.value
    # VB6 child controls often return an empty WM_GETTEXT result across the
    # 32/64-bit boundary even while their caption is available to
    # GetWindowTextW. Restrict the fallback to those lightweight controls;
    # never call the potentially blocking API for a top-level form.
    cls = ctypes.create_unicode_buffer(128)
    user32.GetClassNameW(h, cls, 128)
    if cls.value.startswith('ThunderRT6') or cls.value in {'Edit', 'Button', 'Static'}:
        fallback = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(h, fallback, 512)
        return fallback.value
    return ''

def get_text_len(h):
    res = ctypes.c_size_t(0)
    ok = user32.SendMessageTimeoutW(h, 0x000E, 0, None,  # WM_GETTEXTLENGTH
                                    SMTO_ABORTIFHUNG | SMTO_BLOCK, 500,
                                    ctypes.byref(res))
    return res.value if ok else 0

def get_class(h):
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(h, buf, 256)
    return buf.value

def get_rect(h):
    r = wintypes.RECT()
    user32.GetWindowRect(h, ctypes.byref(r))
    return (r.left, r.top, r.right, r.bottom)

def dump(parent):
    out = []
    @WNDENUMPROC
    def cb(h, l):
        out.append((h, get_class(h), get_text(h), get_rect(h)))
        return True
    EnumChildWindows(parent, cb, 0)
    return out

def set_text(h, text):
    user32.SendMessageW(h, 0x000C, 0, text)  # WM_SETTEXT

def click(h):
    user32.PostMessageW(h, 0x00F5, 0, 0)  # BM_CLICK

if __name__ == '__main__':
    import sys
    hwnd = int(sys.argv[1])
    for h, cls, text, rect in dump(hwnd):
        print(h, cls, repr(text[:60]), rect)
