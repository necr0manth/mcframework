import assert from 'node:assert/strict';
import test from 'node:test';
import { ApiClient, normalizeDeltaItem, runAutoCaptureFlow } from '../src/api.js';
import { decisionsForPlan } from '../src/file-explorer-model.js';
import { candidateKey, semanticCaptureAction, singleFileImportItem } from '../src/semantic-import.js';

const revision = 7;
const candidate = {
  provider: 'modrinth', project_id: 'sample', version_id: 'version-2',
  version: '2.0', confidence: 'exact', download_url: 'https://example.invalid/mod.jar',
  hashes: { sha256: 'actual-hash' },
};
const entry = {
  path: 'mods/sample.jar', type: 'file', actual: true, delta_id: 'opaque/id',
  status: 'changed', capture_modes: ['payload'],
};

for (const status of ['add', 'changed']) {
  for (const choice of ['exact', 'fuzzy', 'local', 'payload']) {
    test(`Files and Delta ${status} JAR use the same enriched ${choice} import request`, async () => {
      const requestsByView = [];
      for (const source of ['files', 'delta']) {
        const requests = [];
        const candidates = choice === 'local' ? [] : [{ ...candidate, confidence: choice === 'fuzzy' ? 'fuzzy' : 'exact' }];
        const item = normalizeDeltaItem({
          id: entry.delta_id, path: entry.path, status, enriched: true,
          capture_modes: ['package', 'payload'], actual_sha256: 'actual-hash',
          jar_metadata: { name: 'Sample', version: '2.0', mod_ids: ['sample'], loader: 'fabric' },
          provider_candidates: candidates,
        });
        const client = new ApiClient(async (url, options = {}) => {
          requests.push({ url, method: options.method, body: options.body ? JSON.parse(options.body) : undefined, revision: options.headers.get('If-Match') });
          return new Response(JSON.stringify(requests.length === 1 ? { revision, item } : { revision: revision + 1 }), { status: 200 });
        });
        client.revision = revision;
        client.csrf = 'test-token';
        const seed = source === 'files'
          ? singleFileImportItem({ ...entry, status })
          : normalizeDeltaItem({ id: entry.delta_id, path: entry.path, status, enriched: false });
        assert.equal(seed.enriched, false);
        const detail = await client.deltaItem(seed.id, revision);
        assert.equal(detail.item.jar_metadata.name, 'Sample');
        assert.deepEqual(detail.item.provider_candidates.map((value) => value.provider), candidates.map((value) => value.provider));
        const draft = {
          item: detail.item, revision, target: 'another-writable-layer',
          mode: choice === 'payload' ? 'capture' : 'import',
          providerKey: ['local', 'payload'].includes(choice) ? 'local' : candidateKey(candidate),
          fuzzyConfirmed: false,
        };
        if (choice === 'fuzzy') {
          assert.throws(() => semanticCaptureAction(client, draft), /explicit confirmation/);
          assert.equal(requests.length, 1, 'unconfirmed fuzzy selection cannot write');
          draft.fuzzyConfirmed = true;
        }
        await semanticCaptureAction(client, draft)();
        assert.equal(requests[0].url, '/api/v1/delta/opaque%2Fid?revision=7');
        assert.equal(requests[1].body.target_layer, 'another-writable-layer');
        assert.equal(requests[1].body.expected_sha256, 'actual-hash');
        assert.equal(requests[1].revision, '"7"');
        if (choice === 'payload') {
          assert.equal(requests[1].url, '/api/v1/delta/opaque%2Fid/capture');
        } else {
          assert.equal(requests[1].url, '/api/v1/packages/import');
          assert.equal(requests[1].body.delta_id, 'opaque/id');
          assert.equal(requests[1].body.provider_match?.provider, choice === 'local' ? undefined : 'modrinth');
        }
        requestsByView.push(requests);
      }
      assert.deepEqual(requestsByView[0], requestsByView[1]);
    });
  }
}

test('Files routes JARs to inspection even when cheap snapshot only offers payload', () => {
  assert.equal(singleFileImportItem(entry).id, entry.delta_id);
  assert.equal(singleFileImportItem({ ...entry, path: 'mods/MOD.JAR' }).enriched, false);
  const stale = singleFileImportItem({ ...entry, enriched: true });
  assert.throws(() => semanticCaptureAction({}, { item: stale }), /inspection/);
  for (const change of [
    { type: 'directory' }, { actual: false }, { type: 'symlink' },
    { delta_id: null }, { path: 'config/options.txt' },
  ]) assert.equal(singleFileImportItem({ ...entry, ...change }), null);
});

test('ordinary payload retains auto-capture without detail or explicit plan calls', async () => {
  assert.equal(singleFileImportItem({ ...entry, path: 'config/options.txt' }), null);
  let applied = false;
  await runAutoCaptureFlow({
    client: {
      autoCapturePath: async () => ({ applied: true, revision: 8 }),
      filePlan: async () => assert.fail('ordinary payload must not plan'),
      deltaItem: async () => assert.fail('ordinary payload must not inspect'),
    },
    path: 'config/options.txt', targetLayer: 'local',
    onApplied: () => { applied = true; },
    onDecisionRequired: () => assert.fail('ordinary payload needs no dialog'),
    onRevisionConflict: () => assert.fail('unexpected conflict'),
  });
  assert.equal(applied, true);
});

test('folder stays outside single import and local choices preserve all atomic expectations', () => {
  assert.equal(singleFileImportItem({ ...entry, type: 'directory', path: 'mods' }), null);
  const items = [
    { id: 'a', path: 'mods/a.jar', capture_modes: ['package', 'payload'], expected_sha256: 'hash-a', provider_candidates: [candidate] },
    { id: 'b', path: 'mods/b.jar', capture_modes: ['package', 'payload'], expected_sha256: 'hash-b', provider_candidates: [candidate] },
  ];
  assert.throws(() => decisionsForPlan(items, { a: { mode: 'package' } }), /decision|Choose/);
  const decisions = decisionsForPlan(items, { a: { mode: 'package' }, b: { mode: 'payload' } });
  assert.deepEqual(decisions, {
    a: { mode: 'package', expected_sha256: 'hash-a' },
    b: { mode: 'payload', expected_sha256: 'hash-b' },
  });
});
