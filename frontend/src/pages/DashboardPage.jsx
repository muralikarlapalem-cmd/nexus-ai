import { useQuery } from '@tanstack/react-query'
import { BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, Cell } from 'recharts'
import { Ticket, CheckCircle, AlertTriangle, Clock, Database, XCircle } from 'lucide-react'
import { ticketApi, ragApi } from '../api/tickets'
import { format } from 'date-fns'

const PRIORITY_COLORS = { critical: '#ef4444', high: '#f97316', medium: '#eab308', low: '#22c55e' }

function StatCard({ icon: Icon, label, value, color = 'indigo', sub }) {
  return (
    <div className="bg-nexus-surface border border-nexus-border rounded-xl p-5">
      <div className="flex items-start justify-between">
        <div>
          <p className="text-nexus-muted text-xs font-medium mb-1">{label}</p>
          <p className="text-2xl font-bold text-white">{value ?? '—'}</p>
          {sub && <p className="text-xs text-nexus-muted mt-1">{sub}</p>}
        </div>
        <div className={`w-9 h-9 rounded-lg bg-${color}-600/20 flex items-center justify-center`}>
          <Icon size={18} className={`text-${color}-400`} />
        </div>
      </div>
    </div>
  )
}

export default function DashboardPage() {
  const { data: ticketsData } = useQuery({
    queryKey: ['tickets', 'all'],
    queryFn: () => ticketApi.list({ page: 1, page_size: 100 }).then(r => r.data),
  })
  const { data: ragStats } = useQuery({
    queryKey: ['rag', 'stats'],
    queryFn: () => ragApi.stats().then(r => r.data),
    retry: false,
  })

  const tickets = ticketsData?.items || []
  const open     = tickets.filter(t => t.status === 'open').length
  const resolved = tickets.filter(t => t.status === 'resolved').length
  const breached = tickets.filter(t => t.sla_breached).length
  const critical = tickets.filter(t => t.priority === 'critical').length
  const cancelled = tickets.filter(t => t.status === 'cancelled').length

  // Priority distribution for bar chart
  const chartData = ['critical', 'high', 'medium', 'low'].map(p => ({
    name: p.charAt(0).toUpperCase() + p.slice(1),
    count: tickets.filter(t => t.priority === p).length,
    color: PRIORITY_COLORS[p],
  }))

  return (
    <div className="p-8">
      <div className="mb-8">
        <h1 className="text-2xl font-bold text-white">Dashboard</h1>
        <p className="text-nexus-muted text-sm mt-1">{format(new Date(), 'EEEE, MMMM d, yyyy')}</p>
      </div>

      {/* Stats */}
      <div className="grid grid-cols-2 lg:grid-cols-5 gap-4 mb-8">
        <StatCard icon={Ticket}        label="Open Tickets"   value={open}      color="blue"   sub={`of ${tickets.length} total`} />
        <StatCard icon={CheckCircle}   label="Resolved"       value={resolved}  color="green"  />
        <StatCard icon={AlertTriangle} label="SLA Breached"   value={breached}  color="red"    />
        <StatCard icon={Clock}         label="Critical"       value={critical}  color="orange" />
        <StatCard icon={XCircle}       label="Cancelled"      value={cancelled} color="slate"  />
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* Priority Chart */}
        <div className="bg-nexus-surface border border-nexus-border rounded-xl p-6">
          <h2 className="text-sm font-semibold text-white mb-4">Tickets by Priority</h2>
          <ResponsiveContainer width="100%" height={180}>
            <BarChart data={chartData} barSize={32}>
              <XAxis dataKey="name" tick={{ fill: '#64748b', fontSize: 12 }} axisLine={false} tickLine={false} />
              <YAxis tick={{ fill: '#64748b', fontSize: 12 }} axisLine={false} tickLine={false} />
              <Tooltip
                contentStyle={{ background: '#131929', border: '1px solid #1e2a45', borderRadius: 8, color: '#e2e8f0' }}
                cursor={{ fill: 'rgba(99,102,241,0.08)' }}
              />
              <Bar dataKey="count" radius={[4, 4, 0, 0]}>
                {chartData.map((d, i) => <Cell key={i} fill={d.color} />)}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </div>

        {/* RAG Stats + Recent Tickets */}
        <div className="space-y-4">
          {ragStats && (
            <div className="bg-nexus-surface border border-nexus-border rounded-xl p-5">
              <div className="flex items-center gap-2 mb-3">
                <Database size={14} className="text-indigo-400" />
                <h2 className="text-sm font-semibold text-white">Knowledge Base</h2>
              </div>
              <div className="grid grid-cols-2 gap-3 text-sm">
                <div className="bg-nexus-bg rounded-lg p-3">
                  <p className="text-nexus-muted text-xs">Vectors</p>
                  <p className="text-white font-bold">{ragStats.vector_count?.toLocaleString() ?? 0}</p>
                </div>
                <div className="bg-nexus-bg rounded-lg p-3">
                  <p className="text-nexus-muted text-xs">BM25 Docs</p>
                  <p className="text-white font-bold">{ragStats.bm25_doc_count ?? 0}</p>
                </div>
              </div>
            </div>
          )}

          <div className="bg-nexus-surface border border-nexus-border rounded-xl p-5">
            <h2 className="text-sm font-semibold text-white mb-3">Recent Tickets</h2>
            <div className="space-y-2">
              {tickets.slice(0, 4).map(t => (
                <div key={t.id} className="flex items-center gap-3 py-2 border-b border-nexus-border last:border-0">
                  <div className={`w-1.5 h-1.5 rounded-full flex-shrink-0`}
                    style={{ background: PRIORITY_COLORS[t.priority] || '#64748b' }} />
                  <span className="text-xs font-mono text-nexus-muted w-24 flex-shrink-0">{t.ticket_number}</span>
                  <span className="text-xs text-white truncate flex-1">{t.title}</span>
                  <span className={`text-xs px-2 py-0.5 rounded-full font-medium ${
                    t.status === 'resolved' ? 'bg-green-900/40 text-green-400'
                    : t.status === 'open'   ? 'bg-blue-900/40 text-blue-400'
                    : 'bg-yellow-900/40 text-yellow-400'
                  }`}>{t.status}</span>
                </div>
              ))}
              {tickets.length === 0 && <p className="text-nexus-muted text-xs">No tickets yet</p>}
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}
