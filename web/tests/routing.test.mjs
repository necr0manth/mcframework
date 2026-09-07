import assert from 'node:assert/strict';
import test from 'node:test';

import { ApiClient } from '../src/api.js';
import { captureCreatesMetadata, captureRoutingOperation, makeRoutingRule, metadataWrite, packageOwnedBy } from '../src/routing-model.js';
import { semanticCaptureAction } from '../src/semantic-import.js';
import { providerMatchFor } from '../src/view-model.js';

function clientWithCalls() {
  const calls = [];
  const client = new ApiClient(async (url, options) => {
    calls.push({ url, options, body: options.body ? JSON.parse(options.body) : undefined });
    return new Response(JSON.stringify({ revision: 4, csrf: 'token', writes: [{ file: 'mods/.pack/magic.json' }] }));
  });
  client.csrf = 'token';
  client.revision = 4;
  return { client, calls };
}

test('manual destination is one-shot and preview preserves the current revision', async () => {
  const { client, calls } = clientWithCalls();
  const write = metadataWrite(' mods/.pack/magic.json ', ' Magic ');
  assert.deepEqual(write, { file: 'mods/.pack/magic.json', section: 'Magic' });
  assert.equal(metadataWrite('', ' '), undefined);
  await client.previewRouting({ type: 'activation', id: 'a', state: 'disabled' }, 'child', write);
  assert.equal(client.revision, 4);
  await client.setActivation('a', 'disabled', 'child', write);
  await client.setActivation('b', 'enabled', 'child');
  assert.deepEqual(calls[0].body.metadata_write, write);
  assert.equal(calls[0].body.origin, undefined);
  assert.deepEqual(calls[1].body.metadata_write, write);
  assert.equal(calls[2].body.metadata_write, undefined);
  for (const call of calls) {
    assert.equal(call.options.headers.get('If-Match'), '"4"');
    assert.equal(call.options.headers.get('X-Mcpack-CSRF'), 'token');
  }
});

test('rule saving has an explicit scope and does not happen during import', async () => {
  const { client, calls } = clientWithCalls();
  const rule = makeRoutingRule({ id: 'magic', priority: '12', types: 'package, activation',
    packageKind: 'mod', origin: 'user', categories: 'magic', file: '../mods/.pack/magic.json', section: 'Magic' });
  assert.deepEqual(rule.when.package, { kind: ['mod'], categories: ['magic'] });
  assert.equal(rule.write.file.path_mode, 'local');
  await client.saveRoutingRule(rule, 'instance', 'child');
  await client.saveRoutingRule(rule, 'layer', 'child', '.pack/custom-rules.json');
  await semanticCaptureAction(client, {
    item: { id: 'add:a', path: 'mods/a.jar', actual_sha256: 'abc', enriched: true, provider_candidates: [] },
    mode: 'import', target: 'child', providerKey: 'local', metadataWrite: { file: 'mods/.pack/a.json' },
  })();
  assert.deepEqual(calls.map((call) => call.url), [
    '/api/v1/routing/rules', '/api/v1/routing/rules', '/api/v1/packages/import',
  ]);
  assert.equal(calls[0].body.scope, 'instance');
  assert.equal(calls[1].body.scope, 'layer');
  assert.equal(calls[1].body.destination, '.pack/custom-rules.json');
  assert.deepEqual(calls[2].body.metadata_write, { file: 'mods/.pack/a.json' });
});

test('declaration removal eligibility checks the selected layer boundary', async () => {
  const item = { source_file: 'C:\\pack\\child\\mods\\.pack\\mods.json' };
  const layers = [{ id: 'child', path: 'C:/pack/child' }, { id: 'parent', path: 'C:/pack' }];
  assert.equal(packageOwnedBy(item, layers, 'child'), true);
  assert.equal(packageOwnedBy(item, [{ id: 'other', path: 'C:/pack/chi' }], 'other'), false);
  assert.equal(packageOwnedBy(item, layers, 'missing'), false);
  const { client, calls } = clientWithCalls();
  await client.removePackage('magic / mod', 'remove', 'child');
  assert.equal(calls[0].url, '/api/v1/packages/magic%20%2F%20mod');
  assert.deepEqual(calls[0].body, { mode: 'remove', target_layer: 'child' });
});

test('preview descriptors retain available package metadata and file operation paths', () => {
  assert.deepEqual(captureRoutingOperation({ mode: 'import', item: { path: 'mods/a.jar', proposed_package_id: 'a' } }), {
    type: 'package', id: 'a', kind: 'mod', activation: 'enabled', destination: 'mods/a.jar',
  });
  assert.deepEqual(captureRoutingOperation({ mode: 'capture', item: { path: 'config/a.cfg', status: 'remove' } }), {
    type: 'remove', path: 'config/a.cfg',
  });
  assert.deepEqual(captureRoutingOperation({ mode: 'capture', item: {
    path: 'mods/a.jar', package_id: 'a', status: 'remove',
  } }), { type: 'activation', id: 'a', state: 'disabled' });
  assert.equal(captureCreatesMetadata({ mode: 'capture', item: {
    path: 'config/a.cfg', status: 'change',
  } }), false);
  assert.equal(captureCreatesMetadata({ mode: 'capture', item: {
    path: 'old.cfg', to_path: 'config/a.cfg', status: 'rename',
  } }), true);
});

test('capture preview carries its ephemeral origin and payload capture ignores metadata write', async () => {
  const { client, calls } = clientWithCalls();
  await client.previewRouting(
    { type: 'package', id: 'a', kind: 'mod', destination: 'mods/a.jar' },
    'child', undefined, 'instance_capture',
  );
  await semanticCaptureAction(client, {
    item: { id: 'change:a', path: 'config/a.cfg', status: 'change', actual_sha256: 'abc',
      enriched: true, provider_candidates: [] },
    mode: 'capture', target: 'child', providerKey: 'local',
    metadataWrite: { file: '.pack/wrong.json' },
  })();
  assert.equal(calls[0].body.origin, 'instance_capture');
  assert.equal(calls[1].body.metadata_write, undefined);
});

test('verified provider category metadata survives user confirmation and routing preview', () => {
  const candidate = { provider: 'modrinth', project_id: 'magic', version_id: 'v1',
    confidence: 'exact', kind: 'mod', categories: ['magic'], tags: ['library'] };
  const confirmed = providerMatchFor(candidate, false);
  assert.deepEqual(confirmed.categories, ['magic']);
  assert.deepEqual(confirmed.tags, ['library']);
  const preview = captureRoutingOperation({ mode: 'import', providerKey: 'modrinth\0magic\0v1',
    item: { path: 'mods/magic.jar', logical_id: 'magic', provider_candidates: [candidate] } });
  assert.equal(preview.source.provider, 'modrinth');
  assert.deepEqual(preview.categories, ['magic']);
  assert.deepEqual(preview.tags, ['library']);
  assert.equal(preview.activation, 'enabled');
});
