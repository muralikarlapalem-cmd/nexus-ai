import { useParams } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { ticketApi, agentApi } from '../api/tickets'
import { useAuthStore } from '../store/authStore'
import { Bot, Zap, CheckCircle, AlertTriangle, Clock, Loader2 } from 'lucide-react'
import { format } from 'date-fns'

const PRIORITY_COLOR = { critical:'text-red-400', high:'text-orange-400', medium:'text-yellow-400', low:'text-green-400' }

const VALID_STATUS_TRANSITIONS = {
  open: ['triaged', 'closed', 'cancelled'],
  triaged: ['in_progress', 'cancelled'],
  in_progress: ['awaiting_user', 'resolved', 'closed', 'cancelled'],
  awaiting_user: ['in_progress', 'resolved', 'cancelled'],
  resolved: [],
  closed: [],
  cancelled: [],
}

const STATUS_LABELS = {
  open: 'Open', triaged: 'Triaged', in_progress: 'In Progress',
  awaiting_user: 'Awaiting User', resolved: 'Resolved', closed: 'Closed', cancelled: 'Cancelled',
}

function AgentMessage({ msg }) {
  const isAgent = msg.content?.startsWith('[')
  return (
    <div className={`flex gap-3 ${isAgent ? '' : 'flex-row-reverse'}`}>
      <div className="w-7 h-7 rounded-full bg-indigo-600/30 border border-indigo-600/50 flex items-center justify-center flex-shrink-0">
        <Bot size={12} className="text-indigo-400" />
      </div>
      <div className={`max-w-sm rounded-xl px-3 py-2 text-xs ${isAgent ? 'bg-nexus-bg text-nexus-muted' : 'bg-indigo-600/20 text-indigo-300'}`}>
        {msg.content}
      </div>
    </div>
  )
}

