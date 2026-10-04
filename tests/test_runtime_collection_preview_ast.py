"""Finite source-AST contracts; run directly with Python, without project imports.

The interpreter below handles only the syntax of collect_external_sources.
Every service/helper call goes to an inert in-memory fixture. It never imports,
compiles, instantiates, or calls Runtime, a collector, a store, or a callback.
Pass --baseline to compare persist=True traces with the accepted parent source.
"""
from __future__ import annotations

import argparse
import ast
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from pathlib import Path
from types import SimpleNamespace
import unittest


SOURCE = Path(__file__).resolve().parents[1] / "eimemory/api/runtime.py"
BASELINE = None
STAMP = "2030-01-01T00:00:00+00:00"
SCOPE = {"tenant_id": "tenant-a", "agent_id": "agent-a", "workspace_id": "ws-a", "user_id": "user-a"}


def method(path):
    tree = ast.parse(Path(path).read_text())
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Runtime")
    return next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == "collect_external_sources")


class Returned(Exception):
    def __init__(self, value):
        self.value = value


class Broke(Exception):
    pass


class FixtureError(Exception):
    pass


class Interpreter:
    """An explicit syntax allowlist, not Python eval/exec or a module loader."""

    def __init__(self, env, allowed_calls):
        self.env = env
        self.allowed_calls = allowed_calls

    def expression(self, node, env):
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            return env[node.id]
        if isinstance(node, ast.Attribute):
            return getattr(self.expression(node.value, env), node.attr)
        if isinstance(node, ast.List):
            return [self.expression(item, env) for item in node.elts]
        if isinstance(node, ast.Dict):
            out = {}
            for key, value in zip(node.keys, node.values):
                if key is None:
                    out.update(self.expression(value, env))
                else:
                    out[self.expression(key, env)] = self.expression(value, env)
            return out
        if isinstance(node, ast.Slice):
            return slice(*(self.expression(item, env) if item is not None else None
                           for item in (node.lower, node.upper, node.step)))
        if isinstance(node, ast.Subscript):
            return self.expression(node.value, env)[self.expression(node.slice, env)]
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            return not self.expression(node.operand, env)
        if isinstance(node, ast.BinOp):
            left, right = self.expression(node.left, env), self.expression(node.right, env)
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
        if isinstance(node, ast.BoolOp):
            for value in node.values:
                result = self.expression(value, env)
                if isinstance(node.op, ast.And) and not result:
                    return result
                if isinstance(node.op, ast.Or) and result:
                    return result
            return result
        if isinstance(node, ast.Compare):
            left = self.expression(node.left, env)
            for operator, comparator in zip(node.ops, node.comparators):
                right = self.expression(comparator, env)
                if isinstance(operator, ast.Is):
                    result = left is right
                elif isinstance(operator, ast.IsNot):
                    result = left is not right
                elif isinstance(operator, ast.Eq):
                    result = left == right
                elif isinstance(operator, ast.NotEq):
                    result = left != right
                elif isinstance(operator, ast.GtE):
                    result = left >= right
                else:
                    raise AssertionError(f"unsupported comparison: {ast.dump(operator)}")
                if not result:
                    return False
                left = right
            return True
        if isinstance(node, ast.IfExp):
            return self.expression(node.body if self.expression(node.test, env) else node.orelse, env)
        if isinstance(node, (ast.ListComp, ast.GeneratorExp)):
            assert len(node.generators) == 1 and not node.generators[0].is_async
            generator = node.generators[0]

            def values():
                for value in self.expression(generator.iter, env):
                    local = dict(env)
                    self.assign(generator.target, value, local)
                    if all(self.expression(condition, local) for condition in generator.ifs):
                        yield self.expression(node.elt, local)

            return list(values()) if isinstance(node, ast.ListComp) else values()
        if isinstance(node, ast.Call):
            function = self.expression(node.func, env)
            owner = getattr(function, "__self__", None)
            name = getattr(function, "__name__", "")
            safe_method = ((isinstance(owner, list) and name == "append")
                           or (isinstance(owner, dict) and name == "get")
                           or (isinstance(owner, str) and name == "lower"))
            assert function in self.allowed_calls or safe_method, ast.unparse(node.func)
            assert all(keyword.arg is not None for keyword in node.keywords)
            return function(*[self.expression(item, env) for item in node.args],
                            **{item.arg: self.expression(item.value, env) for item in node.keywords})
        raise AssertionError(f"unsupported expression: {ast.dump(node)}")

    def assign(self, target, value, env):
        if isinstance(target, ast.Name):
            env[target.id] = value
        elif isinstance(target, ast.Subscript):
            self.expression(target.value, env)[self.expression(target.slice, env)] = value
        else:
            raise AssertionError(f"unsupported assignment: {ast.dump(target)}")

    def body(self, statements, env):
        for node in statements:
            if isinstance(node, ast.ImportFrom):
                # Bindings are supplied by fixtures. No import is performed.
                assert (node.module, tuple(item.name for item in node.names)) in {
                    ("dataclasses", ("asdict",)),
                    ("eimemory.intake.connectors", ("FetchResult", "collect_from_source_entry")),
                }
            elif isinstance(node, ast.Assign):
                value = self.expression(node.value, env)
                for target in node.targets:
                    self.assign(target, value, env)
            elif isinstance(node, ast.AnnAssign):
                self.assign(node.target, self.expression(node.value, env), env)
            elif isinstance(node, ast.AugAssign) and isinstance(node.op, ast.Add):
                self.assign(node.target, self.expression(node.target, env) + self.expression(node.value, env), env)
            elif isinstance(node, ast.Expr):
                self.expression(node.value, env)
            elif isinstance(node, ast.If):
                self.body(node.body if self.expression(node.test, env) else node.orelse, env)
            elif isinstance(node, ast.For):
                for value in self.expression(node.iter, env):
                    self.assign(node.target, value, env)
                    try:
                        self.body(node.body, env)
                    except Broke:
                        break
                    except Continued:
                        continue
                assert not node.orelse
            elif isinstance(node, ast.Break):
                raise Broke()
            elif isinstance(node, ast.Continue):
                raise Continued()
            elif isinstance(node, ast.Return):
                raise Returned(self.expression(node.value, env))
            else:
                raise AssertionError(f"unsupported statement: {ast.dump(node)}")

    def run(self, node):
        try:
            self.body(node.body, self.env)
        except Returned as done:
            return done.value
        raise AssertionError("missing return")


