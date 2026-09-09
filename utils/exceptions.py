"""兼容旧异常导入；唯一实现位于core.exceptions。"""
import sys
from core import exceptions as _canonical
sys.modules[__name__] = _canonical
