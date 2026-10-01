import assert from 'node:assert/strict';
import test from 'node:test';
import { ApiClient, normalizeDeltaItem, runAutoCaptureFlow } from '../src/api.js';
import { decisionsForPlan } from '../src/file-explorer-model.js';
import {
  candidateKey,
  defaultPackageChoice,
  defaultProviderKey,
  orderedProviderCandidates,
  preferredFolderChoices,
  preferredProviderReady,
  packageLookupProgress,
  packageLookupIssues,
  packageLookupMessage,
  previousFolderWizardState,
  reconcileFolderChoices,
  semanticCaptureAction,
  semanticItemReady,
  singleFileImportItem,
} from '../src/semantic-import.js';

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
  assert.equal(singleFileImportItem({ ...entry, path: 'mods/mod.jar.disabled' }).enriched, false);
  assert.equal(singleFileImportItem({ ...entry, path: 'mods/MOD.JAR.DISABLED' }).enriched, false);
  const stale = singleFileImportItem({ ...entry, enriched: true });
  assert.throws(() => semanticCaptureAction({}, { item: stale }), /inspection/);
  for (const change of [
    { type: 'directory' }, { actual: false }, { type: 'symlink' },
    { delta_id: null }, { path: 'config/options.txt' }, { path: 'mods/mod.jar.disabled.bak' },
  ]) assert.equal(singleFileImportItem({ ...entry, ...change }), null);
});

test('Files recognizes canonical package kinds while retaining legacy JAR fallback', () => {
  for (const [kind, path] of [
    ['mod', 'mods/sample.jar'],
    ['resourcepack', 'resourcepacks/sample.zip'],
    ['shaderpack', 'shaderpacks/sample.zip'],
    ['datapack', 'datapacks/sample.zip'],
  ]) {
    assert.equal(singleFileImportItem({ ...entry, path, proposed_package_kind: kind }).id, entry.delta_id);
  }
  assert.equal(singleFileImportItem({ ...entry, path: 'resourcepacks/sample.zip' }), null);
  assert.equal(singleFileImportItem({ ...entry, path: 'mods/sample.jar' }).id, entry.delta_id);
});

test('exact source default and presentation order prefer Modrinth, CurseForge, then local', () => {
  const fuzzyModrinth = { ...candidate, version_id: 'mr-fuzzy', confidence: 'fuzzy' };
  const curseforge = { ...candidate, provider: 'curseforge', version_id: 'cf-exact' };
  const modrinth = { ...candidate, version_id: 'mr-exact' };
  const item = { provider_candidates: [
    { ...candidate, provider: 'local', version_id: 'local' },
    curseforge,
    fuzzyModrinth,
    modrinth,
  ] };
  assert.equal(defaultProviderKey(item), candidateKey(modrinth));
  assert.deepEqual(
    orderedProviderCandidates(item).map((value) => `${value.provider}:${value.confidence}`),
    ['modrinth:exact', 'modrinth:fuzzy', 'curseforge:exact'],
  );
  assert.equal(defaultProviderKey({ provider_candidates: [fuzzyModrinth, curseforge] }), candidateKey(curseforge));
  assert.equal(defaultProviderKey({ provider_candidates: [fuzzyModrinth] }), 'local');
});

test('single import sends the selected provider candidate object unchanged and local omits it', async () => {
  const calls = [];
  const client = { importPackage: (...args) => { calls.push(args); } };
  const item = {
    ...normalizeDeltaItem({
      id: 'package', path: 'shaderpacks/sample.zip', status: 'add',
      proposed_package_kind: 'shaderpack', actual_sha256: 'hash',
      provider_lookup: 'ready', provider_candidates: [candidate],
    }),
    provider_candidates: [candidate],
  };
  await semanticCaptureAction(client, {
    item, target: 'client', mode: 'import', providerKey: candidateKey(candidate),
    fuzzyConfirmed: false,
  })();
  assert.equal(calls[0][2], candidate);
  await semanticCaptureAction(client, {
    item, target: 'client', mode: 'import', providerKey: 'local', fuzzyConfirmed: false,
  })();
  assert.equal(calls[1][2], undefined);
});

test('pending and failed lookups keep local capture usable, but uninspected legacy seeds do not', () => {
  assert.equal(semanticItemReady({ provider_lookup: 'pending', enriched: false }), true);
  assert.equal(semanticItemReady({ provider_lookup: 'failed', enriched: false }), true);
  assert.equal(semanticItemReady({ enriched: false }), false);
});

