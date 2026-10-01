export function metadataWrite(file, section) {
  const result = {};
  if (file.trim()) result.file = file.trim();
  if (section.trim()) result.section = section.trim();
  return Object.keys(result).length ? result : undefined;
}

export function supportsRoutingSection(operation) {
  return ['package', 'activation', 'group'].includes(operation.type)
    && typeof operation.id === 'string' && operation.id.length > 0;
}

export function packageOwnedBy(item, layers, target) {
  const layer = layers.find((entry) => entry.id === target);
  const normalize = (value) => String(value || '').replaceAll('\\', '/').replace(/\/+$/, '');
  const root = normalize(layer?.path);
  const source = normalize(item.source_file);
  return Boolean(root && source.startsWith(`${root}/`));
}

export function makeRoutingRule({ id, priority, types, packageKind, origin, categories, file, section, pathMode = 'local' }) {
  if (!id.trim() || !file.trim()) throw new Error('Rule ID and destination file are required');
  const value = Number(priority);
  if (!Number.isInteger(value)) throw new Error('Priority must be an integer');
  const list = (text) => text.split(',').map((item) => item.trim()).filter(Boolean);
  const when = {};
  if (list(types).length) when.operation_types = list(types);
  const packageFilter = {};
  if (packageKind) packageFilter.kind = [packageKind];
  if (list(categories).length) packageFilter.categories = list(categories);
  if (Object.keys(packageFilter).length) when.package = packageFilter;
  if (origin) when.origin = [origin];
  const write = { file: { path: file.trim(), path_mode: pathMode } };
  if (section.trim()) write.section = section.trim();
  return { type: 'routing_rule', id: id.trim(), priority: value, when, write };
}

function packageImportPath(path) {
  const disabled = path.toLowerCase().endsWith('.jar.disabled');
  return {
    activation: disabled ? 'disabled' : 'enabled',
    destination: disabled ? path.slice(0, -'.disabled'.length) : path,
  };
}

export function captureRoutingOperation(draft) {
  const { item } = draft;
  if (draft.mode === 'import') {
    const selected = item.provider_candidates?.find((candidate) =>
      `${candidate.provider}\0${candidate.project_id}\0${candidate.version_id}` === draft.providerKey);
    const kind = selected?.kind || item.proposed_package_kind || (item.path.startsWith('resourcepacks/') ? 'resourcepack'
      : item.path.startsWith('datapacks/') ? 'datapack' : 'mod');
    const importedPath = packageImportPath(item.path);
    let source;
    let version;
    if (selected?.provider === 'modrinth' || selected?.provider === 'curseforge') {
      const location = selected.provider === 'modrinth'
        ? (selected.project_slug || selected.project_id)
        : (selected.project_id || selected.project_slug);
      if (location) source = { provider: selected.provider, location };
      version = selected.provider === 'modrinth'
        ? (selected.version || selected.version_id) : selected.version_id;
    } else if (selected?.provider === 'local' || (draft.providerKey && !selected)) {
      const extension = kind === 'mod' ? 'jar' : 'zip';
      if (item.actual_sha256) {
        source = { provider: 'local', location: `.pack/artifacts/${item.actual_sha256}.${extension}`,
          sha256: item.actual_sha256 };
      }
    }
    const folder = kind === 'mod' ? 'mods' : kind === 'resourcepack' ? 'resourcepacks'
      : kind === 'shaderpack' ? 'shaderpacks' : 'datapacks';
    const providerDefaultDestination = selected?.filename
      ? `${folder}/${selected.filename.replaceAll('\\', '/').split('/').at(-1)}` : undefined;
    return { type: 'package', id: item.proposed_package_id || item.package_id || item.logical_id,
      kind, activation: importedPath.activation,
      ...(providerDefaultDestination === importedPath.destination ? {} : { destination: importedPath.destination }),
      ...(source ? { source } : {}),
      ...(version ? { version, update_policy: 'fixed' } : {}),
      ...(selected?.categories ? { categories: selected.categories } : {}),
      ...(selected?.tags ? { tags: selected.tags } : {}) };
  }
  if (item.package_id) {
    return { type: 'activation', id: item.package_id, state: 'disabled' };
  }
  if (item.status === 'remove' || item.status === 'rename') {
    return { type: 'remove', path: item.to_path || item.path };
  }
  return { type: 'put', path: item.path };
}

export function captureCreatesMetadata(draft) {
  return draft.mode === 'import' || captureRoutingOperation(draft).type !== 'put';
}
