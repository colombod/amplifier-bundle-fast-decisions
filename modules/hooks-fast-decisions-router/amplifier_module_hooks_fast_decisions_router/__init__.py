"""Thin Amplifier module entrypoint. Shared code is provided by the root bundle package."""
__amplifier_module_type__ = "hook"
from amplifier_fast_decisions.registry import mount
__all__ = ["mount"]
