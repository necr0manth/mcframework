import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { setImmediate } from 'node:timers';
import { URL } from 'node:url';
import test from 'node:test';
import ts from 'typescript';
import * as explorerModel from '../src/file-explorer-model.js';
import * as api from '../src/api.js';
import * as semanticImport from '../src/semantic-import.js';

// Render the component's JSX and run its handlers without a browser dependency.
// The mutation callback deliberately swallows errors like App.act does.
async function explorerFixture({ capture, missingExpectation = false } = {}) {
  const hooks = [];
  const effects = [];
  let cursor = 0;
  const react = {
    useState(initial) {
      const index = cursor++;
      hooks[index] ??= { value: initial };
      return [hooks[index].value, (value) => {
        hooks[index].value = typeof value === 'function' ? value(hooks[index].value) : value;
      }];
    },
    useRef(initial) {
      const index = cursor++;
      hooks[index] ??= { current: initial };
      return hooks[index];
    },
    useMemo(factory) { cursor++; return factory(); },
    useEffect(effect, dependencies) {
      const index = cursor++;
      if (!hooks[index] || dependencies.some((value, i) => value !== hooks[index][i])) {
        hooks[index] = dependencies;
        effects.push(effect);
      }
    },
  };
  const jsx = (type, props) => ({ type, props });
  const source = await readFile(new URL('../src/FileExplorer.tsx', import.meta.url), 'utf8');
  const { outputText } = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX },
  });
  const imports = {
    react,
    'react/jsx-runtime': { jsx, jsxs: jsx, Fragment: 'fragment' },
    './file-explorer-model.js': explorerModel,
    './api.js': api,
    './semantic-import.js': semanticImport,
    './RoutingControls.js': { WriteDestination() {} },
  };
  const module = { exports: {} };
  new Function('require', 'module', 'exports', outputText)(
    (id) => { assert.ok(imports[id], `Unexpected component import: ${id}`); return imports[id]; },
    module, module.exports,
  );
  const calls = [];
  const errors = [];
  const plan = {
    path: 'mods', revision: 7, unsupported: [], provider_lookup: 'ready',
    items: ['enabled.jar', 'disabled.jar.disabled'].map((name, index) => ({
      id: `mod-${index}`, path: `mods/${name}`, capture_modes: ['package', 'payload'],
      ...(missingExpectation ? {} : { expected_sha256: `hash-${index}` }),
      provider_lookup: 'ready', provider_candidates: [{
        provider: 'modrinth', project_id: `project-${index}`, version_id: `v-${index}`,
        confidence: 'exact',
      }],
    })),
  };
  const props = {
    client: {
      files: async () => ({ parent: '', next_cursor: null, ignore_rules: [], items: [{
        path: 'mods', name: 'mods', type: 'directory', actionable: true,
        changed_count: 2, changed: true, status: 'add',
      }] }),
      filePlan: async () => plan,
      capturePath: async (...args) => { calls.push(args); return capture?.(...args); },
    },
    data: { revision: 7 }, targetLayer: 'client', layers: [{ id: 'client', local: false }],
    onTargetLayer() {}, onCompactMutation() {}, onRevisionConflict() {}, onReviewImport() {},
    onError: (message) => errors.push(message),
    onMutate: async (_label, action) => {
      try { await action(); return true; }
      catch (error) { errors.push(error.message); return false; }
    },
  };
  let tree;
  function render() {
    cursor = 0;
    tree = module.exports.default(props);
    effects.splice(0).forEach((effect) => effect());
    return tree;
  }
  function descendants(node) {
    if (Array.isArray(node)) return node.flatMap((child) => descendants(child));
    if (!node || typeof node !== 'object') return [];
    return [node, ...descendants(node.props?.children)];
  }
  function text(node) {
    if (Array.isArray(node)) return node.map(text).join('');
    if (!node || typeof node === 'boolean') return '';
    return typeof node === 'object' ? text(node.props?.children) : String(node);
  }
  function find(predicate) { return descendants(tree).find(predicate); }
  async function flush() {
    await new Promise((resolve) => setImmediate(resolve));
    render();
  }
  async function click(label) {
    const button = find((node) => node.type === 'button' && text(node) === label);
    assert.ok(button, `Missing button: ${label}`);
    assert.ok(!button.props.disabled, `Disabled button: ${label}`);
    button.props.onClick();
    await flush();
  }
  async function choose(name, index) {
    const input = descendants(tree).filter((node) => node.type === 'input' && node.props.name === name)[index];
    assert.ok(input);
    input.props.onChange();
    await flush();
  }
  render();
  await flush();
  await click('Send to layer');
  await choose('folder-strategy', 0);
  await click('Continue');
  await choose('preferred-provider', 0);
  await click('Continue');
  return { calls, errors, plan, click, flush, find, text };
}

test('atomic submit failure is visible outside the scrolling review and preserves choices for retry', async () => {
  const message = 'disabled packages must be formatted as a separate disable() operation';
  let release;
  let attempts = 0;
  const fixture = await explorerFixture({ capture: () => {
    if (++attempts === 1) throw new Error(message);
    return new Promise((resolve) => { release = resolve; });
  } });
  await fixture.click('Send all atomically');
  const dialog = fixture.find((node) => node.props.role === 'dialog');
  const alert = fixture.find((node) => node.props.role === 'alert');
  assert.equal(fixture.text(alert), message);
  assert.ok(dialog.props.children.includes(alert), 'Error must remain visible outside modal-body scrolling');
  assert.ok(fixture.text(dialog).includes('modrinth'));
  assert.deepEqual(fixture.errors, [message]);
  assert.equal(fixture.calls.length, 1);
  assert.equal(fixture.calls[0][3]['mod-1'].provider_match.provider, 'modrinth');
  await fixture.click('Send all atomically');
  assert.equal(fixture.find((node) => node.props.role === 'alert'), undefined, 'Retry clears stale error');
  assert.equal(fixture.find((node) => node.type === 'button' && fixture.text(node) === 'Send all atomically').props.disabled, true);
  assert.deepEqual(fixture.calls[1], fixture.calls[0], 'Retry retains the exact reviewed plan and provider choices');
  release();
  await fixture.flush();
  assert.equal(fixture.find((node) => node.props.role === 'dialog'), undefined, 'Successful retry closes the review');
});

test('atomic submit validation failure is displayed in the modal without a request', async () => {
  const fixture = await explorerFixture({ missingExpectation: true });
  await fixture.click('Send all atomically');
  assert.match(fixture.text(fixture.find((node) => node.props.role === 'alert')), /missing a source expectation/);
  assert.equal(fixture.calls.length, 0);
  assert.ok(fixture.find((node) => node.props.role === 'dialog'));
});
