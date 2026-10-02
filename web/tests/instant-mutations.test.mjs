import assert from 'node:assert/strict';
import test from 'node:test';
import { ApiClient, runRevisionedMutation } from '../src/api.js';
import { applyCompactReply, applyPendingMutations, optimisticActivation, settleCompactReply } from '../src/mutation-model.js';
import { applyExplorerIgnorePatch, mergeExplorerRefresh, refreshExplorerState } from '../src/file-explorer-model.js';

const response = (payload, status = 200) => new Response(JSON.stringify(payload), { status });
const deferred = () => {
  let resolve;
  const promise = new Promise((release) => { resolve = release; });
  return { promise, resolve };
};
const snapshot = {
  revision: 7, csrf: 'token',
  packages: [{ id: 'mod', activation: 'enabled' }, { id: 'dependency', activation: 'transitive' }],
  groups: [], delta: { loaded: false },
};

test('queued saves wait for the prior reply and bind If-Match to the execution revision', async () => {
  const held = deferred();
  const calls = [];
  const client = new ApiClient(async (url, options) => {
    calls.push({ url, options });
    if (calls.length === 1) await held.promise;
    return response({ ok: true, revision: 7 + calls.length });
  });
  client.csrf = 'token'; client.revision = 7;
  const first = client.setActivation('mod', 'disabled', 'client');
  const second = client.setActivation('mod', 'enabled', 'client');
  await Promise.resolve();
  assert.equal(calls.length, 1, 'The second user intent is queued while the first save is held');
  assert.equal(calls[0].options.headers.get('If-Match'), '"7"');
  assert.equal(calls[0].options.headers.get('Prefer'), 'return=minimal');
  held.resolve();
  await Promise.all([first, second]);
  assert.equal(calls[1].options.headers.get('If-Match'), '"8"');
  assert.deepEqual(calls.map(({ options }) => JSON.parse(options.body).state), ['disabled', 'enabled']);
  assert.equal(client.revision, 9);
});

test('a failed save rolls back its own overlay while preserving later user intent', async () => {
  const first = { optimistic: (value) => optimisticActivation(value, ['mod'], 'disabled') };
  const later = { optimistic: (value) => optimisticActivation(value, ['dependency'], 'disabled') };
  const projected = applyPendingMutations(snapshot, [first, later]);
  assert.deepEqual(projected.packages.map(({ activation }) => activation), ['disabled', 'disabled']);
  const rolledBack = applyPendingMutations(snapshot, [later]);
  assert.deepEqual(rolledBack.packages.map(({ activation }) => activation), ['enabled', 'disabled']);
  assert.deepEqual(snapshot.packages.map(({ activation }) => activation), ['enabled', 'transitive']);

  const calls = [];
  const client = new ApiClient(async (url, options) => {
    calls.push(options.headers.get('If-Match'));
    return calls.length === 1
      ? response({ error: { message: 'Write failed' } }, 500)
      : response({ ok: true, revision: 8 });
  });
  client.csrf = 'token'; client.revision = 7;
  const failed = client.setActivation('mod', 'disabled', 'client');
  const next = client.setActivation('dependency', 'disabled', 'client');
  await assert.rejects(failed, /Write failed/);
  await next;
  assert.deepEqual(calls, ['"7"', '"7"']);
});

test('authoritative dependency activation applies under newer optimistic intent and rejects stale replies', () => {
  const acknowledged = applyCompactReply(snapshot, {
    revision: 8, activation_patch: { packages: [
      { id: 'mod', activation: 'disabled' }, { id: 'dependency', activation: 'available' },
    ] },
  });
  const pending = [{ optimistic: (value) => optimisticActivation(value, ['mod'], 'enabled') }];
  assert.deepEqual(applyPendingMutations(acknowledged, pending).packages.map(({ activation }) => activation), ['enabled', 'available']);
  assert.strictEqual(applyCompactReply(acknowledged, { revision: 7 }), acknowledged);
  assert.strictEqual(settleCompactReply(acknowledged, { revision: 7 },
    (value) => optimisticActivation(value, ['mod'], 'enabled')), acknowledged,
  'An obsolete acknowledgment must not reapply its optimistic intent to a newer snapshot');
});

