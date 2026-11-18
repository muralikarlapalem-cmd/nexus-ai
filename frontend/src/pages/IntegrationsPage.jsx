import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { formatDistanceToNow, format } from 'date-fns'
import {
  Plug, Plus, RefreshCw, PlugZap, Pencil, Power, ChevronDown, ChevronRight,
  CheckCircle2, XCircle, AlertTriangle, Loader2, X, KeyRound,
} from 'lucide-react'
import { integrationsApi } from '../api/tickets'
import { useAuthStore } from '../store/authStore'

const STATUS_STYLE = {
  success: 'bg-green-900/40 text-green-400',
  partial: 'bg-yellow-900/40 text-yellow-400',
  failed: 'bg-red-900/40 text-red-400',
  running: 'bg-indigo-900/40 text-indigo-300',
  skipped: 'bg-nexus-border text-nexus-muted',
}

const errorText = (err) => {
  const detail = err?.response?.data?.detail
  if (Array.isArray(detail)) return detail.map(d => d.msg).join('; ')
  return detail || err?.message || 'Something went wrong'
}

function StatusPill({ status }) {
  if (!status) return <span className="text-xs text-nexus-muted">Never synced</span>
  return <span className={`text-xs px-2 py-0.5 rounded-full font-medium ${STATUS_STYLE[status] || STATUS_STYLE.skipped}`}>{status}</span>
}

function Toggle({ label, checked, onChange, hint, disabled }) {
  return (
    <label className={`flex items-start gap-3 py-1.5 ${disabled ? 'opacity-50' : 'cursor-pointer'}`}>
      <input type="checkbox" className="mt-0.5 accent-indigo-500" checked={!!checked} disabled={disabled}
        onChange={e => onChange(e.target.checked)} />
      <span>
        <span className="text-sm text-white">{label}</span>
        {hint && <span className="block text-xs text-nexus-muted">{hint}</span>}
      </span>
    </label>
  )
}

// ── Add / edit form ─────────────────────────────────────────
function ConfigInput({ field, value, onChange }) {
  const common = 'w-full bg-nexus-bg border border-nexus-border rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-indigo-500'
  if (field.type === 'select') {
    return (
      <select className={common} value={value ?? field.default ?? ''} onChange={e => onChange(e.target.value)}>
        {(field.options || []).map(o => <option key={o} value={o}>{o}</option>)}
      </select>
    )
  }
  if (field.type === 'json') {
    return (
      <textarea rows={4} className={`${common} font-mono text-xs`} value={value ?? ''}
        onChange={e => onChange(e.target.value)} placeholder="{ }" />
    )
  }
  return (
    <input className={`${common} ${field.type === 'env_var' ? 'font-mono text-xs' : ''}`}
      type={field.type === 'number' ? 'number' : 'text'}
      value={value ?? ''} placeholder={field.default != null && field.type !== 'json' ? String(field.default) : ''}
      onChange={e => onChange(e.target.value)} />
  )
}

