const CAPABILITY_KEYS = [
  'activation',
  'features',
  'capture',
  'package_import',
  'files',
  'sync',
  'shutdown',
];

function asObject(value) {
  return value && typeof value === 'object' && !Array.isArray(value) ? value : {};
}

function asList(value) {
  if (Array.isArray(value)) return value;
  if (value && typeof value === 'object') {
    return Object.entries(value).map(([id, item]) =>
      item && typeof item === 'object' ? { id, ...item } : { id, value: item },
    );
  }
  return [];
}

function text(value, fallback = '') {
  return value === undefined || value === null ? fallback : String(value);
}

function normalizePackage(item, index) {
  const value = asObject(item);
  const source = asObject(value.source);
  const id = text(value.id ?? value.identity ?? value.logical_id, `package-${index + 1}`);
  return {
    ...value,
    id,
    logical_id: text(value.logical_id ?? value.logical_identity, id),
    kind: text(value.kind, 'package'),
    version: text(value.version, 'unversioned'),
    activation: text(value.activation, 'available').toLowerCase(),
    destination: text(value.destination),
    source: {
      ...source,
      provider: text(source.provider ?? value.provider, 'local'),
      location: text(source.location ?? value.location),
      sha256: text(source.sha256 ?? value.sha256),
    },
    source_file: text(value.source_file ?? value.metadata_file ?? value.declaration_source),
    editable: value.editable !== false,
  };
}

function normalizeDeclaration(item, index) {
  const value = asObject(item);
  const data = asObject(value.data);
  return {
    ...value,
    id: text(value.id ?? value.identity, `declaration-${index + 1}`),
    kind: text(value.kind ?? data.type, 'operation'),
    layer: text(value.layer),
    producer: text(value.producer),
    source: text(value.source ?? value.source_file),
    operation_index: Number(value.operation_index ?? value.index ?? index),
    editable: value.editable !== false,
    data,
  };
}

function normalizeGroup(item, index) {
  const value = asObject(item);
  const id = text(value.id ?? value.identity, `group-${index + 1}`);
  return {
    ...value,
    id,
    name: text(value.name, id),
    description: text(value.description),
    activation: text(value.activation, 'available').toLowerCase(),
    members: asList(value.members).map((member) =>
      typeof member === 'string' ? member : text(member.id ?? member.value),
    ),
  };
}

function normalizeSection(item, index) {
  const value = asObject(item);
  const id = text(value.id ?? value.identity, `section-${index + 1}`);
  const members = Array.isArray(value.members) ? value.members.map(String) : [];
  return {
    ...value,
    id,
    title: text(value.title ?? value.name, id),
    members,
    order: Number(value.order ?? index),
  };
}

export function normalizeDeltaItem(item, index = 0) {
  const value = asObject(item);
  const status = text(value.status ?? value.change ?? value.kind, 'changed').toLowerCase();
  const path = text(value.path ?? value.destination);
  return {
    ...value,
    id: text(value.id ?? value.stable_id, `${status}:${path || index}`),
    kind: text(value.kind, 'file'),
    status,
    path,
    package_id: text(value.package_id),
    logical_id: text(value.logical_id),
    from_path: text(value.from_path),
    to_path: text(value.to_path),
    desired_version: text(value.desired_version),
    actual_version: text(value.actual_version),
    jar_metadata: {
      ...asObject(value.jar_metadata),
      loader: text(asObject(value.jar_metadata).loader),
      name: text(asObject(value.jar_metadata).name),
      version: text(asObject(value.jar_metadata).version),
      mod_ids: Array.isArray(asObject(value.jar_metadata).mod_ids)
        ? asObject(value.jar_metadata).mod_ids.map(String)
        : [],
      dependencies: Array.isArray(asObject(value.jar_metadata).dependencies)
        ? asObject(value.jar_metadata).dependencies.map(String)
        : [],
    },
    provider_candidates: asList(value.provider_candidates).map((candidate) => {
      const entry = asObject(candidate);
      return {
        ...entry,
        provider: text(entry.provider),
        project_id: text(entry.project_id),
        version_id: text(entry.version_id),
        version: text(entry.version),
        download_url: text(entry.download_url),
        confidence: text(entry.confidence, 'fuzzy'),
        reason: text(entry.reason),
        hashes: asObject(entry.hashes),
      };
    }),
    editable: value.editable !== false,
    enriched: value.enriched !== false,
    capture_modes: Array.isArray(value.capture_modes) ? value.capture_modes.map(String) : [],
    reason: text(value.reason),
  };
}

