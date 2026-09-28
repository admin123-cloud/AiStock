import time
from pywinauto import Application, Desktop

app = Application(backend='win32').start(r'E:\AStockHistory\wstock\解压工具\wsWDZ\wdz_app\wsWDZ.exe')
time.sleep(3)
desktop = Desktop(backend='win32')
for w in desktop.windows():
    try:
        print('WIN:', w.window_text(), w.class_name())
    except Exception as e:
        print('win err', e)
# 找主窗口/登录框
for title in ('wsWDZ', 'wdz', '微盛', 'WSTOCK', '登录', '用户'):
    wins = [w for w in desktop.windows() if title.lower() in (w.window_text() or '').lower() or title.lower() in (w.class_name() or '').lower()]
    for w in wins:
        print('=== controls of', w.window_text(), w.class_name())
        try:
            w.print_control_identifiers(depth=3)
        except Exception as e:
            print('dump err', e)
        break
