# Single source of truth is pyproject's `version`; read it back from the
# installed distribution metadata rather than hand-syncing a copy here.
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _version

try:
    __version__ = _version("fndr")
except PackageNotFoundError:  # running from a source tree with nothing installed
    __version__ = "0.0.0+unknown"
