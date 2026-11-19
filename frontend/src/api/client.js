import axios from 'axios'
import { useAuthStore } from '../store/authStore'
import { refreshSso } from '../auth/sso'

const client = axios.create({ baseURL: '/api/v1', timeout: 30_000 })

// Keycloak access tokens last 15 minutes: renew one a minute before it runs out.
// Several requests at once share a single renewal.
let refreshing = null
export async function getAccessToken() {
  const { token, sso, setSsoTokens, logout } = useAuthStore.getState()
  if (!sso || !token) return token
  if (sso.expiresAt - Date.now() > 60_000) return token
  refreshing ??= refreshSso(sso)
    .then(session => { setSsoTokens(session); return session.accessToken })
    .catch(() => { logout(); return null })
    .finally(() => { refreshing = null })
  return refreshing
}

client.interceptors.request.use(async cfg => {
  const token = await getAccessToken()
  if (token) cfg.headers.Authorization = `Bearer ${token}`
  return cfg
})

client.interceptors.response.use(
  r => r,
  err => {
    if (err.response?.status === 401) useAuthStore.getState().logout()
    return Promise.reject(err)
  }
)

export default client
