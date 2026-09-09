"""Compatibility import; canonical implementation: strategies.contracts."""
import importlib
import sys
sys.modules[__name__] = importlib.import_module("strategies.contracts")
