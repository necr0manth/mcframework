import { useEffect, useMemo, useState } from 'react';
import { decisionsForPlan, flattenExplorer, mergeExplorerPage } from './file-explorer-model.js';

type Entry = {
  path: string; name: string; type: string; actual: boolean; desired: boolean;
  synthetic: boolean; ignored: boolean; status: string; changed: boolean;
  changed_count: number; actionable: boolean; reason: string; depth?: number;
};
type Rule = {
  id: string; selector: { kind: string; path: string }; layer: string; source: string;
  own: boolean; removable: boolean; identity: Record<string, unknown>;
};
type PlanItem = {
  id: string; path: string; status: string; capture_modes: string[];
  supported?: boolean; reason?: string;
  provider_candidates?: Array<Record<string, unknown>>;
};
type Choice = { mode: string; provider_match?: Record<string, unknown>; fuzzyConfirmed?: boolean };
type ExplorerPage = {
  parent: string; items: Entry[]; next_cursor: number | null; ignore_rules: Rule[];
};
type ExplorerPlan = {
  path: string; items: PlanItem[]; unsupported: Array<{ path: string; reason: string }>;
};
type ExplorerClient = {
  files: (parent: string, options: Record<string, unknown>) => Promise<ExplorerPage>;
  filePlan: (path: string, targetLayer: string) => Promise<ExplorerPlan>;
  capturePath: (path: string, target: string, decisions: Record<string, unknown>) => Promise<unknown>;
  ignorePath: (path: string, kind: string, target: string) => Promise<unknown>;
  removeIgnore: (identity: Record<string, unknown>, target: string) => Promise<unknown>;
};

