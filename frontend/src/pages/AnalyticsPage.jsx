import { useState } from 'react'
import { useQuery, keepPreviousData } from '@tanstack/react-query'
import { LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer } from 'recharts'
import { ArrowDownRight, ArrowUpRight, Minus } from 'lucide-react'
import { format, parseISO } from 'date-fns'
import clsx from 'clsx'
import { analyticsApi } from '../api/tickets'

const PERIODS = [7, 30, 90]

// Chart colours, checked for colour-blind separation and contrast on the card surface (#131929)
const SERIES = {
  created:  { label: 'Created',  color: '#3987e5' },
  resolved: { label: 'Resolved', color: '#d95926' },
}
const BAR = '#3987e5'
const GRID = '#1e2a45'
const AXIS_TICK = { fill: '#64748b', fontSize: 12 }

const TIME_ZONE = Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'

const titleCase = (s) => s.charAt(0).toUpperCase() + s.slice(1)
const day = (iso) => format(parseISO(iso), 'MMM d')

function formatDuration(hours) {
  if (hours == null) return '—'
  if (hours < 1) return `${Math.round(hours * 60)} min`
  if (hours < 48) return `${hours.toFixed(1)} h`
  return `${(hours / 24).toFixed(1)} d`
}

/** Change against the previous period. `goodWhen` is 'up', 'down' or null (neither is better). */
function Delta({ change, text, goodWhen }) {
  if (change == null) return <span className="text-xs text-nexus-muted">No earlier data to compare</span>
  const Icon = change > 0 ? ArrowUpRight : change < 0 ? ArrowDownRight : Minus
  const good = goodWhen && change !== 0 && (change > 0) === (goodWhen === 'up')
  const bad = goodWhen && change !== 0 && !good
  return (
    <span className="flex items-center gap-1 text-xs text-nexus-muted">
      <Icon size={13} className={clsx(good && 'text-green-400', bad && 'text-red-400')} />
      <span className={clsx(good && 'text-green-400', bad && 'text-red-400')}>{text}</span>
      <span>vs previous period</span>
    </span>
  )
}

function Tile({ label, value, sub, children }) {
  return (
    <div className="bg-nexus-surface border border-nexus-border rounded-xl p-5">
      <p className="text-nexus-muted text-xs font-medium mb-1">{label}</p>
      <p className="text-2xl font-semibold text-white">{value}</p>
      {sub && <p className="text-xs text-nexus-muted mt-1">{sub}</p>}
      {children && <div className="mt-2">{children}</div>}
    </div>
  )
}

function Card({ title, note, action, children }) {
  return (
    <div className="bg-nexus-surface border border-nexus-border rounded-xl p-6">
      <div className="flex items-start justify-between gap-4 mb-4">
        <div>
          <h2 className="text-sm font-semibold text-white">{title}</h2>
          {note && <p className="text-xs text-nexus-muted mt-1">{note}</p>}
        </div>
        {action}
      </div>
      {children}
    </div>
  )
}

function Segmented({ options, value, onChange, label }) {
  return (
    <div role="group" aria-label={label} className="inline-flex rounded-lg border border-nexus-border overflow-hidden">
      {options.map(o => (
        <button key={o.value} onClick={() => onChange(o.value)} aria-pressed={value === o.value}
          className={clsx('px-3 py-1.5 text-xs font-medium transition-colors',
            value === o.value ? 'bg-indigo-600/20 text-indigo-300' : 'text-nexus-muted hover:text-white hover:bg-white/5')}>
          {o.label}
        </button>
      ))}
    </div>
  )
}

function TrendTooltip({ active, payload, label }) {
  if (!active || !payload?.length) return null
  return (
    <div className="rounded-lg border border-nexus-border bg-nexus-surface px-3 py-2 text-xs shadow-lg">
      <p className="text-nexus-muted mb-1">{format(parseISO(label), 'EEE, MMM d')}</p>
      {payload.map(p => (
        <p key={p.dataKey} className="flex items-center gap-2 text-white">
          <span className="w-2 h-2 rounded-full" style={{ background: p.color }} />
          <span className="flex-1">{SERIES[p.dataKey].label}</span>
          <span className="font-semibold tabular-nums">{p.value}</span>
        </p>
      ))}
    </div>
  )
}

