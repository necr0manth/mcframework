import { useEffect, useId, useState } from 'react';
import { makeRoutingRule, metadataWrite, supportsRoutingSection } from './routing-model.js';

export type MetadataWrite = { file?: string; section?: string };
type RoutingRecord = { id: string; [key: string]: unknown };
type Options = { schema?: number; files: string[]; sections: string[]; effective_rules: unknown[];
  own_rule_ids?: string[]; layer_rules?: RoutingRecord[]; instance_rules?: RoutingRecord[] };
type Preview = { writes: Array<{ file: string; document: unknown }> };
type UpgradePreview = { target_layer: string; files: string[]; from_schema: number;
  to_schema: number; already_current: boolean; revision: number };
type RoutingClient = {
  routingOptions: (target: string) => Promise<Options>;
  previewRouting: (operation: Record<string, unknown>, target: string, write?: MetadataWrite,
    origin?: string) => Promise<Preview>;
};

export function WriteDestination({ client, target, operation, value, onChange, origin = 'user' }: {
  client: RoutingClient; target: string; operation: Record<string, unknown>;
  value?: MetadataWrite; onChange: (value?: MetadataWrite) => void; origin?: string;
}) {
  const id = useId();
  const [options, setOptions] = useState<Options | null>(null);
  const [result, setResult] = useState<{ key: string; preview?: Preview; error?: string } | null>(null);
  const [loading, setLoading] = useState(false);
  const sectionSupported = supportsRoutingSection(operation);
  const key = JSON.stringify([target, operation, value, origin]);
  useEffect(() => {
    let current = true;
    client.routingOptions(target).then((next) => { if (current) setOptions(next); })
      .catch(() => { if (current) setOptions(null); });
    return () => { current = false; };
  }, [client, target]);
  async function preview() {
    setLoading(true);
    try {
      const next = await client.previewRouting(operation, target, value, origin);
      setResult({ key, preview: next });
    } catch (reason) {
      setResult({ key, error: reason instanceof Error ? reason.message : 'Could not preview routing' });
    } finally { setLoading(false); }
  }
  return <fieldset className="routing-fields"><legend>Metadata destination · this write only</legend>
    <p>Leave blank to use rules. Existing declarations stay in their source file. This choice does not save a rule.</p>
    <label><span>Metadata file (from layer root)</span><input list={`${id}-files`} value={value?.file || ''} placeholder="Automatic"
      onChange={(event) => onChange(metadataWrite(event.target.value, sectionSupported ? value?.section || '' : ''))} /></label>
    <datalist id={`${id}-files`}>{options?.files.map((file) => <option key={file} value={file} />)}</datalist>
    {sectionSupported ? <><label><span>Section</span><input list={`${id}-sections`} value={value?.section || ''} placeholder="Automatic"
      onChange={(event) => onChange(metadataWrite(value?.file || '', event.target.value))} /></label>
    <datalist id={`${id}-sections`}>{options?.sections.map((section) => <option key={section} value={section} />)}</datalist></> :
      <small>Sections group packages and groups. This record has only a file destination.</small>}
    <button type="button" className="secondary" disabled={loading} onClick={() => void preview()}>{loading ? 'Checking…' : 'Preview rule match'}</button>
    <small>Preview uses the operation fields currently known here. Provider metadata is used only when available.</small>
    {result?.key === key && result.error && <p className="inline-error" role="alert">{result.error}</p>}
    {result?.key === key && result.preview && <div role="status">{result.preview.writes.map((write) => <details key={write.file}><summary>{write.file}</summary><pre>{JSON.stringify(write.document, null, 2)}</pre></details>)}</div>}
  </fieldset>;
}