test('old background bootstrap cannot downgrade the client revision', async () => {
  const held = deferred();
  const client = new ApiClient(async (url) => {
    if (url === '/api/v1/bootstrap') { await held.promise; return response(snapshot); }
    return response({ ok: true, revision: 8 });
  });
  client.csrf = 'token'; client.revision = 7;
  const old = client.bootstrap();
  await client.setActivation('mod', 'disabled', 'client');
  held.resolve();
  const stale = await old;
  assert.equal(stale.revision, 7);
  assert.equal(client.revision, 8);
});

test('conflict refresh completes before the next write, without retrying the rejected operation', async () => {
  const refresh = deferred();
  const calls = [];
  const client = new ApiClient(async (url, options) => {
    calls.push({ url, options });
    if (calls.length === 1) return response({ error: { code: 'revision_conflict', details: { actual: 8 } } }, 409);
    if (url === '/api/v1/bootstrap') { await refresh.promise; return response({ ...snapshot, revision: 8 }); }
    return response({ ok: true, revision: 9 });
  });
  client.csrf = 'token'; client.revision = 7;
  const first = runRevisionedMutation(client, () => client.setActivation('mod', 'disabled', 'client'));
  const second = client.setActivation('dependency', 'disabled', 'client');
  // Advance through the rejected request's JSON decoding to its held refresh.
  for (let step = 0; step < 20 && calls.length < 2; ++step) await Promise.resolve();
  assert.equal(calls.length, 2);
  refresh.resolve();
  assert.equal((await first).applied, false);
  await second;
  assert.deepEqual(calls.map(({ url }) => url), [
    '/api/v1/packages/mod/activation', '/api/v1/bootstrap', '/api/v1/packages/dependency/activation',
  ]);
  assert.equal(calls[2].options.headers.get('If-Match'), '"8"');
});

test('reviewed capture keeps the explicit source revision while other writes use the latest revision', async () => {
  const calls = [];
  const client = new ApiClient(async (_url, options) => {
    calls.push(options.headers.get('If-Match'));
    return response({ ok: true, revision: 8 + calls.length });
  });
  client.csrf = 'token'; client.revision = 8;
  await client.capturePath('mods', 'client', 7, {});
  await client.ignorePath('config/a.txt', 'exact', 'client');
  assert.deepEqual(calls, ['"7"', '"9"']);
});

test('ignore immediately projects a subtree and rollback leaves the source tree intact', () => {
  const children = {
    '': [{ path: 'config', ignored: false }, { path: 'other', ignored: false }],
    config: [{ path: 'config/a.txt', ignored: false }],
  };
  const patch = { path: 'config', kind: 'subtree', ignored: true };
  assert.deepEqual(applyExplorerIgnorePatch(children, patch, false)[''].map(({ path }) => path), ['other']);
  assert.deepEqual(applyExplorerIgnorePatch(children, patch, false).config, []);
  assert.equal(applyExplorerIgnorePatch(children, patch, true).config[0].ignored, true);
  assert.equal(children.config[0].ignored, false);
});

test('expanded page refreshes start in parallel and do not discard newly opened or paginated folders', async () => {
  const held = deferred();
  const state = {
    children: { '': [{ path: 'config' }], config: [{ path: 'config/a.txt' }] },
    next: { '': 200, config: null }, rules: [], expanded: new Set(['config']),
    loadedPages: { '': [0], config: [0] },
  };
  const calls = [];
  const loading = refreshExplorerState(state, async ({ parent }) => {
    calls.push(parent);
    await held.promise;
    return { parent, items: [{ path: parent ? 'config/new.txt' : 'config' }], next_cursor: null, ignore_rules: [] };
  });
  assert.deepEqual(calls, ['', 'config']);
  const current = {
    ...state,
    children: { ...state.children, '': [...state.children[''], { path: 'later-page' }], newlyOpened: [{ path: 'newlyOpened/b.txt' }] },
    expanded: new Set(['config', 'newlyOpened']),
    loadedPages: { ...state.loadedPages, '': [0, 200], newlyOpened: [0] },
  };
  held.resolve();
  const merged = mergeExplorerRefresh(current, await loading, state);
  assert.strictEqual(merged.children[''], current.children['']);
  assert.deepEqual(merged.children.config, [{ path: 'config/new.txt' }]);
  assert.deepEqual(merged.children.newlyOpened, [{ path: 'newlyOpened/b.txt' }]);
  assert.strictEqual(merged.expanded, current.expanded);
});