function ConnectionForm({ types, existing, onClose }) {
  const qc = useQueryClient()
  const [typeKey, setTypeKey] = useState(
    existing?.connector_type || types.find(t => t.type === 'servicenow')?.type || types[0]?.type)
  const type = types.find(t => t.type === typeKey)
  const [name, setName] = useState(existing?.name || '')
  const [values, setValues] = useState(() => {
    const cfg = existing?.config || {}
    const out = {}
    Object.entries(cfg).forEach(([k, v]) => { out[k] = typeof v === 'object' && v !== null ? JSON.stringify(v, null, 2) : v })
    return out
  })
  const [settings, setSettings] = useState({
    sync_interval_minutes: existing?.sync_interval_minutes ?? 5,
    auto_run_agents: existing?.auto_run_agents ?? true,
    write_back_notes: existing?.write_back_notes ?? true,
    write_back_status: existing?.write_back_status ?? false,
    webhook_secret_env: existing?.webhook_secret_env ?? '',
    enabled: existing?.enabled ?? true,
  })
  const [formError, setFormError] = useState('')
  const [testResult, setTestResult] = useState(null)

  // Build the config object, parsing JSON fields; returns null and sets an error if invalid
  const buildConfig = () => {
    const config = {}
    for (const field of type?.config_fields || []) {
      let v = values[field.key]
      if (v === undefined || v === '') continue
      if (field.type === 'number') v = Number(v)
      if (field.type === 'json') {
        try { v = JSON.parse(v) } catch { setFormError(`"${field.label}" is not valid JSON`); return null }
      }
      config[field.key] = v
    }
    if (values.mappings) {
      try { config.mappings = typeof values.mappings === 'string' ? JSON.parse(values.mappings) : values.mappings }
      catch { setFormError('"Value mappings" is not valid JSON'); return null }
    }
    return config
  }

  const test = useMutation({
    mutationFn: (config) => integrationsApi.testSettings({ connector_type: typeKey, config }).then(r => r.data),
    onSuccess: setTestResult,
    onError: (e) => setTestResult({ ok: false, message: errorText(e) }),
  })

  const save = useMutation({
    mutationFn: (payload) => existing
      ? integrationsApi.update(existing.id, payload)
      : integrationsApi.create(payload),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['integrations'] }); onClose() },
    onError: (e) => setFormError(errorText(e)),
  })

  const submit = () => {
    setFormError('')
    const config = buildConfig()
    if (!config) return
    const payload = {
      name: name.trim(), config,
      ...settings,
      sync_interval_minutes: Number(settings.sync_interval_minutes) || 5,
      webhook_secret_env: settings.webhook_secret_env?.trim() || null,
    }
    if (!existing) payload.connector_type = typeKey
    save.mutate(payload)
  }

  const runTest = () => {
    setFormError(''); setTestResult(null)
    const config = buildConfig()
    if (config) test.mutate(config)
  }

  return (
    <div className="fixed inset-0 bg-black/60 flex items-center justify-center z-50 p-4">
      <div className="bg-nexus-surface border border-nexus-border rounded-2xl w-full max-w-2xl max-h-[90vh] flex flex-col">
        <div className="flex items-center justify-between px-6 py-4 border-b border-nexus-border">
          <h2 className="text-lg font-semibold text-white">{existing ? `Edit ${existing.name}` : 'Add connection'}</h2>
          <button onClick={onClose} className="text-nexus-muted hover:text-white" aria-label="Close"><X size={18} /></button>
        </div>

        <div className="overflow-y-auto px-6 py-5 space-y-5">
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
            <div>
              <label className="text-xs text-nexus-muted mb-1.5 block">System</label>
              <select disabled={!!existing} value={typeKey}
                onChange={e => { setTypeKey(e.target.value); setValues({}); setTestResult(null) }}
                className="w-full bg-nexus-bg border border-nexus-border rounded-lg px-3 py-2 text-sm text-white disabled:opacity-60">
                {types.map(t => <option key={t.type} value={t.type}>{t.display_name}</option>)}
              </select>
            </div>
            <div>
              <label className="text-xs text-nexus-muted mb-1.5 block">Connection name</label>
              <input value={name} onChange={e => setName(e.target.value)} placeholder="e.g. ServiceNow (production)"
                className="w-full bg-nexus-bg border border-nexus-border rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-indigo-500" />
            </div>
          </div>
          {type?.description && <p className="text-xs text-nexus-muted -mt-2">{type.description}</p>}

          <section>
            <h3 className="text-sm font-semibold text-white mb-1">Connection settings</h3>
            <p className="text-xs text-nexus-muted mb-3">
              <KeyRound size={12} className="inline -mt-0.5 mr-1.5" />
              Passwords and tokens stay in your <code className="text-indigo-300">.env</code> file.
              Enter the variable name here (it must start with INTEGRATION_).
            </p>
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
              {(type?.config_fields || []).map(field => (
                <div key={field.key} className={field.type === 'json' ? 'sm:col-span-2' : ''}>
                  <label className="text-xs text-nexus-muted mb-1.5 block">
                    {field.label}{field.required && <span className="text-red-400"> *</span>}
                  </label>
                  <ConfigInput field={field} value={values[field.key]}
                    onChange={v => setValues(s => ({ ...s, [field.key]: v }))} />
                  {field.help && <p className="text-[11px] text-nexus-muted mt-1">{field.help}</p>}
                </div>
              ))}
              <div className="sm:col-span-2">
                <label className="text-xs text-nexus-muted mb-1.5 block">Value mappings (optional)</label>
                <textarea rows={3} value={values.mappings ?? ''}
                  onChange={e => setValues(s => ({ ...s, mappings: e.target.value }))}
                  placeholder={type ? JSON.stringify({ priority: type.default_mappings?.priority || {} }) : ''}
                  className="w-full bg-nexus-bg border border-nexus-border rounded-lg px-3 py-2 text-xs font-mono text-white focus:outline-none focus:border-indigo-500" />
                <p className="text-[11px] text-nexus-muted mt-1">Overrides how source values become NEXUS priority, category and status. Leave empty to use the defaults.</p>
              </div>
            </div>
          </section>

          <section>
            <h3 className="text-sm font-semibold text-white mb-2">What NEXUS does</h3>
            <div className="flex items-center gap-3 mb-2">
              <label className="text-sm text-white" htmlFor="interval">Check for changes every</label>
              <input id="interval" type="number" min={1} max={1440} value={settings.sync_interval_minutes}
                onChange={e => setSettings(s => ({ ...s, sync_interval_minutes: e.target.value }))}
                className="w-20 bg-nexus-bg border border-nexus-border rounded-lg px-2 py-1 text-sm text-white" />
              <span className="text-sm text-nexus-muted">minutes</span>
            </div>
            <Toggle label="Run the agents on new tickets" checked={settings.auto_run_agents}
              onChange={v => setSettings(s => ({ ...s, auto_run_agents: v }))}
              hint="Only tickets that arrive open. You can still run agents by hand." />
            <Toggle label="Post agent results back as internal notes" checked={settings.write_back_notes}
              onChange={v => setSettings(s => ({ ...s, write_back_notes: v }))}
              hint="ServiceNow: a work note, visible to IT staff only" />
            <Toggle label="Also set the ticket to Resolved in the source system" checked={settings.write_back_status}
              disabled={!type?.supports_status_update}
              onChange={v => setSettings(s => ({ ...s, write_back_status: v }))}
              hint="Off by default: the source system's team keeps control of status" />
            {type?.supports_webhook && (
              <div className="mt-3">
                <label className="text-xs text-nexus-muted mb-1.5 block">Webhook secret variable (optional)</label>
                <input value={settings.webhook_secret_env}
                  onChange={e => setSettings(s => ({ ...s, webhook_secret_env: e.target.value }))}
                  placeholder="INTEGRATION_SERVICENOW_WEBHOOK_SECRET"
                  className="w-full bg-nexus-bg border border-nexus-border rounded-lg px-3 py-2 text-xs font-mono text-white" />
                <p className="text-[11px] text-nexus-muted mt-1">Lets the source system push changes instantly instead of waiting for the next check.</p>
              </div>
            )}
          </section>

        </div>

        {(testResult || formError) && (
          <div role="status" className="px-6 pt-3 border-t border-nexus-border space-y-2">
            {testResult && (
              <div className={`rounded-lg px-3 py-2 text-sm flex items-start gap-2 ${testResult.ok ? 'bg-green-900/30 text-green-300' : 'bg-red-900/30 text-red-300'}`}>
                {testResult.ok ? <CheckCircle2 size={16} className="mt-0.5 flex-shrink-0" /> : <XCircle size={16} className="mt-0.5 flex-shrink-0" />}
                <span>{testResult.message}</span>
              </div>
            )}
            {formError && <p className="text-sm text-red-400">{formError}</p>}
          </div>
        )}

        <div className={`flex gap-3 px-6 py-4 ${testResult || formError ? '' : 'border-t border-nexus-border'}`}>
          <button onClick={runTest} disabled={test.isPending}
            className="flex items-center gap-2 border border-nexus-border text-nexus-muted hover:text-white rounded-lg px-4 py-2 text-sm disabled:opacity-50">
            {test.isPending ? <Loader2 size={14} className="animate-spin" /> : <PlugZap size={14} />} Test settings
          </button>
          <div className="flex-1" />
          <button onClick={onClose} className="border border-nexus-border text-nexus-muted hover:text-white rounded-lg px-4 py-2 text-sm">Cancel</button>
          <button onClick={submit} disabled={save.isPending || name.trim().length < 3}
            className="bg-indigo-600 hover:bg-indigo-500 disabled:opacity-50 text-white rounded-lg px-4 py-2 text-sm font-medium flex items-center gap-2">
            {save.isPending && <Loader2 size={14} className="animate-spin" />}{existing ? 'Save changes' : 'Add connection'}
          </button>
        </div>
      </div>
    </div>
  )
}