export function RoutingRuleEditor({ client, target, busy, onSave, onUpgrade, onDelete }: {
  client: RoutingClient & { previewSchemaUpgrade: (target: string) => Promise<UpgradePreview> };
  target: string; busy: boolean;
  onSave: (rule: Record<string, unknown>, scope: string, file: string) => Promise<boolean>;
  onUpgrade: (revision: number) => Promise<boolean>;
  onDelete: (id: string, scope: string) => Promise<boolean>;
}) {
  const [form, setForm] = useState({ id: '', priority: '0', types: 'package', packageKind: 'mod',
    origin: '', categories: '', file: '../mods/.pack/mods.json', section: '', pathMode: 'local' });
  const [scope, setScope] = useState('layer');
  const [source, setSource] = useState('.pack/routing.json');
  const [advanced, setAdvanced] = useState(false);
  const [raw, setRaw] = useState('');
  const [error, setError] = useState('');
  const [options, setOptions] = useState<Options | null>(null);
  const [upgrade, setUpgrade] = useState<{ target: string; preview: UpgradePreview } | null>(null);
  const [checking, setChecking] = useState(false);
  const [deleteChoice, setDeleteChoice] = useState<{ id: string; scope: string } | null>(null);
  useEffect(() => {
    let current = true;
    client.routingOptions(target).then((next) => { if (current) setOptions(next); })
      .catch((reason) => { if (current) setError(String(reason)); });
    return () => { current = false; };
  }, [client, target]);
  function edit(key: keyof typeof form, value: string) {
    setForm({ ...form, [key]: value, ...(key === 'types' ? { section: '' } : {}) });
  }
  async function save() {
    try {
      setError('');
      const rule = advanced ? JSON.parse(raw) : makeRoutingRule(form);
      if (!rule || typeof rule !== 'object' || Array.isArray(rule)) throw new Error('Rule must be an object');
      await onSave(rule, scope, source);
    } catch (reason) { setError(reason instanceof Error ? reason.message : 'Invalid routing rule'); }
  }
  async function previewUpgrade() {
    setChecking(true);
    setError('');
    try { setUpgrade({ target, preview: await client.previewSchemaUpgrade(target) }); }
    catch (reason) { setError(reason instanceof Error ? reason.message : 'Could not preview upgrade'); }
    finally { setChecking(false); }
  }
  async function applyUpgrade() {
    if (!upgrade || upgrade.target !== target) return;
    try {
      setError('');
      if (await onUpgrade(upgrade.preview.revision)) {
        setUpgrade(null);
        setOptions(await client.routingOptions(target));
      }
    } catch (reason) { setError(reason instanceof Error ? reason.message : 'Could not upgrade layer'); }
  }
  async function deleteRule() {
    if (!deleteChoice) return;
    try {
      setError('');
      if (await onDelete(deleteChoice.id, deleteChoice.scope)) {
        setDeleteChoice(null);
        setOptions(await client.routingOptions(target));
      }
    } catch (reason) { setError(reason instanceof Error ? reason.message : 'Could not remove rule'); }
  }
  return <div className="routing-rule-editor">
    <p>Rules affect future writes. Saving with an existing ID overrides that rule. Existing declarations are not moved.</p>
    {options?.schema === 1 && <section className="callout" aria-label="Metadata schema upgrade">
      <h3>Nested metadata requires schema 2</h3><p>This layer currently uses schema 1. Preview an explicit upgrade to preserve existing path meanings and enable nested metadata defaults. No files change until you confirm.</p>
      <button className="secondary" disabled={busy || checking} onClick={() => void previewUpgrade()}>{checking ? 'Checking…' : 'Preview schema upgrade'}</button>
      {upgrade?.target === target && <div><p>Schema {upgrade.preview.from_schema} → {upgrade.preview.to_schema}. Files to update:</p>
        <ul>{upgrade.preview.files.map((file) => <li key={file}><code>{file}</code></li>)}</ul>
        <button className="primary" disabled={busy || checking} onClick={() => void applyUpgrade()}>Upgrade selected layer to schema 2</button>
      </div>}
    </section>}
    <label><span>Save rule in</span><select value={scope} onChange={(event) => setScope(event.target.value)}><option value="layer">Selected layer · tracked and inherited</option><option value="instance">This instance · local state only</option></select></label>
    {scope === 'layer' && <label><span>Rule declaration file (from layer root)</span><input value={source} onChange={(event) => setSource(event.target.value)} /></label>}
    {scope === 'instance' && <p>Local destinations resolve relative to the selected layer’s .pack directory.</p>}
    <label className="choice"><input type="checkbox" checked={advanced} onChange={(event) => {
      if (event.target.checked && !raw) {
        try { setRaw(JSON.stringify(makeRoutingRule({ ...form, id: form.id || 'my-rule' }), null, 2)); }
        catch { setRaw('{"type":"routing_rule","id":"my-rule"}'); }
      }
      setAdvanced(event.target.checked);
    }} /> Advanced JSON record</label>
    {advanced ? <label><span>Routing rule record</span><textarea rows={15} value={raw} onChange={(event) => setRaw(event.target.value)} /></label> : <>
      <label><span>Rule ID</span><input value={form.id} onChange={(event) => edit('id', event.target.value)} placeholder="mods.magic" /></label>
      <label><span>Priority (higher wins)</span><input type="number" step="1" value={form.priority} onChange={(event) => edit('priority', event.target.value)} /></label>
      <label><span>Operation types (comma-separated; blank = any)</span><input value={form.types} onChange={(event) => edit('types', event.target.value)} /></label>
      <label><span>Package kind</span><select value={form.packageKind} onChange={(event) => edit('packageKind', event.target.value)}><option value="">Any</option><option value="mod">Mod</option><option value="resourcepack">Resource pack</option><option value="datapack">Data pack</option></select></label>
      <label><span>Operation origin</span><select value={form.origin} onChange={(event) => edit('origin', event.target.value)}><option value="">Any</option><option value="user">User</option><option value="automatic_dependency">Automatic dependency</option><option value="instance_capture">Instance capture</option></select></label>
      <label><span>Package categories (all, comma-separated)</span><input value={form.categories} onChange={(event) => edit('categories', event.target.value)} placeholder="magic, library" /></label>
      <label><span>Destination path base</span><select value={form.pathMode} onChange={(event) => edit('pathMode', event.target.value)}><option value="local">Relative to rule declaration file</option><option value="layer">From layer root</option></select></label>
      <label><span>Destination metadata file</span><input value={form.file} onChange={(event) => edit('file', event.target.value)} /></label>
      {form.types.split(',').map((type) => type.trim()).every((type) => supportsRoutingSection({ type, id: 'preview' })) &&
        <label><span>Destination section (optional)</span><input value={form.section} onChange={(event) => edit('section', event.target.value)} /></label>}
    </>}
    {error && <p className="inline-error" role="alert">{error}</p>}
    <button className="primary" disabled={busy} onClick={() => void save()}>Save {scope === 'layer' ? 'layer' : 'instance'} rule</button>
    <section aria-label="Saved routing rules"><h3>Saved rules you can remove</h3><p>Removing an override reveals any inherited rule with the same ID. Inherited rules must be edited in their own layer.</p>
      {[{ scope: 'layer', records: options?.layer_rules?.filter((rule) => options.own_rule_ids?.includes(rule.id)) || [] }, { scope: 'instance', records: options?.instance_rules || [] }].map((group) => <div key={group.scope}>
        {group.records.map((rule) => <p key={rule.id}><code>{rule.id}</code> · {group.scope} <button className="secondary" disabled={busy} onClick={() => setDeleteChoice({ id: rule.id, scope: group.scope })}>Remove {group.scope} rule {rule.id}</button></p>)}
      </div>)}
      {deleteChoice && <div className="callout"><p>Remove {deleteChoice.scope} rule <strong>{deleteChoice.id}</strong>?</p><button className="secondary" onClick={() => setDeleteChoice(null)}>Cancel removal</button><button className="primary" disabled={busy} onClick={() => void deleteRule()}>Confirm rule removal</button></div>}
    </section>
    <details><summary>Effective inherited and local rules</summary><pre>{JSON.stringify(options?.effective_rules || [], null, 2)}</pre></details>
  </div>;
}