function TrendChart({ trend }) {
  return (
    <>
      <div className="flex items-center gap-5 mb-3">
        {Object.entries(SERIES).map(([key, s]) => (
          <span key={key} className="flex items-center gap-2 text-xs text-nexus-text">
            <span className="w-4 h-0.5 rounded" style={{ background: s.color }} />{s.label}
          </span>
        ))}
      </div>
      <ResponsiveContainer width="100%" height={240}>
        <LineChart data={trend} margin={{ top: 8, right: 12, bottom: 0, left: -16 }}>
          <CartesianGrid stroke={GRID} vertical={false} />
          <XAxis dataKey="date" tickFormatter={day} tick={AXIS_TICK} axisLine={{ stroke: GRID }} tickLine={false} minTickGap={32} />
          <YAxis allowDecimals={false} tick={AXIS_TICK} axisLine={false} tickLine={false} />
          <Tooltip content={<TrendTooltip />} cursor={{ stroke: '#334155', strokeWidth: 1 }} />
          {Object.entries(SERIES).map(([key, s]) => (
            <Line key={key} dataKey={key} name={s.label} stroke={s.color} strokeWidth={2} type="linear"
              strokeLinecap="round" strokeLinejoin="round" isAnimationActive={false}
              dot={trend.length <= 7 ? { r: 4, fill: s.color, stroke: '#131929', strokeWidth: 2 } : false}
              activeDot={{ r: 5, fill: s.color, stroke: '#131929', strokeWidth: 2 }} />
          ))}
        </LineChart>
      </ResponsiveContainer>
    </>
  )
}

