export function mergeExplorerPage(previous, page, append = false) {
  const current = append ? previous[page.parent] || [] : [];
  const byPath = new Map(current.map((item) => [item.path, item]));
  for (const item of page.items || []) byPath.set(item.path, item);
  return { ...previous, [page.parent]: [...byPath.values()] };
}

export function flattenExplorer(children, expanded, parent = '', depth = 0) {
  const result = [];
  for (const item of children[parent] || []) {
    result.push({ ...item, depth });
    if (item.type === 'directory' && expanded.has(item.path)) {
      result.push(...flattenExplorer(children, expanded, item.path, depth + 1));
    }
  }
  return result;
}

export function toggleExplorerDirectory(expanded, path, loaded) {
  const updated = new Set(expanded);
  if (updated.has(path)) {
    updated.delete(path);
    return { expanded: updated, loadParent: null };
  }
  updated.add(path);
  return { expanded: updated, loadParent: loaded ? null : path };
}

export function explorerTreeNavigationDisabled(busy, loading, path) {
  return loading.has(`load:${path}`);
}

export function explorerPathPending(pending, path) {
  return [...pending].some((scope) => insideScope(path, scope) || insideScope(scope, path));
}

export function explorerRefreshRequests(children, expanded, loadedPages) {
  const parents = [''];
  for (const parent of Object.keys(loadedPages || {})) {
    if (parent && expanded.has(parent) && children[parent]) parents.push(parent);
  }
  return parents.flatMap((parent) => {
    const cursors = loadedPages[parent]?.length ? loadedPages[parent] : [0];
    return cursors.map((cursor) => ({ parent, cursor }));
  });
}

export async function refreshExplorerState(state, loadPage) {
  const requests = explorerRefreshRequests(
    state.children,
    state.expanded,
    state.loadedPages,
  );
  const pages = await Promise.all(requests.map((request) => loadPage(request)));

  let children = state.children;
  const next = { ...state.next };
  let rules = state.rules;
  const refreshedParents = new Set();
  for (const page of pages) {
    const append = refreshedParents.has(page.parent);
    children = mergeExplorerPage(children, page, append);
    refreshedParents.add(page.parent);
    next[page.parent] = page.next_cursor;
    if (page.parent === '') rules = page.ignore_rules || [];
  }
  return { ...state, children, next, rules };
}

// A user can open a previously collapsed folder while this refresh is in flight.
// Only replace the parents that the refresh actually requested.
export function mergeExplorerRefresh(current, refreshed, requestedState) {
  const parents = new Set(explorerRefreshRequests(
    requestedState.children, requestedState.expanded, requestedState.loadedPages,
  ).map(({ parent }) => parent));
  const children = { ...current.children };
  const next = { ...current.next };
  for (const parent of parents) {
    const loaded = current.loadedPages[parent] || [];
    const requested = requestedState.loadedPages[parent] || [0];
    // A newly loaded pagination page owns this parent until the next refresh.
    if (loaded.some((cursor) => !requested.includes(cursor))) continue;
    children[parent] = refreshed.children[parent];
    next[parent] = refreshed.next[parent];
  }
  return { ...current, children, next, rules: refreshed.rules };
}

/**
 * @template T
 * @param {Record<string, T[]>} children
 * @param {{path?: string, kind?: string, ignored?: boolean}} patch
 * @returns {Record<string, T[]>}
 */
export function applyExplorerIgnorePatch(children, patch, showIgnored) {
  /** @type {Record<string, T[]>} */
  const updated = {};
  for (const [parent, entries] of Object.entries(children)) {
    const items = entries.map((entry) => (entry.path === patch.path
      || (patch.kind === 'subtree' && insideScope(entry.path, patch.path)))
      ? { ...entry, ignored: patch.ignored }
      : entry);
    updated[parent] = showIgnored ? items : items.filter((entry) => !entry.ignored);
  }
  return updated;
}

function insideScope(path, scope) {
  return path === scope || path.startsWith(`${scope}/`);
}

