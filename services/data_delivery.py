"""Compatibility import; canonical implementation: services.operations.delivery."""
import importlib
import sys
sys.modules[__name__] = importlib.import_module("services.operations.delivery")
