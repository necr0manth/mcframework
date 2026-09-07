import { useEffect, useMemo, useRef, useState } from 'react';
import {
  applyExplorerCapturePatch,
  decisionsForPlan,
  flattenExplorer,
  mergeExplorerPage,
  refreshExplorerState,
} from './file-explorer-model.js';
import { runAutoCaptureFlow } from './api.js';
import type { CaptureChoice, CaptureDecisions } from './file-explorer-model.js';
import { singleFileImportItem } from './semantic-import.js';
import { WriteDestination } from './RoutingControls.js';
import type { MetadataWrite } from './RoutingControls.js';

type Entry = {
  path: string; name: string; type: string; actual: boolean; desired: boolean;
  synthetic: boolean; ignored: boolean; status: string; changed: boolean;
  changed_count: number; actionable: boolean; reason: string; depth?: number;
  delta_id?: string | null; capture_modes?: string[];
};
type Rule = {
  id: string; selector: { kind: string; path: string }; layer: string; source: string;
  own: boolean; removable: boolean; identity: Record<string, unknown>;
};
type PlanItem = {
  id: string; path: string; status: string; capture_modes: string[];
  supported?: boolean; reason?: string;
  expected_sha256?: string; expected_absent?: boolean;
};
type Choice = CaptureChoice;
type ExplorerPage = {
  parent: string; items: Entry[]; next_cursor: number | null; ignore_rules: Rule[];
};
type ExplorerPlan = {
  path: string; revision: number; items: PlanItem[];
  unsupported: Array<{ path: string; reason: string }>;
};
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
  const [plan, setPlan] = useState<ExplorerPlan | null>(null);
  const [choices, setChoices] = useState<Record<string, Choice>>({});
  const [ignoreDraft, setIgnoreDraft] = useState<{ item: Entry; write?: MetadataWrite } | null>(null);
  const childrenRef = useRef(children);
  const expandedRef = useRef(expanded);
  const nextRef = useRef(next);
  const rulesRef = useRef(rules);
  const loadedPagesRef = useRef(loadedPages);
  const scopeRef = useRef('');
  const revisionRef = useRef(data.revision);
  const requestGenerationRef = useRef(0);

  async function load(parent = '', cursor = 0) {
    const label = `load:${parent}`;
    const generation = requestGenerationRef.current;
    const revision = revisionRef.current;
    setBusy(label);
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
      setBusy((value) => value === label ? '' : value);
    }
  }

  useEffect(() => {
    const scope = JSON.stringify([mode, showIgnored, targetLayer]);
    const scopeChanged = scopeRef.current !== scope;
    const revisionChanged = revisionRef.current !== data.revision;
    if (!scopeChanged && !revisionChanged) return;
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

  const rows = useMemo(() => flattenExplorer(children, expanded), [children, expanded]);

  async function toggle(item: Entry) {
    if (item.type !== 'directory') return;
    const opened = new Set(expanded);
    if (opened.has(item.path)) opened.delete(item.path);
    else {
      opened.add(item.path);
      if (!children[item.path]) await load(item.path);
    }
    expandedRef.current = opened;
    setExpanded(opened);
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
    const packageItems = value.items.filter((entry: PlanItem) =>
      entry.capture_modes.includes('package') && entry.capture_modes.length > 1);
    if (!packageItems.length) {
      const decisions = decisionsForPlan(value.items, {});
      await onMutate(
        `capture:${item.path}`,
        () => client.capturePath(item.path, targetLayer, value.revision, decisions),
        `${item.path} was sent to ${targetLayer}.`,
      );
      return;
    }
    setChoices({});
    setPlan(value);
  }

  async function reviewCapture(item: Entry) {
    const label = `capture:auto:${item.path}`;
    setBusy(label);
    try {
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
    try {
      const decisions = decisionsForPlan(plan.items, choices);
      setBusy(label);
      const applied = await onMutate(
        label,
        () => client.capturePath(plan.path, targetLayer, plan.revision, decisions),
        `${plan.path} and all changed descendants were sent atomically.`,
      );
      setPlan(null);
      if (!applied) return;
    } catch (reason) {
      onError(reason instanceof Error ? reason.message : 'Could not capture files');
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

  return <>
    <div className="page-heading files-heading"><div><p className="eyebrow">Local filesystem</p><h1>Files</h1><p>Actual files, desired ghost deletions and layer actions in one tree.</p></div><div className="explorer-controls"><label><span>Writable target</span><select value={targetLayer} onChange={(event) => onTargetLayer(event.target.value)}>{layers.map((layer) => <option value={layer.id} key={layer.id}>{layer.id}{layer.local ? ' · local' : ''}</option>)}</select></label><label><span>Order</span><select value={mode} onChange={(event) => setMode(event.target.value)}><option value="all">All files</option><option value="changed-first">Changed first</option><option value="changed-only">Changed only</option></select></label><label className="show-ignored"><input type="checkbox" checked={showIgnored} onChange={(event) => setShowIgnored(event.target.checked)} /> Show ignored</label></div></div>
    <div className="explorer-layout"><section className="file-tree" aria-label="Instance files">
      {rows.length ? rows.map((item: Entry) => <div className={`file-row ${item.synthetic ? 'ghost' : ''} ${item.ignored ? 'ignored' : ''} ${item.changed ? 'changed' : ''}`} style={{ paddingLeft: `${14 + (item.depth || 0) * 22}px` }} key={item.path}>
        <button className="file-main" disabled={!!busy} onClick={() => void toggle(item)} title={item.reason || item.path}><span>{item.type === 'directory' ? (expanded.has(item.path) ? '▾' : '▸') : item.type === 'symlink' ? '↗' : '·'}</span><strong>{item.name}</strong>{item.synthetic && <em>deleted</em>}{item.ignored && <em>ignored</em>}{item.changed_count > 0 && <small>{item.status !== 'unchanged' ? item.status : `${item.changed_count} changed`}</small>}</button>
        <div className="file-actions"><button disabled={!!busy || !item.actionable || item.ignored} onClick={() => setIgnoreDraft({ item })}>Ignore</button><button className="primary" disabled={!!busy || item.changed_count === 0 || !item.actionable} onClick={() => void reviewCapture(item)}>Send to layer</button></div>
      </div>) : <div className="empty"><h2>No files in this mode</h2><p>Change the filter or enable ignored files.</p></div>}
      {next[''] !== null && next[''] !== undefined && <button className="load-more" disabled={!!busy} onClick={() => void load('', next[''] || 0)}>Load more</button>}
      {Object.entries(next).filter(([parent, cursor]) => parent && cursor !== null).map(([parent, cursor]) => <button className="load-more" disabled={!!busy} key={parent} onClick={() => void load(parent, cursor || 0)}>Load more in {parent}</button>)}
    </section><aside className="ignore-inspector"><p className="eyebrow">Selected layer</p><h2>Ignore rules</h2>{rules.length ? rules.map((rule) => <article key={rule.id}><code>{rule.selector.kind}: {rule.selector.path}</code><small>{rule.own ? 'Own rule' : `Inherited from ${rule.layer}`}</small>{rule.removable && <button disabled={!!busy} onClick={() => void removeRule(rule)}>Remove</button>}</article>) : <p>No ignore rules apply in this composition.</p>}</aside></div>
    {ignoreDraft && <div className="modal-backdrop"><section className="modal" role="dialog" aria-modal="true" aria-labelledby="ignore-title">
      <header><h2 id="ignore-title">Ignore {ignoreDraft.item.path}</h2><button aria-label="Close" onClick={() => setIgnoreDraft(null)}>×</button></header>
      <div className="modal-body"><label><span>Writable target</span><select value={targetLayer} onChange={(event) => onTargetLayer(event.target.value)}>{layers.map((layer) => <option key={layer.id} value={layer.id}>{layer.id}</option>)}</select></label>
        <WriteDestination client={client} target={targetLayer} operation={{ type: 'ignore', selector: { kind: ignoreDraft.item.type === 'directory' ? 'subtree' : 'exact', path: ignoreDraft.item.path } }} value={ignoreDraft.write} onChange={(write) => setIgnoreDraft({ ...ignoreDraft, write })} />
      </div><footer><button className="secondary" onClick={() => setIgnoreDraft(null)}>Cancel</button><button className="primary" disabled={!!busy} onClick={() => void ignore(ignoreDraft.item, ignoreDraft.write)}>Add ignore</button></footer>
    </section></div>}
    {plan && <div className="modal-backdrop"><section className="modal" role="dialog" aria-modal="true"><header><div><p className="eyebrow">Atomic folder capture</p><h2>Choose package handling</h2></div><button onClick={() => setPlan(null)}>×</button></header><div className="modal-body"><p>Every changed descendant must have a valid decision before anything is written.</p>{plan.items.filter((item) => item.capture_modes.includes('package') && item.capture_modes.length > 1).map((item) => <fieldset key={item.id}><legend>{item.path}</legend>{item.capture_modes.includes('payload') && <label className="choice"><input type="radio" checked={choices[item.id]?.mode === 'payload'} onChange={() => setChoices({ ...choices, [item.id]: { mode: 'payload' } })} /><span><strong>Raw file</strong><small>Capture this JAR as ordinary payload.</small></span></label>}<label className="choice"><input type="radio" checked={choices[item.id]?.mode === 'package'} onChange={() => setChoices({ ...choices, [item.id]: { mode: 'package' } })} /><span><strong>Local package</strong><small>Store a verified local SHA-256 artifact.</small></span></label></fieldset>)}</div><footer><button onClick={() => setPlan(null)}>Cancel</button><button className="primary" disabled={!!busy} onClick={() => void submitPlan()}>Send all atomically</button></footer></section></div>}
  </>;
}