function normalizeCapabilities(value) {
  const raw = asObject(value);
  const listed = new Set(Array.isArray(value) ? value.map(String) : []);
  return Object.fromEntries(
    CAPABILITY_KEYS.map((key) => [key, raw[key] === true || listed.has(key)]),
  );
}

export function normalizeBootstrap(payload) {
  const root = asObject(payload);
  const instance = asObject(root.instance);
  const link = asObject(root.link);
  const deltaValue = Array.isArray(root.delta) ? { items: root.delta } : asObject(root.delta);
  const packages = asList(root.packages).map(normalizePackage);
  const declarations = asList(root.declarations).map(normalizeDeclaration);
  const sections = asList(root.sections).map(normalizeSection).sort((a, b) => a.order - b.order);
  const groups = asList(root.groups).map(normalizeGroup);
  const writableLayers = asList(root.writable_layers).map((item, index) => {
    const value = typeof item === 'string' ? { id: item } : asObject(item);
    const id = text(value.id ?? value.name, `layer-${index + 1}`);
    return {
      ...value,
      id,
      path: text(value.path),
      local: value.local === true,
      virtual: value.virtual === true,
    };
  });

  return {
    schema: Number(root.schema ?? 1),
    revision: Number(root.revision ?? 0),
    csrf: text(root.csrf),
    instance: {
      name: text(instance.name, 'Unknown instance'),
      path: text(instance.path),
    },
    link: {
      branch: text(link.branch ?? root.effective_layer, 'unlinked'),
      effective_layer: text(link.effective_layer ?? root.effective_layer ?? link.branch, 'unlinked'),
      features: Array.isArray(link.features) ? link.features.map(String) : [],
    },
    packages,
    declarations,
    groups,
    sections,
    metadata_files: asList(root.metadata_files).map((item, index) => {
      const value = asObject(item);
      return {
        ...value,
        id: text(value.id, `metadata-${index + 1}`),
        layer: text(value.layer),
        path: text(value.path ?? value.source, 'Unknown metadata file'),
        sections: Array.isArray(value.sections) ? value.sections.map(String) : [],
        declarations: Array.isArray(value.declarations) ? value.declarations.map(String) : [],
      };
    }),
    delta: {
      items: asList(deltaValue.items).map(normalizeDeltaItem),
      summary: asObject(deltaValue.summary),
      loaded: deltaValue.loaded !== false,
      complete: deltaValue.complete !== false,
      revision: Number(deltaValue.revision ?? root.revision ?? 0),
    },
    writable_layers: writableLayers,
    feature_candidates: Array.isArray(root.feature_candidates)
      ? root.feature_candidates.map(String)
      : [],
    capabilities: normalizeCapabilities(root.capabilities),
  };
}

export function errorMessage(payload, fallback = 'Request failed') {
  const error = asObject(asObject(payload).error);
  return text(error.message, fallback);
}

export function createRevisionedDeltaLoader(loadDelta) {
  let current = null;
  return {
    load(revision) {
      if (current?.revision !== revision) {
        const promise = Promise.resolve(loadDelta());
        current = { revision, promise };
        void promise.catch(() => {
          if (current?.revision === revision && current.promise === promise) current = null;
        });
      }
      return current.promise;
    },
  };
}

export function createKeyedRequestLoader(loadValue) {
  const requests = new Map();
  return {
    load(key, ...args) {
      if (!requests.has(key)) {
        const promise = Promise.resolve(loadValue(...args));
        requests.set(key, promise);
        void promise.catch(() => {
          if (requests.get(key) === promise) requests.delete(key);
        });
      }
      return requests.get(key);
    },
    invalidate(key) {
      requests.delete(key);
    },
  };
}

