import { useEffect, useMemo, useRef, useState } from 'react';
import { ApiClient, ApiError, createKeyedRequestLoader, createRevisionedDeltaLoader, mergeDeltaForRevision, recoverDeltaRevisionConflict, runRevisionedMutation } from './api.js';
import FileExplorer from './FileExplorer.js';
import { RoutingRuleEditor, WriteDestination } from './RoutingControls.js';
import type { MetadataWrite } from './RoutingControls.js';
import { captureCreatesMetadata, captureRoutingOperation, packageOwnedBy } from './routing-model.js';
import { candidateKey, defaultProviderKey, orderedProviderCandidates, providerLookupPending, semanticCaptureAction, semanticItemReady } from './semantic-import.js';
import { allMembersActive, buildMetadataTree, canImportDelta, deltaPresentation, featureSidebarSummary, metadataDeclarationCount, packageActivationTarget, paginateDelta } from './view-model.js';

type View = 'pack' | 'delta' | 'files';
type Activation = 'available' | 'enabled' | 'disabled' | 'transitive' | string;
const DELTA_PAGE_SIZE = 200;

type PackageItem = {
  id: string;
  logical_id: string;
  proposed_package_id?: string;
  proposed_package_kind?: string;
  kind: string;
  version: string;
  activation: Activation;
  activation_reason?: string;
  unsatisfied_dependencies?: string[];
  destination: string;
  source: { provider: string; location: string; sha256: string };
  source_file: string;
  editable: boolean;
};

type Declaration = {
  id: string;
  kind: string;
  layer: string;
  producer: string;
  source: string;
  operation_index: number;
  editable: boolean;
  data: Record<string, unknown>;
};

type ProviderCandidate = {
  provider: string;
  project_id: string;
  version_id: string;
  version: string | null;
  download_url: string;
  confidence: 'exact' | 'fuzzy' | string;
  reason: string;
  hashes: Record<string, unknown>;
  kind?: string;
  categories?: string[];
  tags?: string[];
};

type DeltaItem = {
  id: string;
  kind: string;
  status: string;
  path: string;
  from_path: string;
  to_path: string;
  package_id: string;
  logical_id: string;
  desired_version: string;
  actual_version: string;
  editable: boolean;
  enriched: boolean;
  provider_lookup?: 'pending' | 'ready' | 'failed';
  proposed_package_kind?: 'mod' | 'resourcepack' | 'shaderpack' | 'datapack';
  capture_modes: string[];
  reason: string;
  desired_sha256?: string;
  actual_sha256?: string;
  jar_metadata?: {
    loader: string;
    mod_ids: string[];
    name: string;
    version: string;
    dependencies: string[];
  };
  provider_candidates: ProviderCandidate[];
};

type Layer = { id: string; path: string; local: boolean; virtual: boolean };
type Section = { id: string; title: string; members: string[]; order: number };
type Group = { id: string; name: string; description: string; activation: Activation; members: string[] };
type MetadataFile = { id: string; layer: string; path: string; sections: string[]; declarations: string[] };

type Bootstrap = {
  revision: number;
  csrf: string;
  instance: { name: string; path: string };
  link: { branch: string; effective_layer: string; features: string[] };
  packages: PackageItem[];
  declarations: Declaration[];
  groups: Group[];
  sections: Section[];
  metadata_files: MetadataFile[];
  delta: { items: DeltaItem[]; summary: Record<string, unknown>; loaded: boolean; complete: boolean; revision: number };
  writable_layers: Layer[];
  feature_candidates: string[];
  capabilities: Record<string, boolean>;
};

type MetadataTree = Omit<MetadataFile, 'sections'> & {
  sections: Array<Section & { declarations: Declaration[] }>;
};

type Selected =
  | { type: 'package'; value: PackageItem }
  | { type: 'declaration'; value: Declaration };

type CaptureDraft = {
  item: DeltaItem;
  revision: number;
  target: string;
  mode: 'capture' | 'import';
  providerKey: string;
  fuzzyConfirmed: boolean;
  sourceTouched: boolean;
  modeTouched: boolean;
  metadataWrite?: MetadataWrite;
};

const client = new ApiClient(window.fetch.bind(window));

function active(state: Activation) {
  return state === 'enabled' || state === 'transitive';
}

function declarationTarget(item: Declaration) {
  return String(item.data.id ?? item.data.source ?? item.data.target ?? '');
}

function Empty({ title, children }: { title: string; children: React.ReactNode }) {
  return <div className="empty"><span aria-hidden="true">◇</span><h2>{title}</h2><p>{children}</p></div>;
}

function Badge({ state }: { state: string }) {
  return <span className={`badge activation-${state}`}>{state}</span>;
}

function Toggle({ checked, disabled, label, onChange }: {
  checked: boolean;
  disabled?: boolean;
  label: string;
  onChange: () => void;
}) {
  return (
    <button type="button" className={`switch ${checked ? 'on' : ''}`} role="switch"
      aria-checked={checked} aria-label={label} disabled={disabled} onClick={onChange}>
      <span />
    </button>
  );
}