function TrendTable({ trend }) {
  return (
    <div className="max-h-[272px] overflow-y-auto">
      <table className="w-full text-xs">
        <thead className="sticky top-0 bg-nexus-surface text-nexus-muted">
          <tr>
            <th className="text-left font-medium py-2">Day</th>
            <th className="text-right font-medium py-2">Created</th>
            <th className="text-right font-medium py-2">Resolved</th>
          </tr>
        </thead>
        <tbody className="text-nexus-text tabular-nums">
          {[...trend].reverse().map(d => (
            <tr key={d.date} className="border-t border-nexus-border">
              <td className="py-1.5">{format(parseISO(d.date), 'EEE, MMM d')}</td>
              <td className="py-1.5 text-right">{d.created}</td>
              <td className="py-1.5 text-right">{d.resolved}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function CategoryBars({ rows }) {
  const shown = rows.filter(r => r.count > 0)
  const total = shown.reduce((sum, r) => sum + r.count, 0)
  if (!total) return <p className="text-xs text-nexus-muted">No tickets were created in this period.</p>
  const max = shown[0].count
  return (
    <div className="space-y-3">
      {shown.map(r => (
        <div key={r.category} className="flex items-center gap-3 text-xs"
          title={`${titleCase(r.category)}: ${r.count} of ${total} tickets (${Math.round(100 * r.count / total)}%)`}>
          <span className="w-24 flex-shrink-0 text-nexus-text">{titleCase(r.category)}</span>
          <div className="flex-1 flex items-center gap-2">
            <div className="h-3 rounded-r" style={{ width: `${Math.max(1, 85 * r.count / max)}%`, background: BAR }} />
            <span className="text-white font-medium tabular-nums">{r.count}</span>
          </div>
        </div>
      ))}
    </div>
  )
}

function SlaRows({ rows }) {
  return (
    <div className="space-y-4">
      {rows.map(r => {
        const judged = r.met + r.breached
        return (
          <div key={r.priority} className="flex items-center gap-3 text-xs">
            <span className="w-16 flex-shrink-0 text-nexus-text">{titleCase(r.priority)}</span>
            <div className="flex-1 h-2 rounded-full overflow-hidden" style={{ background: 'rgba(57,135,229,0.18)' }}
              role="img" aria-label={judged ? `${r.compliance_pct}% on time` : 'No tickets judged yet'}>
              {judged > 0 && <div className="h-full rounded-full" style={{ width: `${r.compliance_pct}%`, background: BAR }} />}
            </div>
            <span className="w-12 text-right text-white font-medium tabular-nums">{judged ? `${Math.round(r.compliance_pct)}%` : '—'}</span>
            <span className="w-28 text-nexus-muted tabular-nums">{judged ? `${r.met} of ${judged} on time` : 'none judged yet'}</span>
          </div>
        )
      })}
    </div>
  )
}

export default function AnalyticsPage() {
  const [days, setDays] = useState(30)
  const [trendView, setTrendView] = useState('chart')

  const { data, isLoading, error } = useQuery({
    queryKey: ['analytics', 'summary', days],
    queryFn: () => analyticsApi.summary({ days, tz: TIME_ZONE }).then(r => r.data),
    placeholderData: keepPreviousData,
    refetchInterval: 60000,
  })

  const header = (
    <div className="flex items-start justify-between gap-4 mb-8">
      <div>
        <h1 className="text-2xl font-bold text-white">Analytics</h1>
        <p className="text-nexus-muted text-sm mt-1">
          {data ? `${day(data.period.start)} to ${format(parseISO(data.period.end), 'MMM d, yyyy')}` : 'Service desk performance'}
        </p>
      </div>
      <Segmented label="Period" value={days} onChange={setDays}
        options={PERIODS.map(d => ({ value: d, label: `Last ${d} days` }))} />
    </div>
  )

  if (error) {
    const forbidden = error.response?.status === 403
    return (
      <div className="p-8">
        {header}
        <p className="text-sm text-nexus-muted">
          {forbidden ? 'Analytics is available to analysts, agents and admins.' : 'Could not load analytics. Try again in a moment.'}
        </p>
      </div>
    )
  }
  if (isLoading || !data) {
    return <div className="p-8">{header}<p className="text-sm text-nexus-muted">Loading…</p></div>
  }

  const { current: cur, previous: prev } = data
  const judged = cur.sla_met + cur.sla_breached

  const createdChange = prev.created ? Math.round(100 * (cur.created - prev.created) / prev.created) : null
  const slaChange = cur.sla_compliance_pct != null && prev.sla_compliance_pct != null
    ? Math.round((cur.sla_compliance_pct - prev.sla_compliance_pct) * 10) / 10 : null
  const medianChange = cur.median_resolve_hours != null && prev.median_resolve_hours
    ? Math.round(100 * (cur.median_resolve_hours - prev.median_resolve_hours) / prev.median_resolve_hours) : null
  const signed = (n, unit) => `${n > 0 ? '+' : ''}${n}${unit}`

  return (
    <div className="p-8">
      {header}

      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4 mb-6">
        <Tile label="Open backlog" value={data.open_backlog}
          sub={data.open_overdue ? `${data.open_overdue} past SLA deadline` : 'None past SLA deadline'} />
        <Tile label="Tickets created" value={cur.created} sub={`${cur.resolved} resolved`}>
          <Delta change={createdChange} text={createdChange != null && signed(createdChange, '%')} goodWhen={null} />
        </Tile>
        <Tile label="SLA compliance" value={cur.sla_compliance_pct != null ? `${Math.round(cur.sla_compliance_pct)}%` : '—'}
          sub={judged ? `${cur.sla_met} of ${judged} on time` : 'No tickets judged yet'}>
          <Delta change={slaChange} text={slaChange != null && signed(slaChange, ' pts')} goodWhen="up" />
        </Tile>
        <Tile label="Median time to resolve" value={formatDuration(cur.median_resolve_hours)}
          sub={`${cur.resolved} resolved in this period`}>
          <Delta change={medianChange} text={medianChange != null && signed(medianChange, '%')} goodWhen="down" />
        </Tile>
      </div>

      <div className="mb-6">
        <Card title="Tickets created and resolved per day"
          action={<Segmented label="View" value={trendView} onChange={setTrendView}
            options={[{ value: 'chart', label: 'Chart' }, { value: 'table', label: 'Table' }]} />}>
          {trendView === 'chart' ? <TrendChart trend={data.trend} /> : <TrendTable trend={data.trend} />}
        </Card>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <Card title="Tickets by category" note="Tickets created in this period">
          <CategoryBars rows={data.by_category} />
        </Card>
        <Card title="SLA compliance by priority"
          note="Tickets created in this period. On time means resolved by the deadline; open tickets not yet due and cancelled tickets are left out.">
          <SlaRows rows={data.sla_by_priority} />
        </Card>
      </div>
    </div>
  )
}
