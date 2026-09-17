import { useQuery } from '@tanstack/react-query'
import { ticketApi, agentApi } from '../api/tickets'
import { Bot, Zap, CheckCircle, Clock, AlertTriangle } from 'lucide-react'

const AGENTS = [
  { key: 'triage',    label: 'Triage',    icon: '🎫', color: 'pink'   },
  { key: 'diagnosis', label: 'Diagnosis', icon: '🔍', color: 'blue'   },
  { key: 'knowledge', label: 'Knowledge', icon: '📚', color: 'yellow' },
  { key: 'security',  label: 'Security',  icon: '🛡',  color: 'red'    },
]

function AgentCard({ agent }) {
  return (
    <div className="bg-nexus-surface border border-nexus-border rounded-xl p-5">
      <div className="flex items-center gap-3 mb-3">
        <span className="text-2xl">{agent.icon}</span>
        <div>
          <p className="text-sm font-semibold text-white">{agent.label} Agent</p>
          <div className="flex items-center gap-1.5 mt-0.5">
            <div className="w-1.5 h-1.5 rounded-full bg-green-500 animate-pulse" />
            <span className="text-xs text-green-400">Ready</span>
          </div>
        </div>
      </div>
      <div className="grid grid-cols-2 gap-2 mt-3">
        {[['Model', 'Claude Sonnet 4.6'], ['Tools', agent.key === 'triage' ? '2' : agent.key === 'diagnosis' ? '3' : '3']].map(([k,v]) => (
          <div key={k} className="bg-nexus-bg rounded-lg p-2">
            <p className="text-xs text-nexus-muted">{k}</p>
            <p className="text-xs text-white font-medium">{v}</p>
          </div>
        ))}
      </div>
    </div>
  )
}

export default function AgentMonitorPage() {
  const { data } = useQuery({
    queryKey: ['tickets', 'in_progress'],
    queryFn: () => ticketApi.list({ status: 'in_progress', page_size: 10 }).then(r => r.data),
    refetchInterval: 5000,
  })

  const active = data?.items || []

  return (
    <div className="p-8">
      <div className="mb-8">
        <h1 className="text-2xl font-bold text-white">Agent Monitor</h1>
        <p className="text-nexus-muted text-sm mt-1">Live view of the LangGraph supervisor pipeline</p>
      </div>

      {/* Graph diagram */}
      <div className="bg-nexus-surface border border-nexus-border rounded-xl p-6 mb-6">
        <p className="text-xs text-nexus-muted font-medium mb-4">Supervisor Graph Topology</p>
        <div className="flex items-center gap-2 flex-wrap">
          {[
            { label: 'START', color: 'bg-indigo-600' },
            { label: '→' },
            { label: 'Triage', color: 'bg-pink-900/40 text-pink-400 border border-pink-900/50' },
            { label: '→' },
            { label: 'Security?', color: 'bg-red-900/40 text-red-400 border border-red-900/50' },
            { label: '→' },
            { label: 'Diagnosis', color: 'bg-blue-900/40 text-blue-400 border border-blue-900/50' },
            { label: '→' },
            { label: 'Knowledge', color: 'bg-yellow-900/40 text-yellow-400 border border-yellow-900/50' },
            { label: '→' },
            { label: 'END', color: 'bg-green-600' },
          ].map((item, i) =>
            item.label === '→'
              ? <span key={i} className="text-nexus-muted">→</span>
              : <span key={i} className={`text-xs px-3 py-1.5 rounded-full font-medium text-white ${item.color}`}>{item.label}</span>
          )}
        </div>
        <p className="text-xs text-nexus-muted mt-3 flex items-center gap-1.5">
          <AlertTriangle size={10} className="text-orange-400" />
          HITL checkpoint triggers before human_review node on critical/security escalations
        </p>
      </div>

      {/* Agent cards */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4 mb-8">
        {AGENTS.map(a => <AgentCard key={a.key} agent={a} />)}
      </div>

      {/* Active runs */}
      <div className="bg-nexus-surface border border-nexus-border rounded-xl">
        <div className="px-5 py-4 border-b border-nexus-border flex items-center justify-between">
          <h2 className="text-sm font-semibold text-white flex items-center gap-2">
            <Zap size={14} className="text-indigo-400" /> Active Runs
          </h2>
          <span className="text-xs bg-indigo-600/20 text-indigo-400 px-2 py-0.5 rounded-full">{active.length}</span>
        </div>
        {active.length === 0
          ? <p className="text-center text-nexus-muted text-sm py-10">No active agent runs</p>
          : active.map(t => (
            <div key={t.id} className="px-5 py-4 border-b border-nexus-border last:border-0 flex items-center gap-4">
              <div className="w-2 h-2 rounded-full bg-indigo-500 animate-pulse flex-shrink-0" />
              <span className="text-xs font-mono text-indigo-400 w-28">{t.ticket_number}</span>
              <span className="text-sm text-white flex-1 truncate">{t.title}</span>
              <span className="text-xs text-nexus-muted capitalize">{t.assigned_agent?.replace('_agent', '')}</span>
              <Clock size={12} className="text-nexus-muted" />
            </div>
          ))
        }
      </div>
    </div>
  )
}