export default function App() {
  const deltaLoader = useRef(createRevisionedDeltaLoader(() => client.delta())).current;
  const detailLoader = useRef(createKeyedRequestLoader(
    (id: string, revision: number) => client.deltaItem(id, revision),
  )).current;
  const detailKey = useRef('');
  const [data, setData] = useState<Bootstrap | null>(null);
  const [view, setView] = useState<View>('pack');
  const [selected, setSelected] = useState<Selected | null>(null);
  const [targetLayer, setTargetLayer] = useState('');
  const [routingDialog, setRoutingDialog] = useState(false);
  const [packageEdit, setPackageEdit] = useState<{
    item: PackageItem; target: string; mode: string; metadataWrite?: MetadataWrite;
  } | null>(null);
  const [captureDraft, setCaptureDraft] = useState<CaptureDraft | null>(null);
  const [captureError, setCaptureError] = useState('');
  const [capturePollError, setCapturePollError] = useState('');
  const [detailBusy, setDetailBusy] = useState('');
  const [featureDialog, setFeatureDialog] = useState(false);
  const [featureChoice, setFeatureChoice] = useState('');
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [stopped, setStopped] = useState(false);
  const [deltaPageState, setDeltaPageState] = useState({ key: '', limit: DELTA_PAGE_SIZE });
  const [compositionStaleRevision, setCompositionStaleRevision] = useState<number | null>(null);

  function applyBootstrap(next: Bootstrap) {
    setData(next);
    setCompositionStaleRevision(null);
    if (!next.writable_layers.some((layer) => layer.id === targetLayer)) {
      setTargetLayer(next.writable_layers[0]?.id || '');
    }
    if (selected?.type === 'package') {
      const replacement = next.packages.find((item) => item.id === selected.value.id);
      if (replacement) setSelected({ type: 'package', value: replacement });
    } else if (selected?.type === 'declaration') {
      const replacement = next.declarations.find((item) => item.id === selected.value.id);
      if (replacement) setSelected({ type: 'declaration', value: replacement });
    } else {
      setSelected(next.packages[0]
        ? { type: 'package', value: next.packages[0] }
        : next.declarations[0]
          ? { type: 'declaration', value: next.declarations[0] }
          : null);
    }
  }

  useEffect(() => {
    let live = true;
    client.bootstrap().then((next: Bootstrap) => {
      if (!live) return;
      setData(next);
      setTargetLayer(next.writable_layers[0]?.id || '');
      setSelected(next.packages[0]
        ? { type: 'package', value: next.packages[0] }
        : next.declarations[0]
          ? { type: 'declaration', value: next.declarations[0] }
          : null);
    }).catch((reason: Error) => live && setError(reason.message));
    return () => { live = false; };
  }, []);

  useEffect(() => () => { detailKey.current = ''; }, []);

  useEffect(() => {
    detailKey.current = '';
    setDetailBusy('');
    setCaptureDraft((current) => current && current.revision !== data?.revision ? null : current);
  }, [data?.revision]);

  useEffect(() => {
    if (view !== 'delta' || !data || data.delta.loaded) return;
    let live = true;
    const revision = data.revision;
    deltaLoader.load(revision).then((delta: Bootstrap['delta']) => {
      if (!live) return;
      setData((current) => mergeDeltaForRevision(current, revision, delta));
    }).catch(async (reason: Error) => {
      if (!live) return;
      if (await recoverDeltaRevisionConflict(reason, refreshAfterConflict)) return;
      setError(reason.message);
    });
    return () => { live = false; };
  }, [view, data?.delta.loaded, data?.revision, deltaLoader]);

  useEffect(() => {
    if (view !== 'pack' || compositionStaleRevision === null) return;
    let live = true;
    setBusy('refresh:pack');
    client.bootstrap().then((next: Bootstrap) => {
      if (!live) return;
      applyBootstrap(next);
    }).catch((reason: Error) => live && setError(reason.message))
      .finally(() => live && setBusy(''));
    return () => { live = false; };
  }, [view, compositionStaleRevision]);

  function applyCompactMutation(revision: number, success: string) {
    setData((current) => current ? {
      ...current,
      revision,
      delta: {
        ...current.delta,
        loaded: false,
        complete: false,
        revision,
      },
    } : current);
    setCompositionStaleRevision(revision);
    setError('');
    setNotice(success);
  }

  const metadataTree = useMemo(
    () => data ? buildMetadataTree(data) as MetadataTree[] : [],
    [data],
  );

  async function refreshAfterConflict(reason: ApiError) {
    try {
      const next = await client.bootstrap() as Bootstrap;
      applyBootstrap(next);
      const actual = (reason.details as Record<string, unknown>).actual;
      setError(`Another change was saved${actual === undefined ? '' : ` (revision ${actual})`}. The view was refreshed; review and try again.`);
    } catch (refreshReason) {
      setError(refreshReason instanceof Error ? refreshReason.message : 'The view is stale and refresh failed');
    }
  }

  async function act(label: string, action: () => Promise<Bootstrap>, success: string) {
    setBusy(label);
    setError('');
    setNotice('');
    try {
      const outcome = await runRevisionedMutation(client, action);
      applyBootstrap(outcome.value as Bootstrap);
      if (!outcome.applied) {
        const actual = (outcome.conflict?.details as Record<string, unknown> | undefined)?.actual;
        setError(`Another change was saved${actual === undefined ? '' : ` (revision ${actual})`}. The view was refreshed; review and try again.`);
        return false;
      }
      setNotice(success);
      return true;
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Request failed');
      return false;
    } finally {
      setBusy('');
    }
  }

  async function retry() {
    setError('');
    setBusy('bootstrap');
    try {
      applyBootstrap(await client.bootstrap() as Bootstrap);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Request failed');
    } finally {
      setBusy('');
    }
  }

  function togglePackage(item: PackageItem) {
    const state = active(item.activation) ? 'disabled' : 'enabled';
    const target = packageActivationTarget(item, data?.writable_layers || [], targetLayer);
    void act(
      `activation:${item.id}`,
      () => client.setActivation(item.id, state, target),
      `${item.id} is now ${state}.`,
    );
  }

  async function submitPackageEdit() {
    if (!packageEdit) return;
    const { item, mode, target, metadataWrite } = packageEdit;
    const changed = await act(
      `package:${item.id}`,
      () => mode === 'enabled'
        ? client.setActivation(item.id, 'enabled', target, metadataWrite)
        : client.removePackage(item.id, mode, target, metadataWrite),
      mode === 'remove' ? `${item.id} declaration removed.` : `${item.id} updated.`,
    );
    if (changed) { setPackageEdit(null); if (mode === 'remove') setSelected(null); }
  }

  function toggleGroup(item: Group) {
    const state = active(item.activation) ? 'disabled' : 'enabled';
    void act(
      `group:${item.id}`,
      () => client.setGroupActivation(item.id, state, targetLayer),
      `${item.name} group is now ${state}.`,
    );
  }

  function toggleSection(item: Section) {
    if (!data) return;
    const state = allMembersActive(data.packages, item.members) ? 'disabled' : 'enabled';
    void act(
      `section:${item.id}`,
      () => client.setSectionActivation(item.id, state, targetLayer),
      `${item.title} section was updated atomically.`,
    );
  }

  async function openCapture(item: DeltaItem, requestedRevision?: number) {
    if (!data) return;
    const revision = requestedRevision ?? data.revision;
    const requestKey = `${revision}:${item.id}`;
    const canImport = canImportDelta(item) && data?.capabilities.package_import;
    detailKey.current = requestKey;
    setDetailBusy(requestKey);
    setCaptureError('');
    setCapturePollError('');
    setCaptureDraft({
      item,
      revision,
      target: targetLayer,
      mode: canImport ? 'import' : 'capture',
      providerKey: 'local',
      fuzzyConfirmed: false,
      sourceTouched: false,
      modeTouched: false,
    });
    try {
      const detail = await detailLoader.load(requestKey, item.id, revision) as {
        revision: number;
        item: DeltaItem;
      };
      if (!applyCaptureDetail(requestKey, revision, detail)) return;
      if (providerLookupPending(detail.item)) void pollCaptureDetail(requestKey, item.id, revision);
    } catch (reason) {
      if (reason instanceof ApiError && reason.code === 'revision_conflict') {
        if (detailKey.current === requestKey) closeCapture();
        await refreshAfterConflict(reason);
        return;
      }
      if (detailKey.current === requestKey && client.revision === revision) {
        setCapturePollError(reason instanceof Error ? reason.message : 'Could not inspect this item');
      }
    } finally {
      setDetailBusy((current) => current === requestKey ? '' : current);
    }
  }

  function applyCaptureDetail(
    requestKey: string,
    revision: number,
    detail: { revision: number; item: DeltaItem },
  ) {
    if (
      detailKey.current !== requestKey
      || detail.revision !== revision
      || client.revision !== revision
    ) return false;
    setData((current) => {
      if (current?.revision !== revision) return current;
      return {
        ...current,
        delta: {
          ...current.delta,
          items: current.delta.items.map((entry) =>
            entry.id === detail.item.id ? detail.item : entry
          ),
        },
      };
    });
    setCaptureDraft((current) => {
      if (current?.revision !== revision || current.item.id !== detail.item.id) return current;
      const detailCanImport = canImportDelta(detail.item) && data?.capabilities.package_import;
      const selected = current.item.provider_candidates.find(
        (candidate) => candidateKey(candidate) === current.providerKey,
      );
      const selectedStillPresent = detail.item.provider_candidates.some(
        (candidate) => candidateKey(candidate) === current.providerKey,
      );
      const nextItem = current.sourceTouched && selected && !selectedStillPresent
        ? { ...detail.item, provider_candidates: [...detail.item.provider_candidates, selected] }
        : detail.item;
      return {
        ...current,
        item: nextItem,
        mode: current.modeTouched ? current.mode : detailCanImport ? 'import' : 'capture',
        providerKey: current.sourceTouched ? current.providerKey : defaultProviderKey(detail.item),
      };
    });
    return true;
  }

  async function pollCaptureDetail(requestKey: string, id: string, revision: number) {
    while (detailKey.current === requestKey && client.revision === revision) {
      await new Promise((resolve) => window.setTimeout(resolve, 500));
      if (detailKey.current !== requestKey || client.revision !== revision) return;
      try {
        detailLoader.invalidate(requestKey);
        const detail = await detailLoader.load(requestKey, id, revision) as {
          revision: number;
          item: DeltaItem;
        };
        if (!applyCaptureDetail(requestKey, revision, detail)) return;
        if (!providerLookupPending(detail.item)) return;
      } catch (reason) {
        if (reason instanceof ApiError && reason.code === 'revision_conflict') {
          if (detailKey.current === requestKey) closeCapture();
          await refreshAfterConflict(reason);
          return;
        }
        if (detailKey.current === requestKey) {
          setCapturePollError(reason instanceof Error ? reason.message : 'Could not refresh package sources');
        }
        return;
      }
    }
  }

  async function retryCaptureSources() {
    const draft = captureDraft;
    if (!draft) return;
    const requestKey = `${draft.revision}:${draft.item.id}`;
    detailKey.current = requestKey;
    setCapturePollError('');
    try {
      await client.retryProviderLookups();
      if (detailKey.current !== requestKey || client.revision !== draft.revision) return;
      detailLoader.invalidate(requestKey);
      const detail = await detailLoader.load(requestKey, draft.item.id, draft.revision) as {
        revision: number;
        item: DeltaItem;
      };
      if (!applyCaptureDetail(requestKey, draft.revision, detail)) return;
      if (providerLookupPending(detail.item)) {
        void pollCaptureDetail(requestKey, draft.item.id, draft.revision);
      }
    } catch (reason) {
      if (detailKey.current === requestKey) {
        setCapturePollError(reason instanceof Error ? reason.message : 'Could not retry package sources');
      }
    }
  }

  function closeCapture() {
    detailKey.current = '';
    setDetailBusy('');
    setCaptureDraft(null);
  }

  async function submitCapture() {
    if (!captureDraft) return;
    const { item, mode } = captureDraft;
    try {
      const action = semanticCaptureAction(client, captureDraft);
      detailLoader.invalidate(`${captureDraft.revision}:${item.id}`);
      closeCapture();
      await act(
        `delta:${item.id}`,
        action,
        mode === 'import'
          ? `${item.logical_id || item.path || item.id} was imported.`
          : `${item.path || item.id} was captured.`,
      );
    } catch (reason) {
      setCaptureError(reason instanceof Error ? reason.message : 'Choose a verified provider match');
    }
  }

  async function addFeature() {
    if (!featureChoice) return;
    const feature = featureChoice;
    setFeatureDialog(false);
    await act(
      `feature:${feature}`,
      () => client.addFeature(feature),
      `Feature ${feature} was added.`,
    );
  }

  async function stopServer() {
    if (!window.confirm('Stop the local web server for this instance?')) return;
    setBusy('shutdown');
    try {
      await client.shutdown();
      setStopped(true);
    } catch (reason) {
      if (reason instanceof ApiError && reason.code === 'revision_conflict') {
        await refreshAfterConflict(reason);
      } else {
        setError(reason instanceof Error ? reason.message : 'Could not stop the server');
      }
    } finally {
      setBusy('');
    }
  }

  if (stopped) {
    return <main className="center-state"><div className="state-card"><span className="brand-symbol">M</span><h1>Web server stopped</h1><p>This page is no longer connected. Start it again with <code>mcpack web {data?.instance.name || '&lt;instance&gt;'}</code>.</p></div></main>;
  }

  if (!data) {
    return <main className="center-state"><div className="state-card"><span className="brand-symbol">M</span>{busy === 'bootstrap' || !error ? <><div className="spinner" /><h1>Loading instance</h1><p>Resolving packages and comparing local files…</p></> : <><h1>Could not load the instance</h1><p>{error}</p><button className="primary" onClick={retry}>Retry</button></>}</div></main>;
  }

  const readOnly = !data.capabilities.activation && !data.capabilities.features &&
    !data.capabilities.capture && !data.capabilities.package_import;
  const remainingFeatures = data.feature_candidates.filter(
    (feature) => !data.link.features.includes(feature),
  );
  const activePackages = data.packages.filter((item) => active(item.activation)).length;
  const selectedCandidate = captureDraft?.item.provider_candidates.find(
    (candidate) => candidateKey(candidate) === captureDraft.providerKey,
  );
  const deltaPageKey = `${data.revision}:${data.delta.loaded}:${data.delta.items.length}`;
  const deltaLimit = deltaPageState.key === deltaPageKey
    ? deltaPageState.limit
    : DELTA_PAGE_SIZE;
  const deltaPage = paginateDelta(data.delta.items, deltaLimit, DELTA_PAGE_SIZE);

  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="brand"><span className="brand-symbol">M</span><div><strong>Minecraft Pack Framework</strong><small>Instance workspace</small></div></div>
        <div className="instance-context"><span className="live-dot" /><div><small>{data.instance.name} · revision {data.revision}</small><strong>{data.link.effective_layer}</strong></div></div>
        <div className="top-actions">
          {data.capabilities.sync && <button className="secondary" disabled={!!busy} onClick={() => void act('sync', () => client.sync(), 'Instance synchronized.')}>{busy === 'sync' ? 'Syncing…' : 'Sync instance'}</button>}
          {data.capabilities.shutdown && <button className="quiet" disabled={!!busy} onClick={() => void stopServer()}>Stop web</button>}
        </div>
      </header>

      <aside className="sidebar">
        <nav aria-label="Instance views">
          <p className="eyebrow">Workspace</p>
          <button className={view === 'pack' ? 'nav-item active' : 'nav-item'} aria-current={view === 'pack' ? 'page' : undefined} onClick={() => setView('pack')}><span>▦</span><b>Pack</b><em>{data.packages.length}</em></button>
          <button className={view === 'delta' ? 'nav-item active' : 'nav-item'} aria-current={view === 'delta' ? 'page' : undefined} onClick={() => setView('delta')}><span>≠</span><b>Instance delta</b><em>{data.delta.loaded ? data.delta.items.length : '…'}</em></button>
          <button className={view === 'files' ? 'nav-item active' : 'nav-item'} aria-current={view === 'files' ? 'page' : undefined} onClick={() => setView('files')}><span>⌘</span><b>Files</b><em>{data.delta.loaded ? data.delta.items.length : '…'}</em></button>
        </nav>

        <section className="feature-panel">
          <div className="panel-title"><div><p className="eyebrow">Temporary layer</p><h2>Features</h2></div>{data.capabilities.features && remainingFeatures.length > 0 && <button aria-label="Add feature" onClick={() => { setFeatureChoice(remainingFeatures[0] || ''); setFeatureDialog(true); }}>+</button>}</div>
          {data.link.features.length > 0 && <ul>{data.link.features.map((feature) => <li key={feature}><span>{feature}</span>{data.capabilities.features && <button disabled={!!busy} aria-label={`Remove ${feature}`} onClick={() => void act(`feature:${feature}`, () => client.removeFeature(feature), `Feature ${feature} was removed.`)}>×</button>}</li>)}</ul>}
          <p className="muted-copy">{featureSidebarSummary(data.link)}</p>
        </section>

        <div className="path-card"><p className="eyebrow">Minecraft directory</p><code title={data.instance.path}>{data.instance.path || 'Path unavailable'}</code></div>
      </aside>

      <main className="workspace">
        {(error || notice) && <div className={`banner ${error ? 'error' : 'success'}`} role={error ? 'alert' : 'status'}><span>{error ? '!' : '✓'}</span><p>{error || notice}</p><button aria-label="Dismiss" onClick={() => { setError(''); setNotice(''); }}>×</button></div>}
        {readOnly && <div className="banner warning"><span>i</span><p><strong>Read-only session.</strong> This server did not grant editing capabilities.</p></div>}

        {view === 'pack' ? <>
          <div className="page-heading"><div><p className="eyebrow">Resolved composition</p><h1>Pack structure</h1><p>Metadata files, semantic sections and declarations in resolver order.</p></div><div className="summary"><strong>{activePackages}</strong><span>active</span><i /><strong>{metadataDeclarationCount(data)}</strong><span>declarations</span></div></div>

          <div className="routing-toolbar"><TargetSelect layers={data.writable_layers} value={targetLayer} onChange={setTargetLayer} /><button className="secondary" disabled={!!busy} onClick={() => setRoutingDialog(true)}>Write routing rules</button></div>
          {data.groups.length > 0 && <section className="groups" aria-label="Package groups">{data.groups.map((group) => <article key={group.id}><span className="group-icon">G</span><div><small>Package group</small><strong>{group.name}</strong><p>{group.description || group.id}</p></div><Badge state={group.activation} /><Toggle checked={active(group.activation)} disabled={!!busy || !data.capabilities.activation} label={`Toggle ${group.name} group`} onChange={() => toggleGroup(group)} /></article>)}</section>}

          <div className="pack-grid">
            <div className="content-stack">
              {metadataTree.length === 0 ? <Empty title="No logical declarations">Add metadata to the linked layer, then reload this page.</Empty> : metadataTree.map((file) => <section className="metadata-file" key={`${file.layer}:${file.path}`}><header><div><p className="eyebrow">Metadata file</p><h2>{file.path}</h2></div><span>{file.layer || 'unknown layer'} · {file.sections.reduce((count, section) => count + section.declarations.length, 0)} declarations</span></header>{file.sections.map((section) => {
                const sectionChecked = allMembersActive(
                  [...data.packages, ...data.groups],
                  section.members,
                );
                const canToggleSection = !section.id.startsWith('_unsectioned') && section.members.length > 0;
                return <div className="metadata-section" key={section.id}><div className="metadata-section-head"><div><p className="eyebrow">Section</p><h3>{section.title}</h3></div><span>{section.declarations.length} operations · ordered</span>{canToggleSection && <Toggle checked={sectionChecked} disabled={!!busy || !data.capabilities.activation} label={`Toggle ${section.title} section atomically`} onChange={() => toggleSection(section)} />}</div><div className="ordered-declarations">{section.declarations.map((declaration) => {
                  const target = declarationTarget(declaration);
                  const packageItem = data.packages.find((item) => item.id === target || item.logical_id === target);
                  return <DeclarationRow key={declaration.id} declaration={declaration} packageItem={packageItem} selected={selected} busy={busy} activationAvailable={data.capabilities.activation} onSelect={setSelected} onToggle={togglePackage} />;
                })}</div></div>;
              })}</section>)}
            </div>

            <aside className="details" aria-label="Selection details">
              {selected?.type === 'package' && <button className="secondary" disabled={!!busy || !data.capabilities.activation} onClick={() => setPackageEdit({ item: selected.value, target: targetLayer, mode: 'disable' })}>Edit package / remove</button>}
              {selected?.type === 'package' ? <PackageDetails item={selected.value} /> : selected?.type === 'declaration' ? <DeclarationDetails item={selected.value} /> : <Empty title="Nothing selected">Choose a declaration to inspect its provenance.</Empty>}
            </aside>
          </div>
        </> : view === 'delta' ? <>
          <div className="page-heading delta-heading"><div><p className="eyebrow">Semantic comparison</p><h1>Instance delta</h1><p>Package-aware changes, file changes and detected renames against the resolved layer.</p></div><TargetSelect layers={data.writable_layers} value={targetLayer} onChange={setTargetLayer} /></div>

          <section className="delta-summary">{['add', 'remove', 'change', 'update', 'rename'].map((key) => <div key={key}><strong>{data.delta.loaded ? !data.delta.complete && ['change', 'update', 'rename'].includes(key) ? '?' : String(data.delta.summary[key] ?? data.delta.items.filter((item) => item.status === key).length) : '…'}</strong><span>{key}</span></div>)}</section>

          {!data.delta.loaded ? <Empty title="Loading instance delta">Inspecting local files and package metadata…</Empty> : data.delta.items.length ? <div className="delta-list"><p className="muted-copy" role="status">Showing {deltaPage.rendered} of {deltaPage.total} changes.</p>{deltaPage.items.map((item: DeltaItem) => {
            const presentation = deltaPresentation(item);
            const canReviewImport = canImportDelta(item) && data.capabilities.package_import;
            return <article className={`delta-card delta-${item.kind}`} key={item.id}><span className={`change-kind ${presentation.tone}`}>{presentation.label}</span><div className="delta-copy"><div className="delta-title"><h2>{presentation.title}</h2><span>{item.kind}</span></div><code>{presentation.path}</code><p>{presentation.detail}</p>{(item.actual_version || item.desired_version) && item.status !== 'update' && <small>Local {item.actual_version || '?'} · desired {item.desired_version || '?'}</small>}{item.desired_sha256 && <small>Expected {item.desired_sha256.slice(0, 12)}…</small>}{item.actual_sha256 && <small>Local {item.actual_sha256.slice(0, 12)}…</small>}</div><div className="delta-actions">{!item.editable && <span className="readonly-pill">Read-only producer</span>}<button className="primary" disabled={!!busy || !!detailBusy || !item.editable || (!data.capabilities.capture && !data.capabilities.package_import) || !data.writable_layers.length} onClick={() => void openCapture(item)}>{detailBusy === `${data.revision}:${item.id}` ? 'Inspecting…' : busy === `delta:${item.id}` ? 'Capturing…' : canReviewImport ? 'Review import' : 'Capture change'}</button></div></article>;
          })}{deltaPage.rendered < deltaPage.total && <button className="secondary" onClick={() => setDeltaPageState({ key: deltaPageKey, limit: deltaPage.nextLimit })}>Load {Math.min(DELTA_PAGE_SIZE, deltaPage.total - deltaPage.rendered)} more</button>}</div> : <Empty title="Instance matches the layer">There are no uncaptured semantic changes in this instance.</Empty>}
        </> : <FileExplorer client={client} data={data} targetLayer={targetLayer} layers={data.writable_layers} onTargetLayer={setTargetLayer} onMutate={(label, action, success) => act(label, async () => await action() as Bootstrap, success)} onCompactMutation={applyCompactMutation} onRevisionConflict={(reason) => refreshAfterConflict(reason as ApiError)} onReviewImport={openCapture} onError={setError} />}
      </main>

      {routingDialog && <div className="modal-backdrop"><section className="modal" role="dialog" aria-modal="true" aria-labelledby="routing-title">
        <header><h2 id="routing-title">Write routing rules</h2><button aria-label="Close" onClick={() => setRoutingDialog(false)}>×</button></header>
        <div className="modal-body"><TargetSelect layers={data.writable_layers} value={targetLayer} onChange={setTargetLayer} />
          {error && <p className="inline-error" role="alert">{error}</p>}
          <RoutingRuleEditor key={targetLayer} client={client} target={targetLayer} busy={!!busy} onSave={async (rule, scope, file) => {
            const changed = await act('routing', () => client.saveRoutingRule(rule, scope, targetLayer, file), `${scope} routing rule saved.`);
            if (changed) setRoutingDialog(false);
            return changed;
          }} onUpgrade={(revision) => act('schema-upgrade', () => client.upgradeLayerSchema(targetLayer, revision), 'Selected layer upgraded to schema 2.')}
          onDelete={(id, scope) => act('routing-delete', () => client.deleteRoutingRule(id, scope, targetLayer), `${scope} rule ${id} removed.`)} />
        </div>
      </section></div>}

      {packageEdit && <div className="modal-backdrop"><section className="modal" role="dialog" aria-modal="true" aria-labelledby="package-edit-title">
        <header><h2 id="package-edit-title">Edit {packageEdit.item.id}</h2><button aria-label="Close" onClick={() => setPackageEdit(null)}>×</button></header>
        <div className="modal-body"><TargetSelect layers={data.writable_layers} value={packageEdit.target} onChange={(target) => setPackageEdit({ ...packageEdit, target, mode: 'disable' })} />
          {error && <p className="inline-error" role="alert">{error}</p>}
          <label><span>Action</span><select value={packageEdit.mode} onChange={(event) => setPackageEdit({ ...packageEdit, mode: event.target.value })}>
            <option value="enabled">Enable</option><option value="disable">Disable, keep declaration</option>
            {packageOwnedBy(packageEdit.item, data.writable_layers, packageEdit.target) && <option value="remove">Remove declaration from selected layer</option>}
          </select></label>
          {!packageOwnedBy(packageEdit.item, data.writable_layers, packageEdit.target) && <p>This package is inherited. Disabling writes an override into the selected layer.</p>}
          {packageEdit.mode === 'remove' ? <p>The declaration is removed from its existing metadata file; the artifact itself is not deleted. An inherited declaration may become visible again.</p> :
            <WriteDestination client={client} target={packageEdit.target} operation={{ type: 'activation', id: packageEdit.item.id, state: packageEdit.mode === 'enabled' ? 'enabled' : 'disabled' }} value={packageEdit.metadataWrite} onChange={(metadataWrite) => setPackageEdit({ ...packageEdit, metadataWrite })} />}
        </div><footer><button className="secondary" onClick={() => setPackageEdit(null)}>Cancel</button><button className="primary" disabled={!!busy} onClick={() => void submitPackageEdit()}>Apply package change</button></footer>
      </section></div>}

      {captureDraft && <div className="modal-backdrop" role="presentation" onMouseDown={(event) => event.target === event.currentTarget && closeCapture()}><section className="modal" role="dialog" aria-modal="true" aria-labelledby="capture-title"><header><div><p className="eyebrow">Semantic write</p><h2 id="capture-title">{deltaPresentation(captureDraft.item).title}</h2></div><button aria-label="Close" onClick={closeCapture}>×</button></header><div className="modal-body"><p>Only this semantic item will be written. Unrelated instance changes stay untouched.</p><TargetSelect layers={data.writable_layers} value={captureDraft.target} onChange={(target) => setCaptureDraft({ ...captureDraft, target })} />{detailBusy && <div className="inline-error" role="status">Inspecting this file and checking package metadata…</div>}{providerLookupPending(captureDraft.item) && !capturePollError && <div className="inline-error" role="status">Checking remote package sources… Local artifact remains available.</div>}{captureDraft.item.provider_lookup === 'failed' && !capturePollError && <div className="inline-error" role="status"><span>Remote package sources are unavailable. You can still use the local artifact.</span><button className="secondary" onClick={() => void retryCaptureSources()}>Retry source lookup</button></div>}{!detailBusy && canImportDelta(captureDraft.item) && data.capabilities.package_import && <fieldset><legend>Capture mode</legend><label className="choice"><input type="radio" name="capture-mode" checked={captureDraft.mode === 'import'} onChange={() => setCaptureDraft({ ...captureDraft, mode: 'import', modeTouched: true })} /><span><strong>Import as package</strong><small>Use verified provider metadata or a local SHA-256 artifact.</small></span></label>{data.capabilities.capture && <label className="choice"><input type="radio" name="capture-mode" checked={captureDraft.mode === 'capture'} onChange={() => setCaptureDraft({ ...captureDraft, mode: 'capture', modeTouched: true, metadataWrite: undefined })} /><span><strong>Capture as file</strong><small>Preserve the change as a file operation.</small></span></label>}</fieldset>}{!detailBusy && captureDraft.mode === 'import' && <ImportChoices draft={captureDraft} selectedCandidate={selectedCandidate} onChange={setCaptureDraft} />}{captureCreatesMetadata(captureDraft) && <WriteDestination client={client} target={captureDraft.target} operation={captureRoutingOperation(captureDraft)} value={captureDraft.metadataWrite} origin="instance_capture" onChange={(metadataWrite) => setCaptureDraft({ ...captureDraft, metadataWrite })} />}{capturePollError && <div className="inline-error" role="alert"><span>{capturePollError}</span><button className="secondary" onClick={() => void retryCaptureSources()}>Retry source lookup</button></div>}{captureError && <div className="inline-error" role="alert"><span>{captureError}</span></div>}</div><footer><button className="secondary" onClick={closeCapture}>Cancel</button><button className="primary" disabled={!!detailBusy || !semanticItemReady(captureDraft.item)} onClick={() => void submitCapture()}>{detailBusy ? 'Inspecting…' : captureDraft.mode === 'import' ? 'Import package' : 'Capture change'}</button></footer></section></div>}

      {featureDialog && <div className="modal-backdrop" role="presentation" onMouseDown={(event) => event.target === event.currentTarget && setFeatureDialog(false)}><section className="modal compact" role="dialog" aria-modal="true" aria-labelledby="feature-title"><header><div><p className="eyebrow">Temporary layer</p><h2 id="feature-title">Add feature</h2></div><button aria-label="Close" onClick={() => setFeatureDialog(false)}>×</button></header><div className="modal-body"><p>The instance will be relinked to a local temporary layer containing this feature.</p><label><span>Feature</span><select value={featureChoice} onChange={(event) => setFeatureChoice(event.target.value)}>{remainingFeatures.map((feature) => <option value={feature} key={feature}>{feature}</option>)}</select></label></div><footer><button className="secondary" onClick={() => setFeatureDialog(false)}>Cancel</button><button className="primary" disabled={!featureChoice} onClick={() => void addFeature()}>Add feature</button></footer></section></div>}
    </div>
  );
}

