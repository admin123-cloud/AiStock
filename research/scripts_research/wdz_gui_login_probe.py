import time
from pywinauto import Desktop

desktop = Desktop(backend='win32')

def find_win(pred):
    for w in desktop.windows():
        try:
            if pred(w):
                return w
        except Exception:
            pass
    return None

# 1) 登录窗口:填用户名/密码
login = None
for w in desktop.windows():
    try:
        t = w.window_text()
        cls = w.class_name()
    except Exception:
        continue
    if t == '登录' and cls == 'ThunderRT6FormDC':
        login = w
if login is None:
    print('NO LOGIN WINDOW')
    raise SystemExit
print('login found:', login.window_text(), login.handle)
try:
    login.print_control_identifiers(depth=4)
except Exception as e:
    print('dump fail:', str(e)[:200])
