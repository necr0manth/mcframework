import { useEffect, useMemo, useRef, useState } from 'react';
import {
  applyExplorerCapturePatch,
  decisionsForPlan,
  explorerTreeNavigationDisabled,
  flattenExplorer,
  mergeExplorerPage,
  refreshExplorerState,
  toggleExplorerDirectory,
} from './file-explorer-model.js';
import { runAutoCaptureFlow } from './api.js';
import type { CaptureChoice, CaptureDecisions } from './file-explorer-model.js';
import {
  candidateKey,
  defaultPackageChoice,
  orderedProviderCandidates,
  packagePlanItems,
  packageProviderLookupPending,
  preferredFolderChoices,
  preferredProviderReady,
  packageLookupProgress,
  packageLookupIssues,
  packageLookupMessage,
  previousFolderWizardState,
  reconcileFolderChoices,
  singleFileImportItem,
} from './semantic-import.js';
import { WriteDestination } from './RoutingControls.js';
import type { MetadataWrite } from './RoutingControls.js';

type Entry = {
  path: string; name: string; type: string; actual: boolean; desired: boolean;
  synthetic: boolean; ignored: boolean; status: string; changed: boolean;
  changed_count: number; actionable: boolean; reason: string; depth?: number;
  delta_id?: string | null; capture_modes?: string[];
  proposed_package_kind?: string;
};
type Rule = {
  id: string; selector: { kind: string; path: string }; layer: string; source: string;
  own: boolean; removable: boolean; identity: Record<string, unknown>;
};
type PlanItem = {
  id: string; path: string; status: string; capture_modes: string[];
  supported?: boolean; reason?: string;
  expected_sha256?: string; expected_absent?: boolean;
  expected_kind?: string; expected_status?: string;
  proposed_package_id?: string; proposed_package_kind?: string;
  provider_lookup?: 'pending' | 'ready' | 'failed';
  provider_errors?: Array<{ provider: string; code: string; message: string; retryable: boolean }>;
  provider_candidates?: Array<{
    provider: string; project_id: string; version_id: string; version?: string | null;
    confidence: string; reason?: string; download_url?: string;
    hashes?: Record<string, unknown>; kind?: string; categories?: string[]; tags?: string[];
  }>;
};
type Choice = CaptureChoice & { userSelected?: boolean };
type ExplorerPage = {
  parent: string; items: Entry[]; next_cursor: number | null; ignore_rules: Rule[];
};
type ExplorerPlan = {
  path: string; revision: number; items: PlanItem[];
  unsupported: Array<{ path: string; reason: string }>;
  provider_lookup?: 'pending' | 'ready' | 'failed';
};
type WizardStep = 'strategy' | 'preferred' | 'items' | 'review';
type WizardStrategy = 'preferred' | 'individual';
type ExplorerClient = {
  files: (parent: string, options: Record<string, unknown>) => Promise<ExplorerPage>;
  filePlan: (path: string, targetLayer: string) => Promise<ExplorerPlan>;
  autoCapturePath: (path: string, target: string) => Promise<{
    applied: boolean; revision: number;
    explorer_patch: { path?: unknown; captured_count?: unknown };
  }>;
  capturePath: (
    path: string, target: string, planRevision: number, decisions: CaptureDecisions,
  ) => Promise<unknown>;
  ignorePath: (path: string, kind: string, target: string, write?: MetadataWrite) => Promise<unknown>;
  routingOptions: (target: string) => Promise<{ files: string[]; sections: string[]; effective_rules: unknown[] }>;
  previewRouting: (operation: Record<string, unknown>, target: string, write?: MetadataWrite) => Promise<{ writes: Array<{ file: string; document: unknown }> }>;
  retryProviderLookups: () => Promise<number>;
  removeIgnore: (identity: Record<string, unknown>, target: string) => Promise<unknown>;
};

