"""Compatibility import; canonical implementation: strategies.g3.confirmation."""
import importlib
import sys
sys.modules[__name__] = importlib.import_module("strategies.g3.confirmation")
