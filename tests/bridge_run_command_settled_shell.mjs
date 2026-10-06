import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';

// Offline local shell test. The VM receives only runCommand and explicit fakes.
// No host process, require, real child process, signal, network, SDK or env is injected.
const sourcePath = process.argv[2] || new URL('../integrations/openclaw/eimemory-bridge/index.js', import.meta.url);
const allLines = fs.readFileSync(sourcePath, 'utf8').split('\n');
const startLine = 2304;
// Inspect only the prior reviewed window before allowing two-line expansion.
const reviewedHeadLines = allLines.slice(startLine - 1, 2406);
assert.equal(reviewedHeadLines.length, 103, 'prior reviewed window must have 103 lines');
const reviewedHead = reviewedHeadLines.join('\n');
const guard = '    const collect = (target, chunk) => {\n      if (settled) {\n        return;\n      }\n';
const timerDeclaration = '    let timer;\n    let forceKill;\n';
const closeCleanup = "    child.on('close', (status, signal) => {\n      clearTimeout(forceKill);\n";
const forceAssignment = "        forceKill = setTimeout(() => child.kill('SIGKILL'), 250);";
for (const marker of [guard, timerDeclaration, closeCleanup, forceAssignment]) {
  assert.equal(reviewedHead.split(marker).length - 1, 1, 'reviewed marker count must be one before window expansion');
}
assert.ok(reviewedHeadLines[0] === "function runCommand(command, args, { input = '', timeout = 0, deadlineAtMs = 0 } = {}) {", 'reviewed runCommand signature mismatch');
const candidateLines = allLines.slice(startLine - 1, 2408);
assert.equal(candidateLines.length, 105, 'candidate range must have 105 lines');
assert.ok(candidateLines[102] === '  }), { deadlineAtMs: commandDeadlineAtMs });', 'candidate closing call mismatch');
assert.ok(candidateLines[103] === '}', 'candidate closing brace mismatch');
assert.ok(candidateLines[104] === '', 'candidate trailing blank line mismatch');
const candidate = candidateLines.join('\n');
assert.equal(candidate.split(guard).length - 1, 1, 'candidate collect guard count must be one');
assert.equal(candidate.includes('invokeHook'), false, 'no subsequent function in VM');
assert.equal((candidate.match(/^function /gm) || []).length, 1);
// Retain the old six settled-data counterexamples with cleanup in both variants.
const baseline = candidate.replace(guard, '    const collect = (target, chunk) => {\n');
assert.equal(baseline.split('\n').length, 102);
assert.ok(baseline.split('\n')[100] === '}', 'in-memory guard baseline closing brace mismatch');
// Exact inverse of this patch gives the current master runCommand body.
const timerOriginal = candidate.replace(timerDeclaration, '    let timer;\n')
  .replace(closeCleanup, "    child.on('close', (status, signal) => {\n")
  .replace(forceAssignment, "        const forceKill = setTimeout(() => child.kill('SIGKILL'), 250);");
assert.equal(timerOriginal.split('\n').length, 103);
assert.ok(timerOriginal.split('\n')[101] === '}', 'timer original closing brace mismatch');