test('preferred folder source applies exact matches and leaves missing or fuzzy matches for review', () => {
  const exact = { ...candidate };
  const fuzzy = { ...candidate, confidence: 'fuzzy', version_id: 'fuzzy' };
  const items = [
    { id: 'exact', capture_modes: ['package', 'payload'], provider_candidates: [exact] },
    { id: 'fuzzy', capture_modes: ['package'], provider_candidates: [fuzzy] },
    { id: 'missing', capture_modes: ['package', 'payload'], provider_candidates: [] },
    { id: 'text', capture_modes: ['payload'], provider_candidates: [] },
  ];
  const selected = preferredFolderChoices(items, 'modrinth');
  assert.equal(selected.choices.exact.provider_match, exact);
  assert.deepEqual(selected.unresolved, ['fuzzy', 'missing']);
  assert.deepEqual(preferredFolderChoices(items, 'local'), {
    choices: {
      exact: { mode: 'package' },
      fuzzy: { mode: 'package' },
      missing: { mode: 'package' },
    },
    unresolved: [],
  });
});

test('remote preference waits for settled sources and refresh preserves explicit local and raw choices', () => {
  const pending = {
    provider_lookup: 'pending',
    items: [
      { id: 'touched', capture_modes: ['package'], provider_lookup: 'pending', provider_candidates: [] },
      { id: 'raw', capture_modes: ['package', 'payload'], provider_lookup: 'pending', provider_candidates: [] },
      { id: 'untouched', capture_modes: ['package'], provider_lookup: 'pending', provider_candidates: [] },
    ],
  };
  assert.equal(preferredProviderReady(pending, 'modrinth'), false);
  assert.equal(preferredProviderReady(pending, 'curseforge'), false);
  assert.equal(preferredProviderReady(pending, 'local'), true);
  const pendingChoices = {
    touched: { ...defaultPackageChoice(pending.items[0]), userSelected: true },
    raw: { mode: 'payload', userSelected: true },
    untouched: defaultPackageChoice(pending.items[2]),
  };
  const ready = {
    provider_lookup: 'ready',
    items: pending.items.map((item, index) => ({
      ...item,
      provider_lookup: 'ready',
      provider_candidates: [{ ...candidate, version_id: `ready-${index}` }],
    })),
  };
  assert.equal(preferredProviderReady(ready, 'modrinth'), true);
  const reconciled = reconcileFolderChoices(ready.items, pendingChoices);
  assert.equal(reconciled.touched.provider_match, undefined, 'manual local remains local');
  assert.equal(reconciled.touched.userSelected, true);
  assert.deepEqual(reconciled.raw, { mode: 'payload', userSelected: true });
  assert.deepEqual(packageLookupProgress(pending), { completed: 0, total: 3 });
  assert.deepEqual(packageLookupProgress(ready), { completed: 3, total: 3 });
  const mixed = { items: [ready.items[0], pending.items[1]] };
  assert.equal(preferredProviderReady(mixed, 'modrinth'), false);
  assert.equal(preferredProviderReady({ items: [{ ...pending.items[0], provider_lookup: 'failed' }] }, 'modrinth'), true);
  assert.match(packageLookupMessage(pending.items[0]), /Still checking/);
  assert.match(packageLookupMessage({ provider_lookup: 'failed' }), /incomplete/);
  assert.match(packageLookupMessage({ provider_lookup: 'ready' }), /No remote matches/);
  assert.equal(packageLookupMessage(ready.items[0]), '');
  assert.equal(reconciled.untouched.provider_match.provider, 'modrinth');
});

test('folder Back preserves strategy, preferred provider, and manual choices', () => {
  const choices = { a: { mode: 'package', userSelected: true } };
  const firstItem = previousFolderWizardState({
    step: 'items', strategy: 'individual', preferredProvider: null,
    choices, itemIds: ['a', 'b'], index: 0,
  });
  assert.equal(firstItem.step, 'strategy');
  assert.equal(firstItem.strategy, 'individual');
  assert.equal(firstItem.choices, choices);
  const preferred = previousFolderWizardState({
    step: 'review', strategy: 'preferred', preferredProvider: 'modrinth',
    choices, itemIds: [], index: 0,
  });
  assert.equal(preferred.step, 'preferred');
  assert.equal(preferred.preferredProvider, 'modrinth');
  assert.equal(preferred.choices, choices);
});

