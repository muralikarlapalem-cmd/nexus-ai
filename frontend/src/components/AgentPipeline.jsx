// The supervisor graph drawn as a row of pills. A pill lights up while a run
// is on that node, so the diagram doubles as the "who is working now" view.

export const NODE_LABELS = {
  __start__:       'Starting',
  triage_agent:    'Triage',
  security_agent:  'Security',
  diagnosis_agent: 'Diagnosis',
  knowledge_agent: 'Knowledge',
  human_review:    'Human review',
  guard:           'Iteration guard',
}

const STEPS = [
  { node: 'triage_agent',    label: 'Triage',    idle: 'bg-pink-900/40 text-pink-400 border-pink-900/50',       active: 'bg-pink-600 text-white border-pink-400 ring-pink-400/50' },
  { node: 'security_agent',  label: 'Security?', idle: 'bg-red-900/40 text-red-400 border-red-900/50',          active: 'bg-red-600 text-white border-red-400 ring-red-400/50' },
  { node: 'diagnosis_agent', label: 'Diagnosis', idle: 'bg-blue-900/40 text-blue-400 border-blue-900/50',       active: 'bg-blue-600 text-white border-blue-400 ring-blue-400/50' },
  { node: 'knowledge_agent', label: 'Knowledge', idle: 'bg-yellow-900/40 text-yellow-400 border-yellow-900/50', active: 'bg-yellow-600 text-white border-yellow-400 ring-yellow-400/50' },
]

const HUMAN = {
  node: 'human_review', label: 'Human review',
  idle: 'bg-orange-900/40 text-orange-400 border-orange-900/50',
  active: 'bg-orange-600 text-white border-orange-400 ring-orange-400/50',
}

function Pill({ step, count }) {
  const on = count > 0
  return (
    <span
      title={on ? `${count} run${count > 1 ? 's' : ''} on this step now` : undefined}
      className={`inline-flex items-center gap-1.5 text-xs px-3 py-1.5 rounded-full font-medium border transition-colors
        ${on ? `${step.active} ring-4` : step.idle}`}>
      {on && (
        <span className="relative flex h-2 w-2">
          <span className="absolute inline-flex h-full w-full rounded-full bg-white opacity-75 animate-ping" />
          <span className="relative inline-flex h-2 w-2 rounded-full bg-white" />
        </span>
      )}
      {step.label}
      {count > 1 && <span className="bg-black/30 rounded-full px-1.5">{count}</span>}
    </span>
  )
}

const Arrow = () => <span className="text-nexus-muted">→</span>

/**
 * counts:    { node name: number of runs on that node right now }
 * showEnds:  draw the START / END caps
 * showHuman: always draw the human-review pill (otherwise only when a run is paused there)
 */
export default function AgentPipeline({ counts = {}, showEnds = true, showHuman = true }) {
  const humanCount = counts.human_review || 0
  return (
    <div className="flex items-center gap-2 flex-wrap">
      {showEnds && <>
        <span className="text-xs px-3 py-1.5 rounded-full font-medium text-white bg-indigo-600">START</span>
        <Arrow />
      </>}
      {STEPS.map((s, i) => (
        <span key={s.node} className="inline-flex items-center gap-2">
          {i > 0 && <Arrow />}
          <Pill step={s} count={counts[s.node] || 0} />
        </span>
      ))}
      {showEnds && <>
        <Arrow />
        <span className="text-xs px-3 py-1.5 rounded-full font-medium text-white bg-green-600">END</span>
      </>}
      {(showHuman || humanCount > 0) && (
        <span className="inline-flex items-center gap-2 ml-2 pl-4 border-l border-nexus-border">
          <Pill step={HUMAN} count={humanCount} />
        </span>
      )}
    </div>
  )
}