export default function FileExplorer({ client, data, targetLayer, layers, onTargetLayer, onMutate, onError }: {
  client: ExplorerClient; data: { revision: number }; targetLayer: string;
  layers: Array<{ id: string; local: boolean }>;
  onTargetLayer: (value: string) => void;
  onMutate: (
    label: string, action: () => Promise<unknown>, success: string,
  ) => Promise<boolean>;
  onError: (message: string) => void;
}) {
  const [mode, setMode] = useState('all');
  const [showIgnored, setShowIgnored] = useState(false);
  const [children, setChildren] = useState<Record<string, Entry[]>>({});
  const [expanded, setExpanded] = useState(new Set<string>());
  const [next, setNext] = useState<Record<string, number | null>>({});
  const [rules, setRules] = useState<Rule[]>([]);
  const [busy, setBusy] = useState('');
  const [plan, setPlan] = useState<{ path: string; items: PlanItem[] } | null>(null);
  const [choices, setChoices] = useState<Record<string, Choice>>({});

  async function load(parent = '', cursor = 0) {
    setBusy(`load:${parent}`);
    try {
      const page = await client.files(parent, {
        mode, showIgnored, cursor, limit: 200, targetLayer,
      });
      setChildren((value) => mergeExplorerPage(value, page, cursor > 0));
      setNext((value) => ({ ...value, [parent]: page.next_cursor }));
      setRules(page.ignore_rules || []);
    } catch (reason) {
      onError(reason instanceof Error ? reason.message : 'Could not load files');
    } finally {
      setBusy('');
    }
  }

  useEffect(() => {
    setChildren({});
    setExpanded(new Set());
    void load();
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
    setExpanded(opened);
  }

  async function ignore(item: Entry) {
    setBusy(`ignore:${item.path}`);
    try {
      await onMutate(
        `ignore:${item.path}`,
        () => client.ignorePath(
          item.path, item.type === 'directory' ? 'subtree' : 'exact', targetLayer,
        ),
        `${item.path} is now ignored in ${targetLayer}.`,
      );
    } catch (reason) {
      onError(reason instanceof Error ? reason.message : 'Could not add ignore rule');
    } finally { setBusy(''); }
  }

  async function reviewCapture(item: Entry) {
    setBusy(`plan:${item.path}`);
    try {
      const value = await client.filePlan(item.path, targetLayer);
      if (value.unsupported?.length) throw new Error(value.unsupported[0].reason);
      const unsupportedItem = value.items.find((entry) => entry.supported === false);
      if (unsupportedItem) throw new Error(unsupportedItem.reason || `${unsupportedItem.path} is not supported`);
      const packageItems = value.items.filter((entry: PlanItem) =>
        entry.capture_modes.includes('package'));
      if (!packageItems.length) {
        await onMutate(
          `capture:${item.path}`,
          () => client.capturePath(item.path, targetLayer, {}),
          `${item.path} was sent to ${targetLayer}.`,
        );
      } else {
        setChoices({});
        setPlan(value);
      }
    } catch (reason) {
      onError(reason instanceof Error ? reason.message : 'Could not plan capture');
    } finally { setBusy(''); }
  }

  async function submitPlan() {
    if (!plan) return;
    try {
      const decisions = decisionsForPlan(plan.items, choices);
      setBusy(`capture:${plan.path}`);
      const applied = await onMutate(
        `capture:${plan.path}`,
        () => client.capturePath(plan.path, targetLayer, decisions),
        `${plan.path} and all changed descendants were sent atomically.`,
      );
      setPlan(null);
      if (!applied) return;
    } catch (reason) {
      onError(reason instanceof Error ? reason.message : 'Could not capture files');
    } finally { setBusy(''); }
  }

  async function removeRule(rule: Rule) {
    setBusy(`rule:${rule.id}`);
    try {
      await onMutate(
        `rule:${rule.id}`,
        () => client.removeIgnore(rule.identity, targetLayer),
        `Ignore rule ${rule.selector.path} was removed.`,
      );
    } catch (reason) {
      onError(reason instanceof Error ? reason.message : 'Could not remove ignore rule');
    } finally { setBusy(''); }
  }

  return <>
    <div className="page-heading files-heading"><div><p className="eyebrow">Local filesystem</p><h1>Files</h1><p>Actual files, desired ghost deletions and layer actions in one tree.</p></div><div className="explorer-controls"><label><span>Writable target</span><select value={targetLayer} onChange={(event) => onTargetLayer(event.target.value)}>{layers.map((layer) => <option value={layer.id} key={layer.id}>{layer.id}{layer.local ? ' · local' : ''}</option>)}</select></label><label><span>Order</span><select value={mode} onChange={(event) => setMode(event.target.value)}><option value="all">All files</option><option value="changed-first">Changed first</option><option value="changed-only">Changed only</option></select></label><label className="show-ignored"><input type="checkbox" checked={showIgnored} onChange={(event) => setShowIgnored(event.target.checked)} /> Show ignored</label></div></div>
    <div className="explorer-layout"><section className="file-tree" aria-label="Instance files">
      {rows.length ? rows.map((item: Entry) => <div className={`file-row ${item.synthetic ? 'ghost' : ''} ${item.ignored ? 'ignored' : ''} ${item.changed ? 'changed' : ''}`} style={{ paddingLeft: `${14 + (item.depth || 0) * 22}px` }} key={item.path}>
        <button className="file-main" onClick={() => void toggle(item)} title={item.reason || item.path}><span>{item.type === 'directory' ? (expanded.has(item.path) ? '▾' : '▸') : item.type === 'symlink' ? '↗' : '·'}</span><strong>{item.name}</strong>{item.synthetic && <em>deleted</em>}{item.ignored && <em>ignored</em>}{item.changed_count > 0 && <small>{item.status !== 'unchanged' ? item.status : `${item.changed_count} changed`}</small>}</button>
        <div className="file-actions"><button disabled={!!busy || !item.actionable || item.ignored} onClick={() => void ignore(item)}>Ignore</button><button className="primary" disabled={!!busy || item.changed_count === 0 || !item.actionable} onClick={() => void reviewCapture(item)}>Send to layer</button></div>
      </div>) : <div className="empty"><h2>No files in this mode</h2><p>Change the filter or enable ignored files.</p></div>}
      {next[''] !== null && next[''] !== undefined && <button className="load-more" onClick={() => void load('', next[''] || 0)}>Load more</button>}
      {Object.entries(next).filter(([parent, cursor]) => parent && cursor !== null).map(([parent, cursor]) => <button className="load-more" key={parent} onClick={() => void load(parent, cursor || 0)}>Load more in {parent}</button>)}
    </section><aside className="ignore-inspector"><p className="eyebrow">Selected layer</p><h2>Ignore rules</h2>{rules.length ? rules.map((rule) => <article key={rule.id}><code>{rule.selector.kind}: {rule.selector.path}</code><small>{rule.own ? 'Own rule' : `Inherited from ${rule.layer}`}</small>{rule.removable && <button disabled={!!busy} onClick={() => void removeRule(rule)}>Remove</button>}</article>) : <p>No ignore rules apply in this composition.</p>}</aside></div>
    {plan && <div className="modal-backdrop"><section className="modal" role="dialog" aria-modal="true"><header><div><p className="eyebrow">Atomic folder capture</p><h2>Choose package handling</h2></div><button onClick={() => setPlan(null)}>×</button></header><div className="modal-body"><p>Every changed descendant must have a valid decision before anything is written.</p>{plan.items.filter((item) => item.capture_modes.includes('package')).map((item) => <fieldset key={item.id}><legend>{item.path}</legend>{item.capture_modes.includes('payload') && <label className="choice"><input type="radio" checked={choices[item.id]?.mode === 'payload'} onChange={() => setChoices({ ...choices, [item.id]: { mode: 'payload' } })} /><span><strong>Raw file</strong><small>Capture this JAR as ordinary payload.</small></span></label>}<label className="choice"><input type="radio" checked={choices[item.id]?.mode === 'package' && !choices[item.id]?.provider_match} onChange={() => setChoices({ ...choices, [item.id]: { mode: 'package' } })} /><span><strong>Local package</strong><small>Store a verified local SHA-256 artifact.</small></span></label>{(item.provider_candidates || []).filter((candidate) => candidate.provider !== 'local').map((candidate) => <label className="choice" key={`${candidate.provider}:${candidate.version_id}`}><input type="radio" checked={choices[item.id]?.provider_match === candidate} onChange={() => setChoices({ ...choices, [item.id]: { mode: 'package', provider_match: candidate } })} /><span><strong>{String(candidate.provider)} · {String(candidate.version || candidate.version_id)}</strong><small>{String(candidate.confidence)} provider match</small></span></label>)}{choices[item.id]?.provider_match?.confidence === 'fuzzy' && <label><input type="checkbox" checked={choices[item.id]?.fuzzyConfirmed || false} onChange={(event) => setChoices({ ...choices, [item.id]: { ...choices[item.id], fuzzyConfirmed: event.target.checked } })} /> I reviewed and confirm this fuzzy match</label>}</fieldset>)}</div><footer><button onClick={() => setPlan(null)}>Cancel</button><button className="primary" disabled={!!busy} onClick={() => void submitPlan()}>Send all atomically</button></footer></section></div>}
  </>;
}
