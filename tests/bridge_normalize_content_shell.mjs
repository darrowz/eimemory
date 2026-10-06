// Offline regression test. No target-module import or project dependencies.
// Default source assumes this file is installed at repository tests/.
import { readFileSync } from 'node:fs';
import { runInNewContext } from 'node:vm';
import { strict as assert } from 'node:assert';
import { resolve } from 'node:path';

const options = new Map();
for (let i = 2; i < process.argv.length; i += 2) {
  const flag = process.argv[i];
  assert.ok(flag === '--source' || flag === '--baseline', 'Unknown flag: ' + flag);
  assert.ok(process.argv[i + 1] && !process.argv[i + 1].startsWith('--'), 'Missing file path for ' + flag);
  assert.ok(!options.has(flag), 'Repeated flag: ' + flag);
  options.set(flag, resolve(process.argv[i + 1]));
}
const sourcePath = options.get('--source') ?? new URL('../integrations/openclaw/eimemory-bridge/index.js', import.meta.url);
const extract = (path) => {
  const lines = readFileSync(path, 'utf8').split('\n');
  // Deliberately pinned reviewed windows: line drift fails loudly.
  const list = lines.slice(1145, 1163);
  const content = lines.slice(1459, 1489);
  assert.equal(list[0], 'function normalizeStringList(value) {', 'normalizeStringList window moved');
  assert.equal(content[0], 'function normalizeContent(content) {', 'normalizeContent window moved');
  assert.equal(list.filter((s) => s.trim()).at(-1), '}', 'Unexpected list window end');
  assert.equal(content.filter((s) => s.trim()).at(-1), '}', 'Unexpected content window end');
  return list.join('\n') + '\n' + content.join('\n');
};
const sources = { candidate: extract(sourcePath) };
if (options.has('--baseline')) sources.baseline = extract(options.get('--baseline'));

const cases = [
  ['toJSON_undefined', '({toJSON(){return undefined;}})', ['undefined', null], ['error', 'TypeError'], ['value', ''], ['value', []]],
  ['toJSON_function', '({toJSON(){return function(){};}})', ['undefined', null], ['error', 'TypeError'], ['value', ''], ['value', []]],
  ['toJSON_symbol', '({toJSON(){return Symbol("x");}})', ['undefined', null], ['error', 'TypeError'], ['value', ''], ['value', []]],
  ['toJSON_null', '({toJSON(){return null;}})', ['value', 'null'], ['value', ['null']]],
  ['toJSON_number', '({toJSON(){return 7;}})', ['value', '7'], ['value', ['7']]],
  ['toJSON_string', '({toJSON(){return "hello";}})', ['value', '"hello"'], ['value', ['"hello"']]],
  ['toJSON_object', '({toJSON(){return {a:1};}})', ['value', '{"a":1}'], ['value', ['{"a":1}']]],
  ['null', 'null', ['value', ''], ['value', []]],
  ['undefined', 'undefined', ['value', ''], ['value', []]],
  ['empty_string', '""', ['value', ''], ['value', []]],
  ['space_string', '"  x  "', ['value', '  x  '], ['value', ['x']]],
  ['whitespace_string', '"  "', ['value', '  '], ['value', []]],
  ['zero', '0', ['value', '0'], ['value', ['0']]],
  ['false', 'false', ['value', 'false'], ['value', ['false']]],
  ['empty_array', '[]', ['value', ''], ['value', []]],
  ['nested_array', '[" a ", [" b ", null], ""]', ['value', 'a \nb'], ['value', ['a', 'b']]],
  ['duplicate_list', '[" a ", "a", "A", " "]', ['value', 'a \na\nA'], ['value', ['a', 'A']]],
  ['plain_object', '({b:2,a:1})', ['value', '{"b":2,"a":1}'], ['value', ['{"b":2,"a":1}']]],
  ['text_priority', '({text:"  t  ",content:"ignored"})', ['value', '  t  '], ['value', ['t']]],
  ['empty_text_priority', '({text:"",content:"ignored"})', ['value', ''], ['value', []]],
  ['content_field', '({content:[" a ","b"]})', ['value', 'a \nb'], ['value', ['a \nb']]],
  ['content_zero', '({content:0})', ['value', '0'], ['value', ['0']]],
  ['content_false', '({content:false})', ['value', 'false'], ['value', ['false']]],
  ['null_content', '({content:null})', ['value', '{"content":null}'], ['value', ['{"content":null}']]],
  ['cycle_json_fallback', '(()=>{const x={};x.self=x;return x;})()', ['value', '[object Object]'], ['value', ['[object Object]']]],
  ['array_unserializable_item', '["a",{toJSON(){return undefined;}},"b"]', ['value', 'a\nb'], ['error', 'TypeError'], ['value', 'a\nb'], ['value', ['a','b']]],
  ['nested_unserializable_item', '({content:{toJSON(){return undefined;}}})', ['undefined', null], ['error', 'TypeError'], ['value', ''], ['value', []]],
];
const results = [];
for (const [name, expression, oldContent, oldList, newContent = oldContent, newList = oldList] of cases) {
  for (const [version, source] of Object.entries(sources)) {
    const expected = version === 'baseline' ? [oldContent, oldList] : [newContent, newList];
    const program = source + `\n(() => {
      const capture = (fn) => {try {const value=fn();return value===undefined ? ['undefined',null] : ['value',value];} catch(error) {return ['error',error.name];}};
      return JSON.stringify([capture(()=>normalizeContent(${expression})), capture(()=>normalizeStringList(${expression}))]);
    })()`;
    const actual = JSON.parse(runInNewContext(program, Object.create(null), {timeout:1000, contextCodeGeneration:{strings:false,wasm:false}}));
    const pass = JSON.stringify(actual) === JSON.stringify(expected);
    results.push({name,version,actual,expected,pass});
  }
}
const failed = results.filter((r)=>!r.pass);
console.log(JSON.stringify({scope:'Only normalizeStringList lines 1146-1163 and normalizeContent lines 1460-1489 in fresh pure VM contexts',cases:cases.length,checks:results.length,passed:results.length-failed.length,failed:failed.length,results}, null, 2));
assert.equal(failed.length, 0, 'Local expectation mismatch: ' + failed.map((r)=>r.name+'/'+r.version).join(', '));
