import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Brain, Loader2, KeyRound } from 'lucide-react'
import { useAuthStore } from '../store/authStore'
import { authApi } from '../api/tickets'
import { getSsoConfig, startSsoLogin } from '../auth/sso'

export default function LoginPage() {
  const [form, setForm]   = useState({ username: '', password: '' })
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  const { login } = useAuthStore()
  const navigate  = useNavigate()
  const [sso, setSso] = useState(null)          // /auth/sso-config: is Keycloak sign-in ready?
  const [ssoBusy, setSsoBusy] = useState(false)

  useEffect(() => { getSsoConfig().then(setSso) }, [])

  const handleSso = async () => {
    setError(''); setSsoBusy(true)
    try { await startSsoLogin() }                 // leaves the page for Keycloak
    catch (e) { setError(e.message); setSsoBusy(false) }
  }

  const handleSubmit = async e => {
    e.preventDefault()
    setError(''); setLoading(true)
    try {
      const { data } = await authApi.login(form)
      
      // re-fetch me with new token
      import('../api/client').then(async ({ default: client }) => {
        client.defaults.headers.Authorization = `Bearer ${data.access_token}`
        const { data: user } = await authApi.me()
        login(data.access_token, user)
        navigate('/dashboard')
      })
    } catch {
      setError('Invalid username or password')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="min-h-screen flex items-center justify-center bg-nexus-bg px-4">
      <div className="w-full max-w-sm">
        {/* Logo */}
        <div className="flex flex-col items-center mb-8">
          <div className="w-14 h-14 rounded-2xl bg-gradient-to-br from-indigo-500 to-purple-600 flex items-center justify-center mb-4 shadow-lg shadow-indigo-500/25">
            <Brain size={28} className="text-white" />
          </div>
          <h1 className="text-2xl font-bold text-white">NEXUS AI</h1>
          <p className="text-nexus-muted text-sm mt-1">IT Service Intelligence Platform</p>
        </div>

        {/* Card */}
        <div className="bg-nexus-surface border border-nexus-border rounded-2xl p-6 shadow-xl">
          <h2 className="text-lg font-semibold text-white mb-1">Sign in</h2>
          <p className="text-nexus-muted text-sm mb-6">
            {sso?.enabled ? 'Use your organisation account, or a local NEXUS account' : 'Use your NEXUS account credentials'}
          </p>

          {sso?.enabled && (
            <>
              <button type="button" onClick={handleSso} disabled={ssoBusy}
                className="w-full bg-white hover:bg-slate-100 disabled:opacity-60 text-slate-900 font-medium rounded-lg py-2.5 text-sm transition-colors flex items-center justify-center gap-2">
                {ssoBusy ? <Loader2 size={14} className="animate-spin" /> : <KeyRound size={14} />}
                Sign in with Keycloak
              </button>
              <div className="flex items-center gap-3 my-5" aria-hidden="true">
                <div className="h-px flex-1 bg-nexus-border" />
                <span className="text-xs text-nexus-muted">or with a local account</span>
                <div className="h-px flex-1 bg-nexus-border" />
              </div>
            </>
          )}

          <form onSubmit={handleSubmit} className="space-y-4">
            <div>
              <label className="block text-xs font-medium text-nexus-muted mb-1.5">Username</label>
              <input
                type="text" required autoFocus
                value={form.username}
                onChange={e => setForm(f => ({ ...f, username: e.target.value }))}
                className="w-full bg-nexus-bg border border-nexus-border rounded-lg px-3 py-2.5 text-sm text-white placeholder-nexus-muted focus:outline-none focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500/30 transition"
                placeholder="admin"
              />
            </div>
            <div>
              <label className="block text-xs font-medium text-nexus-muted mb-1.5">Password</label>
              <input
                type="password" required
                value={form.password}
                onChange={e => setForm(f => ({ ...f, password: e.target.value }))}
                className="w-full bg-nexus-bg border border-nexus-border rounded-lg px-3 py-2.5 text-sm text-white placeholder-nexus-muted focus:outline-none focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500/30 transition"
                placeholder="••••••••"
              />
            </div>

            {error && <p className="text-red-400 text-xs">{error}</p>}

            <button
              type="submit" disabled={loading}
              className="w-full bg-indigo-600 hover:bg-indigo-500 disabled:opacity-50 text-white font-medium rounded-lg py-2.5 text-sm transition-colors flex items-center justify-center gap-2"
            >
              {loading ? <><Loader2 size={14} className="animate-spin" /> Signing in…</> : 'Sign in'}
            </button>
          </form>
        </div>
        <p className="text-center text-xs text-nexus-muted mt-4">
          {sso?.enabled ? 'Single sign-on by Keycloak' : (sso?.reason || 'Local sign-in')}
        </p>
      </div>
    </div>
  )
}
