"""Storage facade (lazy DB). See storage/database.py."""
from .database import PLUGIN_ID, connect, set_override

__all__ = ["PLUGIN_ID", "connect", "set_override"]
