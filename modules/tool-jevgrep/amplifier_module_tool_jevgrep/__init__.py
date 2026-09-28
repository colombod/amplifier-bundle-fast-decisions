"""Thin module shim for the optional Jevgrep retrieval tool."""
__amplifier_module_type__ = "tool"
from amplifier_fast_decisions.jevgrep import mount

__all__ = ["mount"]
