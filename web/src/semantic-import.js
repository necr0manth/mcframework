import { normalizeDeltaItem } from './api.js';
import { providerMatchFor } from './view-model.js';

export function singleFileImportItem(entry) {
  if (entry.type !== 'file' || !entry.actual || !entry.delta_id ||
      !entry.path.toLowerCase().endsWith('.jar')) return null;
  // Explorer snapshots deliberately omit provider inspection. Never treat their
  // capture modes or copied candidates as the final semantic import decision.
  return normalizeDeltaItem({ ...entry, id: entry.delta_id, enriched: false });
}

export function candidateKey(item) {
  return `${item.provider}\0${item.project_id}\0${item.version_id}`;
}

export function semanticCaptureAction(client, draft) {
  const { item, target, mode, providerKey, fuzzyConfirmed } = draft;
  if (!item.enriched) throw new Error('Wait for item inspection to finish before capturing it');
  if (mode === 'import') {
    const candidate = item.provider_candidates.find((entry) => candidateKey(entry) === providerKey);
    const match = providerMatchFor(candidate, fuzzyConfirmed);
    return () => client.importPackage(item.id, target, match, item.actual_sha256);
  }
  return () => client.capture(item.id, target, item.actual_sha256);
}