export default function TicketDetailPage() {
  const { id } = useParams()
  const qc = useQueryClient()

  const { data: ticket, isLoading } = useQuery({
    queryKey: ['ticket', id],
    queryFn: () => ticketApi.get(id).then(r => r.data),
  })
  const { data: trace } = useQuery({
    queryKey: ['trace', id],
    queryFn: () => agentApi.trace(id).then(r => r.data),
    refetchInterval: ticket?.status === 'in_progress' ? 3000 : false,
  })

  const runMutation = useMutation({
    mutationFn: () => agentApi.run(id),
    onSuccess: () => qc.invalidateQueries(['ticket', id]),
  })

  const statusMutation = useMutation({
    mutationFn: (newStatus) => ticketApi.updateStatus(id, { status: newStatus }),
    onSuccess: () => qc.invalidateQueries(['ticket', id]),
    onError: (err) => {
      alert(err?.response?.data?.detail || 'Failed to update status')
    },
  })

  const { user } = useAuthStore()
  const isAdmin = user?.role === 'admin'
  const allowedNextStatuses = VALID_STATUS_TRANSITIONS[ticket?.status] || []

  if (isLoading) return <div className="flex items-center justify-center h-full text-nexus-muted text-sm">Loading…</div>
  if (!ticket)   return <div className="p-8 text-red-400">Ticket not found</div>

  return (
    <div className="p-8 max-w-5xl">
      {/* Header */}
      <div className="flex items-start justify-between mb-6">
        <div>
          <div className="flex items-center gap-3 mb-1">
            <span className={`text-xs px-2 py-0.5 rounded-full font-medium ${
              ticket.status === 'resolved' ? 'bg-green-900/40 text-green-400'
              : ticket.status === 'open'   ? 'bg-blue-900/40 text-blue-400'
              : 'bg-yellow-900/40 text-yellow-400'}`}>{ticket.status}</span>

            {isAdmin && allowedNextStatuses.length > 0 && (
              <select
                value=""
                onChange={(e) => e.target.value && statusMutation.mutate(e.target.value)}
                disabled={statusMutation.isPending}
                className="text-xs bg-nexus-bg border border-nexus-border rounded-full px-2 py-0.5 text-nexus-muted disabled:opacity-50"
              >
                <option value="">Change status…</option>
                {allowedNextStatuses.map((s) => (
                  <option key={s} value={s}>{STATUS_LABELS[s]}</option>
                ))}
              </select>
            )}
            {ticket.sla_breached && <span className="text-xs px-2 py-0.5 rounded-full bg-red-900/40 text-red-400 font-medium">SLA Breached</span>}
          </div>
          <h1 className="text-xl font-bold text-white">{ticket.title}</h1>
          <p className="text-nexus-muted text-xs mt-1">
            <span className={PRIORITY_COLOR[ticket.priority] || 'text-nexus-muted'}>{ticket.priority}</span>
            {' · '}{ticket.category}
            {' · '}Created {format(new Date(ticket.created_at), 'MMM d, yyyy HH:mm')}
          </p>
        </div>
        {(ticket.status === 'open' || ticket.status === 'triaged') && (
          <button onClick={() => runMutation.mutate()} disabled={runMutation.isPending}
            className="flex items-center gap-2 bg-indigo-600 hover:bg-indigo-500 disabled:opacity-50 text-white rounded-lg px-4 py-2 text-sm font-medium">
            {runMutation.isPending ? <Loader2 size={14} className="animate-spin" /> : <Zap size={14} />}
            Run Agents
          </button>
        )}
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        {/* Left: Description + Resolution */}
        <div className="lg:col-span-2 space-y-4">
          <div className="bg-nexus-surface border border-nexus-border rounded-xl p-5">
            <h2 className="text-sm font-semibold text-white mb-3">Description</h2>
            <p className="text-sm text-nexus-muted leading-relaxed whitespace-pre-wrap">{ticket.description}</p>
          </div>

          {ticket.resolution_notes && (
            <div className="bg-nexus-surface border border-green-900/40 rounded-xl p-5">
              <div className="flex items-center gap-2 mb-3">
                <CheckCircle size={14} className="text-green-400" />
                <h2 className="text-sm font-semibold text-green-400">Resolution</h2>
              </div>
              <pre className="text-xs text-nexus-muted whitespace-pre-wrap font-mono leading-relaxed">{ticket.resolution_notes}</pre>
            </div>
          )}

          {ticket.triage_output && (
            <div className="bg-nexus-surface border border-nexus-border rounded-xl p-5">
              <h2 className="text-sm font-semibold text-white mb-3">Triage Analysis</h2>
              <div className="grid grid-cols-2 gap-3 text-xs">
                {Object.entries(ticket.triage_output).filter(([k]) => !['next_agent'].includes(k)).slice(0, 6).map(([k, v]) => (
                  <div key={k} className="bg-nexus-bg rounded-lg p-2.5">
                    <p className="text-nexus-muted mb-0.5 capitalize">{k.replace(/_/g, ' ')}</p>
                    <p className="text-white font-medium truncate">{Array.isArray(v) ? v.join(', ') || '—' : String(v)}</p>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>

        {/* Right: Agent Trace */}
        <div className="bg-nexus-surface border border-nexus-border rounded-xl p-5">
          <div className="flex items-center gap-2 mb-4">
            <Bot size={14} className="text-indigo-400" />
            <h2 className="text-sm font-semibold text-white">Agent Trace</h2>
            {ticket.status === 'in_progress' && <Loader2 size={12} className="text-indigo-400 animate-spin ml-auto" />}
          </div>
          <div className="space-y-3">
            {trace?.trace?.length ? trace.trace.map((msg, i) => (
              <AgentMessage key={i} msg={msg} />
            )) : (
              <p className="text-xs text-nexus-muted text-center py-4">
                {ticket.status === 'open' ? 'Click "Run Agents" to start' : 'No trace available'}
              </p>
            )}
          </div>
          {trace?.hitl_required && (
            <div className="mt-4 p-3 bg-orange-900/20 border border-orange-900/40 rounded-lg">
              <p className="text-xs text-orange-400 font-medium flex items-center gap-1.5">
                <AlertTriangle size={12} /> Human review required
              </p>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
