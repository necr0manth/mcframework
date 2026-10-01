import assert from 'node:assert/strict';
import test from 'node:test';

import {
  ApiClient,
  ApiError,
  createKeyedRequestLoader,
  createRevisionedDeltaLoader,
  errorMessage,
  mergeDeltaForRevision,
  normalizeBootstrap,
  recoverDeltaRevisionConflict,
  runAutoCaptureFlow,
  runRevisionedMutation,
} from '../src/api.js';
import {
  applyExplorerCapturePatch,
  decisionsForPlan,
  explorerTreeNavigationDisabled,
  explorerRefreshRequests,
  flattenExplorer,
  mergeExplorerPage,
  refreshExplorerState,
  toggleExplorerDirectory,
} from '../src/file-explorer-model.js';
import {
  allMembersActive,
  buildMetadataTree,
  canImportDelta,
  deltaPresentation,
  metadataDeclarationCount,
  packageActivationTarget,
  paginateDelta,
  providerMatchFor,
} from '../src/view-model.js';

const completeBootstrap = {
  schema: 1,
  revision: 7,
  csrf: 'csrf-token',
  instance: { name: 'Prism Client', path: 'C:/Games/.minecraft' },
  link: { branch: 'client', effective_layer: '.mcpack/features/client-dev', features: ['debug'] },
  packages: [
    {
      id: 'sodium',
      logical_id: 'renderer',
      kind: 'mod',
      version: '1.2.3',
      activation: 'enabled',
      destination: 'mods/sodium.jar',
      source: { provider: 'modrinth', location: 'project/version', sha256: 'abc' },
      source_file: 'client/packages.json',
      editable: true,
    },
  ],
  declarations: [
    { id: 'decl-package', kind: 'package', layer: 'client', producer: 'json', source: 'client/packages.json', operation_index: 2, editable: true, data: { type: 'package', id: 'sodium' } },
    { id: 'decl-section', kind: 'section', layer: 'client', producer: 'json', source: 'client/packages.json', operation_index: 0, editable: true, data: { type: 'section', id: 'performance' } },
  ],
  metadata_files: [
    { id: 'client-packages', layer: 'client', path: 'client/packages.json', sections: ['performance'], declarations: ['decl-section', 'decl-package'] },
  ],
  groups: [{ id: 'performance-stack', name: 'Performance stack', activation: 'enabled' }],
  sections: [{ id: 'performance', title: 'Performance', members: ['sodium'], order: 1 }],
  delta: {
    items: [{
      id: 'package:zoom',
      kind: 'package',
      status: 'add',
      path: 'mods/zoom.jar',
      logical_id: 'zoom',
      editable: true,
      capture_modes: ['package_import', 'payload'],
      jar_metadata: { loader: 'fabric', mod_ids: ['zoom'], name: 'Zoom', version: '2.0', dependencies: ['fabric-api'] },
      provider_candidates: [{ provider: 'modrinth', project_id: 'zoom', version_id: 'v2', version: '2.0', confidence: 'exact', reason: 'SHA-512 match', hashes: { sha512: 'hash' } }],
    }],
    summary: { add: 1, remove: 0, change: 0, update: 0, rename: 0 },
  },
  writable_layers: [{ id: 'client', path: 'client', local: false, virtual: false }],
  feature_candidates: ['debug', 'shaders'],
  capabilities: { activation: true, features: true, capture: true, package_import: true, sync: true, shutdown: true },
};

