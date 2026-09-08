"""Shared request identity; preserve the established proactive decision digest."""
from hashlib import sha256

QUERY_IDENTITY_SCHEMA = "proactive-query-identity.v2"


def query_text_digest(text: str) -> str:
    return sha256(text.encode("utf-8", errors="replace")).hexdigest()


def effective_query_digest(task_type: str, query: str) -> str:
    return query_text_digest(f"{task_type}\x1f{query}")