export function mergeDeltaForRevision(current, revision, delta) {
  const responseRevision = Number(delta?.revision ?? revision);
  return current?.revision === revision && responseRevision === revision
    ? { ...current, delta }
    : current;
}

export class ApiClient {
  constructor(fetchImpl = fetch) {
    this.fetchImpl = fetchImpl;
    this.csrf = '';
    this.revision = null;
  }

  async request(path, options = {}) {
    const { revision = this.revision, ...requestOptions } = options;
    const headers = new Headers(options.headers || {});
    headers.set('Accept', 'application/json');
    if (options.body !== undefined) headers.set('Content-Type', 'application/json');
    if (this.csrf && options.method && options.method !== 'GET') {
      headers.set('X-Mcpack-CSRF', this.csrf);
      if (revision === null) throw new Error('Bootstrap revision is unavailable');
      headers.set('If-Match', `"${revision}"`);
    }

    const response = await this.fetchImpl(path, {
      ...requestOptions,
      headers,
      credentials: 'same-origin',
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
    });
    const payload = response.status === 204 ? {} : await response.json().catch(() => ({}));
    if (!response.ok) {
      const error = asObject(asObject(payload).error);
      throw new ApiError(
        errorMessage(payload, `${response.status} ${response.statusText}`),
        response.status,
        text(error.code, 'request_failed'),
        asObject(error.details),
      );
    }
    return payload;
  }

  remember(bootstrap) {
    const normalized = normalizeBootstrap(bootstrap);
    this.csrf = normalized.csrf;
    this.revision = normalized.revision;
    return normalized;
  }

  async bootstrap() {
    return this.remember(await this.request('/api/v1/bootstrap'));
  }

  async delta() {
    const payload = await this.request('/api/v1/delta');
    return normalizeBootstrap({ delta: payload }).delta;
  }

  async deltaItem(id, revision) {
    const query = new URLSearchParams({ revision: String(revision) });
    const payload = asObject(await this.request(
      `/api/v1/delta/${encodeURIComponent(id)}?${query}`,
    ));
    return {
      revision: Number(payload.revision ?? 0),
      item: normalizeDeltaItem(payload.item),
    };
  }

  async addFeature(feature) {
    return this.remember(await this.request('/api/v1/features', { method: 'POST', body: { feature } }));
  }

  async removeFeature(feature) {
    return this.remember(await this.request(`/api/v1/features/${encodeURIComponent(feature)}`, { method: 'DELETE' }));
  }

  async setActivation(id, state, targetLayer) {
    const body = { state };
    if (targetLayer) body.target_layer = targetLayer;
    return this.remember(await this.request(`/api/v1/packages/${encodeURIComponent(id)}/activation`, { method: 'PATCH', body }));
  }

  async setGroupActivation(id, state, targetLayer) {
    const body = { state };
    if (targetLayer) body.target_layer = targetLayer;
    return this.remember(await this.request(`/api/v1/groups/${encodeURIComponent(id)}/activation`, { method: 'PATCH', body }));
  }

  async setSectionActivation(id, state, targetLayer) {
    const body = { state };
    if (targetLayer) body.target_layer = targetLayer;
    return this.remember(await this.request(`/api/v1/sections/${encodeURIComponent(id)}/activation`, { method: 'PATCH', body }));
  }

  async capture(deltaId, targetLayer, expectedSha256) {
    const body = {};
    if (targetLayer) body.target_layer = targetLayer;
    if (expectedSha256) body.expected_sha256 = expectedSha256;
    return this.remember(await this.request(`/api/v1/delta/${encodeURIComponent(deltaId)}/capture`, { method: 'POST', body }));
  }

