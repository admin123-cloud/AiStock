import sys, time
sys.path.insert(0, r'F:\AiStock\app\scripts\research')
import ctypes
from winutil import dump, get_class
from wstock_rotation_day import find_wins, click
user32 = ctypes.windll.user32
NEEDLE = '可能不是正确的通达信文件夹'
deadline = time.time() + 3 * 3600
n = 0
while time.time() < deadline:
    answered = False
    for h, t, c in find_wins(''):
        if c != '#32770' or not user32.IsWindowVisible(h):
            continue
        kids = dump(h)
        texts = [t2 for ch, c2, t2, r in kids if 'Static' in c2 and t2]
        if any(NEEDLE in x for x in texts):
            for ch, c2, t2, r in kids:
                if c2 == 'Button' and t2.replace('&', '').startswith('是'):
                    click(ch)
                    n += 1
                    answered = True
                    print('answered', n, flush=True)
                    break
        if answered:
            break
    time.sleep(3 if answered else 5)
print('answerer exit', n)
