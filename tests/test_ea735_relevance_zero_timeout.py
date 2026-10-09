"""Pure offline selected scorer method with inert slot and HTTP collaborators."""
from pathlib import Path
from types import MethodType, SimpleNamespace
import json
import math
import re
import textwrap
import unittest


def scorer():
    path = Path(__file__).parents[1] / "eimemory/retrieval/relevance.py"
    lines = path.read_text().splitlines(keepends=True)
    names = {"score", "validate_scores"}
    chunks = []
    for index, line in enumerate(lines):
        match = re.match(r"( *)def (\w+)\(", line)
        if not match or match[2] not in names:
            continue
        indent = len(match[1])
        end = index + 1
        while end < len(lines):
            if re.match(r" *(?:def |class |@)", lines[end]) and len(lines[end]) - len(lines[end].lstrip()) <= indent:
                break
            end += 1
        chunks.append(textwrap.dedent("".join(lines[index:end])))

    class Unavailable(Exception):
        pass

    calls = []

    class Slot:
        available = True
        acquisitions = 0

        def acquire(self, blocking=False):
            self.acquisitions += 1
            acquired = self.available
            self.available = False
            return acquired

        def release(self):
            self.available = True

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def read(self, _):
            return b'[{"index":0,"score":0.5}]'

    def open_response(request, timeout):
        calls.append(timeout)
        return Response()

    namespace = {"json": json, "math": math, "perf_counter": lambda: 10.0,
                 "RelevanceUnavailable": Unavailable,
                 "Request": lambda url, **kwargs: SimpleNamespace(url=url, **kwargs)}
    exec(compile("from __future__ import annotations\n" + "\n".join(chunks), str(path), "exec"), namespace)
    assert names <= namespace.keys()
    fixture = SimpleNamespace(config=SimpleNamespace(max_candidates=5, timeout_seconds=1.0,
                              enabled=False, endpoint="https://inert.invalid", api_key="fake"),
                              _slot=Slot(), _opener=SimpleNamespace(open=open_response))
    fixture.score = MethodType(namespace["score"], fixture)
    return fixture, calls, Unavailable


class TimeoutTest(unittest.TestCase):
    def test_explicit_zero_budget_rejects_without_request_and_releases_slot(self):
        client, calls, unavailable = scorer()
        with self.assertRaisesRegex(unavailable, "reranker_deadline_exceeded"):
            client.score("query", ["text"], timeout_seconds=0.0)
        self.assertEqual(calls, [])
        self.assertTrue(client._slot.available)

    def test_none_keeps_configured_default(self):
        client, calls, _ = scorer()
        self.assertEqual(client.score("query", ["text"], timeout_seconds=None), [0.5])
        self.assertEqual(calls, [1.0])

    def test_positive_caller_budget_is_capped(self):
        for supplied, expected in ((0.4, 0.4), (2.0, 1.0)):
            with self.subTest(supplied=supplied):
                client, calls, _ = scorer()
                self.assertEqual(client.score("query", ["text"], timeout_seconds=supplied), [0.5])
                self.assertEqual(calls, [expected])
                self.assertTrue(client._slot.available)

    def test_expired_deadline_remains_rejected(self):
        client, calls, unavailable = scorer()
        with self.assertRaisesRegex(unavailable, "reranker_deadline_exceeded"):
            client.score("query", ["text"], deadline_at=9.0)
        self.assertEqual(calls, [])
        self.assertTrue(client._slot.available)

    def test_empty_input_does_not_acquire_or_request(self):
        client, calls, _ = scorer()
        self.assertEqual(client.score("query", [], timeout_seconds=0.0), [])
        self.assertEqual(client._slot.acquisitions, 0)
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
