import sys
import dataclasses

if sys.version_info < (3, 10):
    _orig_dataclass = dataclasses.dataclass

    def _compat_dataclass(*args, **kwargs):
        kwargs.pop("slots", None)
        return _orig_dataclass(*args, **kwargs)

    dataclasses.dataclass = _compat_dataclass
