import assert from 'node:assert/strict';
import test from 'node:test';

import {
  ApiClient,
  ApiError,
  errorMessage,
  normalizeBootstrap,
  runRevisionedMutation,
} from '../src/api.js';
import {
  decisionsForPlan,
  flattenExplorer,
  mergeExplorerPage,
} from '../src/file-explorer-model.js';
import {
  allMembersActive,
  buildMetadataTree,
  canImportDelta,
  deltaPresentation,
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

test('normalizes revision, metadata hierarchy and provider evidence', () => {
  const result = normalizeBootstrap(completeBootstrap);

  assert.equal(result.revision, 7);
  assert.equal(result.metadata_files[0].path, 'client/packages.json');
  assert.deepEqual(result.metadata_files[0].declarations, ['decl-section', 'decl-package']);
  assert.deepEqual(result.delta.items[0].jar_metadata.mod_ids, ['zoom']);
  assert.equal(result.delta.items[0].provider_candidates[0].confidence, 'exact');
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
  assert.equal(result.capabilities.capture, true);
  assert.equal(result.capabilities.activation, false);
});

test('metadata tree preserves file, section and declaration order', () => {
  const normalized = normalizeBootstrap(completeBootstrap);
  const tree = buildMetadataTree(normalized);

  assert.equal(tree[0].path, 'client/packages.json');
  assert.equal(tree[0].sections[0].title, 'Performance');
  assert.deepEqual(
    tree[0].sections[0].declarations.map((item) => item.id),
    ['decl-package'],
  );
  assert.deepEqual(
    tree[0].sections[1].declarations.map((item) => item.id),
    ['decl-section'],
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
  await client.capturePath('config', 'client', { delta: { mode: 'payload' } });
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
  assert.equal(calls.every((call) => call.options.credentials === 'same-origin'), true);
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

test('batch package decisions require explicit raw or package choice and fuzzy confirmation', () => {
  const items = [{ id: 'jar', path: 'mods/a.jar', capture_modes: ['package', 'payload'] }];
  assert.throws(() => decisionsForPlan(items, {}), /Choose how/);
  assert.deepEqual(decisionsForPlan(items, { jar: { mode: 'payload' } }), {
    jar: { mode: 'payload' },
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
    { jar: { mode: 'package', provider_match: fuzzy } },
  );
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
  const action = () => client.capturePath('config', 'client', {});

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