class Continued(Exception):
    pass


@dataclass
class Item:
    record_id: str
    status: str = "candidate"


@dataclass
class FetchResult:
    ok: bool
    items: list = field(default_factory=list)
    error: str = ""
    metadata: dict = field(default_factory=dict)


def source(source_id="source-a", kind="url", max_items=10, enabled=True):
    return SimpleNamespace(source_id=source_id, source_kind=kind, max_items=max_items, enabled=enabled)


def inert_fetcher(_url):
    raise AssertionError("the caller's callback must never execute in this harness")


def inert_default_fetcher(_url):
    raise AssertionError("the network helper must never execute in this harness")


class World:
    """Fixtures and in-memory observation traces, never project services."""

    def __init__(self, sources=None, results=None, existing=(), enrich_error=False):
        self.entries = deepcopy([source()] if sources is None else sources)
        self.results = deepcopy(results if results is not None else {"source-a": FetchResult(True, [Item("one")])})
        self.existing = set(existing)
        self.enrich_error = enrich_error
        self.trace = []
        self.state = {item.source_id: {"last_scanned_at": "old", "last_scan": {"status": "old"}}
                      for item in self.entries}
        self.initial_state = deepcopy(self.state)
        self.sources = SimpleNamespace(list_sources=self.list_sources, mark_source_scanned=self.mark)
        self.store = SimpleNamespace(get_by_id=self.get_by_id, append=self.append)

    def list_sources(self, *, enabled, source_kind):
        self.trace.append(("list", enabled, source_kind))
        return [item for item in self.entries if item.enabled is enabled
                and (not source_kind or item.source_kind == source_kind)]

    def collect(self, selected, *, fetch_text):
        self.trace.append(("collect", selected.source_id,
                           "injected" if fetch_text is inert_fetcher else "default" if fetch_text is inert_default_fetcher else "none"))
        result = self.results[selected.source_id]
        if isinstance(result, Exception):
            raise result
        return deepcopy(result)

    def enrich(self, result, *, fetch_text):
        self.trace.append(("enrich",))
        if self.enrich_error:
            raise FixtureError("enrich")
        return result

    def record(self, item, **kwargs):
        self.trace.append(("record", item.record_id))
        return SimpleNamespace(record_id=item.record_id, status=item.status, scope=kwargs["scope"])

    def get_by_id(self, record_id, *, scope):
        self.trace.append(("lookup", record_id, deepcopy(scope)))
        return object() if record_id in self.existing else None

    def append(self, record):
        self.trace.append(("append", record.record_id, deepcopy(record.scope)))
        self.existing.add(record.record_id)

    def mark(self, source_id, **kwargs):
        self.trace.append(("mark", source_id, deepcopy(kwargs)))
        self.state[source_id] = {"last_scanned_at": kwargs["scanned_at"], "last_scan": deepcopy(kwargs)}

    def run(self, node, **options):
        def scope_from_dict(value):
            return deepcopy(value or {})

        def max_items(item):
            return item.max_items

        def now():
            return STAMP

        env = {"self": self, "source_kind": None, "limit": None, "fetch_text": None,
               "fetch": False, "persist": False, "scope": SCOPE,
               "FetchResult": FetchResult, "collect_from_source_entry": self.collect,
               "_enrich_rss_result_with_fulltext": self.enrich, "_collected_item_record": self.record,
               "_source_max_items": max_items, "_default_fetch_text": inert_default_fetcher,
               "ScopeRef": SimpleNamespace(from_dict=scope_from_dict), "now_iso": now,
               "asdict": asdict, "max": max, "int": int, "str": str, "len": len,
               "dict": dict, "list": list, "all": all, "bool": bool}
        env.update(options)
        allowed = {FetchResult, self.collect, self.enrich, self.record, max_items, scope_from_dict,
                   now, asdict, max, int, str, len, dict, list, all, bool,
                   self.list_sources, self.mark, self.get_by_id, self.append}
        return Interpreter(env, allowed).run(node)


class PreviewContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.node = method(SOURCE)
        cls.baseline = method(BASELINE) if BASELINE is not None else None

    def assert_preview_clean(self, world):
        self.assertEqual(world.state, world.initial_state)
        self.assertFalse([event for event in world.trace if event[0] in {"mark", "append", "lookup", "record"}], world.trace)

    def test_persistence_calls_have_persist_true_ancestor(self):
        found = set()

        def inspect(node, guards):
            if isinstance(node, ast.Call) and ast.unparse(node.func) in {"self.store.append", "self.sources.mark_source_scanned"}:
                found.add(ast.unparse(node.func))
                self.assertIn("persist", guards, f"unguarded write at {node.lineno}")
            if isinstance(node, ast.If):
                for child in node.body:
                    inspect(child, guards + [ast.unparse(node.test)])
                for child in node.orelse:
                    inspect(child, guards)
                return
            for child in ast.iter_child_nodes(node):
                inspect(child, guards)

        inspect(self.node, [])
        self.assertEqual(found, {"self.store.append", "self.sources.mark_source_scanned"})

    def test_false_fetch_result_branch_matrix(self):
        for fetch in (False, True):
            for injected in (False, True):
                for kind in ("url", "rss"):
                    for ok, items in ((True, []), (True, [Item("one")]), (False, []), (False, [Item("one")])):
                        with self.subTest(fetch=fetch, injected=injected, kind=kind, ok=ok, items=bool(items)):
                            world = World([source(kind=kind)], {"source-a": FetchResult(ok, items, "" if ok else "synthetic_error")})
                            result = world.run(self.node, fetch=fetch, fetch_text=inert_fetcher if injected else None)
                            self.assert_preview_clean(world)
                            self.assertEqual(result["ok"], ok)
                            self.assertEqual(result["error_count"], 0 if ok else 1)
                            self.assertEqual(result["item_count"], len(items))
                            self.assertEqual(result["written_count"], 0)
                            self.assertEqual(result["persisted_record_ids"], [])
                            self.assertEqual([e[2] for e in world.trace if e[0] == "collect"],
                                             ["injected" if injected else "default" if fetch else "none"])
                            self.assertEqual(sum(e[0] == "enrich" for e in world.trace), int(fetch and kind == "rss"))

    def test_false_limits_truncation_and_source_selection(self):
        for limit in (None, -1, 0, 1, 2, 4):
            for cap in (0, 1, 10):
                with self.subTest(limit=limit, cap=cap):
                    world = World([source(max_items=cap), source("source-b")],
                                  {"source-a": FetchResult(True, [Item("one"), Item("two"), Item("three")]),
                                   "source-b": FetchResult(False, [Item("four")], "synthetic_error")})
                    result = world.run(self.node, limit=limit)
                    self.assert_preview_clean(world)
                    if limit in (-1, 0):
                        self.assertEqual(result["results"], [])
                    else:
                        first_count = min(3, cap, limit if limit is not None else 3)
                        self.assertEqual(len(result["results"][0]["items"]), first_count)
                        self.assertEqual(result["results"][0]["metadata"].get("truncated", False), first_count != 3)
                        self.assertLessEqual(result["item_count"], limit if limit is not None else 4)

    def test_false_empty_disabled_and_kind_filtered_sources(self):
        for entries, selected_kind in (([], None), ([source(enabled=False)], None), ([source(kind="rss")], "url")):
            with self.subTest(entries=entries, kind=selected_kind):
                world = World(entries)
                result = world.run(self.node, source_kind=selected_kind)
                self.assert_preview_clean(world)
                self.assertEqual(result["results"], [])
                self.assertEqual(result["source_count"], 0)
                self.assertTrue(result["ok"])

    def test_false_later_collect_exception_has_no_prior_or_later_write(self):
        world = World([source(), source("source-b")],
                      {"source-a": FetchResult(True, [Item("one")]), "source-b": FixtureError("collect")})
        with self.assertRaisesRegex(FixtureError, "collect"):
            world.run(self.node)
        self.assert_preview_clean(world)
        self.assertEqual([e[1] for e in world.trace if e[0] == "collect"], ["source-a", "source-b"])

    def test_false_enrichment_exception_has_no_write(self):
        world = World([source(kind="rss")], enrich_error=True)
        with self.assertRaisesRegex(FixtureError, "enrich"):
            world.run(self.node, fetch=True, fetch_text=inert_fetcher)
        self.assert_preview_clean(world)

    def test_false_invalid_limit_and_serialization_exception_have_no_write(self):
        invalid_limit = World()
        with self.assertRaises(ValueError):
            invalid_limit.run(self.node, limit="invalid")
        self.assert_preview_clean(invalid_limit)
        malformed_result = World(results={"source-a": SimpleNamespace(ok=True, items=[], error="", metadata={})})
        with self.assertRaises(TypeError):
            malformed_result.run(self.node)
        self.assert_preview_clean(malformed_result)

    def test_true_counts_errors_idempotency_and_scan_metadata(self):
        world = World([source(), source("source-b")],
                      {"source-a": FetchResult(True, [Item("existing"), Item("quarantined", "quarantined"), Item("rejected", "rejected")]),
                       "source-b": FetchResult(False, [], "synthetic_error")}, existing={"existing"})
        result = world.run(self.node, persist=True)
        self.assertEqual((result["written_count"], result["skipped_existing_count"], result["quarantined_count"], result["rejected_count"]), (2, 1, 1, 1))
        self.assertEqual(result["persisted_record_ids"], ["quarantined", "rejected"])
        self.assertEqual(result["failed_sources"], ["source-b"])
        self.assertEqual([event[1] for event in world.trace if event[0] == "mark"], ["source-a", "source-b"])
        self.assertEqual(world.state["source-a"]["last_scan"]["written_count"], 2)
        self.assertEqual(world.state["source-a"]["last_scan"]["skipped_existing_count"], 1)
        self.assertEqual(world.state["source-b"]["last_scan"]["status"], "error")
        self.assertEqual(world.state["source-b"]["last_scan"]["item_count"], 0)

    def test_true_traces_match_accepted_baseline(self):
        if self.baseline is None:
            self.skipTest("--baseline is required for the before/after comparison")
        for fetch in (False, True):
            for limit in (None, 0, 1, 2, 4):
                for second_error in (False, True):
                    with self.subTest(fetch=fetch, limit=limit, second_error=second_error):
                        options = {"sources": [source(kind="rss", max_items=2), source("source-b")],
                                   "results": {"source-a": FetchResult(True, [Item("existing"), Item("new", "quarantined"), Item("truncated")]),
                                               "source-b": FetchResult(not second_error, [Item("second", "rejected")], "error" if second_error else "")},
                                   "existing": {"existing"}}
                        before, after = World(**options), World(**options)
                        kwargs = {"persist": True, "fetch": fetch, "fetch_text": inert_fetcher, "limit": limit}
                        self.assertEqual(before.run(self.baseline, **kwargs), after.run(self.node, **kwargs))
                        self.assertEqual(before.trace, after.trace)
                        self.assertEqual(before.state, after.state)

    def test_true_partial_write_exception_trace_is_unchanged(self):
        if self.baseline is None:
            self.skipTest("--baseline is required for the before/after comparison")
        options = {"sources": [source(), source("source-b")],
                   "results": {"source-a": FetchResult(True, [Item("one")]), "source-b": FixtureError("collect")}}
        before, after = World(**options), World(**options)
        for world, node in ((before, self.baseline), (after, self.node)):
            with self.assertRaisesRegex(FixtureError, "collect"):
                world.run(node, persist=True)
        self.assertEqual(before.trace, after.trace)
        self.assertEqual(before.state, after.state)
        self.assertEqual([event[0] for event in after.trace if event[0] in {"append", "mark"}], ["append", "mark"])

    def test_other_source_AST_is_unchanged(self):
        if BASELINE is None:
            self.skipTest("--baseline is required for preservation comparison")
        trees = [ast.parse(Path(path).read_text()) for path in (BASELINE, SOURCE)]
        for tree in trees:
            cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Runtime")
            cls.body = [node for node in cls.body if not (isinstance(node, ast.FunctionDef) and node.name == "collect_external_sources")]
        self.assertEqual(ast.dump(trees[0], include_attributes=False), ast.dump(trees[1], include_attributes=False))
        self.assertIn('report["self_model_persistence"]', Path(SOURCE).read_text())


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=SOURCE)
    parser.add_argument("--baseline", type=Path)
    arguments, remaining = parser.parse_known_args()
    SOURCE, BASELINE = arguments.source, arguments.baseline
    unittest.main(argv=[__file__, *remaining])
