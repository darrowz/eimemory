"""Only stdlib, AST-extracted generator functions, synthetic text, inert Path mock.
Never imports or executes generated bridge/helper, and never invokes real project IO.
"""
import argparse, ast, contextlib, difflib, hashlib, io, json, pathlib, re, sys
import unittest
SOURCE = pathlib.Path(__file__).resolve().parents[1] / 'deploy/luna_bridge/make_luna_patch.py'
IMPORT = 'from agent.auxiliary_client import resolve_provider_client\n'
SETUP = "client = resolve_provider_client('openai-codex', model='gpt-5.6-luna')\n"
API = "response = client.chat.completions.create(reasoning_effort='low')\n"
BASE = IMPORT + SETUP + API

def load(text):
    tree = ast.parse(text)
    selected = [n for n in tree.body if isinstance(n, (ast.ClassDef, ast.FunctionDef))]
    assert [n.name for n in selected] == ['Unsupported', 'dotted', '_literal_values', '_assert_literal_fidelity', 'instrument', 'main']
    space = dict(ast=ast, argparse=argparse, difflib=difflib, sha256=hashlib.sha256, json=json, re=re, sys=sys, __file__='inert_generator.py')
    exec(compile(ast.Module(body=selected, type_ignores=[]), '<extracted-generator-only>', 'exec'), space)
    return space

class FakePath:
    files = {}
    newlines = []
    native_newline = '\r\n'
    def __init__(self, name): self.name = str(name)
    def __truediv__(self, name): return FakePath(self.name + '/' + name)
    def read_bytes(self): assert self.name == 'source'; return BASE.encode()
    def with_name(self, name): assert name == 'luna_observability.py'; return FakePath('helper')
    def read_text(self): assert self.name == 'helper'; return '# inert synthetic helper\n'
    def mkdir(self, **kwargs): assert kwargs == dict(mode=0o700, parents=False, exist_ok=False)
    def chmod(self, mode): assert mode == 0o600
    def open(self, mode, encoding, newline=None):
        assert mode == 'x' and encoding == 'utf-8'
        self.newlines.append(newline)
        parent = self
        class Sink:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def write(self, text):
                # Model Windows TextIOWrapper translation, no filesystem effects.
                sep = parent.native_newline if newline is None else newline
                buf = io.BytesIO()
                stream = io.TextIOWrapper(buf, encoding='utf-8', newline=sep)
                stream.write(text); stream.flush()
                parent.files[parent.name] = buf.getvalue()
                stream.detach()
                return len(text)
        return Sink()


class LunaPatchLocalRegressions(unittest.TestCase):
    """Compile synthetic output for syntax only; never execute bridge/helper code."""

    @classmethod
    def setUpClass(cls):
        cls.generator = load(SOURCE.read_text(encoding='utf-8'))

    def accepted(self, source):
        ast.parse(source)
        output = self.generator['instrument'](source)
        ast.parse(output)
        return output

    def refused(self, source, reason):
        ast.parse(source)
        with self.assertRaisesRegex(self.generator['Unsupported'], '^' + reason + '$'):
            self.generator['instrument'](source)

    def test_ordinary_and_same_statement_list(self):
        self.accepted(BASE)
        self.accepted(IMPORT + 'if flag:\n    ' + SETUP + '    ' + API)

    def test_semicolons_in_trailing_comments_are_allowed(self):
        self.accepted(IMPORT.rstrip() + ' # note; details\n' + SETUP + API)

    def test_unicode_target_with_semicolon_comment_is_allowed(self):
        self.accepted(IMPORT + SETUP.rstrip()[:-1] + ", note='中文') # note; details\n" + API)

    def test_ascii_following_statement_is_refused(self):
        self.refused(IMPORT + SETUP.rstrip() + '; x = 1\n' + API,
                     'semicolon_statement_unsupported')

    def test_unicode_following_statement_is_refused(self):
        self.refused(IMPORT + SETUP.rstrip()[:-1] + ", note='中文'); x = 1\n" + API,
                     'semicolon_statement_unsupported')

    def test_if_body_and_else_are_different_blocks(self):
        self.refused(IMPORT + 'if flag:\n    ' + SETUP + 'else:\n    ' + API,
                     'setup_and_api_must_be_ordered_in_same_block')

    def test_try_body_and_else_are_different_blocks(self):
        self.refused(IMPORT + 'try:\n    ' + SETUP + 'except Exception:\n    pass\nelse:\n    ' + API,
                     'setup_and_api_must_be_ordered_in_same_block')

    def test_comment_characters_in_string_are_preserved(self):
        self.accepted(IMPORT + SETUP.rstrip()[:-1] + ", note='#;中文')\n" + API)

    def test_inert_windows_output_matches_manifest(self):
        FakePath.files = {}
        FakePath.newlines = []
        # Fresh extraction avoids modifying the namespace shared by other cases.
        space = load(SOURCE.read_text(encoding='utf-8'))
        space['Path'] = FakePath
        with contextlib.redirect_stdout(io.StringIO()):
            code = space['main']([
                '--source', 'source', '--expected-sha256', hashlib.sha256(BASE.encode()).hexdigest(),
                '--output-dir', 'output'])
        self.assertEqual(code, 0)
        manifest = json.loads(FakePath.files['output/manifest.json'])
        for filename, key in [('luna_review_command.py', 'candidate_sha256'),
                              ('luna_observability.py', 'helper_sha256')]:
            with self.subTest(filename=filename):
                self.assertEqual(hashlib.sha256(FakePath.files['output/' + filename]).hexdigest(),
                                 manifest[key])
        self.assertEqual(FakePath.newlines, ['\n'] * 4)


if __name__ == '__main__':
    unittest.main()
