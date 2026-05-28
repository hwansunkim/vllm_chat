"""ABM database package — re-exports SimDB to preserve `from ABM.db import SimDB`."""

from .base import SimDB

__all__ = ["SimDB"]
