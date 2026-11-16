import { useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { agentApi } from '../api/tickets'
import { Bot, Zap, CheckCircle, Clock, AlertTriangle } from 'lucide-react'
import AgentPipeline from '../components/AgentPipeline'

const AGENTS = [
  { key: 'triage',    label: 'Triage',    icon: '🎫', color: 'pink'   },
  { key: 'diagnosis', label: 'Diagnosis', icon: '🔍', color: 'blue'   },
  { key: 'knowledge', label: 'Knowledge', icon: '📚', color: 'yellow' },
  { key: 'security',  label: 'Security',  icon: '🛡',  color: 'red'    },
]

// Colour of the live-step badge in Active Runs
const STEP_STYLE = {
  triage_agent:    'bg-pink-900/40 text-pink-400',
  security_agent:  'bg-red-900/40 text-red-400',
  diagnosis_agent: 'bg-blue-900/40 text-blue-400',
  knowledge_agent: 'bg-yellow-900/40 text-yellow-400',
  human_review:    'bg-orange-900/40 text-orange-400',
}

function AgentCard({ agent, model, tickets = [] }) {
  // tickets: the runs this agent is working on right now
  const working = tickets.length > 0
  return (
    <div className={`bg-nexus-surface border rounded-xl p-5 transition-colors ${working ? 'border-indigo-500 ring-2 ring-indigo-500/30' : 'border-nexus-border'}`}>
      <div className="flex items-center gap-3 mb-3">
        <span className="text-2xl">{agent.icon}</span>
        <div className="min-w-0">
          <p className="text-sm font-semibold text-white">{agent.label} Agent</p>
          {working ? (
            <div className="flex items-center gap-1.5 mt-0.5">
              <div className="w-1.5 h-1.5 rounded-full bg-indigo-400 animate-ping flex-shrink-0" />
              <span className="text-xs text-indigo-300 truncate">
                Working on {tickets[0].ticket_number}{tickets.length > 1 ? ` +${tickets.length - 1} more` : ''}
              </span>
            </div>
          ) : (
            <div className="flex items-center gap-1.5 mt-0.5">
              <div className="w-1.5 h-1.5 rounded-full bg-green-500" />
              <span className="text-xs text-green-400">Idle</span>
            </div>
          )}
        </div>
      </div>
      <div className="grid grid-cols-2 gap-2 mt-3">
        {[['Model', model || '…'],['Tools', agent.key === 'triage' ? '2' : agent.key === 'diagnosis' ? '3' : '3']].map(([k,v]) => (
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
  const navigate = useNavigate()
  // Live step of each run comes from the LangGraph checkpoint, not ticket.assigned_agent
  const { data } = useQuery({
    queryKey: ['agents', 'active'],
    queryFn: () => agentApi.active({ limit: 10 }).then(r => r.data),
    // Poll faster while something is running, so each step is seen as it happens
    refetchInterval: (query) => (query.state.data?.items?.length ? 2000 : 5000),
  })
  const { data: info } = useQuery({
    queryKey: ['agents', 'info'],
    queryFn: () => agentApi.info().then(r => r.data),
    staleTime: Infinity,
  })

  const active = data?.items || []

  // Graph node → the runs sitting on it right now
  const byNode = {}
  for (const t of active) {
    if (t.current_node) (byNode[t.current_node] ||= []).push(t)
  }
  const counts = Object.fromEntries(Object.entries(byNode).map(([node, runs]) => [node, runs.length]))
  const running = active.filter(t => t.current_node && t.current_node !== 'human_review').length

  return (
    <div className="p-8">
      <div className="mb-8">
        <h1 className="text-2xl font-bold text-white">Agent Monitor</h1>
        <p className="text-nexus-muted text-sm mt-1">Live view of the LangGraph supervisor pipeline</p>
      </div>

      {/* Graph diagram */}
      <div className="bg-nexus-surface border border-nexus-border rounded-xl p-6 mb-6">
        <div className="flex items-center justify-between mb-4">
          <p className="text-xs text-nexus-muted font-medium">Supervisor Graph Topology</p>
          <span className={`text-xs ${running ? 'text-indigo-300' : 'text-nexus-muted'}`}>
            {running ? `${running} run${running > 1 ? 's' : ''} in flight — highlighted step is working now` : 'No agent is working right now'}
          </span>
        </div>
        <AgentPipeline counts={counts} />
        <p className="text-xs text-nexus-muted mt-3 flex items-center gap-1.5">
          <AlertTriangle size={10} className="text-orange-400" />
          HITL checkpoint triggers before human_review node on critical/security escalations
        </p>
      </div>

      {/* Agent cards */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4 mb-8">
        {AGENTS.map(a => <AgentCard key={a.key} agent={a} model={info?.llm_model} tickets={byNode[`${a.key}_agent`]} />)}
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
            <div key={t.id} onClick={() => navigate(`/tickets/${t.id}`)}
              className="px-5 py-4 border-b border-nexus-border last:border-0 flex items-center gap-4 cursor-pointer hover:bg-white/[0.02]">
              <div className={`w-2 h-2 rounded-full flex-shrink-0 ${t.current_node ? 'bg-indigo-500 animate-pulse' : 'bg-nexus-muted'}`} />
              <span className="text-xs font-mono text-indigo-400 w-28">{t.ticket_number}</span>
              <span className="text-sm text-white flex-1 truncate">{t.title}</span>
              <span className={`text-xs px-2 py-0.5 rounded-full font-medium ${STEP_STYLE[t.current_node] || 'bg-nexus-bg text-nexus-muted'}`}
                title={t.iteration ? `Step ${t.iteration} of the run` : undefined}>
                {t.current_step}
              </span>
              <Clock size={12} className="text-nexus-muted" />
            </div>
          ))
        }
      </div>
    </div>
  )
}
