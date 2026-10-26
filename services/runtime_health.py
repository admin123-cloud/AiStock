"""Compatibility import; canonical implementation: services.operations.health."""
import importlib
import sys
sys.modules[__name__] = importlib.import_module("services.operations.health")
