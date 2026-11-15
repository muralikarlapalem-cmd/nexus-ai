import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient, keepPreviousData } from '@tanstack/react-query'
import { Plus, Search, ChevronRight, ChevronLeft, Loader2, X } from 'lucide-react'
import { ticketApi } from '../api/tickets'
import { format } from 'date-fns'

const PAGE_SIZE = 20

const PRIORITY_DOT ={ critical: 'bg-red-500', high: 'bg-orange-500', medium: 'bg-yellow-500', low: 'bg-green-500' }

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
  const [searchInput, setSearchInput] = useState('')
  const [search, setSearch] = useState('')
  const [page, setPage] = useState(1)

  // Wait for a short pause in typing before searching, then go back to page 1
  useEffect(() => {
    const t = setTimeout(() => { setSearch(searchInput.trim()); setPage(1) }, 300)
    return () => clearTimeout(t)
  }, [searchInput])

  const { data, isLoading, isFetching } = useQuery({
    queryKey: ['tickets', filters, search, page],
    queryFn: () => ticketApi.list({ page, page_size: PAGE_SIZE, q: search, ...filters }).then(r => r.data),
    placeholderData: keepPreviousData,   // keep the current page visible while the next loads
  })

  const total = data?.total ?? 0
  const pages = data?.pages ?? 0
  const first = total ? (page - 1) * PAGE_SIZE + 1 : 0
  const last = Math.min(page * PAGE_SIZE, total)

  return (
    <div className="p-8">
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-2xl font-bold text-white">Tickets</h1>
          <p className="text-nexus-muted text-sm mt-1">{total} {search || filters.status || filters.priority ? 'matching' : 'total'}</p>
        </div>
        <button onClick={() => setShowCreate(true)}
          className="flex items-center gap-2 bg-indigo-600 hover:bg-indigo-500 text-white rounded-lg px-4 py-2 text-sm font-medium transition">
          <Plus size={14} /> New Ticket
        </button>
      </div>

      {/* Filters */}
      <div className="flex flex-wrap gap-3 mb-5">
        <div className="relative w-72">
          <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-nexus-muted" />
          <input value={searchInput} onChange={e => setSearchInput(e.target.value)}
            placeholder="Search ticket number, title, description…"
            className="w-full bg-nexus-surface border border-nexus-border rounded-lg pl-9 pr-8 py-1.5 text-sm text-white placeholder:text-nexus-muted focus:outline-none focus:border-indigo-500" />
          {searchInput && (
            <button onClick={() => setSearchInput('')} title="Clear search"
              className="absolute right-2 top-1/2 -translate-y-1/2 text-nexus-muted hover:text-white">
              <X size={14} />
            </button>
          )}
        </div>
        {[['status', ['','open','triaged','in_progress','awaiting_user','resolved','closed','cancelled']],
          ['priority', ['','low','medium','high','critical']]].map(([field, opts]) => (
          <select key={field} value={filters[field]}
            onChange={e => { setFilters(f => ({...f, [field]: e.target.value})); setPage(1) }}
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
            {!isLoading && total === 0 && (
              <tr><td colSpan={7} className="text-center text-nexus-muted py-8 text-sm">
                {search ? <>No tickets match “{search}”</> : 'No tickets found'}
              </td></tr>
            )}
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
        {pages > 1 && (
          <div className="flex items-center justify-between px-4 py-3 border-t border-nexus-border text-xs text-nexus-muted">
            <span className="flex items-center gap-2">
              Showing {first}–{last} of {total}
              {isFetching && <Loader2 size={12} className="animate-spin" />}
            </span>
            <div className="flex items-center gap-2">
              <button onClick={() => setPage(p => Math.max(1, p - 1))} disabled={page <= 1}
                className="flex items-center gap-1 border border-nexus-border rounded-lg px-2.5 py-1 hover:text-white disabled:opacity-40 disabled:hover:text-nexus-muted">
                <ChevronLeft size={12} /> Previous
              </button>
              <span>Page {page} of {pages}</span>
              <button onClick={() => setPage(p => Math.min(pages, p + 1))} disabled={page >= pages}
                className="flex items-center gap-1 border border-nexus-border rounded-lg px-2.5 py-1 hover:text-white disabled:opacity-40 disabled:hover:text-nexus-muted">
                Next <ChevronRight size={12} />
              </button>
            </div>
          </div>
        )}
      </div>
      {showCreate && <CreateTicketModal onClose={() => setShowCreate(false)} />}
    </div>
  )
}
