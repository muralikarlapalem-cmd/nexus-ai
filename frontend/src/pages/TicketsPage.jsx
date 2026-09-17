import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Plus, Search, Filter, ChevronRight, Loader2 } from 'lucide-react'
import { ticketApi } from '../api/tickets'
import { format } from 'date-fns'

const PRIORITY_DOT = { critical: 'bg-red-500', high: 'bg-orange-500', medium: 'bg-yellow-500', low: 'bg-green-500' }

function CreateTicketModal({ onClose }) {
  const qc = useQueryClient()
  const [form, setForm] = useState({ title: '', description: '', category: 'other', priority: 'medium' })
  const { mutate, isPending } = useMutation({
    mutationFn: ticketApi.create,
    onSuccess: () => { qc.invalidateQueries(['tickets']); onClose() },
  })

  return (
    <div className="fixed inset-0 bg-black/60 flex items-center justify-center z-50 p-4">
      <div className="bg-nexus-surface border border-nexus-border rounded-2xl w-full max-w-lg p-6">
        <h2 className="text-lg font-semibold text-white mb-5">New Ticket</h2>
        <div className="space-y-4">
          <div>
            <label className="text-xs text-nexus-muted mb-1.5 block">Title</label>
            <input value={form.title} onChange={e => setForm(f => ({...f, title: e.target.value}))}
              className="w-full bg-nexus-bg border border-nexus-border rounded-lg px-3 py-2.5 text-sm text-white focus:outline-none focus:border-indigo-500"
              placeholder="Brief description of the issue" />
          </div>
          <div>
            <label className="text-xs text-nexus-muted mb-1.5 block">Description</label>
            <textarea rows={4} value={form.description} onChange={e => setForm(f => ({...f, description: e.target.value}))}
              className="w-full bg-nexus-bg border border-nexus-border rounded-lg px-3 py-2.5 text-sm text-white focus:outline-none focus:border-indigo-500 resize-none"
              placeholder="Detailed steps to reproduce, error messages, affected systems..." />
          </div>
          <div className="grid grid-cols-2 gap-3">
            {[['category', ['network','database','software','hardware','security','access','performance','other']],
              ['priority', ['low','medium','high','critical']]].map(([field, opts]) => (
              <div key={field}>
                <label className="text-xs text-nexus-muted mb-1.5 block capitalize">{field}</label>
                <select value={form[field]} onChange={e => setForm(f => ({...f, [field]: e.target.value}))}
                  className="w-full bg-nexus-bg border border-nexus-border rounded-lg px-3 py-2.5 text-sm text-white focus:outline-none focus:border-indigo-500">
                  {opts.map(o => <option key={o} value={o}>{o}</option>)}
                </select>
              </div>
            ))}
          </div>
        </div>
        <div className="flex gap-3 mt-6">
          <button onClick={onClose} className="flex-1 border border-nexus-border text-nexus-muted rounded-lg py-2.5 text-sm hover:text-white transition">Cancel</button>
          <button disabled={isPending || !form.title || !form.description}
            onClick={() => mutate(form)}
            className="flex-1 bg-indigo-600 hover:bg-indigo-500 disabled:opacity-50 text-white rounded-lg py-2.5 text-sm font-medium flex items-center justify-center gap-2">
            {isPending ? <><Loader2 size={14} className="animate-spin" />Creating…</> : 'Create Ticket'}
          </button>
        </div>
      </div>
    </div>
  )
}

export default function TicketsPage() {
  const navigate = useNavigate()
  const [showCreate, setShowCreate] = useState(false)
  const [filters, setFilters] = useState({ status: '', priority: '' })

  const { data, isLoading } = useQuery({
    queryKey: ['tickets', filters],
    queryFn: () => ticketApi.list({ page: 1, page_size: 50, ...filters }).then(r => r.data),
  })

  return (
    <div className="p-8">
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-2xl font-bold text-white">Tickets</h1>
          <p className="text-nexus-muted text-sm mt-1">{data?.total ?? 0} total</p>
        </div>
        <button onClick={() => setShowCreate(true)}
          className="flex items-center gap-2 bg-indigo-600 hover:bg-indigo-500 text-white rounded-lg px-4 py-2 text-sm font-medium transition">
          <Plus size={14} /> New Ticket
        </button>
      </div>

      {/* Filters */}
      <div className="flex gap-3 mb-5">
        {[['status', ['','open','triaged','in_progress','resolved','closed']],
          ['priority', ['','low','medium','high','critical']]].map(([field, opts]) => (
          <select key={field} value={filters[field]}
            onChange={e => setFilters(f => ({...f, [field]: e.target.value}))}
            className="bg-nexus-surface border border-nexus-border rounded-lg px-3 py-1.5 text-sm text-nexus-muted focus:outline-none focus:border-indigo-500">
            <option value="">All {field}s</option>
            {opts.filter(Boolean).map(o => <option key={o} value={o}>{o}</option>)}
          </select>
        ))}
      </div>

      {/* Table */}
      <div className="bg-nexus-surface border border-nexus-border rounded-xl overflow-hidden">
        <table className="w-full">
          <thead>
            <tr className="border-b border-nexus-border">
              {['Ticket', 'Title', 'Category', 'Priority', 'Status', 'Created', ''].map(h => (
                <th key={h} className="text-left text-xs font-medium text-nexus-muted px-4 py-3">{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {isLoading && <tr><td colSpan={7} className="text-center text-nexus-muted py-8 text-sm">Loading…</td></tr>}
            {data?.items?.map(t => (
              <tr key={t.id} onClick={() => navigate(`/tickets/${t.id}`)}
                className="border-b border-nexus-border hover:bg-white/[0.02] cursor-pointer transition">
                <td className="px-4 py-3 text-xs font-mono text-indigo-400">{t.ticket_number}</td>
                <td className="px-4 py-3 text-sm text-white max-w-xs truncate">{t.title}</td>
                <td className="px-4 py-3 text-xs text-nexus-muted capitalize">{t.category}</td>
                <td className="px-4 py-3">
                  <div className="flex items-center gap-2">
                    <div className={`w-1.5 h-1.5 rounded-full ${PRIORITY_DOT[t.priority]}`} />
                    <span className="text-xs text-nexus-muted capitalize">{t.priority}</span>
                  </div>
                </td>
                <td className="px-4 py-3">
                  <span className={`text-xs px-2 py-0.5 rounded-full font-medium ${
                    t.status === 'resolved' ? 'bg-green-900/40 text-green-400'
                    : t.status === 'open'   ? 'bg-blue-900/40 text-blue-400'
                    : 'bg-yellow-900/40 text-yellow-400'}`}>{t.status}</span>
                </td>
                <td className="px-4 py-3 text-xs text-nexus-muted">{format(new Date(t.created_at), 'MMM d, HH:mm')}</td>
                <td className="px-4 py-3"><ChevronRight size={14} className="text-nexus-muted" /></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {showCreate && <CreateTicketModal onClose={() => setShowCreate(false)} />}
    </div>
  )
}
