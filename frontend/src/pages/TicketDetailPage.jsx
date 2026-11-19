import { useEffect, useState } from 'react'
import { useParams } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { ticketApi, agentApi, integrationsApi } from '../api/tickets'
import { useAuthStore } from '../store/authStore'
import { Bot, Zap, CheckCircle, AlertTriangle, Clock, Loader2, Send, ExternalLink } from 'lucide-react'
import { format } from 'date-fns'
import AgentPipeline, { NODE_LABELS } from '../components/AgentPipeline'
import SourceBadge from '../components/SourceBadge'

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

// Shown while a run is paused for a person. Approve resolves the ticket with the
// reviewer's notes; reject stops the agents and leaves the ticket with a human.
function ReviewPanel({ reason, onSubmit, isPending }) {
  const [notes, setNotes] = useState('')
  const hasNotes = notes.trim().length > 0
  // next_agent "END": no further agents run after the review
  const submit = (approved) => onSubmit({ approved, next_agent: 'END', notes: notes.trim() })

  return (
    <div className="mb-6 rounded-xl border border-orange-500/50 bg-orange-900/10 p-5">
      <div className="flex items-center gap-2 mb-1">
        <AlertTriangle size={14} className="text-orange-400" />
        <h2 className="text-sm font-semibold text-orange-300">Your review is needed</h2>
      </div>
      <p className="text-xs text-nexus-muted mb-4">
        The agents paused and will not go further.
        {reason ? <> Reason: <span className="text-orange-200">{reason}</span>.</> : null}
        {' '}Their findings so far are shown below.
      </p>

      <label className="block text-xs text-nexus-muted mb-1" htmlFor="review-notes">Review notes (required)</label>
      <textarea
        id="review-notes"
        value={notes}
        onChange={(e) => setNotes(e.target.value)}
        rows={3}
        placeholder="What you checked and decided. Saved as the ticket's resolution."
        className="w-full bg-nexus-bg border border-nexus-border rounded-lg px-3 py-2 text-sm text-white placeholder:text-nexus-muted focus:outline-none focus:border-orange-500/60 mb-3"
      />

      <div className="flex items-center justify-end gap-2 flex-wrap">
        <button
          onClick={() => submit(false)}
          disabled={isPending || !hasNotes}
          title={hasNotes ? undefined : 'Add your review notes first'}
          className="flex items-center gap-2 border border-red-900/60 text-red-400 hover:bg-red-900/20 disabled:opacity-40 disabled:hover:bg-transparent rounded-lg px-4 py-2 text-sm font-medium">
          Reject
        </button>
        <button
          onClick={() => submit(true)}
          disabled={isPending || !hasNotes}
          title={hasNotes ? undefined : 'Add your review notes first'}
          className="flex items-center gap-2 bg-green-600 hover:bg-green-500 disabled:opacity-40 disabled:hover:bg-green-600 text-white rounded-lg px-4 py-2 text-sm font-medium">
          {isPending ? <Loader2 size={14} className="animate-spin" /> : <CheckCircle size={14} />}
          Approve and resolve
        </button>
      </div>
      <p className="text-xs text-nexus-muted mt-3">
        Approve closes the ticket as resolved with your notes. Reject keeps it open and assigned to a human.
      </p>
    </div>
  )
}

