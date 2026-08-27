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

export function decisionsForPlan(items, choices) {
  const decisions = {};
  for (const item of items) {
    if (!(item.capture_modes || []).includes('package')) continue;
    const choice = choices[item.id];
    if (!choice || !choice.mode) throw new Error(`Choose how to capture ${item.path}`);
    decisions[item.id] = { mode: choice.mode };
    if (choice.mode === 'package' && choice.provider_match) {
      if (choice.provider_match.confidence === 'fuzzy' && !choice.fuzzyConfirmed) {
        throw new Error(`Confirm the fuzzy provider match for ${item.path}`);
      }
      decisions[item.id].provider_match = choice.provider_match;
    }
  }
  return decisions;
}