test('delta detail preserves a CurseForge candidate losslessly through the import request', async () => {
  const curseforge = {
    provider: 'curseforge', project_id: '123', version_id: '456', version: null,
    confidence: 'exact', reason: 'SHA-1 match',
    download_url: 'https://example.invalid/cf.jar', hashes: { sha1: 'one', sha256: 'two' },
    kind: 'mod', categories: ['technology'], tags: ['client'],
    file_id: 456, evidence: { fingerprint: 123456789 },
  };
  const requests = [];
  const client = new ApiClient(async (url, options = {}) => {
    requests.push({ url, body: options.body ? JSON.parse(options.body) : undefined });
    if (!options.method) return new Response(JSON.stringify({
      revision,
      item: {
        id: 'cf-package', kind: 'package', status: 'add', path: 'mods/cf.jar',
        actual_sha256: 'two', capture_modes: ['package'], provider_lookup: 'ready',
        provider_candidates: [curseforge],
      },
    }));
    return new Response(JSON.stringify({ revision: revision + 1 }));
  });
  client.revision = revision;
  client.csrf = 'token';
  const detail = await client.deltaItem('cf-package', revision);
  assert.deepEqual(detail.item.provider_candidates[0], curseforge);
  await semanticCaptureAction(client, {
    item: detail.item, target: 'client', mode: 'import',
    providerKey: candidateKey(curseforge), fuzzyConfirmed: false,
  })();
  assert.deepEqual(requests[1].body.provider_match, curseforge);
  assert.equal(requests[1].body.provider_match.version, null);
  assert.deepEqual(requests[1].body.provider_match.evidence, { fingerprint: 123456789 });
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
    { id: 'a', path: 'mods/a.jar', capture_modes: ['package', 'payload'], expected_sha256: 'hash-a', expected_kind: 'file', expected_status: 'changed', provider_candidates: [candidate] },
    { id: 'b', path: 'mods/b.jar', capture_modes: ['package', 'payload'], expected_sha256: 'hash-b', provider_candidates: [candidate] },
  ];
  assert.throws(() => decisionsForPlan(items, { a: { mode: 'package' } }), /decision|Choose/);
  const decisions = decisionsForPlan(items, { a: { mode: 'package' }, b: { mode: 'payload' } });
  assert.deepEqual(decisions, {
    a: { mode: 'package', expected_sha256: 'hash-a', expected_kind: 'file', expected_status: 'changed' },
    b: { mode: 'payload', expected_sha256: 'hash-b' },
  });
});


test('partial lookup diagnostics keep successful matches and distinguish incomplete from absent', () => {
  const error = { provider: 'curseforge', code: 'http_401', message: 'API key is invalid or expired.', retryable: true };
  const matched = { id: 'matched', capture_modes: ['package'], provider_lookup: 'failed', provider_candidates: [candidate], provider_errors: [error] };
  const unmatched = { ...matched, id: 'unmatched', provider_candidates: [] };
  const issues = packageLookupIssues({ items: [matched, unmatched] });
  assert.equal(issues.count, 2);
  assert.deepEqual(issues.errors, [error]);
  assert.equal(issues.retryable, true);
  assert.match(packageLookupMessage(unmatched), /incomplete/);
  assert.doesNotMatch(packageLookupMessage(unmatched), /No remote matches/);
  assert.match(packageLookupMessage(matched), /Available matches/);
  assert.deepEqual(orderedProviderCandidates(matched), [candidate]);
  const preferred = preferredFolderChoices([matched, unmatched], 'modrinth');
  assert.deepEqual(preferred.unresolved, ['unmatched']);
  assert.deepEqual(preferred.choices.matched.provider_match, candidate);
});

test('unconfigured provider is an incomplete lookup warning without pointless retry', () => {
  const item = { capture_modes: ['package'], provider_lookup: 'ready', provider_errors: [
    { provider: 'curseforge', code: 'not_configured', message: 'API key is not configured.', retryable: false },
  ] };
  assert.match(packageLookupMessage(item), /incomplete/);
  assert.equal(packageLookupIssues({ items: [item] }).retryable, false);
  assert.equal(packageLookupIssues({ items: [item] }).count, 1);
  assert.equal(packageLookupIssues({ items: [{ ...item, provider_lookup: 'failed', provider_errors: [] }] }).retryable, true);
  assert.match(packageLookupMessage({ capture_modes: ['package'], provider_lookup: 'ready', provider_errors: [] }), /No remote matches/);
});
