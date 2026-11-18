import { ExternalLink } from 'lucide-react'

// Short labels for connector types; unknown types fall back to the connection name
const SOURCE_LABELS = { servicenow: 'ServiceNow', generic_rest: 'REST' }

/**
 * Shows where a ticket came from, e.g. "ServiceNow · INC0010023 ↗".
 * Renders nothing for tickets created in NEXUS.
 */
export default function SourceBadge({ ticket, size = 'sm', stopPropagation = false }) {
  if (!ticket?.source || ticket.source === 'nexus') return null
  const label = SOURCE_LABELS[ticket.source] || ticket.source_name || ticket.source
  const text = ticket.external_number ? `${label} · ${ticket.external_number}` : label
  const classes = size === 'xs'
    ? 'text-[10px] px-1.5 py-0.5'
    : 'text-xs px-2 py-0.5'
  const base = `inline-flex items-center gap-1 rounded-full font-medium bg-emerald-900/30 text-emerald-300 border border-emerald-800/50 ${classes}`

  if (!ticket.external_url) return <span className={base} title={ticket.source_name}>{text}</span>
  return (
    <a
      href={ticket.external_url}
      target="_blank"
      rel="noreferrer"
      title={`Open in ${ticket.source_name || label}`}
      onClick={stopPropagation ? (e) => e.stopPropagation() : undefined}
      className={`${base} hover:bg-emerald-900/50 transition-colors`}
    >
      {text}
      <ExternalLink size={size === 'xs' ? 9 : 11} />
    </a>
  )
}