function makeHarness(code, options = {}) {
  const state = { now: 1000, timers: [], kills: [], inputs: [], spawnCalls: [], queueCalls: [], concatenations: [], configCalls: [], released: false };
  function pipe() {
    const listeners = new Map();
    return {
      on(name, handler) { const list = listeners.get(name) || []; list.push(handler); listeners.set(name, list); return this; },
      emit(name, ...args) { for (const handler of listeners.get(name) || []) handler(...args); },
    };
  }
  const child = pipe();
  child.stdout = pipe(); child.stderr = pipe(); child.stdin = pipe();
  child.stdin.end = (input) => { state.inputs.push(input); if (options.stdinThrow) throw options.stdinThrow; };
  child.kill = (signal) => { state.kills.push(signal); if (options.killThrow) throw options.killThrow; return options.killReturn ?? true; };
  function fakeSetTimeout(fn, delay) {
    const timer = { fn, delay, active: true, unrefs: 0, unref() { this.unrefs += 1; } };
    state.timers.push(timer); return timer;
  }
  const context = vm.createContext({
    scheduleCommand(start, queueOptions) {
      state.queueCalls.push(queueOptions);
      if (options.queueReject) return Promise.reject(options.queueReject);
      if (options.defer) return new Promise((resolve, reject) => { state.release = () => { state.released = true; try { resolve(start()); } catch (error) { reject(error); } }; });
      return start();
    },
    positiveIntEnv(name, fallback) { state.configCalls.push({ name, fallback }); return options.limit ?? 8; },
    DEFAULT_MAX_COMMAND_OUTPUT_BYTES: 8,
    spawn(command, args, spawnOptions) { state.spawnCalls.push({ command, args, spawnOptions }); if (options.spawnThrow) throw options.spawnThrow; return child; },
    Date: { now: () => state.now },
    setTimeout: fakeSetTimeout,
    clearTimeout(timer) { if (timer) timer.active = false; },
    Buffer: { concat(parts) { state.concatenations.push(parts.slice()); return Buffer.concat(parts); } },
  }, { codeGeneration: { strings: false, wasm: false } });
  vm.runInContext(code + '\n;globalThis.testRunCommand = runCommand;', context, { timeout: 1000 });
  assert.equal(context.process, undefined);
  assert.equal(context.require, undefined);
  return {
    state, child,
    run(opts = {}) { return context.testRunCommand('FAKE_COMMAND', ['FIXED_ARG'], opts); },
    fire(index) { const timer = state.timers[index]; assert.ok(timer && timer.active, 'active fake timer required'); timer.active = false; timer.fn(); },
  };
}
function observe(promise) {
  return promise.then((value) => ({ kind: 'resolve', value }), (error) => ({ kind: 'reject', error }));
}
function summary(result) {
  if (result.kind === 'resolve') return { kind: result.kind, ...result.value };
  const { message, code, status, signal } = result.error;
  return { kind: result.kind, message, code, status, signal };
}
const tests = [];
function test(name, fn) { tests.push({ name, fn }); }
function both(name, fn) {
  test(name, async () => {
    const oldResult = await fn(baseline);
    const newResult = await fn(candidate);
    assert.deepEqual(newResult, oldResult, 'baseline/candidate original behavior differs');
  });
}