function DeclarationRow({ declaration, packageItem, selected, busy, activationAvailable, onSelect, onToggle }: {
  declaration: Declaration;
  packageItem?: PackageItem;
  selected: Selected | null;
  busy: string;
  activationAvailable: boolean;
  onSelect: (selected: Selected) => void;
  onToggle: (item: PackageItem) => void;
}) {
  const isSelected = packageItem
    ? selected?.type === 'package' && selected.value.id === packageItem.id
    : selected?.type === 'declaration' && selected.value.id === declaration.id;
  return <article className={isSelected ? 'package-row selected' : 'package-row'}><span className="declaration-order">{String(declaration.operation_index + 1).padStart(2, '0')}</span><button className="package-main" onClick={() => onSelect(packageItem ? { type: 'package', value: packageItem } : { type: 'declaration', value: declaration })}><span className="package-icon">{(packageItem?.id || declaration.kind).slice(0, 1).toUpperCase()}</span><span><strong>{packageItem?.id || declarationTarget(declaration) || declaration.kind}</strong><small>{declaration.kind} · {declaration.producer}{packageItem?.destination ? ` · ${packageItem.destination}` : ''}</small></span></button>{packageItem ? <><div className="package-meta"><small>Provider</small><strong>{packageItem.source.provider}</strong></div><div className="package-meta"><small>Version</small><strong>{packageItem.version}</strong></div><Badge state={packageItem.activation} /><Toggle checked={active(packageItem.activation)} disabled={!!busy || !activationAvailable || !packageItem.editable} label={`Toggle ${packageItem.id}`} onChange={() => onToggle(packageItem)} /></> : <><div className="package-meta declaration-source"><small>Source</small><strong>{declaration.source}</strong></div><Badge state={declaration.editable ? 'editable' : 'read-only'} /></>}</article>;
}