function ancestorOfScope(path, scope) {
  return scope.startsWith(`${path}/`);
}

export function applyExplorerCapturePatch(state, patch, mode = 'all') {
  const scope = patch.path;
  const capturedCount = Math.max(0, Number(patch.captured_count) || 0);
  const selected = Object.values(state.children)
    .flat()
    .find((entry) => entry.path === scope);
  const displayedCapturedCount = Number.isFinite(selected?.changed_count)
    ? selected.changed_count
    : capturedCount;
  const children = {};
  for (const [parent, entries] of Object.entries(state.children)) {
    const updated = entries.map((entry) => {
      if (insideScope(entry.path, scope)) {
        return {
          ...entry,
          desired: true,
          synthetic: false,
          status: 'unchanged',
          changed: false,
          changed_count: 0,
          visible_changed_count: 0,
        };
      }
      if (ancestorOfScope(entry.path, scope)) {
        const changedCount = Math.max(
          0,
          (entry.changed_count || 0) - displayedCapturedCount,
        );
        return {
          ...entry,
          changed_count: changedCount,
          visible_changed_count: Math.max(
            0,
            (entry.visible_changed_count ?? entry.changed_count ?? 0) - displayedCapturedCount,
          ),
          changed: changedCount > 0 || entry.status !== 'unchanged',
        };
      }
      return entry;
    });
    children[parent] = mode === 'changed-only'
      ? updated.filter((entry) => entry.changed || entry.changed_count > 0)
      : updated;
  }
  return { ...state, children };
}

/**
 * @typedef {Object} CaptureChoice
 * @property {string} mode
 * @property {Record<string, unknown>} [provider_match]
 * @property {boolean} [fuzzyConfirmed]
 *
 * @typedef {Object} CapturePlanItem
 * @property {string} id
 * @property {string} path
 * @property {string[]} capture_modes
 * @property {string} [expected_sha256]
 * @property {boolean} [expected_absent]
 * @property {string} [expected_kind]
 * @property {string} [expected_status]
 *
 * @typedef {{ mode: string, provider_match?: Record<string, unknown>, expected_kind?: string, expected_status?: string } & (
 *   { expected_sha256: string, expected_absent?: never } |
 *   { expected_absent: true, expected_sha256?: never }
 * )} CaptureDecision
 * @typedef {Record<string, CaptureDecision>} CaptureDecisions
 */

/**
 * @param {CapturePlanItem[]} items
 * @param {Record<string, CaptureChoice>} choices
 * @returns {CaptureDecisions}
 */
export function decisionsForPlan(items, choices) {
  /** @type {CaptureDecisions} */
  const decisions = {};
  for (const item of items) {
    const modes = item.capture_modes || [];
    const choice = choices[item.id];
    const mode = modes.length === 1 ? modes[0] : choice?.mode;
    if (!mode || !modes.includes(mode)) throw new Error(`Choose how to capture ${item.path}`);
    const hasHash = typeof item.expected_sha256 === 'string' && item.expected_sha256.length > 0;
    const expectsAbsent = item.expected_absent === true;
    if (hasHash === expectsAbsent) {
      throw new Error(`Capture plan is missing a source expectation for ${item.path}`);
    }
    decisions[item.id] = {
      mode,
      ...(hasHash ? { expected_sha256: item.expected_sha256 } : { expected_absent: true }),
      ...(typeof item.expected_kind === 'string' && item.expected_kind
        ? { expected_kind: item.expected_kind }
        : {}),
      ...(typeof item.expected_status === 'string' && item.expected_status
        ? { expected_status: item.expected_status }
        : {}),
    };
    if (mode === 'package' && choice?.provider_match) {
      if (choice.provider_match.confidence === 'fuzzy' && !choice.fuzzyConfirmed) {
        throw new Error(`Confirm the fuzzy provider match for ${item.path}`);
      }
      decisions[item.id].provider_match = choice.provider_match;
    }
  }
  return decisions;
}
