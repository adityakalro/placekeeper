"""Keep service implementations out of lightweight plugin descriptors."""
from importlib import import_module


def deferred(module, name):
    def call(*args, **kwargs):
        return getattr(import_module(module), name)(*args, **kwargs)
    return call
