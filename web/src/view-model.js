function declarationTarget(declaration) {
  const data = declaration?.data || {};
  return String(data.id ?? data.source ?? data.target ?? '');
}

export function featureSidebarSummary(link) {
  const prefix = link.features.length ? '' : 'No features are active. ';
  return `${prefix}The instance resolves from ${link.effective_layer} on branch ${link.branch}.`;
}

export function packageActivationTarget(item, writableLayers, selectedTarget) {
  const source = String(item.source_file || '').replaceAll('\\', '/');
  const owner = writableLayers.find((layer) => {
    const root = String(layer.path || '').replaceAll('\\', '/').replace(/\/+$/, '');
    return layer.local && layer.virtual && root && source.startsWith(`${root}/`);
  });
  return owner?.id ?? selectedTarget;
}

export function allMembersActive(packages, members) {
  return members.length > 0 && members.every((member) => {
    const item = packages.find(
      (candidate) => candidate.id === member || candidate.logical_id === member,
    );
    return item && (item.activation === 'enabled' || item.activation === 'transitive');
  });
}

export function metadataDeclarationCount(data) {
  return data.declarations.filter((declaration) => declaration.kind !== 'section').length;
}

export function buildMetadataTree(data) {
  const declarationsById = new Map(data.declarations.map((item) => [item.id, item]));
  const sectionsById = new Map(data.sections.map((item) => [item.id, item]));
  const files = data.metadata_files.length
    ? data.metadata_files
    : Array.from(new Set(data.declarations.map((item) => `${item.layer}\0${item.source}`))).map(
        (key, index) => {
          const [layer, path] = key.split('\0');
          return {
            id: `derived-${index}`,
            layer,
            path: path || 'Unknown metadata file',
            sections: [],
            declarations: data.declarations
              .filter((item) => item.layer === layer && item.source === path)
              .map((item) => item.id),
          };
        },
      );

  return files.map((file, fileIndex) => {
    const fileDeclarations = (file.declarations.length
      ? file.declarations.map((id) => declarationsById.get(id)).filter(Boolean)
      : data.declarations.filter(
          (item) => item.source === file.path && (!file.layer || item.layer === file.layer),
        )
    ).filter((declaration) => declaration.kind !== 'section')
      .sort((a, b) => a.operation_index - b.operation_index);
    const relevantSections = (file.sections.length
      ? [...new Set(file.sections)].map((id) => sectionsById.get(id)).filter(Boolean)
      : data.sections.filter((section) =>
          fileDeclarations.some((declaration) => section.members.includes(declarationTarget(declaration))),
        )
    ).sort((a, b) => a.order - b.order);
    const assigned = new Set();
    const sections = relevantSections.map((section) => {
      const declarations = fileDeclarations.filter((declaration) => {
        if (assigned.has(declaration.id)) return false;
        const matches = section.members.includes(declarationTarget(declaration));
        if (matches) assigned.add(declaration.id);
        return matches;
      });
      return { ...section, declarations };
    });
    const remaining = fileDeclarations.filter((declaration) => !assigned.has(declaration.id));
    if (remaining.length) {
      sections.push({
        id: `_unsectioned_${fileIndex}`,
        title: 'Other declarations',
        members: [],
        order: Number.MAX_SAFE_INTEGER,
        declarations: remaining,
      });
    }
    return { ...file, sections };
  });
}

export function deltaPresentation(item) {
  const labels = {
    add: 'Added',
    added: 'Added',
    created: 'Removed locally',
    extra: 'Added',
    remove: 'Removed',
    removed: 'Removed',
    missing: 'Removed',
    change: 'Changed',
    changed: 'Changed',
    modified: 'Changed',
    update: 'Updated',
    updated: 'Updated',
    rename: 'Renamed',
    renamed: 'Renamed',
    ignored: 'Ignored',
  };
  const status = String(item.status || 'change').toLowerCase();
  let tone = 'changed';
  if (['add', 'added', 'extra'].includes(status)) tone = 'added';
  else if (['remove', 'removed', 'missing', 'created'].includes(status)) tone = 'removed';
  else if (['update', 'updated'].includes(status)) tone = 'updated';
  else if (['rename', 'renamed'].includes(status)) tone = 'renamed';
  else if (status === 'ignored') tone = 'muted';

  const title = item.logical_id || item.package_id || item.jar_metadata?.name || item.path ||
    item.to_path || item.from_path || item.id;
  let path = item.path || item.id;
  if (status === 'rename' || status === 'renamed') {
    path = `${item.from_path || '?'} → ${item.to_path || '?'}`;
  }
  let detail = item.reason || `${item.kind} differs from the resolved layer.`;
  if ((status === 'update' || status === 'updated') && (item.actual_version || item.desired_version)) {
    detail = `Local ${item.actual_version || '?'} → desired ${item.desired_version || '?'}`;
  }
  return { label: labels[status] || status.replaceAll('_', ' '), tone, title, path, detail };
}

export function canImportDelta(item) {
  const status = String(item.status || '').toLowerCase();
  if (['remove', 'removed', 'missing', 'created'].includes(status)) return false;
  return (item.capture_modes || []).some(
    (mode) => ['package', 'import', 'package_import', 'provider'].includes(mode),
  ) || item.kind === 'package' || (item.provider_candidates || []).length > 0 ||
    Boolean(item.jar_metadata?.mod_ids?.length);
}

export function paginateDelta(items, limit, pageSize = 200) {
  const rendered = Math.min(items.length, limit);
  return {
    items: items.slice(0, rendered),
    rendered,
    total: items.length,
    nextLimit: Math.min(items.length, rendered + pageSize),
  };
}

export function providerMatchFor(candidate, fuzzyConfirmed) {
  if (!candidate) return undefined;
  if (candidate.confidence === 'fuzzy' && !fuzzyConfirmed) {
    throw new Error('Fuzzy provider matches require explicit confirmation');
  }
  return {
    provider: candidate.provider,
    project_id: candidate.project_id,
    version_id: candidate.version_id,
    version: candidate.version,
    download_url: candidate.download_url,
    hashes: candidate.hashes,
    confidence: candidate.confidence,
    ...(candidate.kind ? { kind: candidate.kind } : {}),
    ...(Array.isArray(candidate.categories) ? { categories: candidate.categories } : {}),
    ...(Array.isArray(candidate.tags) ? { tags: candidate.tags } : {}),
  };
}