both('active success: split UTF-8, both streams, input and spawn options', async (code) => {
  const h = makeHarness(code); const p = observe(h.run({ input: 'fixed input' }));
  h.child.stdout.emit('data', Buffer.from([0xe4, 0xb8]));
  h.child.stdout.emit('data', Buffer.from([0xad]));
  h.child.stderr.emit('data', Buffer.from('warn'));
  h.child.emit('close', 0, null);
  const result = summary(await p);
  assert.deepEqual(result, { kind: 'resolve', status: 0, stdout: '中', stderr: 'warn' });
  assert.deepEqual(h.state.inputs, ['fixed input']);
  assert.equal(h.state.spawnCalls[0].spawnOptions.windowsHide, true);
  assert.equal(Array.from(h.state.spawnCalls[0].spawnOptions.stdio).join(','), 'pipe,pipe,pipe');
  assert.equal(h.state.concatenations.length, 2);
  return result;
});
both('nonzero exit: stderr whitespace wins and status/signal retained', async (code) => {
  const h = makeHarness(code); const p = observe(h.run());
  h.child.stdout.emit('data', Buffer.from('out')); h.child.stderr.emit('data', Buffer.from(' '));
  h.child.emit('close', 2, 'FAKE_SIGNAL'); const result = summary(await p);
  assert.equal(result.message, ' '); assert.equal(result.status, 2); assert.equal(result.signal, 'FAKE_SIGNAL'); return result;
});
both('nonzero exit: stdout fallback', async (code) => {
  const h = makeHarness(code); const p = observe(h.run()); h.child.stdout.emit('data', Buffer.from('out')); h.child.emit('close', 1, null);
  const result = summary(await p); assert.equal(result.message, 'out'); return result;
});
both('nonzero exit: empty output signal fallback', async (code) => {
  const h = makeHarness(code); const p = observe(h.run()); h.child.emit('close', null, 'FAKE_SIGNAL');
  const result = summary(await p); assert.equal(result.message, 'eimemory command exited with FAKE_SIGNAL'); return result;
});
both('child error preserves original error and clears timer', async (code) => {
  const h = makeHarness(code); const p = observe(h.run({ timeout: 50 })); const error = new Error('fixed child error');
  h.child.emit('error', error); const result = await p; assert.equal(result.error, error); assert.equal(h.state.timers[0].active, false); assert.equal(h.state.kills.length, 0); return summary(result);
});
both('stdin EPIPE ignored until close', async (code) => {
  const h = makeHarness(code); const p = observe(h.run()); h.child.stdin.emit('error', { code: 'EPIPE' }); h.child.emit('close', 0, null);
  const result = summary(await p); assert.equal(result.kind, 'resolve'); return result;
});
both('stdin non-EPIPE rejects unchanged', async (code) => {
  const h = makeHarness(code); const p = observe(h.run()); const error = Object.assign(new Error('fixed stdin error'), { code: 'FIXED_STDIN' });
  h.child.stdin.emit('error', error); const result = await p; assert.equal(result.error, error); assert.equal(h.state.kills.length, 0); return summary(result);
});
both('timeout rejects before forceKill; close cancels forceKill', async (code) => {
  const h = makeHarness(code); const p = observe(h.run({ timeout: 50 }));
  assert.equal(h.state.timers[0].delay, 50); h.fire(0); const result = summary(await p);
  assert.equal(result.code, 'ETIMEDOUT'); assert.deepEqual(h.state.kills, ['SIGTERM']);
  assert.equal(h.state.timers[1].delay, 250); h.child.emit('close', 0, null); assert.equal(h.state.timers[1].active, false);
  assert.deepEqual(h.state.kills, ['SIGTERM']); return result;
});
both('combined output equals limit succeeds', async (code) => {
  const h = makeHarness(code, { limit: 5 }); const p = observe(h.run());
  h.child.stdout.emit('data', Buffer.from('abc')); h.child.stderr.emit('data', Buffer.from('de')); h.child.emit('close', 0, null);
  const result = summary(await p); assert.equal(result.kind, 'resolve'); assert.equal(h.state.kills.length, 0); return result;
});
both('combined output exceeds limit rejects and excludes conversion', async (code) => {
  const h = makeHarness(code, { limit: 5 }); const p = observe(h.run());
  h.child.stdout.emit('data', Buffer.from('abc')); h.child.stderr.emit('data', Buffer.from('def'));
  const result = summary(await p); assert.equal(result.code, 'EIMEMORY_OUTPUT_LIMIT'); assert.deepEqual(h.state.kills, ['SIGKILL']); assert.equal(h.state.concatenations.length, 0); return result;
});
both('automatic timeout deadline consumes deferred fake scheduling budget', async (code) => {
  const h = makeHarness(code, { defer: true }); const p = observe(h.run({ timeout: 100 })); assert.equal(h.state.queueCalls[0].deadlineAtMs, 1100);
  h.state.now = 1080; h.state.release(); assert.equal(h.state.timers[0].delay, 20); h.child.emit('close', 0, null); return summary(await p);
});
both('explicit deadline overrides call-time timeout deadline', async (code) => {
  const h = makeHarness(code, { defer: true }); const p = observe(h.run({ timeout: 100, deadlineAtMs: 2000 })); assert.equal(h.state.queueCalls[0].deadlineAtMs, 2000);
  h.state.now = 1500; h.state.release(); assert.equal(h.state.timers[0].delay, 100); h.child.emit('close', 0, null); return summary(await p);
});
both('explicit deadline caps remaining runtime', async (code) => {
  const h = makeHarness(code, { defer: true }); const p = observe(h.run({ timeout: 100, deadlineAtMs: 1050 }));
  h.state.now = 1040; h.state.release(); assert.equal(h.state.timers[0].delay, 10); h.child.emit('close', 0, null); return summary(await p);
});
both('expired before spawn uses queue timeout and never fake-spawns', async (code) => {
  const h = makeHarness(code, { defer: true }); const p = observe(h.run({ deadlineAtMs: 1050 })); h.state.now = 1050; h.state.release();
  const result = summary(await p); assert.equal(result.code, 'EIMEMORY_QUEUE_TIMEOUT'); assert.equal(h.state.spawnCalls.length, 0); return result;
});
both('no positive budget means no runtime timer', async (code) => {
  const h = makeHarness(code); const p = observe(h.run({ timeout: -1 })); assert.equal(h.state.timers.length, 0); h.child.emit('close', 0, null); return summary(await p);
});
both('fake queue rejection passes through without spawn', async (code) => {
  const error = Object.assign(new Error('fixed queue full'), { code: 'EIMEMORY_QUEUE_FULL' }); const h = makeHarness(code, { queueReject: error });
  const result = await observe(h.run()); assert.equal(result.error, error); assert.equal(h.state.spawnCalls.length, 0); return summary(result);
});
both('synchronous spawn throw rejects original error', async (code) => {
  const error = new Error('fixed spawn throw'); const h = makeHarness(code, { spawnThrow: error }); const result = await observe(h.run()); assert.equal(result.error, error); return summary(result);
});
both('synchronous stdin.end throw retains original behavior', async (code) => {
  const error = new Error('fixed stdin throw'); const h = makeHarness(code, { stdinThrow: error }); const result = await observe(h.run({ timeout: 50 }));
  assert.equal(result.error, error); assert.equal(h.state.timers[0].active, true); assert.equal(h.state.kills.length, 0); return summary(result);
});
both('kill throw still precedes fail; known lifecycle behavior unchanged', async (code) => {
  const error = new Error('fixed kill throw'); const h = makeHarness(code, { limit: 1, killThrow: error }); const p = observe(h.run());
  assert.throws(() => h.child.stdout.emit('data', Buffer.from('ab')), (actual) => actual === error);
  h.child.emit('close', 0, null); const result = summary(await p); assert.equal(result.kind, 'resolve'); return result;
});

