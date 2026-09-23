"""AWTRIX NG compatibility bridge for the Ulanzi TC002."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("tc002-awtrix-bridge")
except PackageNotFoundError:
    __version__ = "0.0.0+local"
