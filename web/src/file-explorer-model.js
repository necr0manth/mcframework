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
  const pages = [];
  for (const request of requests) pages.push(await loadPage(request));

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

export function decisionsForPlan(items, choices) {
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
