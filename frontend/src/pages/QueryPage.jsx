import { useState, useRef, useEffect } from 'react'
import { Send, Loader2, BookOpen, ExternalLink } from 'lucide-react'
import { useAuthStore } from '../store/authStore'

function Message({ msg }) {
  return (
    <div className={`flex gap-3 ${msg.role === 'user' ? 'flex-row-reverse' : ''}`}>
      <div className={`w-7 h-7 rounded-full flex items-center justify-center flex-shrink-0 text-xs font-bold ${
        msg.role === 'user' ? 'bg-indigo-600 text-white' : 'bg-nexus-border text-nexus-muted'
      }`}>
        {msg.role === 'user' ? 'U' : 'AI'}
      </div>
      <div className={`max-w-2xl rounded-2xl px-4 py-3 text-sm leading-relaxed ${
        msg.role === 'user'
          ? 'bg-indigo-600 text-white rounded-tr-sm'
          : 'bg-nexus-surface border border-nexus-border text-nexus-text rounded-tl-sm'
      }`}>
        <p className="whitespace-pre-wrap">{msg.content}</p>
        {msg.sources?.length > 0 && (
          <div className="mt-3 pt-3 border-t border-nexus-border space-y-1">
            <p className="text-xs text-nexus-muted font-medium mb-2">Sources used:</p>
            {msg.sources.map((s, i) => (
              <div key={i} className="flex items-center gap-2 text-xs text-nexus-muted">
                <BookOpen size={10} className="flex-shrink-0" />
                <span className="font-mono">{s.source}</span>
                <span className="text-indigo-400">({(s.score * 100).toFixed(0)}%)</span>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}

export default function QueryPage() {
  const [messages, setMessages] = useState([
    { role: 'assistant', content: 'Hello! I\'m NEXUS AI. Ask me anything about IT issues, runbooks, or past ticket resolutions. I\'ll search the knowledge base and give you a grounded answer.' }
  ])
  const [input, setInput]     = useState('')
  const [loading, setLoading] = useState(false)
  const bottomRef = useRef(null)
  const token = useAuthStore(s => s.token)

  useEffect(() => { bottomRef.current?.scrollIntoView({ behavior: 'smooth' }) }, [messages])

  const send = async () => {
    if (!input.trim() || loading) return
    const query = input.trim()
    setInput('')
    setMessages(m => [...m, { role: 'user', content: query }])
    setLoading(true)

    // Use streaming SSE endpoint
    const assistantIdx = messages.length + 1
    setMessages(m => [...m, { role: 'assistant', content: '' }])

    try {
      const url = `/api/v1/rag/query/stream?query=${encodeURIComponent(query)}`
      const resp = await fetch(url, { headers: { Authorization: `Bearer ${token}` } })
      const reader = resp.body.getReader()
      const decoder = new TextDecoder()
      let accumulated = ''

      while (true) {
        const { done, value } = await reader.read()
        if (done) break
        const chunk = decoder.decode(value)
        const lines  = chunk.split('\n')
        for (const line of lines) {
          if (line.startsWith('data: ')) {
            const data = line.slice(6).trim()
            if (data === '[DONE]') break
            try {
              const parsed = JSON.parse(data)
              if (parsed.token) {
                accumulated += parsed.token
                setMessages(m => m.map((msg, i) =>
                  i === m.length - 1 ? { ...msg, content: accumulated } : msg
                ))
              }
              if (parsed.error) throw new Error(parsed.error)
            } catch {}
          }
        }
      }
    } catch (err) {
      // Fallback to non-streaming
      try {
        const { data } = await (await import('../api/tickets')).ragApi.query({ query, return_sources: true })
        setMessages(m => m.map((msg, i) =>
          i === m.length - 1 ? { ...msg, content: data.answer, sources: data.sources } : msg
        ))
      } catch {
        setMessages(m => m.map((msg, i) =>
          i === m.length - 1 ? { ...msg, content: 'Error: could not reach the knowledge base.' } : msg
        ))
      }
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="flex flex-col h-full">
      {/* Header */}
      <div className="px-8 py-5 border-b border-nexus-border">
        <h1 className="text-xl font-bold text-white">AI Knowledge Query</h1>
        <p className="text-nexus-muted text-xs mt-0.5">Ask about IT issues, runbooks, or past resolutions</p>
      </div>

      {/* Messages */}
      <div className="flex-1 overflow-y-auto px-8 py-6 space-y-5">
        {messages.map((msg, i) => <Message key={i} msg={msg} />)}
        {loading && !messages[messages.length - 1]?.content && (
          <div className="flex gap-3">
            <div className="w-7 h-7 rounded-full bg-nexus-border flex items-center justify-center">
              <Loader2 size={12} className="text-nexus-muted animate-spin" />
            </div>
            <div className="bg-nexus-surface border border-nexus-border rounded-2xl rounded-tl-sm px-4 py-3">
              <div className="flex gap-1">
                {[0,1,2].map(i => <div key={i} className="w-1.5 h-1.5 bg-nexus-muted rounded-full animate-bounce" style={{animationDelay:`${i*0.15}s`}} />)}
              </div>
            </div>
          </div>
        )}
        <div ref={bottomRef} />
      </div>

      {/* Input */}
      <div className="px-8 py-5 border-t border-nexus-border">
        <div className="flex gap-3">
          <input value={input} onChange={e => setInput(e.target.value)}
            onKeyDown={e => e.key === 'Enter' && !e.shiftKey && send()}
            placeholder="Ask about a network issue, database error, or past ticket…"
            className="flex-1 bg-nexus-surface border border-nexus-border rounded-xl px-4 py-3 text-sm text-white placeholder-nexus-muted focus:outline-none focus:border-indigo-500 transition" />
          <button onClick={send} disabled={!input.trim() || loading}
            className="bg-indigo-600 hover:bg-indigo-500 disabled:opacity-50 text-white rounded-xl px-4 transition">
            <Send size={16} />
          </button>
        </div>
      </div>
    </div>
  )
}
