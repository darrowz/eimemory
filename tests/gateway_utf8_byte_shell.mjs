#!/usr/bin/env node
// Standalone, source/hash-gated byte-boundary regression test.
// Reads source as text; executes only the exact one-shot else body in a VM context.
// Never imports the gateway or executes initialize, complete, safeError, or serve.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createHash } from 'node:crypto';
import vm from 'node:vm';

const args = process.argv.slice(2);
assert.equal(args.length, 4, 'usage: --source PATH --expected-sha256 HEX');
assert.equal(args[0], '--source');
assert.equal(args[2], '--expected-sha256');
assert.match(args[3], /^[a-f0-9]{64}$/);
const sourceBytes = readFileSync(args[1]);
const sourceHash = createHash('sha256').update(sourceBytes).digest('hex');
assert.equal(sourceHash, args[3], 'source hash mismatch; review exact source first');
const source = sourceBytes.toString('utf8');
const start = '    clearTimeout(idle);\n  } else {\n';
const end = '\n  }\n} catch (error) {\n';
assert.equal(source.split(start).length, 2, 'one-shot start anchor must be unique');
assert.equal(source.split(end).length, 2, 'one-shot end anchor must be unique');
const body = source.split(start)[1].split(end)[0];
assert.ok(body.length > 0 && body.length < 2000, 'unexpected extraction size');
assert.ok(!/\b(?:import|require|initialize|safeError|createInterface)\b/.test(body),
  'prohibited gateway behavior in extracted body');
