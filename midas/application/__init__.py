"""Lazy service construction keeps inspection and CLI help inexpensive."""
__all__ = ['Application', 'DEFAULT_LIMITS', 'Inspection', 'Inspector', 'Run']


def __getattr__(name):
    if name in ('Application', 'Run'):
        from . import service
        return getattr(service, name)
    if name == 'DEFAULT_LIMITS':
        from .preparation import DEFAULT_LIMITS
        return DEFAULT_LIMITS
    if name == 'Inspection':
        from ..status import Inspection
        return Inspection
    if name == 'Inspector':
        from .inspection import Inspector
        return Inspector
    raise AttributeError(name)