// ── Sync history ────────────────────────────────────────────
function SyncHistory({ connectionId }) {
  const { data: runs, isLoading } = useQuery({
    queryKey: ['integrations', connectionId, 'runs'],
    queryFn: () => integrationsApi.runs(connectionId, { limit: 15 }).then(r => r.data),
    refetchInterval: 10_000,
  })
  if (isLoading) return <p className="text-xs text-nexus-muted px-5 py-4">Loading…</p>
  if (!runs?.length) return <p className="text-xs text-nexus-muted px-5 py-4">No syncs yet.</p>
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-xs">
        <thead>
          <tr className="text-nexus-muted border-b border-nexus-border">
            {['Started', 'Trigger', 'Result', 'Read', 'New', 'Changed', 'Same', 'Failed', 'Agents'].map(h =>
              <th key={h} className="text-left font-medium px-4 py-2">{h}</th>)}
          </tr>
        </thead>
        <tbody>
          {runs.map(r => (
            <tr key={r.id} className="border-b border-nexus-border last:border-0 align-top">
              <td className="px-4 py-2 text-nexus-muted whitespace-nowrap">{format(new Date(r.started_at), 'MMM d, HH:mm:ss')}</td>
              <td className="px-4 py-2 text-nexus-muted capitalize">{r.trigger}</td>
              <td className="px-4 py-2">
                <StatusPill status={r.status} />
                {r.error && <p className="text-red-300 mt-1 max-w-xs">{r.error}</p>}
                {r.details?.errors?.slice(0, 3).map((e, i) =>
                  <p key={i} className="text-nexus-muted mt-0.5 max-w-xs truncate" title={e.error}>{e.external}: {e.error}</p>)}
              </td>
              {[r.fetched, r.created, r.updated, r.unchanged, r.failed, r.agents_queued].map((n, i) =>
                <td key={i} className={`px-4 py-2 tabular-nums ${i === 4 && n ? 'text-red-400' : 'text-white'}`}>{n}</td>)}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

// ── One connection ──────────────────────────────────────────
function ConnectionCard({ connection, isAdmin, canSync, onEdit }) {
  const qc = useQueryClient()
  const [open, setOpen] = useState(false)
  const [notice, setNotice] = useState(null)
  const refresh = () => qc.invalidateQueries({ queryKey: ['integrations'] })

  const sync = useMutation({
    mutationFn: () => integrationsApi.sync(connection.id).then(r => r.data),
    onSuccess: () => {
      setNotice({ ok: true, message: 'Sync started. Results appear below in a few seconds.' })
      setOpen(true)
      setTimeout(refresh, 2500); setTimeout(refresh, 8000)
    },
    onError: e => setNotice({ ok: false, message: errorText(e) }),
  })
  const test = useMutation({
    mutationFn: () => integrationsApi.test(connection.id).then(r => r.data),
    onSuccess: setNotice,
    onError: e => setNotice({ ok: false, message: errorText(e) }),
  })
  const toggle = useMutation({
    mutationFn: () => integrationsApi.update(connection.id, { enabled: !connection.enabled }),
    onSuccess: refresh,
    onError: e => setNotice({ ok: false, message: errorText(e) }),
  })

  const missing = Object.entries(connection.credentials || {}).filter(([, set]) => !set).map(([k]) => k)
  const lastSync = connection.last_run?.started_at

  return (
    <div className={`bg-nexus-surface border rounded-xl ${connection.enabled ? 'border-nexus-border' : 'border-nexus-border opacity-75'}`}>
      <div className="p-5">
        <div className="flex flex-wrap items-start gap-4">
          <div className="w-10 h-10 rounded-lg bg-emerald-900/30 border border-emerald-800/50 flex items-center justify-center flex-shrink-0">
            <Plug size={18} className="text-emerald-300" />
          </div>
          <div className="flex-1 min-w-[200px]">
            <div className="flex flex-wrap items-center gap-2">
              <h2 className="text-base font-semibold text-white">{connection.name}</h2>
              <span className="text-xs text-nexus-muted">{connection.display_name}</span>
              {!connection.enabled && <span className="text-xs px-2 py-0.5 rounded-full bg-nexus-border text-nexus-muted">Off</span>}
            </div>
            <p className="text-xs text-nexus-muted mt-1 font-mono truncate">
              {connection.config.instance_url || connection.config.base_url}
            </p>
          </div>
          <div className="flex flex-wrap gap-2">
            {canSync && (
              <button onClick={() => sync.mutate()} disabled={sync.isPending || !connection.enabled}
                className="flex items-center gap-1.5 bg-indigo-600 hover:bg-indigo-500 disabled:opacity-50 text-white rounded-lg px-3 py-1.5 text-xs font-medium">
                {sync.isPending ? <Loader2 size={12} className="animate-spin" /> : <RefreshCw size={12} />} Sync now
              </button>
            )}
            {isAdmin && <>
              <button onClick={() => test.mutate()} disabled={test.isPending}
                className="flex items-center gap-1.5 border border-nexus-border text-nexus-muted hover:text-white rounded-lg px-3 py-1.5 text-xs">
                {test.isPending ? <Loader2 size={12} className="animate-spin" /> : <PlugZap size={12} />} Test
              </button>
              <button onClick={() => onEdit(connection)}
                className="flex items-center gap-1.5 border border-nexus-border text-nexus-muted hover:text-white rounded-lg px-3 py-1.5 text-xs">
                <Pencil size={12} /> Edit
              </button>
              <button onClick={() => toggle.mutate()} disabled={toggle.isPending}
                className="flex items-center gap-1.5 border border-nexus-border text-nexus-muted hover:text-white rounded-lg px-3 py-1.5 text-xs">
                <Power size={12} /> {connection.enabled ? 'Turn off' : 'Turn on'}
              </button>
            </>}
          </div>
        </div>

        <dl className="grid grid-cols-2 lg:grid-cols-4 gap-3 mt-5">
          <div className="bg-nexus-bg rounded-lg p-3">
            <dt className="text-xs text-nexus-muted">Last sync</dt>
            <dd className="mt-1 flex items-center gap-2">
              <StatusPill status={connection.last_run?.status || connection.last_sync_status} />
              {lastSync && <span className="text-xs text-nexus-muted">{formatDistanceToNow(new Date(lastSync), { addSuffix: true })}</span>}
            </dd>
          </div>
          <div className="bg-nexus-bg rounded-lg p-3">
            <dt className="text-xs text-nexus-muted">Tickets from here</dt>
            <dd className="text-white font-bold mt-1 tabular-nums">{connection.linked_tickets}</dd>
          </div>
          <div className="bg-nexus-bg rounded-lg p-3">
            <dt className="text-xs text-nexus-muted">Checks every</dt>
            <dd className="text-white font-bold mt-1">{connection.sync_interval_minutes} min</dd>
          </div>
          <div className="bg-nexus-bg rounded-lg p-3">
            <dt className="text-xs text-nexus-muted">On new tickets</dt>
            <dd className="text-xs text-white mt-1 leading-relaxed">
              {connection.auto_run_agents ? 'Agents run' : 'Agents off'}
              {' · '}{connection.write_back_notes ? 'notes posted back' : 'no notes'}
              {connection.write_back_status && ' · status synced'}
            </dd>
          </div>
        </dl>

        {missing.length > 0 && (
          <p className="mt-4 text-xs text-yellow-300 flex items-start gap-2">
            <AlertTriangle size={14} className="flex-shrink-0 mt-0.5" />
            <span>Not set in .env: <span className="font-mono">{missing.join(', ')}</span>. Add {missing.length > 1 ? 'them' : 'it'} and restart the api and celery-worker containers.</span>
          </p>
        )}
        {connection.last_error && (
          <p className="mt-3 text-xs text-red-300 flex items-start gap-2">
            <XCircle size={14} className="flex-shrink-0 mt-0.5" /> {connection.last_error}
          </p>
        )}
        {connection.webhook_url && (
          <p className="mt-3 text-xs text-nexus-muted">
            Push URL for the source system: <span className="font-mono text-indigo-300">{window.location.protocol}//{window.location.hostname}:8000{connection.webhook_url}</span>
          </p>
        )}
        {notice && (
          <p role="status" className={`mt-3 text-xs flex items-start gap-2 ${notice.ok ? 'text-green-300' : 'text-red-300'}`}>
            {notice.ok ? <CheckCircle2 size={14} className="flex-shrink-0 mt-0.5" /> : <XCircle size={14} className="flex-shrink-0 mt-0.5" />}
            {notice.message}
          </p>
        )}
      </div>

      <button onClick={() => setOpen(o => !o)} aria-expanded={open}
        className="w-full flex items-center gap-2 px-5 py-2.5 border-t border-nexus-border text-xs text-nexus-muted hover:text-white">
        {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />} Sync history
      </button>
      {open && <div className="border-t border-nexus-border"><SyncHistory connectionId={connection.id} /></div>}
    </div>
  )
}

// ── Page ────────────────────────────────────────────────────
export default function IntegrationsPage() {
  const user = useAuthStore(s => s.user)
  const isAdmin = user?.role === 'admin'
  const canSync = ['admin', 'agent'].includes(user?.role)
  const [editing, setEditing] = useState(null)      // null | 'new' | connection

  const { data: connections, isLoading, error } = useQuery({
    queryKey: ['integrations'],
    queryFn: () => integrationsApi.list().then(r => r.data),
    refetchInterval: 15_000,
  })
  const { data: types } = useQuery({
    queryKey: ['integrations', 'types'],
    queryFn: () => integrationsApi.types().then(r => r.data),
    staleTime: Infinity,
  })

  const totals = useMemo(() => ({
    tickets: (connections || []).reduce((n, c) => n + c.linked_tickets, 0),
    on: (connections || []).filter(c => c.enabled).length,
  }), [connections])

  useEffect(() => { document.title = 'Integrations · NEXUS AI' }, [])

  return (
    <div className="p-8 max-w-6xl">
      <div className="flex flex-wrap items-start justify-between gap-4 mb-6">
        <div>
          <h1 className="text-2xl font-bold text-white">Integrations</h1>
          <p className="text-nexus-muted text-sm mt-1 max-w-2xl">
            Bring tickets in from ServiceNow and other systems. NEXUS runs its agents on new tickets and posts the results back.
          </p>
          {connections?.length > 0 && (
            <p className="text-xs text-nexus-muted mt-2">{totals.on} of {connections.length} connections on · {totals.tickets} tickets imported</p>
          )}
        </div>
        {isAdmin && types?.length > 0 && (
          <button onClick={() => setEditing('new')}
            className="flex items-center gap-2 bg-indigo-600 hover:bg-indigo-500 text-white rounded-lg px-4 py-2 text-sm font-medium">
            <Plus size={14} /> Add connection
          </button>
        )}
      </div>

      {isLoading && <p className="text-nexus-muted text-sm">Loading…</p>}
      {error && <p className="text-red-400 text-sm">{errorText(error)}</p>}

      {connections?.length === 0 && (
        <div className="bg-nexus-surface border border-dashed border-nexus-border rounded-xl p-10 text-center">
          <Plug size={28} className="text-nexus-muted mx-auto mb-3" />
          <h2 className="text-white font-semibold">No connections yet</h2>
          <p className="text-sm text-nexus-muted mt-1 max-w-md mx-auto">
            Connect ServiceNow (or the mock ServiceNow that runs in Docker) to start importing tickets.
          </p>
          {isAdmin
            ? <button onClick={() => setEditing('new')} className="mt-4 bg-indigo-600 hover:bg-indigo-500 text-white rounded-lg px-4 py-2 text-sm font-medium">Add connection</button>
            : <p className="text-xs text-nexus-muted mt-3">Ask an admin to add one.</p>}
        </div>
      )}

      <div className="space-y-4">
        {connections?.map(c => (
          <ConnectionCard key={c.id} connection={c} isAdmin={isAdmin} canSync={canSync} onEdit={setEditing} />
        ))}
      </div>

      {editing && types && (
        <ConnectionForm types={types} existing={editing === 'new' ? null : editing} onClose={() => setEditing(null)} />
      )}
    </div>
  )
}
