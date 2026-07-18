"""Pluggable optional LLM clients for EIMemory enhancement paths."""

from eimemory.llm.command_client import CommandLLMClient, LLMResult, llm_client_from_env

__all__ = ["CommandLLMClient", "LLMResult", "llm_client_from_env"]