function TargetSelect({ layers, value, onChange }: { layers: Layer[]; value: string; onChange: (value: string) => void }) {
  return <label className="target-field"><span>Writable target</span><select value={value} disabled={!layers.length} onChange={(event) => onChange(event.target.value)}>{layers.length ? layers.map((layer) => <option value={layer.id} key={layer.id}>{layer.id}{layer.local ? ' · local' : ''}{layer.virtual ? ' · feature layer' : ''}</option>) : <option>No writable layers</option>}</select></label>;
}

function ImportChoices({ draft, selectedCandidate, onChange }: {
  draft: CaptureDraft;
  selectedCandidate?: ProviderCandidate;
  onChange: (draft: CaptureDraft) => void;
}) {
  const metadata = draft.item.jar_metadata;
  const providerCandidates = orderedProviderCandidates(draft.item);
  return <div className="import-panel">{metadata && (metadata.name || metadata.version || metadata.mod_ids.length > 0) && <section className="jar-details"><p className="eyebrow">Parsed package metadata</p><strong>{metadata.name || metadata.mod_ids.join(', ') || 'Unknown package'}</strong><span>{[metadata.version, metadata.loader].filter(Boolean).join(' · ') || 'Version details unavailable'}</span>{metadata.mod_ids.length > 0 && <code>{metadata.mod_ids.join(', ')}</code>}{metadata.dependencies.length > 0 && <small>Dependencies: {metadata.dependencies.join(', ')}</small>}</section>}<fieldset className="provider-list"><legend>Artifact source</legend>{providerCandidates.map((candidate) => <label className="provider-choice" key={candidateKey(candidate)}><input type="radio" name="provider" checked={draft.providerKey === candidateKey(candidate)} onChange={() => onChange({ ...draft, providerKey: candidateKey(candidate), fuzzyConfirmed: false, sourceTouched: true })} /><span><strong>{candidate.provider} · {candidate.version || candidate.version_id}</strong><small>{candidate.project_id}{candidate.reason ? ` · ${candidate.reason}` : ''}</small></span><Badge state={candidate.confidence} /></label>)}<label className="provider-choice"><input type="radio" name="provider" checked={draft.providerKey === 'local'} onChange={() => onChange({ ...draft, providerKey: 'local', fuzzyConfirmed: false, sourceTouched: true })} /><span><strong>Local artifact</strong><small>Exact SHA-256 fallback{draft.item.actual_sha256 ? ` · ${draft.item.actual_sha256.slice(0, 12)}…` : ''}</small></span><Badge state="exact" /></label></fieldset>{selectedCandidate?.confidence === 'fuzzy' && <label className="fuzzy-confirm"><input type="checkbox" checked={draft.fuzzyConfirmed} onChange={(event) => onChange({ ...draft, fuzzyConfirmed: event.target.checked, sourceTouched: true })} /><span><strong>Use this fuzzy match</strong><small>I reviewed the project and version identity. This match will not be selected automatically.</small></span></label>}</div>;
}

