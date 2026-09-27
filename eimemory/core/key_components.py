"""Validate components of the persisted unit-separator identity format.

Existing safe keys stay byte-for-byte identical. Invalid legacy namespaces
must be inspected offline rather than silently remapped onto another owner.
"""
from __future__ import annotations


def validate_key_component(value: str, *, name: str = "identity") -> str:
    if not isinstance(value, str):
        raise ValueError(f"identity_component_must_be_string:{name}")
    if "\x1f" in value:
        raise ValueError(f"identity_component_contains_reserved_separator:{name}")
    return value