for (const terminal of ['success', 'nonzero', 'child-error', 'stdin-error', 'timeout', 'output-limit']) {
  test('settled data ignored after ' + terminal + '; baseline counterexample', async () => {
    const records = [];
    for (const code of [baseline, candidate]) {
      const h = makeHarness(code, { limit: 8 }); const p = observe(h.run(terminal === 'timeout' ? { timeout: 50 } : {}));
      if (terminal === 'success') h.child.emit('close', 0, null);
      if (terminal === 'nonzero') h.child.emit('close', 1, null);
      if (terminal === 'child-error') h.child.emit('error', new Error('fixed terminal'));
      if (terminal === 'stdin-error') h.child.stdin.emit('error', new Error('fixed terminal'));
      if (terminal === 'timeout') h.fire(0);
      if (terminal === 'output-limit') h.child.stdout.emit('data', Buffer.from('123456789'));
      const result = summary(await p); const before = h.state.kills.length; let lengthReads = 0;
      const lateSmall = { get length() { lengthReads += 1; return 1; } };
      const lateLarge = { get length() { lengthReads += 1; return 9; } };
      h.child.stdout.emit('data', lateSmall); h.child.stderr.emit('data', lateLarge);
      records.push({ result, lengthReads, extraKills: h.state.kills.length - before });
    }
    assert.deepEqual(records[1].result, records[0].result);
    assert.equal(records[0].lengthReads, 2, 'baseline must touch both late chunks');
    assert.ok(records[0].extraKills >= 1, 'baseline must reproduce extra kill request');
    assert.equal(records[1].lengthReads, 0, 'candidate must return before chunk length access');
    assert.equal(records[1].extraKills, 0, 'candidate must not request extra kill');
    console.log('COUNTEREXAMPLE ' + JSON.stringify({ terminal, baseline: { lengthReads: records[0].lengthReads, extraKills: records[0].extraKills }, candidate: { lengthReads: records[1].lengthReads, extraKills: records[1].extraKills } }));
  });
}

