"""The factory package. Its version is the installed distribution's, never a second copy."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("software-factory")
except PackageNotFoundError:  # a source tree nobody installed - `python -m factory`
    __version__ = "0+source"
