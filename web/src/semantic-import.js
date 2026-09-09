import { normalizeDeltaItem } from './api.js';
import { captureCreatesMetadata } from './routing-model.js';

const PACKAGE_KINDS = new Set(['mod', 'resourcepack', 'shaderpack', 'datapack']);
const PROVIDER_ORDER = ['modrinth', 'curseforge'];

export function isPackageImportItem(entry) {
  if (PACKAGE_KINDS.has(entry.proposed_package_kind)) return true;
  return !entry.proposed_package_kind && entry.path.toLowerCase().endsWith('.jar');
}

export function singleFileImportItem(entry) {
  if (entry.type !== 'file' || !entry.actual || !entry.delta_id ||
      !isPackageImportItem(entry)) return null;
  // Explorer snapshots deliberately omit provider inspection. Never treat their
  // capture modes or copied candidates as the final semantic import decision.
  return normalizeDeltaItem({ ...entry, id: entry.delta_id, enriched: false });
}

export function candidateKey(item) {
  return `${item.provider}\0${item.project_id}\0${item.version_id}`;
}

export function orderedProviderCandidates(item) {
  const candidates = (item.provider_candidates || []).filter(
    (candidate) => candidate.provider !== 'local',
  );
  return [...candidates].sort((left, right) => {
    const leftProvider = PROVIDER_ORDER.indexOf(left.provider);
    const rightProvider = PROVIDER_ORDER.indexOf(right.provider);
    const provider = (leftProvider < 0 ? PROVIDER_ORDER.length : leftProvider)
      - (rightProvider < 0 ? PROVIDER_ORDER.length : rightProvider);
    if (provider) return provider;
    return Number(right.confidence === 'exact') - Number(left.confidence === 'exact');
  });
}

export function defaultProviderKey(item) {
  const candidate = orderedProviderCandidates(item).find(
    (entry) => entry.confidence === 'exact' && PROVIDER_ORDER.includes(entry.provider),
  );
  return candidate ? candidateKey(candidate) : 'local';
}

export function packagePlanItems(items) {
  return items.filter((item) => (item.capture_modes || []).includes('package'));
}

export function preferredFolderChoices(items, provider) {
  const choices = {};
  const unresolved = [];
  for (const item of packagePlanItems(items)) {
    if (provider === 'local') {
      choices[item.id] = { mode: 'package' };
      continue;
    }
    const candidate = (item.provider_candidates || []).find(
      (entry) => entry.provider === provider && entry.confidence === 'exact',
    );
    if (candidate) choices[item.id] = { mode: 'package', provider_match: candidate };
    else unresolved.push(item.id);
  }
  return { choices, unresolved };
}

export function defaultPackageChoice(item) {
  const key = defaultProviderKey(item);
  const candidate = (item.provider_candidates || []).find(
    (entry) => candidateKey(entry) === key,
  );
  return candidate ? { mode: 'package', provider_match: candidate } : { mode: 'package' };
}

export function reconcileFolderChoices(items, choices) {
  const updated = { ...choices };
  for (const item of packagePlanItems(items)) {
    if (!updated[item.id]?.userSelected) updated[item.id] = defaultPackageChoice(item);
  }
  return updated;
}

export function providerLookupPending(value) {
  return value?.provider_lookup === 'pending';
}

export function packageProviderLookupPending(plan) {
  return providerLookupPending(plan)
    || packagePlanItems(plan?.items || []).some(providerLookupPending);
}

export function preferredProviderSelectable(plan, provider) {
  return provider === 'local' || !packageProviderLookupPending(plan);
}

export function previousFolderWizardState(state) {
  if (state.step === 'preferred') return { ...state, step: 'strategy' };
  if (state.step === 'items') {
    return state.index > 0
      ? { ...state, index: state.index - 1 }
      : { ...state, step: state.strategy === 'preferred' ? 'preferred' : 'strategy' };
  }
  if (state.step === 'review') {
    return state.itemIds.length
      ? { ...state, step: 'items', index: state.itemIds.length - 1 }
      : { ...state, step: state.strategy === 'preferred' ? 'preferred' : 'strategy' };
  }
  return state;
}

export function semanticItemReady(item) {
  return item.provider_lookup === 'pending'
    || item.provider_lookup === 'ready'
    || item.provider_lookup === 'failed'
    || item.enriched !== false;
}

export function semanticCaptureAction(client, draft) {
  const { item, target, mode, providerKey, fuzzyConfirmed } = draft;
  if (!semanticItemReady(item)) throw new Error('Wait for item inspection to finish before capturing it');
  if (mode === 'import') {
    const candidate = item.provider_candidates.find((entry) => candidateKey(entry) === providerKey);
    if (candidate?.confidence === 'fuzzy' && !fuzzyConfirmed) {
      throw new Error('Fuzzy provider matches require explicit confirmation');
    }
    return () => client.importPackage(
      item.id, target, candidate, item.actual_sha256, draft.metadataWrite,
    );
  }
  const write = captureCreatesMetadata(draft) ? draft.metadataWrite : undefined;
  return () => client.capture(item.id, target, item.actual_sha256, write);
}