function response(payload, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

test('provider retry preserves revision and sends authenticated mutation', async () => {
  let request;
  const client = new ApiClient(async (url, options) => {
    request = { url, options };
    return response({ ok: true, revision: 7, retried: 2 });
  });
  client.csrf = 'csrf-token';
  client.revision = 7;

  assert.equal(await client.retryProviderLookups(), 2);
  assert.equal(request.url, '/api/v1/providers/retry');
  assert.equal(request.options.method, 'POST');
  assert.equal(request.options.headers.get('X-Mcpack-CSRF'), 'csrf-token');
  assert.equal(request.options.headers.get('If-Match'), '"7"');
});

test('normalizes revision, metadata hierarchy and provider evidence', () => {
  const result = normalizeBootstrap(completeBootstrap);

  assert.equal(result.revision, 7);
  assert.equal(result.metadata_files[0].path, 'client/packages.json');
  assert.deepEqual(result.metadata_files[0].declarations, ['decl-section', 'decl-package']);
  assert.deepEqual(result.delta.items[0].jar_metadata.mod_ids, ['zoom']);
  assert.equal(result.delta.items[0].provider_candidates[0].confidence, 'exact');
  assert.equal(result.delta.loaded, true);
  assert.equal(result.delta.complete, true);
  assert.equal(result.packages[0].source.provider, 'modrinth');
});

test('normalizes optional omissions and mappings defensively', () => {
  const result = normalizeBootstrap({
    effective_layer: 'client',
    packages: { lithium: { source: { provider: 'local' } } },
    sections: { misc: { title: 'Misc', members: ['lithium'] } },
    delta: [{ stable_id: 'add:jar', status: 'add', path: 'mods/a.jar' }],
    writable_layers: ['client'],
    capabilities: ['capture'],
  });

  assert.equal(result.revision, 0);
  assert.equal(result.packages[0].id, 'lithium');
  assert.deepEqual(result.delta.items[0].provider_candidates, []);
  assert.deepEqual(result.delta.items[0].jar_metadata.mod_ids, []);
  assert.equal(result.delta.loaded, true);
  assert.equal(result.delta.complete, true);
  assert.equal(result.capabilities.capture, true);
  assert.equal(result.capabilities.activation, false);
});

test('normalizes an explicitly deferred delta and loads it through the delta route', async () => {
  const deferred = normalizeBootstrap({
    delta: {
      items: [], summary: { add: 0, update: null, rename: null },
      loaded: false, complete: false,
    },
  });
  assert.equal(deferred.delta.loaded, false);
  assert.equal(deferred.delta.complete, false);
  assert.equal(deferred.delta.summary.update, null);

  const calls = [];
  const client = new ApiClient(async (url, options) => {
    calls.push({ url, options });
    return response({
      items: [{ stable_id: 'add:file', status: 'add', path: 'extra.txt' }],
      summary: { add: 1 },
      loaded: true,
    });
  });
  client.csrf = 'csrf-token';
  client.revision = 9;

  const delta = await client.delta();
  assert.equal(delta.loaded, true);
  assert.equal(delta.items[0].path, 'extra.txt');
  assert.deepEqual(calls.map((call) => [call.url, call.options.method || 'GET']), [
    ['/api/v1/delta', 'GET'],
  ]);
  assert.equal(client.revision, 9);
});

test('reuses an in-flight delta request for the same revision and rejects stale merge', async () => {
  const calls = [];
  let resolveDelta;
  const pending = new Promise((resolve) => { resolveDelta = resolve; });
  const client = new ApiClient(async (url, options) => {
    calls.push({ url, options });
    await pending;
    return response({ items: [], summary: {}, loaded: true });
  });
  const loader = createRevisionedDeltaLoader(() => client.delta());

  const firstEntry = loader.load(7);
  const reentry = loader.load(7);
  assert.strictEqual(reentry, firstEntry);
  assert.deepEqual(calls.map((call) => call.url), ['/api/v1/delta']);

  const newerBootstrap = { revision: 8, delta: { loaded: false } };
  resolveDelta();
  const staleDelta = await reentry;
  assert.strictEqual(
    mergeDeltaForRevision(newerBootstrap, 7, staleDelta),
    newerBootstrap,
  );

  await loader.load(8);
  assert.deepEqual(calls.map((call) => call.url), ['/api/v1/delta', '/api/v1/delta']);
});

test('retries the same delta revision after a transient request failure', async () => {
  const calls = [];
  const client = new ApiClient(async (url, options) => {
    calls.push({ url, options });
    if (calls.length === 1) {
      return response({ error: { message: 'temporary failure' } }, 503);
    }
    return response({ items: [], summary: {}, loaded: true });
  });
  const loader = createRevisionedDeltaLoader(() => client.delta());

  await assert.rejects(() => loader.load(7), /temporary failure/);
  const recovered = await loader.load(7);

  assert.equal(recovered.loaded, true);
  assert.deepEqual(calls.map((call) => call.url), ['/api/v1/delta', '/api/v1/delta']);
});

test('delta revision conflict refreshes and allows a retry at the new revision', async () => {
  let attempts = 0;
  const conflict = new ApiError('stale revision', 409, 'revision_conflict', {
    expected: 7, actual: 8,
  });
  const loader = createRevisionedDeltaLoader(async () => {
    attempts += 1;
    if (attempts === 1) throw conflict;
    return { revision: 8, items: [], summary: {}, loaded: true };
  });
  let refreshed = null;

  let reason;
  try {
    await loader.load(7);
  } catch (error) {
    reason = error;
  }
  assert.equal(await recoverDeltaRevisionConflict(reason, async (error) => {
    refreshed = error.details.actual;
  }), true);
  assert.equal(refreshed, 8);
  assert.equal((await loader.load(8)).revision, 8);
  assert.equal(attempts, 2);

  assert.equal(await recoverDeltaRevisionConflict(new Error('offline'), () => {
    assert.fail('ordinary errors must not trigger conflict refresh');
  }), false);
});

test('detail loader keys requests by revision and item, evicts rejection, and preserves stale data', async () => {
  const attempts = new Map();
  const loader = createKeyedRequestLoader(async (key) => {
    const count = (attempts.get(key) || 0) + 1;
    attempts.set(key, count);
    if (key === '7:a' && count === 1) throw new Error('temporary detail failure');
    return { revision: Number(key.split(':')[0]), item: { id: key.split(':')[1] } };
  });

  const rejected = loader.load('7:a', '7:a');
  assert.strictEqual(loader.load('7:a', '7:a'), rejected);
  await assert.rejects(() => rejected, /temporary detail failure/);
  assert.equal((await loader.load('7:a', '7:a')).item.id, 'a');
  assert.equal((await loader.load('7:b', '7:b')).item.id, 'b');
  assert.equal((await loader.load('8:a', '8:a')).revision, 8);
  assert.deepEqual(Object.fromEntries(attempts), { '7:a': 2, '7:b': 1, '8:a': 1 });

  const current = { revision: 8, delta: { revision: 8 } };
  assert.strictEqual(
    mergeDeltaForRevision(current, 7, { revision: 7, items: [] }),
    current,
  );
});

test('delta detail and mutations carry revision and selected file hash', async () => {
  const calls = [];
  const fetchImpl = async (url, options) => {
    calls.push({ url, options });
    if (url.startsWith('/api/v1/delta/selected?')) {
      return response({
        revision: 7,
        item: {
          id: 'selected', status: 'add', path: 'mods/selected.jar', enriched: true,
          actual_sha256: 'selected-sha',
        },
      });
    }
    return response(completeBootstrap);
  };
  const client = new ApiClient(fetchImpl);
  client.csrf = 'csrf-token';
  client.revision = 7;

  const detail = await client.deltaItem('selected', 7);
  await client.capture('selected', 'client', detail.item.actual_sha256);
  await client.importPackage('selected', 'client', undefined, detail.item.actual_sha256);

  assert.equal(detail.revision, 7);
  assert.deepEqual(calls.map((call) => [call.url, call.options.method || 'GET']), [
    ['/api/v1/delta/selected?revision=7', 'GET'],
    ['/api/v1/delta/selected/capture', 'POST'],
    ['/api/v1/packages/import', 'POST'],
  ]);
  assert.deepEqual(JSON.parse(calls[1].options.body), {
    target_layer: 'client', expected_sha256: 'selected-sha',
  });
  assert.deepEqual(JSON.parse(calls[2].options.body), {
    delta_id: 'selected', target_layer: 'client', expected_sha256: 'selected-sha',
  });
});

test('metadata tree shows section headers once without repeating them as declarations', () => {
  const normalized = normalizeBootstrap(completeBootstrap);
  normalized.metadata_files[0].sections = ['performance', 'performance'];
  const tree = buildMetadataTree(normalized);

  assert.equal(tree[0].path, 'client/packages.json');
  assert.equal(tree[0].sections.length, 1);
  assert.equal(
    metadataDeclarationCount(normalized),
    tree.reduce(
      (total, file) => total + file.sections.reduce((count, section) => count + section.declarations.length, 0),
      0,
    ),
  );
  assert.equal(tree[0].sections[0].title, 'Performance');
  assert.deepEqual(
    tree[0].sections[0].declarations.map((item) => item.id),
    ['decl-package'],
  );
});

test('section activation treats enabled and transitive members as active', () => {
  const packages = [
    { id: 'root', logical_id: 'root', activation: 'enabled' },
    { id: 'dependency', logical_id: 'lib', activation: 'transitive' },
  ];
  assert.equal(allMembersActive(packages, ['root', 'lib']), true);
  assert.equal(allMembersActive(packages, ['root', 'missing']), false);
});

test('semantic delta presentation distinguishes package update and rename', () => {
  assert.deepEqual(
    deltaPresentation({ id: 'update', kind: 'package', status: 'update', logical_id: 'sodium', actual_version: '1.0', desired_version: '1.1' }),
    { label: 'Updated', tone: 'updated', title: 'sodium', path: 'update', detail: 'Local 1.0 → desired 1.1' },
  );
  assert.deepEqual(
    deltaPresentation({ id: 'rename', kind: 'file', status: 'rename', from_path: 'a.txt', to_path: 'b.txt' }),
    { label: 'Renamed', tone: 'renamed', title: 'b.txt', path: 'a.txt → b.txt', detail: 'file differs from the resolved layer.' },
  );
});

test('delta pagination renders bounded chunks and advances without exceeding total', () => {
  const items = Array.from({ length: 450 }, (_, id) => ({ id }));
  const first = paginateDelta(items, 200);
  assert.equal(first.items.length, 200);
  assert.equal(first.rendered, 200);
  assert.equal(first.total, 450);
  assert.equal(first.nextLimit, 400);

  const second = paginateDelta(items, first.nextLimit);
  assert.equal(second.items.length, 400);
  assert.equal(second.nextLimit, 450);
  assert.deepEqual(paginateDelta(items, second.nextLimit).items, items);
});

test('package import is offered only when an actual artifact exists', () => {
  assert.equal(canImportDelta({ status: 'add', kind: 'package', capture_modes: ['package'] }), true);
  assert.equal(canImportDelta({ status: 'update', kind: 'package', capture_modes: ['package'] }), true);
  assert.equal(canImportDelta({ status: 'remove', kind: 'package', capture_modes: ['activation'] }), false);
});

test('fuzzy provider match requires explicit confirmation while local fallback is empty', () => {
  const fuzzy = {
    provider: 'modrinth',
    project_id: 'zoom',
    version_id: 'v2',
    version: '2.0',
    download_url: 'https://cdn.example.invalid/zoom.jar',
    hashes: { sha512: 'verified-hash' },
    confidence: 'fuzzy',
  };
  assert.equal(providerMatchFor(undefined, false), undefined);
  assert.throws(() => providerMatchFor(fuzzy, false), /explicit confirmation/);
  assert.deepEqual(providerMatchFor(fuzzy, true), fuzzy);
});

test('provider confirmation preserves URL and hashes required by the backend', () => {
  const exact = {
    provider: 'modrinth',
    project_id: 'zoom',
    version_id: 'v2',
    version: '2.0',
    download_url: 'https://cdn.example.invalid/zoom.jar',
    hashes: { sha512: 'verified-hash' },
    confidence: 'exact',
    reason: 'SHA-512 match',
  };

  assert.deepEqual(providerMatchFor(exact, false), {
    provider: 'modrinth',
    project_id: 'zoom',
    version_id: 'v2',
    version: '2.0',
    download_url: 'https://cdn.example.invalid/zoom.jar',
    hashes: { sha512: 'verified-hash' },
    confidence: 'exact',
  });
});

test('package toggle targets the local overlay owner instead of a stale base selection', async () => {
  const overlay = {
    id: '.mcpack/features/dev', path: 'C:\\project\\.mcpack\\features\\dev\\',
    local: true, virtual: true,
  };
  const layers = [
    { id: 'base', path: 'C:/project/base', local: false, virtual: false },
    overlay,
  ];
  const calls = [];
  const client = new ApiClient(async (url, options) => {
    calls.push({ url, options });
    return response({ ...completeBootstrap, revision: 8 + calls.length });
  });
  const item = {
    id: 'extra', source_file: 'C:/project/.mcpack/features/dev/.pack/local-edits.json',
  };
  for (const state of ['disabled', 'enabled', 'disabled', 'enabled']) {
    await client.setActivation(item.id, state, packageActivationTarget(item, layers, 'base'));
  }
  assert.deepEqual(calls.map((call) => call.url), Array(4).fill('/api/v1/packages/extra/activation'));
  assert.deepEqual(calls.map((call) => JSON.parse(call.options.body)), [
    { state: 'disabled', target_layer: overlay.id },
    { state: 'enabled', target_layer: overlay.id },
    { state: 'disabled', target_layer: overlay.id },
    { state: 'enabled', target_layer: overlay.id },
  ]);
  assert.equal(packageActivationTarget(item, layers, overlay.id), overlay.id);
  assert.equal(packageActivationTarget(item, layers, ''), overlay.id);
});

test('package toggle preserves inherited targets and requires a local virtual path boundary', async () => {
  const layers = [
    { id: 'base', path: '/project/base', local: false, virtual: false },
    { id: 'feature', path: '/project/feature', local: false, virtual: false },
    { id: 'local-real', path: '/project/local-real', local: true, virtual: false },
    { id: 'remote-virtual', path: '/project/remote', local: false, virtual: true },
    { id: 'overlay', path: '/project/.mcpack/features/dev/', local: true, virtual: true },
  ];
  const calls = [];
  const client = new ApiClient(async (url, options) => {
    calls.push(JSON.parse(options.body));
    return response(completeBootstrap);
  });
  for (const source of [
    '/project/base/.pack/operations.json',
    '/project/feature/.pack/operations.json',
    '/project/local-real/.pack/operations.json',
    '/project/remote/.pack/operations.json',
    '/project/.mcpack/features/dev-other/.pack/local-edits.json',
    '',
  ]) {
    const item = { id: 'inherited', source_file: source };
    for (const selected of ['base', 'feature', 'overlay', '']) {
      assert.equal(packageActivationTarget(item, layers, selected), selected);
    }
    await client.setActivation(item.id, 'disabled', packageActivationTarget(item, layers, 'feature'));
  }
  assert.deepEqual(calls, Array(6).fill({ state: 'disabled', target_layer: 'feature' }));
  assert.equal(packageActivationTarget({}, [], 'base'), 'base');
});

test('API actions send quoted If-Match and all batch routes', async () => {
  const calls = [];
  const fetchImpl = async (url, options) => {
    calls.push({ url, options });
    return response({ ...completeBootstrap, revision: 8 + calls.length });
  };
  const client = new ApiClient(fetchImpl);

  await client.bootstrap();
  await client.addFeature('shaders');
  await client.removeFeature('debug/night');
  await client.setActivation('sodium extra', 'disabled', 'client');
  await client.setGroupActivation('performance stack', 'enabled', 'client');
  await client.setSectionActivation('client mods', 'disabled', 'client');
  await client.capture('file:config/a.json', 'client');
  await client.importPackage('package:zoom', 'client', {
    provider: 'modrinth', project_id: 'zoom', version_id: 'v2', confidence: 'exact',
  });
  await client.sync();
  await client.files('config/a b', {
    mode: 'changed-only', showIgnored: true, cursor: 2, targetLayer: 'client',
  });
  await client.filePlan('config/a b', 'client');
  await client.capturePath('config', 'client', 17, {
    delta: { mode: 'payload', expected_sha256: 'planned-sha' },
  });
  await client.ignorePath('config/[literal]*', 'exact', 'client');
  await client.removeIgnore({ target: 'client', index: 2 }, 'client');

  assert.deepEqual(calls.map((call) => [call.url, call.options.method || 'GET']), [
    ['/api/v1/bootstrap', 'GET'],
    ['/api/v1/features', 'POST'],
    ['/api/v1/features/debug%2Fnight', 'DELETE'],
    ['/api/v1/packages/sodium%20extra/activation', 'PATCH'],
    ['/api/v1/groups/performance%20stack/activation', 'PATCH'],
    ['/api/v1/sections/client%20mods/activation', 'PATCH'],
    ['/api/v1/delta/file%3Aconfig%2Fa.json/capture', 'POST'],
    ['/api/v1/packages/import', 'POST'],
    ['/api/v1/sync', 'POST'],
    ['/api/v1/files?parent=config%2Fa+b&mode=changed-only&show_ignored=true&cursor=2&limit=200&target_layer=client', 'GET'],
    ['/api/v1/files/plan?path=config%2Fa+b&target_layer=client', 'GET'],
    ['/api/v1/files/capture', 'POST'],
    ['/api/v1/files/ignore', 'POST'],
    ['/api/v1/files/ignore', 'DELETE'],
  ]);
  assert.equal(calls[1].options.headers.get('If-Match'), '"9"');
  assert.equal(calls[1].options.headers.get('X-Mcpack-CSRF'), 'csrf-token');
  assert.equal(calls[1].options.headers.has('X-Mcpack-Token'), false);
  assert.deepEqual(JSON.parse(calls[5].options.body), { state: 'disabled', target_layer: 'client' });
  assert.deepEqual(JSON.parse(calls[7].options.body), {
    delta_id: 'package:zoom',
    target_layer: 'client',
    provider_match: { provider: 'modrinth', project_id: 'zoom', version_id: 'v2', confidence: 'exact' },
  });
  assert.equal(calls[11].options.headers.get('If-Match'), '"17"');
  assert.equal(calls[11].options.revision, undefined);
  assert.deepEqual(JSON.parse(calls[11].options.body), {
    path: 'config',
    plan_revision: 17,
    decisions: { delta: { mode: 'payload', expected_sha256: 'planned-sha' } },
    target_layer: 'client',
  });
  assert.equal(calls.every((call) => call.options.credentials === 'same-origin'), true);
});

test('auto payload capture is one compact POST with current If-Match and no bootstrap', async () => {
  const calls = [];
  const client = new ApiClient(async (url, options) => {
    calls.push({ url, options });
    return response({
      ok: true,
      applied: true,
      revision: 8,
      explorer_patch: { path: 'bulk/[literal]*?', captured_count: 700 },
    });
  });
  client.csrf = 'csrf-token';
  client.revision = 7;
  const applied = [];

  const outcome = await runAutoCaptureFlow({
    client,
    path: 'bulk/[literal]*?',
    targetLayer: 'client',
    onApplied: (result) => applied.push(result.explorer_patch),
    onDecisionRequired: () => assert.fail('decision modal must not open'),
    onRevisionConflict: () => assert.fail('conflict refresh must not run'),
  });

  assert.equal(outcome.state, 'applied');
  assert.equal(client.revision, 8);
  assert.deepEqual(applied, [{ path: 'bulk/[literal]*?', captured_count: 700 }]);
  assert.deepEqual(calls.map((call) => [call.url, call.options.method || 'GET']), [
    ['/api/v1/files/capture', 'POST'],
  ]);
  assert.equal(calls[0].options.headers.get('If-Match'), '"7"');
  assert.deepEqual(JSON.parse(calls[0].options.body), {
    path: 'bulk/[literal]*?', target_layer: 'client', auto_payload: true,
  });
});

test('auto payload decision response hydrates the existing explicit plan path', async () => {
  const calls = [];
  const plan = {
    path: 'mods', revision: 7, unsupported: [],
    items: [{
      id: 'jar', path: 'mods/a.jar', capture_modes: ['package', 'payload'],
      expected_sha256: 'jar-sha',
    }],
  };
  const client = new ApiClient(async (url, options) => {
    calls.push({ url, options });
    if (url === '/api/v1/files/capture') {
      return response({
        ok: true, applied: false, revision: 7,
        decision_required: { path: 'mods', items: [{ id: 'jar' }] },
      });
    }
    return response(plan);
  });
  client.csrf = 'csrf-token';
  client.revision = 7;
  let opened = null;

  const outcome = await runAutoCaptureFlow({
    client,
    path: 'mods',
    targetLayer: 'client',
    onApplied: () => assert.fail('tree must not be patched'),
    onDecisionRequired: (value) => { opened = value; },
    onRevisionConflict: () => assert.fail('conflict refresh must not run'),
  });

  assert.equal(outcome.state, 'decision_required');
  assert.deepEqual(opened, plan);
  assert.equal(client.revision, 7);
  assert.deepEqual(calls.map((call) => [call.url, call.options.method || 'GET']), [
    ['/api/v1/files/capture', 'POST'],
    ['/api/v1/files/plan?path=mods&target_layer=client', 'GET'],
  ]);
});

test('auto payload revision conflict refreshes authoritatively without retrying capture', async () => {
  const calls = [];
  const client = new ApiClient(async (url, options) => {
    calls.push({ url, options });
    if (url === '/api/v1/files/capture') {
      return response({
        error: {
          code: 'revision_conflict', message: 'stale revision',
          details: { expected: 7, actual: 8 },
        },
      }, 409);
    }
    return response({ ...completeBootstrap, revision: 8 });
  });
  client.csrf = 'csrf-token';
  client.revision = 7;
  let refreshed = null;

  const outcome = await runAutoCaptureFlow({
    client,
    path: 'bulk',
    targetLayer: 'client',
    onApplied: () => assert.fail('tree must not be patched'),
    onDecisionRequired: () => assert.fail('decision modal must not open'),
    onRevisionConflict: async () => { refreshed = await client.bootstrap(); },
  });

  assert.equal(outcome.state, 'conflict');
  assert.equal(refreshed.revision, 8);
  assert.equal(client.revision, 8);
  assert.deepEqual(calls.map((call) => [call.url, call.options.method || 'GET']), [
    ['/api/v1/files/capture', 'POST'],
    ['/api/v1/bootstrap', 'GET'],
  ]);
});

test('file tree helpers preserve lazy children and changed-only server order', () => {
  const first = mergeExplorerPage({}, {
    parent: '',
    items: [{ path: 'config', type: 'directory' }, { path: 'mods', type: 'directory' }],
  });
  const second = mergeExplorerPage(first, {
    parent: 'config',
    items: [{ path: 'config/a.txt', type: 'file' }],
  });
  assert.deepEqual(
    flattenExplorer(second, new Set(['config'])).map((item) => item.path),
    ['config', 'config/a.txt', 'mods'],
  );
});

test('file tree expands before lazy loading and scopes loading disables to its parent', () => {
  const opening = toggleExplorerDirectory(new Set(), 'mods', false);
  assert.deepEqual([...opening.expanded], ['mods']);
  assert.equal(opening.loadParent, 'mods');

  const loading = new Set(['load:mods']);
  assert.equal(explorerTreeNavigationDisabled('', loading, 'mods'), true);
  assert.equal(explorerTreeNavigationDisabled('', loading, 'config'), false);
  assert.equal(explorerTreeNavigationDisabled('refresh:files:2', loading, 'config'), true);
  assert.equal(explorerTreeNavigationDisabled('capture:config', loading, 'config'), true);
});

test('compact capture patch updates exact loaded scope and ancestors without collapsing state', () => {
  const scope = 'bulk/[literal]*?';
  const expanded = new Set(['bulk', scope]);
  const next = { '': null, bulk: 200, [scope]: null };
  const loadedPages = { '': [0], bulk: [0], [scope]: [0] };
  const state = {
    children: {
      '': [
        { path: 'bulk', status: 'unchanged', changed: true, changed_count: 705 },
        { path: 'bulk-other', status: 'add', changed: true, changed_count: 1 },
      ],
      bulk: [
        { path: scope, status: 'unchanged', changed: true, changed_count: 700 },
        { path: 'bulk/keep.txt', status: 'add', changed: true, changed_count: 5 },
      ],
      [scope]: [
        {
          path: `${scope}/a.txt`, desired: false, synthetic: false,
          status: 'add', changed: true, changed_count: 1,
        },
      ],
    },
    expanded,
    next,
    rules: [{ id: 'same-rule' }],
    loadedPages,
  };

  const updated = applyExplorerCapturePatch(
    state,
    { path: scope, captured_count: 700 },
  );

  assert.strictEqual(updated.expanded, expanded);
  assert.strictEqual(updated.next, next);
  assert.strictEqual(updated.loadedPages, loadedPages);
  assert.equal(updated.children[''][0].changed_count, 5);
  assert.equal(updated.children[''][0].changed, true);
  assert.equal(updated.children[''][1].status, 'add');
  assert.deepEqual(updated.children.bulk[0], {
    path: scope, status: 'unchanged', changed: false, changed_count: 0,
    visible_changed_count: 0, desired: true, synthetic: false,
  });
  assert.equal(updated.children[scope][0].desired, true);
  assert.equal(updated.children[scope][0].status, 'unchanged');
  assert.equal(updated.children[scope][0].changed, false);
  assert.equal(updated.children[scope][0].changed_count, 0);
  assert.equal(updated.children.bulk[1].changed_count, 5);
});

test('batch decisions serialize every item expectation and require ambiguous package choices', () => {
  const items = [
    {
      id: 'text', path: 'config/a.txt', capture_modes: ['payload'],
      expected_sha256: 'text-sha',
    },
    {
      id: 'removed', path: 'config/removed.txt', capture_modes: ['remove'],
      expected_absent: true,
    },
    {
      id: 'jar', path: 'mods/a.jar', capture_modes: ['package', 'payload'],
      expected_sha256: 'jar-sha',
    },
  ];
  assert.throws(() => decisionsForPlan(items, {}), /Choose how/);
  assert.deepEqual(decisionsForPlan(items, { jar: { mode: 'payload' } }), {
    text: { mode: 'payload', expected_sha256: 'text-sha' },
    removed: { mode: 'remove', expected_absent: true },
    jar: { mode: 'payload', expected_sha256: 'jar-sha' },
  });
  const fuzzy = { provider: 'modrinth', confidence: 'fuzzy' };
  assert.throws(
    () => decisionsForPlan(items, { jar: { mode: 'package', provider_match: fuzzy } }),
    /Confirm the fuzzy/,
  );
  assert.deepEqual(
    decisionsForPlan(items, {
      jar: { mode: 'package', provider_match: fuzzy, fuzzyConfirmed: true },
    }),
    {
      text: { mode: 'payload', expected_sha256: 'text-sha' },
      removed: { mode: 'remove', expected_absent: true },
      jar: { mode: 'package', expected_sha256: 'jar-sha', provider_match: fuzzy },
    },
  );
  assert.throws(
    () => decisionsForPlan([
      { id: 'unsafe', path: 'unsafe.txt', capture_modes: ['payload'] },
    ], {}),
    /missing a source expectation/,
  );
});

test('revision refresh preserves expansion and reloads root plus loaded expanded pages', async () => {
  const literal = 'config/[literal]*?';
  const expanded = new Set(['config', literal]);
  const state = {
    children: {
      '': [
        { path: 'config', type: 'directory' },
        { path: literal, type: 'directory' },
        { path: 'collapsed', type: 'directory' },
      ],
      config: [{ path: 'config/old.txt', type: 'file' }],
      [literal]: [{ path: `${literal}/old.txt`, type: 'file' }],
      collapsed: [{ path: 'collapsed/old.txt', type: 'file' }],
    },
    expanded,
    next: { '': 200, config: 200, [literal]: null, collapsed: null },
    rules: [{ id: 'old-rule' }],
    loadedPages: { '': [0, 200], config: [0, 200], [literal]: [0], collapsed: [0] },
  };
  assert.deepEqual(explorerRefreshRequests(
    state.children, state.expanded, state.loadedPages,
  ), [
    { parent: '', cursor: 0 },
    { parent: '', cursor: 200 },
    { parent: 'config', cursor: 0 },
    { parent: 'config', cursor: 200 },
    { parent: literal, cursor: 0 },
  ]);

  const calls = [];
  const updated = await refreshExplorerState(state, async ({ parent, cursor }) => {
    calls.push([parent, cursor]);
    return {
      parent,
      items: [{ path: `${parent || 'root'}/new-${cursor}.txt`, type: 'file' }],
      next_cursor: cursor === 0 ? 200 : null,
      ignore_rules: parent === '' ? [{ id: 'new-rule' }] : [],
    };
  });

  assert.strictEqual(updated.expanded, expanded);
  assert.deepEqual(calls, [
    ['', 0], ['', 200], ['config', 0], ['config', 200], [literal, 0],
  ]);
  assert.deepEqual(updated.children.config.map((item) => item.path), [
    'config/new-0.txt', 'config/new-200.txt',
  ]);
  assert.deepEqual(updated.children.collapsed, state.children.collapsed);
  assert.deepEqual(updated.rules, [{ id: 'new-rule' }]);
});

test('failed revision refresh leaves the previously rendered tree coherent', async () => {
  const state = {
    children: {
      '': [{ path: 'config', type: 'directory' }],
      config: [{ path: 'config/current.txt', type: 'file' }],
    },
    expanded: new Set(['config']),
    next: { '': null, config: null },
    rules: [{ id: 'current-rule' }],
    loadedPages: { '': [0], config: [0] },
  };
  const before = JSON.parse(JSON.stringify({
    children: state.children, next: state.next, rules: state.rules,
  }));
  let call = 0;
  await assert.rejects(
    () => refreshExplorerState(state, async ({ parent }) => {
      call += 1;
      if (call === 2) throw new Error('refresh failed');
      return { parent, items: [], next_cursor: null, ignore_rules: [] };
    }),
    /refresh failed/,
  );
  assert.deepEqual(
    { children: state.children, next: state.next, rules: state.rules },
    before,
  );
  assert.deepEqual([...state.expanded], ['config']);
});

test('revision conflict exposes status, code and details for refresh recovery', async () => {
  const client = new ApiClient(async () => response({
    error: {
      code: 'revision_conflict',
      message: 'stale bootstrap revision',
      details: { expected: 3, actual: 4 },
    },
  }, 409));
  client.csrf = 'csrf';
  client.revision = 3;

  await assert.rejects(
    () => client.capture('opaque', 'client'),
    (error) => error instanceof ApiError && error.status === 409 &&
      error.code === 'revision_conflict' && error.details.actual === 4,
  );
});

test('file mutation conflict refreshes revision without silently reapplying', async () => {
  const calls = [];
  const fetchImpl = async (url, options) => {
    calls.push({ url, options });
    if (calls.length === 1) {
      return response({
        error: {
          code: 'revision_conflict',
          message: 'stale bootstrap revision',
          details: { expected: 1, actual: 2 },
        },
      }, 409);
    }
    return response({ ...completeBootstrap, revision: calls.length });
  };
  const client = new ApiClient(fetchImpl);
  client.csrf = 'csrf-token';
  client.revision = 1;
  const action = () => client.capturePath('config', 'client', client.revision, {
    delta: { mode: 'payload', expected_sha256: 'planned-sha' },
  });

  const first = await runRevisionedMutation(client, action);
  assert.equal(first.applied, false);
  assert.equal(client.revision, 2);
  assert.equal(calls.length, 2);
  assert.equal(calls[0].options.headers.get('If-Match'), '"1"');
  assert.equal(calls[1].url, '/api/v1/bootstrap');

  const retry = await runRevisionedMutation(client, action);
  assert.equal(retry.applied, true);
  assert.equal(calls.length, 3);
  assert.equal(calls[2].options.headers.get('If-Match'), '"2"');
});

test('uses the API error envelope message', () => {
  assert.equal(errorMessage({ error: { message: 'Layer is read-only' } }), 'Layer is read-only');
  assert.equal(errorMessage({}, 'Fallback'), 'Fallback');
});
