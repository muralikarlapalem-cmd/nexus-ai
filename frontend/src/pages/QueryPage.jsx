import { useState, useRef, useEffect } from 'react'
import { Send, Loader2, BookOpen, ExternalLink } from 'lucide-react'
import { getAccessToken } from '../api/client'

// Where the answer came from (sent by the API as `basis`)
const BASIS_LABEL = {
  knowledge_base:    'From the knowledge base:',
  web:               'Not in the knowledge base — from these web pages:',
  general_knowledge: 'No internal document or web page was used for this answer. Verify any fix steps before applying them.',
}

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
        {msg.content && msg.basis && (
          <div className="mt-3 pt-3 border-t border-nexus-border space-y-1">
            <p className="text-xs text-nexus-muted font-medium mb-2">{BASIS_LABEL[msg.basis] || 'Sources:'}</p>
            {msg.sources?.map((s, i) => (
              <div key={i} className="flex items-center gap-2 text-xs text-nexus-muted min-w-0">
                {s.url
                  ? <ExternalLink size={10} className="flex-shrink-0" />
                  : <BookOpen size={10} className="flex-shrink-0" />}
                {s.url
                  ? <a href={s.url} target="_blank" rel="noopener noreferrer"
                      className="text-indigo-400 hover:underline truncate">{s.source}</a>
                  : <span className="font-mono truncate">{s.source}</span>}
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
    { role: 'assistant', content: 'Hello! I\'m NEXUS AI. Ask me anything about IT issues, error codes, runbooks, or past ticket resolutions. I\'ll search your knowledge base first, then the web if it has no answer, and show you the sources I used.' }
  ])
  const [input, setInput]     = useState('')
  const [loading, setLoading] = useState(false)
  const bottomRef = useRef(null)

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
      const token = await getAccessToken()   // renews a Keycloak token that's about to expire
      const resp = await fetch(url, { headers: { Authorization: `Bearer ${token}` } })
      if (!resp.ok || !resp.body) throw new Error(`HTTP ${resp.status}`)
      const reader = resp.body.getReader()
      const decoder = new TextDecoder()
      let accumulated = ''
      let buffer = ''
      let streamError = null

      while (true) {
        const { done, value } = await reader.read()
        if (done) break
        // An SSE event can be split across network chunks: keep the
        // unfinished last line in the buffer until the rest arrives
        buffer += decoder.decode(value, { stream: true })
        const lines = buffer.split('\n')
        buffer = lines.pop()
        for (const line of lines) {
          if (!line.startsWith('data: ')) continue
          const data = line.slice(6).trim()
          if (data === '[DONE]') continue
          let parsed
          try { parsed = JSON.parse(data) } catch { continue }
          if (parsed.error) streamError = parsed.error
          if (parsed.basis) {
            setMessages(m => m.map((msg, i) =>
              i === m.length - 1 ? { ...msg, sources: parsed.sources, basis: parsed.basis } : msg
            ))
          }
          if (parsed.token) {
            accumulated += parsed.token
            setMessages(m => m.map((msg, i) =>
              i === m.length - 1 ? { ...msg, content: accumulated } : msg
            ))
          }
        }
      }
      // An error the server reported mid-stream used to be swallowed,
      // leaving an empty answer bubble; fall back to the normal endpoint
      if (streamError || !accumulated) throw new Error(streamError || 'empty answer')
    } catch (err) {
      // Fallback to non-streaming
      try {
        const { data } = await (await import('../api/tickets')).ragApi.query({ query, return_sources: true })
        setMessages(m => m.map((msg, i) =>
          i === m.length - 1 ? { ...msg, content: data.answer, sources: data.sources, basis: data.basis } : msg
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