export default function FileExplorer({ client, data, targetLayer, layers, onTargetLayer, onMutate, onCompactMutation, onRevisionConflict, onReviewImport, onError }: {
  client: ExplorerClient; data: { revision: number }; targetLayer: string;
  layers: Array<{ id: string; local: boolean }>;
  onTargetLayer: (value: string) => void;
  onMutate: (
    label: string, action: () => Promise<unknown>, success: string,
  ) => Promise<boolean>;
  onCompactMutation: (revision: number, success: string) => void;
  onRevisionConflict: (reason: unknown) => Promise<void>;
  onReviewImport: (item: NonNullable<ReturnType<typeof singleFileImportItem>>, revision: number) => Promise<void>;
  onError: (message: string) => void;
}) {
  const [mode, setMode] = useState('all');
  const [showIgnored, setShowIgnored] = useState(false);
  const [children, setChildren] = useState<Record<string, Entry[]>>({});
  const [expanded, setExpanded] = useState(new Set<string>());
  const [next, setNext] = useState<Record<string, number | null>>({});
  const [rules, setRules] = useState<Rule[]>([]);
  const [loadedPages, setLoadedPages] = useState<Record<string, number[]>>({ '': [0] });
  const [busy, setBusy] = useState('');
  const [loading, setLoading] = useState(new Set<string>());
  const [plan, setPlan] = useState<ExplorerPlan | null>(null);
  const [choices, setChoices] = useState<Record<string, Choice>>({});
  const [wizardStep, setWizardStep] = useState<WizardStep>('strategy');
  const [wizardStrategy, setWizardStrategy] = useState<WizardStrategy | null>(null);
  const [preferredProvider, setPreferredProvider] = useState<string | null>(null);
  const [wizardItemIds, setWizardItemIds] = useState<string[]>([]);
  const [wizardIndex, setWizardIndex] = useState(0);
  const [planPollError, setPlanPollError] = useState('');
  const [planSubmitError, setPlanSubmitError] = useState('');
  const [retryingLookup, setRetryingLookup] = useState(false);
  const retryLookupRef = useRef(false);
  const [ignoreDraft, setIgnoreDraft] = useState<{ item: Entry; write?: MetadataWrite } | null>(null);
  const childrenRef = useRef(children);
  const expandedRef = useRef(expanded);
  const nextRef = useRef(next);
  const rulesRef = useRef(rules);
  const loadedPagesRef = useRef(loadedPages);
  const scopeRef = useRef('');
  const revisionRef = useRef(data.revision);
  const requestGenerationRef = useRef(0);
  const planGenerationRef = useRef(0);

  async function load(parent = '', cursor = 0) {
    const label = `load:${parent}`;
    const generation = requestGenerationRef.current;
    const revision = revisionRef.current;
    setLoading((value) => new Set(value).add(label));
    try {
      const page = await client.files(parent, {
        mode, showIgnored, cursor, limit: 200, targetLayer,
      });
      if (generation !== requestGenerationRef.current || revision !== revisionRef.current) return;
      setChildren((value) => {
        const updated = mergeExplorerPage(value, page, cursor > 0);
        childrenRef.current = updated;
        return updated;
      });
      setNext((value) => {
        const updated = { ...value, [parent]: page.next_cursor };
        nextRef.current = updated;
        return updated;
      });
      setLoadedPages((value) => {
        const cursors = value[parent] || [];
        if (cursors.includes(cursor)) return value;
        const updated = { ...value, [parent]: [...cursors, cursor] };
        loadedPagesRef.current = updated;
        return updated;
      });
      rulesRef.current = page.ignore_rules || [];
      setRules(rulesRef.current);
    } catch (reason) {
      onError(reason instanceof Error ? reason.message : 'Could not load files');
    } finally {
      setLoading((value) => {
        const updated = new Set(value);
        updated.delete(label);
        return updated;
      });
    }
  }

  useEffect(() => {
    const scope = JSON.stringify([mode, showIgnored, targetLayer]);
    const scopeChanged = scopeRef.current !== scope;
    const revisionChanged = revisionRef.current !== data.revision;
    if (!scopeChanged && !revisionChanged) return;
    ++planGenerationRef.current;
    setPlan(null);
    scopeRef.current = scope;
    revisionRef.current = data.revision;
    const generation = ++requestGenerationRef.current;

    if (scopeChanged) {
      childrenRef.current = {};
      expandedRef.current = new Set();
      nextRef.current = {};
      rulesRef.current = [];
      loadedPagesRef.current = { '': [0] };
      setChildren({});
      setExpanded(expandedRef.current);
      setNext({});
      setRules([]);
      setLoadedPages(loadedPagesRef.current);
    }

    const label = `refresh:files:${generation}`;
    const state = {
      children: childrenRef.current,
      expanded: expandedRef.current,
      next: nextRef.current,
      rules: rulesRef.current,
      loadedPages: loadedPagesRef.current,
    };
    setBusy(label);
    void refreshExplorerState(state, ({ parent, cursor }: { parent: string; cursor: number }) =>
      client.files(parent, { mode, showIgnored, cursor, limit: 200, targetLayer }))
      .then((updated: typeof state) => {
        if (generation !== requestGenerationRef.current || data.revision !== revisionRef.current) return;
        childrenRef.current = updated.children;
        nextRef.current = updated.next;
        rulesRef.current = updated.rules;
        setChildren(updated.children);
        setNext(updated.next);
        setRules(updated.rules);
      })
      .catch((reason: unknown) => {
        if (generation === requestGenerationRef.current) {
          onError(reason instanceof Error ? reason.message : 'Could not refresh files');
        }
      })
      .finally(() => setBusy((value) => value === label ? '' : value));
  }, [mode, showIgnored, targetLayer, data.revision]);

  useEffect(() => () => { ++planGenerationRef.current; }, []);

  const rows = useMemo(() => flattenExplorer(children, expanded), [children, expanded]);

  function closePlan() {
    ++planGenerationRef.current;
    setPlan(null);
    setChoices({});
    setWizardStep('strategy');
    setWizardStrategy(null);
    setPreferredProvider(null);
    setWizardItemIds([]);
    setWizardIndex(0);
    setPlanPollError('');
    setPlanSubmitError('');
  }

  function planIsPending(value: ExplorerPlan) {
    return packageProviderLookupPending(value);
  }

  async function pollPlan(path: string, revision: number, generation: number) {
    while (planGenerationRef.current === generation && revisionRef.current === revision) {
      await new Promise((resolve) => window.setTimeout(resolve, 500));
      if (planGenerationRef.current !== generation || revisionRef.current !== revision) return;
      try {
        const updated = await client.filePlan(path, targetLayer);
        if (
          planGenerationRef.current !== generation
          || revisionRef.current !== revision
          || updated.revision !== revision
        ) return;
        setPlan((current) => current?.path === path && current.revision === revision
          ? updated
          : current);
        setChoices((current) => reconcileFolderChoices(
          updated.items,
          current,
        ) as Record<string, Choice>);
        setPlanPollError('');
        if (!planIsPending(updated)) return;
      } catch (reason) {
        if (planGenerationRef.current === generation) {
          setPlanPollError(reason instanceof Error ? reason.message : 'Could not refresh package sources');
        }
        return;
      }
    }
  }

  async function retryPlanPolling() {
    const current = plan;
    if (!current || retryLookupRef.current) return;
    retryLookupRef.current = true;
    setRetryingLookup(true);
    setPlanPollError('');
    const generation = ++planGenerationRef.current;
    try {
      await client.retryProviderLookups();
      if (planGenerationRef.current !== generation) return;
      await pollPlan(current.path, current.revision, generation);
    } catch (reason) {
      if (planGenerationRef.current === generation) {
        setPlanPollError(reason instanceof Error ? reason.message : 'Could not retry package sources');
      }
    } finally {
      retryLookupRef.current = false;
      setRetryingLookup(false);
    }
  }

  function choicesWithDefaults(
    itemIds: string[],
    base: Record<string, Choice> = {},
  ) {
    if (!plan) return base;
    const updated = { ...base };
    for (const id of itemIds) {
      const item = plan.items.find((entry) => entry.id === id);
      if (item && !updated[id]) updated[id] = defaultPackageChoice(item);
    }
    return updated;
  }

  function chooseStrategy(strategy: WizardStrategy) {
    setWizardStrategy(strategy);
  }

  function continueStrategy() {
    if (!plan || !wizardStrategy) return;
    if (wizardStrategy === 'preferred') return setWizardStep('preferred');
    const ids = packagePlanItems(plan.items).map((item: PlanItem) => item.id);
    setChoices(choicesWithDefaults(ids, choices));
    setWizardItemIds(ids);
    setWizardIndex(0);
    setWizardStep(ids.length ? 'items' : 'review');
  }

  function choosePreferredProvider(provider: string) {
    if (!plan) return;
    setPreferredProvider(provider);
  }

  function continuePreferredProvider() {
    if (!plan || !preferredProvider || !preferredProviderReady(plan, preferredProvider)) return;
    const preferred = preferredFolderChoices(plan.items, preferredProvider);
    const explicitChoices = Object.fromEntries(Object.entries(preferred.choices).map(
      ([id, choice]) => [id, { ...choice, userSelected: true }],
    )) as Record<string, Choice>;
    setChoices(choicesWithDefaults(
      preferred.unresolved,
      explicitChoices,
    ));
    setWizardItemIds(preferred.unresolved);
    setWizardIndex(0);
    setWizardStep(preferred.unresolved.length ? 'items' : 'review');
  }

  function previousWizardStep() {
    const previous = previousFolderWizardState({
      step: wizardStep,
      strategy: wizardStrategy,
      preferredProvider,
      choices,
      itemIds: wizardItemIds,
      index: wizardIndex,
    });
    setWizardStep(previous.step as WizardStep);
    setWizardIndex(previous.index);
  }

  function nextWizardItem() {
    const id = wizardItemIds[wizardIndex];
    const choice = choices[id];
    if (!choice) return;
    if (choice.provider_match?.confidence === 'fuzzy' && !choice.fuzzyConfirmed) {
      onError(`Confirm the fuzzy provider match for ${plan?.items.find((item) => item.id === id)?.path || id}`);
      return;
    }
    if (!choice.userSelected) {
      setChoices({ ...choices, [id]: { ...choice, userSelected: true } });
    }
    if (wizardIndex + 1 < wizardItemIds.length) setWizardIndex(wizardIndex + 1);
    else setWizardStep('review');
  }

  async function toggle(item: Entry) {
    if (item.type !== 'directory') return;
    const { expanded: opened, loadParent } = toggleExplorerDirectory(
      expandedRef.current,
      item.path,
      Boolean(childrenRef.current[item.path]),
    );
    expandedRef.current = opened;
    setExpanded(opened);
    if (loadParent !== null) await load(loadParent);
  }

  async function ignore(item: Entry, write?: MetadataWrite) {
    const label = `ignore:${item.path}`;
    setBusy(label);
    try {
      const applied = await onMutate(
        label,
        () => client.ignorePath(
          item.path, item.type === 'directory' ? 'subtree' : 'exact', targetLayer, write,
        ),
        `${item.path} is now ignored in ${targetLayer}.`,
      );
      if (applied) setIgnoreDraft(null);
    } catch (reason) {
      onError(reason instanceof Error ? reason.message : 'Could not add ignore rule');
    } finally { setBusy((value) => value === label ? '' : value); }
  }

  async function applyPlan(item: Entry, value: ExplorerPlan) {
    if (value.unsupported?.length) throw new Error(value.unsupported[0].reason);
    const unsupportedItem = value.items.find((entry) => entry.supported === false);
    if (unsupportedItem) throw new Error(unsupportedItem.reason || `${unsupportedItem.path} is not supported`);
    const packageItems = packagePlanItems(value.items);
    if (!packageItems.length) {
      const decisions = decisionsForPlan(value.items, {});
      await onMutate(
        `capture:${item.path}`,
        () => client.capturePath(item.path, targetLayer, value.revision, decisions),
        `${item.path} was sent to ${targetLayer}.`,
      );
      return;
    }
    const generation = ++planGenerationRef.current;
    setChoices({});
    setWizardStep('strategy');
    setWizardStrategy(null);
    setPreferredProvider(null);
    setWizardItemIds([]);
    setWizardIndex(0);
    setPlanPollError('');
    setPlanSubmitError('');
    setPlan(value);
    if (planIsPending(value)) void pollPlan(value.path, value.revision, generation);
  }

  async function reviewCapture(item: Entry) {
    const label = `capture:auto:${item.path}`;
    setBusy(label);
    try {
      if (item.type === 'directory') {
        await applyPlan(item, await client.filePlan(item.path, targetLayer));
        return;
      }
      const semanticItem = singleFileImportItem(item);
      if (semanticItem) {
        await onReviewImport(semanticItem, data.revision);
        return;
      }
      await runAutoCaptureFlow({
        client,
        path: item.path,
        targetLayer,
        onApplied: (result: {
          revision: number;
          explorer_patch: { path?: unknown; captured_count?: unknown };
        }) => {
          const patch = result.explorer_patch;
          if (patch.path !== item.path || !Number.isInteger(Number(patch.captured_count))) {
            throw new Error('Capture response explorer patch is invalid');
          }
          ++requestGenerationRef.current;
          revisionRef.current = result.revision;
          const state = applyExplorerCapturePatch({
            children: childrenRef.current,
            expanded: expandedRef.current,
            next: nextRef.current,
            rules: rulesRef.current,
            loadedPages: loadedPagesRef.current,
          }, patch, mode);
          childrenRef.current = state.children;
          setChildren(state.children);
          onCompactMutation(
            result.revision,
            `${item.path} was sent to ${targetLayer}.`,
          );
        },
        onDecisionRequired: (value: ExplorerPlan) => applyPlan(item, value),
        onRevisionConflict,
      });
    } catch (reason) {
      onError(reason instanceof Error ? reason.message : 'Could not capture files');
    } finally { setBusy((current) => current === label ? '' : current); }
  }

  async function submitPlan() {
    if (!plan) return;
    const label = `capture:${plan.path}`;
    setPlanSubmitError('');
    try {
      const decisions = decisionsForPlan(plan.items, choices);
      setBusy(label);
      const applied = await onMutate(
        label,
        async () => {
          try {
            return await client.capturePath(plan.path, targetLayer, plan.revision, decisions);
          } catch (reason) {
            // onMutate handles errors globally, behind this modal. Keep the actual
            // capture failure visible here as well, without losing the choices.
            setPlanSubmitError(reason instanceof Error ? reason.message : 'Could not capture files');
            throw reason;
          }
        },
        `${plan.path} and all changed descendants were sent atomically.`,
      );
      if (applied) closePlan();
      else setPlanSubmitError((current) => current || 'The folder was not sent. Review the choices and try again.');
    } catch (reason) {
      const message = reason instanceof Error ? reason.message : 'Could not capture files';
      setPlanSubmitError(message);
      onError(message);
    } finally { setBusy((value) => value === label ? '' : value); }
  }

  async function removeRule(rule: Rule) {
    const label = `rule:${rule.id}`;
    setBusy(label);
    try {
      await onMutate(
        label,
        () => client.removeIgnore(rule.identity, targetLayer),
        `Ignore rule ${rule.selector.path} was removed.`,
      );
    } catch (reason) {
      onError(reason instanceof Error ? reason.message : 'Could not remove ignore rule');
    } finally { setBusy((value) => value === label ? '' : value); }
  }

  const lookupProgress = packageLookupProgress(plan);
  const lookupIssues = packageLookupIssues(plan);
  const wizardItem = plan?.items.find((item) => item.id === wizardItemIds[wizardIndex]);
  const wizardChoice = wizardItem ? choices[wizardItem.id] : undefined;
  const wizardChoiceNeedsConfirmation = wizardChoice?.provider_match?.confidence === 'fuzzy'
    && !wizardChoice.fuzzyConfirmed;
  const actionBusy = Boolean(busy) || loading.size > 0;

  return <>
    <div className="page-heading files-heading"><div><p className="eyebrow">Local filesystem</p><h1>Files</h1><p>Actual files, desired ghost deletions and layer actions in one tree.</p></div><div className="explorer-controls"><label><span>Writable target</span><select value={targetLayer} onChange={(event) => onTargetLayer(event.target.value)}>{layers.map((layer) => <option value={layer.id} key={layer.id}>{layer.id}{layer.local ? ' · local' : ''}</option>)}</select></label><label><span>Order</span><select value={mode} onChange={(event) => setMode(event.target.value)}><option value="all">All files</option><option value="changed-first">Changed first</option><option value="changed-only">Changed only</option></select></label><label className="show-ignored"><input type="checkbox" checked={showIgnored} onChange={(event) => setShowIgnored(event.target.checked)} /> Show ignored</label></div></div>
    <div className="explorer-layout"><section className="file-tree" aria-label="Instance files">
      {rows.length ? rows.map((item: Entry) => <div className={`file-row ${item.synthetic ? 'ghost' : ''} ${item.ignored ? 'ignored' : ''} ${item.changed ? 'changed' : ''}`} style={{ paddingLeft: `${14 + (item.depth || 0) * 22}px` }} key={item.path}>
        <button className="file-main" disabled={explorerTreeNavigationDisabled(busy, loading, item.path)} onClick={() => void toggle(item)} title={item.reason || item.path}><span>{item.type === 'directory' ? (expanded.has(item.path) ? '▾' : '▸') : item.type === 'symlink' ? '↗' : '·'}</span><strong>{item.name}</strong>{item.synthetic && <em>deleted</em>}{item.ignored && <em>ignored</em>}{item.changed_count > 0 && <small>{item.status !== 'unchanged' ? item.status : `${item.changed_count} changed`}</small>}</button>
        <div className="file-actions"><button disabled={actionBusy || !item.actionable || item.ignored} onClick={() => setIgnoreDraft({ item })}>Ignore</button><button className="primary" disabled={actionBusy || item.changed_count === 0 || !item.actionable} onClick={() => void reviewCapture(item)}>Send to layer</button></div>
      </div>) : <div className="empty"><h2>No files in this mode</h2><p>Change the filter or enable ignored files.</p></div>}
      {next[''] !== null && next[''] !== undefined && <button className="load-more" disabled={explorerTreeNavigationDisabled(busy, loading, '')} onClick={() => void load('', next[''] || 0)}>Load more</button>}
      {Object.entries(next).filter(([parent, cursor]) => parent && cursor !== null).map(([parent, cursor]) => <button className="load-more" disabled={explorerTreeNavigationDisabled(busy, loading, parent)} key={parent} onClick={() => void load(parent, cursor || 0)}>Load more in {parent}</button>)}
    </section><aside className="ignore-inspector"><p className="eyebrow">Selected layer</p><h2>Ignore rules</h2>{rules.length ? rules.map((rule) => <article key={rule.id}><code>{rule.selector.kind}: {rule.selector.path}</code><small>{rule.own ? 'Own rule' : `Inherited from ${rule.layer}`}</small>{rule.removable && <button disabled={actionBusy} onClick={() => void removeRule(rule)}>Remove</button>}</article>) : <p>No ignore rules apply in this composition.</p>}</aside></div>
    {ignoreDraft && <div className="modal-backdrop"><section className="modal" role="dialog" aria-modal="true" aria-labelledby="ignore-title">
      <header><h2 id="ignore-title">Ignore {ignoreDraft.item.path}</h2><button aria-label="Close" onClick={() => setIgnoreDraft(null)}>×</button></header>
      <div className="modal-body"><label><span>Writable target</span><select value={targetLayer} onChange={(event) => onTargetLayer(event.target.value)}>{layers.map((layer) => <option key={layer.id} value={layer.id}>{layer.id}</option>)}</select></label>
        <WriteDestination client={client} target={targetLayer} operation={{ type: 'ignore', selector: { kind: ignoreDraft.item.type === 'directory' ? 'subtree' : 'exact', path: ignoreDraft.item.path } }} value={ignoreDraft.write} onChange={(write) => setIgnoreDraft({ ...ignoreDraft, write })} />
      </div><footer><button className="secondary" onClick={() => setIgnoreDraft(null)}>Cancel</button><button className="primary" disabled={!!busy} onClick={() => void ignore(ignoreDraft.item, ignoreDraft.write)}>Add ignore</button></footer>
    </section></div>}
    {plan && <div className="modal-backdrop" role="presentation" onMouseDown={(event) => event.target === event.currentTarget && closePlan()}><section className="modal" role="dialog" aria-modal="true" aria-labelledby="folder-capture-title"><header><div><p className="eyebrow">Atomic folder capture</p><h2 id="folder-capture-title">{wizardStep === 'strategy' ? 'Choose package handling' : wizardStep === 'preferred' ? 'Choose a preferred source' : wizardStep === 'items' ? `${wizardStrategy === 'preferred' ? 'Review exception' : 'Choose source'} ${wizardIndex + 1} of ${wizardItemIds.length}` : 'Review folder capture'}</h2></div><button aria-label="Close" onClick={closePlan}>×</button></header><div className="modal-body"><p>No files are written until you approve the final review.</p>{planIsPending(plan) && !planPollError && <div className="inline-error" role="status">Checking remote package sources… {lookupProgress.completed} of {lookupProgress.total} checked. Local choices remain available.</div>}{lookupIssues.count > 0 && !planPollError && <div className="inline-error" role="status"><div><span>Remote source lookup is incomplete for {lookupIssues.count} of {lookupProgress.total} packages. Available matches remain usable.</span>{lookupIssues.errors.map((error) => <p className="lookup-detail" key={`${error.provider}:${error.code}:${error.message}`}>{error.provider === 'curseforge' ? 'CurseForge' : error.provider === 'modrinth' ? 'Modrinth' : error.provider}: {error.message}</p>)}</div>{lookupIssues.retryable && <button className="secondary" disabled={retryingLookup || planIsPending(plan)} onClick={() => void retryPlanPolling()}>{retryingLookup ? 'Retrying sources…' : 'Retry source lookup'}</button>}</div>}{planPollError && <div className="inline-error" role="alert"><span>{planPollError}</span><button className="secondary" disabled={retryingLookup} onClick={() => void retryPlanPolling()}>{retryingLookup ? 'Retrying sources…' : 'Retry source lookup'}</button></div>}{wizardStep === 'strategy' && <fieldset><legend>Selection strategy</legend><label className="choice"><input type="radio" name="folder-strategy" checked={wizardStrategy === 'preferred'} onChange={() => chooseStrategy('preferred')} /><span><strong>Preferred source for all</strong><small>Use one source where an exact match exists, then review exceptions one at a time.</small></span></label><label className="choice"><input type="radio" name="folder-strategy" checked={wizardStrategy === 'individual'} onChange={() => chooseStrategy('individual')} /><span><strong>Choose each package</strong><small>Review one package at a time before the final atomic capture.</small></span></label></fieldset>}{wizardStep === 'preferred' && <fieldset><legend>Preferred artifact source</legend>{['modrinth', 'curseforge', 'local'].map((provider) => <label className="choice" key={provider}><input type="radio" name="preferred-provider" checked={preferredProvider === provider} onChange={() => choosePreferredProvider(provider)} /><span><strong>{provider === 'modrinth' ? 'Modrinth' : provider === 'curseforge' ? 'CurseForge' : 'Local artifact'}</strong><small>{provider === 'local' ? 'Use verified local artifacts for every package.' : `Automatically use exact ${provider === 'modrinth' ? 'Modrinth' : 'CurseForge'} matches.`}</small></span></label>)}</fieldset>}{wizardStep === 'items' && wizardStrategy === 'preferred' && <p className="lookup-detail">Reviewing {wizardItemIds.length} exceptions out of {lookupProgress.total} packages without an exact match from {preferredProvider === 'modrinth' ? 'Modrinth' : 'CurseForge'}. Other sources may still have matches.</p>}{wizardStep === 'items' && wizardItem && <fieldset><legend>{wizardItem.path}</legend>{packageLookupMessage(wizardItem) && <p className="package-lookup-status" role="status">{packageLookupMessage(wizardItem)}</p>}{orderedProviderCandidates(wizardItem).map((candidate) => <label className="provider-choice" key={candidateKey(candidate)}><input type="radio" name={`folder-provider-${wizardItem.id}`} checked={candidateKey(wizardChoice?.provider_match || {}) === candidateKey(candidate)} onChange={() => setChoices({ ...choices, [wizardItem.id]: { mode: 'package', provider_match: candidate, userSelected: true } })} /><span><strong>{candidate.provider} · {candidate.version || candidate.version_id}</strong><small>{candidate.project_id}{candidate.reason ? ` · ${candidate.reason}` : ''}</small></span><span className={`badge activation-${candidate.confidence}`}>{candidate.confidence}</span></label>)}<label className="provider-choice"><input type="radio" name={`folder-provider-${wizardItem.id}`} checked={wizardChoice?.mode === 'package' && !wizardChoice.provider_match} onChange={() => setChoices({ ...choices, [wizardItem.id]: { mode: 'package', userSelected: true } })} /><span><strong>Local artifact</strong><small>Use the verified local file.</small></span><span className="badge activation-exact">exact</span></label>{wizardItem.capture_modes.includes('payload') && <label className="provider-choice"><input type="radio" name={`folder-provider-${wizardItem.id}`} checked={wizardChoice?.mode === 'payload'} onChange={() => setChoices({ ...choices, [wizardItem.id]: { mode: 'payload', userSelected: true } })} /><span><strong>Raw file</strong><small>Capture this package file as ordinary payload.</small></span></label>}{wizardChoice?.provider_match?.confidence === 'fuzzy' && <label className="fuzzy-confirm"><input type="checkbox" checked={wizardChoice.fuzzyConfirmed === true} onChange={(event) => setChoices({ ...choices, [wizardItem.id]: { ...wizardChoice, fuzzyConfirmed: event.target.checked } })} /><span><strong>Use this fuzzy match</strong><small>I reviewed the project and version identity.</small></span></label>}</fieldset>}{wizardStep === 'review' && <div><p>Review {packagePlanItems(plan.items).length} package choices. The folder is sent with one atomic request.</p>{packagePlanItems(plan.items).map((item: PlanItem) => { const choice = choices[item.id]; const provider = String(choice?.provider_match?.provider || (choice?.mode === 'payload' ? 'raw file' : 'local')); return <fieldset key={item.id}><legend>{item.path}</legend><p>{provider}</p></fieldset>; })}</div>}</div>{planSubmitError && <div className="inline-error" role="alert">{planSubmitError}</div>}<footer><button className="secondary" onClick={closePlan}>Cancel</button>{wizardStep !== 'strategy' && <button className="secondary" disabled={!!busy} onClick={previousWizardStep}>Back</button>}{wizardStep === 'strategy' && <button className="primary" disabled={!!busy || !wizardStrategy} onClick={continueStrategy}>Continue</button>}{wizardStep === 'preferred' && <button className="primary" disabled={!!busy || !preferredProvider || !preferredProviderReady(plan, preferredProvider)} onClick={continuePreferredProvider}>{preferredProvider && !preferredProviderReady(plan, preferredProvider) ? 'Checking sources...' : 'Continue'}</button>}{wizardStep === 'items' && <button className="primary" disabled={!!busy || !wizardChoice || wizardChoiceNeedsConfirmation} onClick={nextWizardItem}>{wizardIndex + 1 < wizardItemIds.length ? 'Next package' : 'Review choices'}</button>}{wizardStep === 'review' && <button className="primary" disabled={!!busy} onClick={() => void submitPlan()}>Send all atomically</button>}</footer></section></div>}
  </>;
}