assert.equal((body.match(/\bcomplete\s*\(/g) || []).length, 1);
const script = new vm.Script('(async () => {\n' + body + '\n})()', {
  filename: 'extracted-one-shot-body-only.mjs',
  importModuleDynamically() { throw new Error('dynamic_import_forbidden'); },
});
const opaqueResult = Object.freeze({ synthetic_result: 'opaque-byte-shell-only' });
const expectedOutput = JSON.stringify(opaqueResult) + '\n';

async function execute(chunks) {
  const captured = [];
  const output = [];
  let yielded = 0;
  const fakeInput = Object.freeze({
    async *[Symbol.asyncIterator]() {
      for (const chunk of chunks) {
        assert.ok(Buffer.isBuffer(chunk), 'synthetic stdin requires Buffers');
        yielded += 1;
        yield chunk;
      }
    },
  });
  const stdout = new Proxy(Object.freeze({
    write(value) {
      assert.equal(typeof value, 'string');
      output.push(value);
      return true;
    },
  }), {
    get(target, key) {
      if (key !== 'write') throw new Error('stdout_access_forbidden:' + String(key));
      return target[key];
    },
  });
  const fakeProcess = new Proxy(Object.freeze({ stdin: fakeInput, stdout }), {
    get(target, key) {
      if (key !== 'stdin' && key !== 'stdout') {
        throw new Error('process_access_forbidden:' + String(key));
      }
      return target[key];
    },
  });
  const sdk = new Proxy(Object.freeze(Object.create(null)), {
    get(_target, key) { throw new Error('sdk_access_forbidden:' + String(key)); },
  });
  const sandbox = Object.create(null);
  Object.assign(sandbox, {
    Buffer,
    process: fakeProcess,
    sdk,
    async complete(receivedSdk, request) {
      assert.equal(receivedSdk, sdk, 'SDK sentinel identity changed');
      captured.push(JSON.stringify(request));
      return opaqueResult;
    },
  });
  for (const key of ['console', 'require', 'module', 'exports', 'fetch', 'WebSocket',
    'Worker', 'setTimeout', 'setInterval', 'setImmediate', 'queueMicrotask',
    'initialize', 'safeError', 'createInterface', 'config', 'env']) {
    Object.defineProperty(sandbox, key, {
      get() { throw new Error('ambient_access_forbidden:' + key); },
    });
  }
  const context = vm.createContext(sandbox, {
    name: 'synthetic-byte-input-only',
    codeGeneration: { strings: false, wasm: false },
  });
  let error = null;
  try {
    await script.runInContext(context, { timeout: 1000 });
  } catch (caught) {
    error = { name: caught.name, message: caught.message };
  }
  return { captured, output, yielded, error };
}

const results = [];
async function check(name, chunks, expected) {
  const actual = await execute(chunks);
  try {
    if (expected.error) {
      assert.ok(actual.error, 'expected rejection');
      if (expected.error === 'SyntaxError') assert.equal(actual.error.name, 'SyntaxError');
      else assert.equal(actual.error.message, expected.error);
      assert.equal(actual.captured.length, 0, 'complete called on rejected input');
      assert.equal(actual.output.length, 0, 'output written on rejected input');
      if (expected.yielded !== undefined) assert.equal(actual.yielded, expected.yielded);
    } else {
      assert.equal(actual.error, null, 'unexpected error: ' + JSON.stringify(actual.error));
      assert.equal(actual.captured.length, 1);
      assert.ok(actual.captured[0] === JSON.stringify(expected.request),
        'parsed request differs from the original UTF-8 request');
      assert.equal(actual.output.length, 1);
      assert.equal(actual.output[0], expectedOutput, 'output format changed');
      assert.equal(actual.yielded, chunks.length);
    }
    results.push({ name, status: 'pass' });
  } catch (error) {
    results.push({ name, status: 'fail', message: error.message });
  }
}

const asciiRequest = { prompt: 'ASCII test', values: [1, true, null] };
const ascii = Buffer.from(JSON.stringify(asciiRequest));
await check('ascii_unsplit', [ascii], { request: asciiRequest });
await check('ascii_byte_chunks', Array.from(ascii, byte => Buffer.from([byte])),
  { request: asciiRequest });
await check('empty_chunks_around_json', [Buffer.alloc(0), ascii, Buffer.alloc(0)],
  { request: asciiRequest });

// Every possible two-chunk boundary, plus successive single-byte chunks.
// This includes every internal split in every 2-, 3-, and 4-byte code point.
const samples = ['é', '€', '😀', 'AéB€C😀D', 'é€😀é€😀', 'quote"\n😀'];
for (let sample = 0; sample < samples.length; sample += 1) {
  const request = { prompt: samples[sample], list: [samples[sample]], ascii: 'ok' };
  const bytes = Buffer.from(JSON.stringify(request));
  await check(`unicode_${sample}_unsplit`, [bytes], { request });
  for (let split = 1; split < bytes.length; split += 1) {
    await check(`unicode_${sample}_split_${split}`,
      [bytes.subarray(0, split), bytes.subarray(split)], { request });
  }
  await check(`unicode_${sample}_byte_chunks`,
    Array.from(bytes, byte => Buffer.from([byte])), { request });
}

function paddedRequest(size, prefix = '') {
  const overhead = Buffer.byteLength(JSON.stringify({ pad: prefix }));
  const request = { pad: prefix + 'x'.repeat(size - overhead) };
  const bytes = Buffer.from(JSON.stringify(request));
  assert.equal(bytes.length, size);
  return { request, bytes };
}
const limit = 131072;
for (const size of [limit - 1, limit]) {
  const { request, bytes } = paddedRequest(size);
  await check(`raw_${size}_ascii_unsplit`, [bytes], { request });
  await check(`raw_${size}_ascii_split`, [bytes.subarray(0, 17), bytes.subarray(17)],
    { request });
}
for (const char of ['é', '€', '😀']) {
  const { request, bytes } = paddedRequest(limit, char);
  const charStart = bytes.indexOf(Buffer.from(char));
  await check(`raw_limit_${Buffer.byteLength(char)}byte_unsplit`, [bytes], { request });
  for (let internal = 1; internal < Buffer.byteLength(char); internal += 1) {
    const split = charStart + internal;
    await check(`raw_limit_${Buffer.byteLength(char)}byte_internal_${internal}`,
      [bytes.subarray(0, split), bytes.subarray(split)], { request });
  }
}
const oversized = paddedRequest(limit + 1, '😀').bytes;
await check('raw_131073_unsplit_reject_before_next_chunk',
  [oversized, Buffer.from('must not be consumed')],
  { error: 'request_too_large', yielded: 1 });
await check('raw_131073_split_reject_before_next_chunk',
  [oversized.subarray(0, limit), oversized.subarray(limit), Buffer.from('not consumed')],
  { error: 'request_too_large', yielded: 2 });
await check('raw_131073_ascii_reject', [paddedRequest(limit + 1).bytes],
  { error: 'request_too_large', yielded: 1 });

await check('empty_input_parse_failure', [], { error: 'SyntaxError' });
await check('empty_chunk_parse_failure', [Buffer.alloc(0)], { error: 'SyntaxError' });
await check('invalid_json_parse_failure', [Buffer.from('{"prompt":')],
  { error: 'SyntaxError' });
await check('trailing_json_parse_failure', [Buffer.from('{}{}')],
  { error: 'SyntaxError' });
// Retain normal Buffer UTF-8 replacement behavior; introduce no fatal decoder policy.
await check('malformed_utf8_unsplit_retains_replacement',
  [Buffer.concat([Buffer.from('{"prompt":"'), Buffer.from([0xff]), Buffer.from('"}')])],
  { request: { prompt: '\ufffd' } });

const failures = results.filter(result => result.status === 'fail');
console.log(JSON.stringify({
  scope: 'extracted one-shot input body; synthetic Buffer stdin and capture-only complete',
  source_sha256: sourceHash,
  body_sha256: createHash('sha256').update(body).digest('hex'),
  node_version: process.version,
  cases: results.length,
  passed: results.length - failures.length,
  failed: failures.length,
  failures,
  tests: results.map(({ name, status }) => ({ name, status })),
}, null, 2));
process.exitCode = failures.length ? 1 : 0;
