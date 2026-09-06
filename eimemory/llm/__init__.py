"""Pluggable optional LLM clients for EIMemory enhancement paths."""

from eimemory.llm.command_client import CommandLLMClient, LLMResult, llm_client_from_env
from eimemory.llm.hermes_adapter import hermes_llm_argv, resolve_l1_llm_client

__all__ = [
    "CommandLLMClient",
    "LLMResult",
    "llm_client_from_env",
    "hermes_llm_argv",
    "resolve_l1_llm_client",
]
