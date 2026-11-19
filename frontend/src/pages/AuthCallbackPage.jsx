import { useEffect, useRef, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { Brain, Loader2 } from 'lucide-react'
import { useAuthStore } from '../store/authStore'
import { finishSsoLogin } from '../auth/sso'
import { authApi } from '../api/tickets'

// Keycloak sends the browser back here with a one-time code after sign-in
export default function AuthCallbackPage() {
  const [error, setError] = useState('')
  const { login, logout } = useAuthStore()
  const navigate = useNavigate()
  const ran = useRef(false)

  useEffect(() => {
    if (ran.current) return          // React dev mode runs effects twice; the code works once
    ran.current = true
    ;(async () => {
      try {
        const session = await finishSsoLogin(window.location.search)
        const sso = { refreshToken: session.refreshToken, idToken: session.idToken, expiresAt: session.expiresAt }
        login(session.accessToken, null, sso)
        const { data: user } = await authApi.me()     // first sign-in creates the NEXUS account
        login(session.accessToken, user, sso)
        navigate('/dashboard', { replace: true })
      } catch (e) {
        logout()
        setError(e?.response?.data?.detail || e.message || 'Sign-in failed')
      }
    })()
  }, [])

  return (
    <div className="min-h-screen flex items-center justify-center bg-nexus-bg px-4">
      <div className="w-full max-w-sm text-center">
        <div className="w-14 h-14 rounded-2xl bg-gradient-to-br from-indigo-500 to-purple-600 flex items-center justify-center mb-4 mx-auto">
          <Brain size={28} className="text-white" />
        </div>
        {error ? (
          <>
            <p className="text-red-400 text-sm mb-4" role="alert">{error}</p>
            <Link to="/login" className="text-indigo-300 hover:text-white text-sm">Back to sign-in</Link>
          </>
        ) : (
          <p className="text-nexus-muted text-sm flex items-center justify-center gap-2">
            <Loader2 size={14} className="animate-spin" /> Signing you in…
          </p>
        )}
      </div>
    </div>
  )
}
