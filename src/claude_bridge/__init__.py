"""MCP bridge from host Claude Cowork to Claude Code inside a devcontainer."""

# Single source of truth for the package version. ``hatch`` reads this
# at build time (see ``[tool.hatch.version]`` in pyproject.toml), and
# editable installs surface the live value at runtime via
# ``claude_bridge.__version__``.
__version__ = "0.1.0"

from .dispatcher import (
    STOP_SENTINEL,
    DispatchResult,
    Dispatcher,
    Job,
    Schedule,
)

__all__ = [
    "Dispatcher",
    "DispatchResult",
    "Job",
    "STOP_SENTINEL",
    "Schedule",
    "__version__",
]