export default function TicketDetailPage() {
  const { id } = useParams()
  const qc = useQueryClient()

  const { data: ticket, isLoading } = useQuery({
    queryKey: ['ticket', id],
    queryFn: () => ticketApi.get(id).then(r => r.data),
    // Keep watching while agents run, so the page follows the run without a reload.
    // Once it is waiting on a human there is little to see, so check less often.
    refetchInterval: (query) => {
      const t = query.state.data
      if (t?.status !== 'in_progress') return false
      return t.assigned_agent === 'human' ? 10000 : 2000
    },
  })
  const { data: trace } = useQuery({
    queryKey: ['trace', id],
    queryFn: () => agentApi.trace(id).then(r => r.data),
    refetchInterval: ticket?.status === 'in_progress' ? 2000 : false,
  })

  const runMutation = useMutation({
    mutationFn: () => agentApi.run(id),
    // The API marks the ticket in_progress before it answers, so this refetch starts the polling
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['ticket', id] })
      qc.invalidateQueries({ queryKey: ['trace', id] })
    },
    onError: (err) => {
      alert(err?.response?.data?.detail || 'Failed to start the agents')
      qc.invalidateQueries({ queryKey: ['ticket', id] })
    },
  })

  const reviewMutation = useMutation({
    mutationFn: (decision) => agentApi.resume(id, decision),
    // Resolve and reject are finished by the time the API answers
    onSettled: () => {
      qc.invalidateQueries({ queryKey: ['ticket', id] })
      qc.invalidateQueries({ queryKey: ['trace', id] })
    },
    onError: (err) => {
      const detail = err?.response?.data?.detail
      alert(typeof detail === 'string' ? detail : 'Failed to submit the review')
    },
  })

  // When the run ends the trace polling stops; fetch it once more for the last messages
  useEffect(() => {
    qc.invalidateQueries({ queryKey: ['trace', id] })
  }, [qc, id, ticket?.status, ticket?.assigned_agent])

  const statusMutation = useMutation({
    mutationFn: (newStatus) => ticketApi.updateStatus(id, { status: newStatus }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['ticket', id] }),
    onError: (err) => {
      alert(err?.response?.data?.detail || 'Failed to update status')
    },
  })

  // Tickets from ServiceNow etc.: post the agents' result back to the source system by hand
  const [writeBackNotice, setWriteBackNotice] = useState(null)
  const writeBackMutation = useMutation({
    mutationFn: () => integrationsApi.writeBack(id).then(r => r.data),
    onSuccess: (data) => setWriteBackNotice(
      data.status === 'posted' ? { ok: true, text: `Posted to ${ticket?.source_name} (${data.external})` }
      : data.status === 'nothing_to_post' ? { ok: false, text: 'Nothing to post yet: run the agents first.' }
      : data.status === 'disabled' ? { ok: false, text: 'Write-back is turned off for this connection.' }
      : { ok: false, text: data.status }),
    onError: (err) => setWriteBackNotice({ ok: false, text: err?.response?.data?.detail || 'Could not post the note' }),
  })

  const { user } = useAuthStore()
  const isAdmin = user?.role === 'admin'
  const canReview = user?.role === 'admin' || user?.role === 'agent'

  // Link to this ticket's latest agent run in LangSmith (agents and admins only)
  const { data: langsmith } = useQuery({
    queryKey: ['langsmith', id, ticket?.langsmith_run_id],
    queryFn: () => agentApi.langsmith(id).then(r => r.data),
    enabled: canReview && !!ticket?.langsmith_run_id,
    staleTime: Infinity,
  })
  const allowedNextStatuses = VALID_STATUS_TRANSITIONS[ticket?.status] || []

  // Where the run is right now. The trace endpoint reports the graph node that is executing.
  const inProgress = ticket?.status === 'in_progress'
  const currentNode = inProgress ? trace?.current_node?.[0] : undefined
  // A paused run leaves the ticket assigned to a human. Submitting a review hands it back
  // to the agents at once, so "paused and with a human" means a review is still owed.
  const withHuman = inProgress && ticket?.assigned_agent === 'human'
  const awaitingReview = withHuman && currentNode === 'human_review'
  let liveLabel = null
  if (awaitingReview) liveLabel = 'Waiting for human review'
  else if (withHuman) liveLabel = 'Handed to a human — the agents have stopped'
  else if (currentNode === 'human_review') liveLabel = 'Review submitted — resuming…'
  else if (currentNode === '__start__') liveLabel = 'Starting…'
  else if (currentNode) liveLabel = `${NODE_LABELS[currentNode] || currentNode} agent is working…`
  else if (inProgress) liveLabel = trace?.trace?.length ? 'Finishing up…' : 'Queued — waiting for a worker…'

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
          {ticket.source && ticket.source !== 'nexus' && (
            <div className="flex flex-wrap items-center gap-2 mt-2">
              <span className="text-xs text-nexus-muted">From</span>
              <SourceBadge ticket={ticket} />
              {canReview && (ticket.triage_output || ticket.diagnosis_output || ticket.resolution_notes) && (
                <button onClick={() => writeBackMutation.mutate()} disabled={writeBackMutation.isPending}
                  className="inline-flex items-center gap-1.5 text-xs border border-nexus-border text-nexus-muted hover:text-white rounded-full px-2.5 py-0.5 disabled:opacity-50">
                  {writeBackMutation.isPending ? <Loader2 size={11} className="animate-spin" /> : <Send size={11} />}
                  Post result to {ticket.source_name || 'source'}
                </button>
              )}
              {writeBackNotice && (
                <span role="status" className={`text-xs ${writeBackNotice.ok ? 'text-green-400' : 'text-yellow-300'}`}>{writeBackNotice.text}</span>
              )}
            </div>
          )}
        </div>
        {(ticket.status === 'open' || ticket.status === 'triaged') && (
          <button onClick={() => runMutation.mutate()} disabled={runMutation.isPending || runMutation.isSuccess}
            className="flex items-center gap-2 bg-indigo-600 hover:bg-indigo-500 disabled:opacity-50 text-white rounded-lg px-4 py-2 text-sm font-medium">
            {runMutation.isPending ? <Loader2 size={14} className="animate-spin" /> : <Zap size={14} />}
            Run Agents
          </button>
        )}
      </div>

      {/* Live run status: which agent has the ticket right now */}
      {inProgress && (
        <div className={`mb-6 rounded-xl border p-4 ${withHuman ? 'bg-orange-900/10 border-orange-900/40' : 'bg-indigo-600/10 border-indigo-600/40'}`}>
          <div className="flex items-center gap-2 mb-3">
            {withHuman
              ? <AlertTriangle size={14} className="text-orange-400" />
              : <Loader2 size={14} className="text-indigo-400 animate-spin" />}
            <p className={`text-sm font-medium ${withHuman ? 'text-orange-300' : 'text-indigo-200'}`}>{liveLabel}</p>
          </div>
          {withHuman && !awaitingReview && (
            <p className="text-xs text-nexus-muted mb-3">
              Nothing more will run automatically. An admin can mark the ticket resolved or closed with “Change status…” above.
            </p>
          )}
          {awaitingReview && !canReview && (
            <p className="text-xs text-nexus-muted mb-3">A support agent or admin needs to review this ticket before the agents continue.</p>
          )}
          <AgentPipeline
            counts={currentNode ? { [currentNode]: 1 } : withHuman ? { human_review: 1 } : {}}
            showEnds={false}
            showHuman={false}
          />
        </div>
      )}

      {awaitingReview && canReview && (
        <ReviewPanel
          reason={trace?.hitl_reason}
          onSubmit={(decision) => reviewMutation.mutate(decision)}
          isPending={reviewMutation.isPending}
        />
      )}

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
            {inProgress && !withHuman && <Loader2 size={12} className="text-indigo-400 animate-spin" />}
            {langsmith?.url ? (
              <a href={langsmith.url} target="_blank" rel="noopener noreferrer"
                title={`LangSmith run ${langsmith.run_id}`}
                className="ml-auto inline-flex items-center gap-1 text-xs text-indigo-300 hover:text-white">
                View in LangSmith <ExternalLink size={11} />
              </a>
            ) : langsmith?.run_id && (
              <span className="ml-auto text-xs text-nexus-muted" title={langsmith.reason}>LangSmith link unavailable</span>
            )}
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
          {awaitingReview && (
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
