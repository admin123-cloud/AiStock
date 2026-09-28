"""兼容历史研究脚本的直接启动方式，所有产物仍通过utils.paths定位。"""
import sys
from utils.paths import PROJECT_ROOT


def prepare_script() -> None:
    # Old direct scripts could import a sibling by basename.
    path = str(PROJECT_ROOT / 'scripts')
    if path not in sys.path:
        sys.path.append(path)
