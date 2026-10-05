"""Stdlib-only AST checks; no project imports, runtime, store or merge calls."""
import ast
from pathlib import Path
import re
import unittest


def load_fingerprint():
    source = Path(__file__).resolve().parents[1] / 'eimemory/recall/dedupe.py'
    tree = ast.parse(source.read_text(encoding='utf-8'))
    functions = {'_preference_fingerprint', '_preference_atoms',
                 '_is_supported_wrapper_action', '_starts_supported_action'}
    constants = {'_SUPPORTED_WRAPPER_ACTIONS', '_CLAUSE_SPLIT', '_STEP_RE',
                 '_CONDITIONAL_MARKERS', '_CONDITIONAL_RE', '_RECEIVED_DEFAULT_RE'}
    selected = [node for node in tree.body
                if (isinstance(node, ast.FunctionDef) and node.name in functions)
                or (isinstance(node, ast.Assign) and any(
                    isinstance(target, ast.Name) and target.id in constants
                    for target in node.targets))]
    namespace = {'re': re}
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(source), 'exec'), namespace)
    return namespace['_preference_fingerprint']


class WrapperIntegrityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fingerprint = staticmethod(load_fingerprint())

    def assert_separate(self, left, right):
        a, b = self.fingerprint(left), self.fingerprint(right)
        self.assertTrue(a is None or b is None or a != b, (left, right, a))

    def test_literal_noun_final_shi_fails_closed(self):
        for noun in ('工时', '耗时', '计时'):
            with self.subTest(noun=noun):
                left = '只有明确要求检查' + noun + '才提供摘要'
                right = '只有明确要求检查' + noun[:-1] + '才提供摘要'
                self.assertIsNone(self.fingerprint(left))
                self.assert_separate(left, right)

    def test_default_output_action_is_not_erased(self):
        self.assertIsNone(self.fingerprint('默认提供检查结果'))
        self.assert_separate('默认提供检查结果', '默认检查结果')

    def test_prefixed_supported_words_are_not_complete_actions(self):
        for tail in ('检查结果', '评估结果', '摘要长度', '摘要时', '评估时'):
            for prefix in ('先给', '提供', '给'):
                with self.subTest(tail=tail, prefix=prefix):
                    self.assertIsNone(self.fingerprint('默认' + prefix + tail))
                    self.assert_separate('默认' + prefix + tail, '默认' + tail)

    def test_sequence_only_prefix_preserves_object_bearing_actions(self):
        for action in ('检查电源', '复核方案', '查看日志', '核对地址', '评估方案',
                       '评价方案', '摘要文章', '总结报告', '概括全文', '读取日志',
                       '断开电源', '取出残纸', '关闭电源', '卸下镜头', '更换电池', '抽文案'):
            with self.subTest(action=action):
                expected = self.fingerprint('主题链接默认' + action)
                self.assertIsNotNone(expected)
                self.assertEqual(expected, self.fingerprint('主题链接默认先' + action))
                self.assertEqual(expected, self.fingerprint('默认收到主题链接后先' + action))
        self.assert_separate('默认先进模式', '默认进模式')

    def test_conditional_output_action_is_not_erased(self):
        for tail in ('检查结果', '评估结果', '摘要长度'):
            with self.subTest(tail=tail):
                self.assertIsNone(self.fingerprint('明确要求摘要才提供' + tail))
                self.assert_separate('明确要求摘要才提供' + tail,
                                     '明确要求摘要才' + tail)

    def test_received_wrapper_requires_complete_action(self):
        for tail in ('检查结果', '评估结果', '摘要长度'):
            with self.subTest(tail=tail):
                self.assertIsNone(self.fingerprint('默认收到主题链接后先给' + tail))

    def test_supported_conditional_wrappers(self):
        for action in ('检查', '复核', '查看', '核对', '评估', '评价', '摘要', '总结',
                       '概括', '读取', '断开', '取出', '关闭', '卸下', '更换', '抽'):
            expected = self.fingerprint('明确要求' + action + '才' + action)
            self.assertIsNotNone(expected)
            for when in ('', '时'):
                for only in ('', '只有'):
                    for output in ('', '提供', '给'):
                        with self.subTest(action=action, when=when, only=only, output=output):
                            self.assertEqual(expected, self.fingerprint(
                                only + '明确要求' + action + when + '才' + output + action))

    def test_supported_default_wrappers(self):
        for action in ('检查', '复核', '查看', '核对', '评估', '评价', '摘要', '总结',
                       '概括', '读取', '断开', '取出', '关闭', '卸下', '更换', '抽'):
            expected = self.fingerprint('主题链接默认' + action)
            self.assertIsNotNone(expected)
            for prefix in ('先给', '先', '提供', '给'):
                self.assertEqual(expected, self.fingerprint('主题链接默认' + prefix + action))
                self.assertEqual(expected, self.fingerprint('默认收到主题链接后' + prefix + action))

    def test_established_positive_controls(self):
        pairs = [
            ('主题链接默认先评估；只有明确要求摘要才提供摘要。',
             '默认收到主题链接后先给评估，明确要求摘要时才摘要。'),
            ('先检查电源，再读取日志', '先检查电源，然后读取日志'),
        ]
        for left, right in pairs:
            with self.subTest(left=left):
                self.assertIsNotNone(self.fingerprint(left))
                self.assertEqual(self.fingerprint(left), self.fingerprint(right))

    def test_established_negative_controls(self):
        pairs = [
            ('默认不检查电源', '默认检查电源'),
            ('默认检查地址', '默认检查网址'),
            ('默认选择 Variant_A', '默认选择 variant_a'),
            ('先检查电源，再读取日志', '先读取日志，再检查电源'),
            ('默认 threshold < 5', '默认 threshold > 5'),
            ('只有明确要求检查纸路才检查电源', '只有明确要求检查电源才检查纸路'),
            ('主题链接默认先评估', '主题文章链接默认先评估'),
            ('默认提供摘要', '默认提供评估'),
            ('默认提供摘要', '默认提供总结'),
        ]
        for left, right in pairs:
            with self.subTest(left=left):
                self.assert_separate(left, right)

    def test_established_ambiguous_controls(self):
        for text in ('Allow read deny write', '默认提供摘要；需要保留原件',
                     '先检查电源，再读取日志；必须保留原件', '先检查电源再读取日志',
                     '先检查最后期限，再读取日志', '收到链接后检查电源'):
            with self.subTest(text=text):
                self.assertIsNone(self.fingerprint(text))


if __name__ == '__main__':
    unittest.main(verbosity=2)