function PackageDetails({ item }: { item: PackageItem }) {
  return <><span className="details-icon">{item.id.slice(0, 1).toUpperCase()}</span><p className="eyebrow">Package details</p><h2>{item.id}</h2><Badge state={item.activation} />{item.activation_reason === 'dependency' && <p className="muted-copy">Enabled through a package dependency.</p>}{item.unsatisfied_dependencies?.length ? <div className="callout"><strong>Unmet dependencies</strong><p>{item.unsatisfied_dependencies.join(', ')} are explicitly disabled.</p></div> : null}{!item.editable && <div className="callout"><strong>Read-only declaration</strong><p>The producing source cannot be edited safely from this interface.</p></div>}<dl><div><dt>Logical identity</dt><dd>{item.logical_id}</dd></div><div><dt>Kind</dt><dd>{item.kind}</dd></div><div><dt>Provider</dt><dd>{item.source.provider}</dd></div><div><dt>Version</dt><dd>{item.version}</dd></div><div><dt>Destination</dt><dd>{item.destination || 'Not specified'}</dd></div><div><dt>Source metadata</dt><dd>{item.source_file || 'Generated'}</dd></div><div><dt>Artifact location</dt><dd>{item.source.location || 'Not specified'}</dd></div><div><dt>SHA-256</dt><dd className="hash">{item.source.sha256 || 'Not available'}</dd></div></dl></>;
}

function DeclarationDetails({ item }: { item: Declaration }) {
  return <><span className="details-icon">D</span><p className="eyebrow">Declaration details</p><h2>{declarationTarget(item) || item.id}</h2><Badge state={item.editable ? 'editable' : 'read-only'} /><dl><div><dt>Operation</dt><dd>{item.kind}</dd></div><div><dt>Layer</dt><dd>{item.layer || 'Not reported'}</dd></div><div><dt>Producer</dt><dd>{item.producer || 'Not reported'}</dd></div><div><dt>Source</dt><dd>{item.source || 'Not reported'}</dd></div><div><dt>Operation index</dt><dd>{item.operation_index}</dd></div></dl><div className="data-block"><small>Resolved data</small><pre>{JSON.stringify(item.data, null, 2)}</pre></div></>;
}
