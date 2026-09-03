"""collections2mo2: Nexus collections -> MO2 instances -> Wabbajack modlists."""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _version

try:
    __version__ = _version("collections2mo2")
except PackageNotFoundError:
    __version__ = "0.0.0+unknown"