test('timer original counterexample: timeout then close leaves redundant callback', async () => {
  const records = [];
  for (const code of [timerOriginal, candidate]) {
    const h = makeHarness(code); const p = observe(h.run({ timeout: 50 }));
    h.fire(0); const beforeClose = summary(await p);
    assert.equal(beforeClose.code, 'ETIMEDOUT');
    assert.equal(h.state.timers[1].active, true);
    h.child.emit('close', 0, null);
    const activeAfterClose = h.state.timers[1].active;
    if (activeAfterClose) h.fire(1);
    records.push({ result: summary(await p), activeAfterClose, kills: h.state.kills.slice() });
  }
  assert.deepEqual(records[1].result, records[0].result);
  assert.equal(records[0].activeAfterClose, true);
  assert.deepEqual(records[0].kills, ['SIGTERM', 'SIGKILL']);
  assert.equal(records[1].activeAfterClose, false);
  assert.deepEqual(records[1].kills, ['SIGTERM']);
  console.log('TIMER_COUNTEREXAMPLE ' + JSON.stringify(records));
});
test('without close forceKill remains active after timeout rejection and fires once', async () => {
  for (const code of [timerOriginal, candidate]) {
    const h = makeHarness(code); const p = observe(h.run({ timeout: 50 }));
    h.fire(0); const result = summary(await p); assert.equal(result.code, 'ETIMEDOUT');
    assert.equal(h.state.timers[0].active, false);
    assert.equal(h.state.timers[1].active, true); assert.equal(h.state.timers[1].delay, 250);
    assert.equal(h.state.timers[1].unrefs, 1); h.fire(1);
    assert.deepEqual(h.state.kills, ['SIGTERM', 'SIGKILL']);
    assert.equal(h.state.timers[1].active, false); assert.deepEqual(summary(await p), result);
  }
});
test('errors after timeout are not close and must retain forceKill', async () => {
  const h = makeHarness(candidate); const p = observe(h.run({ timeout: 50 })); h.fire(0);
  const result = summary(await p);
  h.child.emit('error', new Error('fixed late error'));
  h.child.stdin.emit('error', new Error('fixed late stdin error'));
  assert.equal(h.state.timers[1].active, true); h.fire(1);
  assert.deepEqual(h.state.kills, ['SIGTERM', 'SIGKILL']); assert.deepEqual(summary(await p), result);
});
test('duplicate close after timeout is harmless and preserves original rejection', async () => {
  const h = makeHarness(candidate); const p = observe(h.run({ timeout: 50 })); h.fire(0);
  const result = summary(await p); h.child.emit('close', 0, null); h.child.emit('close', 9, 'FIXED');
  assert.equal(h.state.timers[1].active, false); assert.equal(h.state.concatenations.length, 0);
  assert.deepEqual(h.state.kills, ['SIGTERM']); assert.deepEqual(summary(await p), result);
});
for (const status of [0, 2]) {
  test('ordinary close before timeout clears main timer, creates no force timer; status ' + status, async () => {
    const h = makeHarness(candidate); const p = observe(h.run({ timeout: 50 }));
    h.child.emit('close', status, null); const result = summary(await p); h.child.emit('close', 9, 'FIXED');
    assert.equal(result.kind, status === 0 ? 'resolve' : 'reject'); assert.equal(result.status, status);
    assert.equal(h.state.timers.length, 1); assert.equal(h.state.timers[0].active, false);
    assert.equal(h.state.concatenations.length, 2); assert.deepEqual(h.state.kills, []);
    assert.deepEqual(summary(await p), result);
  });
}
test('close after forceKill already fired changes neither result nor recorded kill', async () => {
  const h = makeHarness(candidate); const p = observe(h.run({ timeout: 50 })); h.fire(0);
  const result = summary(await p); h.fire(1); h.child.emit('close', null, 'FIXED'); h.child.emit('close', 0, null);
  assert.equal(h.state.timers[1].active, false); assert.deepEqual(h.state.kills, ['SIGTERM', 'SIGKILL']);
  assert.deepEqual(summary(await p), result);
});

let failures = 0;
for (const { name, fn } of tests) {
  try { await fn(); console.log('PASS ' + name); }
  catch (error) { failures += 1; console.error('FAIL ' + name + ': ' + error.stack); }
}
console.log(JSON.stringify({ tests: tests.length, passed: tests.length - failures, failed: failures, baseline: 'guard removal plus exact inverse timer patch', sourceRange: '2304-2408 candidate only', osCancellationVerified: false }));
if (failures) process.exitCode = 1;