  async files(parent = '', options = {}) {
    const query = new URLSearchParams({
      parent,
      mode: options.mode || 'all',
      show_ignored: options.showIgnored ? 'true' : 'false',
      cursor: String(options.cursor || 0),
      limit: String(options.limit || 200),
    });
    if (options.targetLayer) query.set('target_layer', options.targetLayer);
    return this.request(`/api/v1/files?${query}`);
  }

  async filePlan(path, targetLayer) {
    const query = new URLSearchParams({ path });
    if (targetLayer) query.set('target_layer', targetLayer);
    return this.request(`/api/v1/files/plan?${query}`);
  }

  async autoCapturePath(path, targetLayer) {
    const body = { path, auto_payload: true };
    if (targetLayer) body.target_layer = targetLayer;
    const payload = asObject(await this.request('/api/v1/files/capture', {
      method: 'POST', body,
    }));
    const revision = Number(payload.revision);
    if (!Number.isInteger(revision) || revision < 0) {
      throw new Error('Capture response revision is invalid');
    }
    if (payload.applied !== true && payload.applied !== false) {
      throw new Error('Capture response applied state is invalid');
    }
    const explorerPatch = asObject(payload.explorer_patch);
    if (payload.applied && (
      typeof explorerPatch.path !== 'string'
      || !Number.isInteger(Number(explorerPatch.captured_count))
      || Number(explorerPatch.captured_count) < 0
    )) {
      throw new Error('Capture response explorer patch is invalid');
    }
    this.revision = revision;
    return {
      ok: payload.ok === true,
      applied: payload.applied,
      revision,
      explorer_patch: explorerPatch,
      decision_required: asObject(payload.decision_required),
    };
  }

  async capturePath(path, targetLayer, planRevision, decisions) {
    const body = { path, plan_revision: planRevision, decisions };
    if (targetLayer) body.target_layer = targetLayer;
    return this.remember(await this.request('/api/v1/files/capture', {
      method: 'POST', body, revision: planRevision,
    }));
  }

  async ignorePath(path, kind, targetLayer) {
    const body = { path, kind };
    if (targetLayer) body.target_layer = targetLayer;
    return this.remember(await this.request('/api/v1/files/ignore', {
      method: 'POST', body,
    }));
  }

  async removeIgnore(identity, targetLayer) {
    const body = { identity };
    if (targetLayer) body.target_layer = targetLayer;
    return this.remember(await this.request('/api/v1/files/ignore', {
      method: 'DELETE', body,
    }));
  }

  async importPackage(deltaId, targetLayer, providerMatch, expectedSha256) {
    const body = { delta_id: deltaId };
    if (targetLayer) body.target_layer = targetLayer;
    if (providerMatch) body.provider_match = providerMatch;
    if (expectedSha256) body.expected_sha256 = expectedSha256;
    return this.remember(await this.request('/api/v1/packages/import', { method: 'POST', body }));
  }

  async sync() {
    return this.remember(await this.request('/api/v1/sync', { method: 'POST', body: {} }));
  }

  async shutdown() {
    return this.request('/api/v1/shutdown', { method: 'POST', body: {} });
  }
}

export class ApiError extends Error {
  constructor(message, status, code, details = {}) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

export async function runAutoCaptureFlow({
  client,
  path,
  targetLayer,
  onApplied,
  onDecisionRequired,
  onRevisionConflict,
}) {
  try {
    const result = await client.autoCapturePath(path, targetLayer);
    if (result.applied) {
      await onApplied(result);
      return { state: 'applied', result };
    }
    const plan = await client.filePlan(path, targetLayer);
    await onDecisionRequired(plan, result);
    return { state: 'decision_required', result, plan };
  } catch (error) {
    if (error instanceof ApiError && error.code === 'revision_conflict') {
      await onRevisionConflict(error);
      return { state: 'conflict', error };
    }
    throw error;
  }
}

export async function runRevisionedMutation(client, action) {
  try {
    return { applied: true, value: await action(), conflict: null };
  } catch (error) {
    if (!(error instanceof ApiError) || error.code !== 'revision_conflict') throw error;
    const value = await client.bootstrap();
    return { applied: false, value, conflict: error };
  }
}
