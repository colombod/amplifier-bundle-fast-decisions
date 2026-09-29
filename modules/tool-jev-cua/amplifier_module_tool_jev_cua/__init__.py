"""Thin module shim for observed-control Jev decisions."""
__amplifier_module_type__ = "tool"
from amplifier_fast_decisions.jev_cua import mount

__all__ = ["mount"]
