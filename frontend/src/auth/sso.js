// NEXUS AI · Keycloak single sign-on (OpenID Connect, authorization code + PKCE).
// No client secret: the browser proves it started the sign-in with a one-time
// code verifier. Keycloak's address, realm and client come from /auth/sso-config.

import axios from 'axios'

const CALLBACK_PATH = '/auth/callback'
const PENDING_KEY = 'nexus-sso-pending'

let configPromise = null
export function getSsoConfig() {
  configPromise ??= axios.get('/api/v1/auth/sso-config', { timeout: 5000 })
    .then(r => r.data)
    .catch(() => ({ enabled: false, reason: 'Could not reach the NEXUS API' }))
  return configPromise
}

const endpoint = (cfg, name) => `${cfg.url}/realms/${cfg.realm}/protocol/openid-connect/${name}`

function randomString(bytes = 32) {
  const a = crypto.getRandomValues(new Uint8Array(bytes))
  return base64url(a)
}

function base64url(bytes) {
  let s = ''
  bytes.forEach(b => { s += String.fromCharCode(b) })
  return btoa(s).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
}

async function sha256(text) {
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(text))
  return base64url(new Uint8Array(digest))
}

/** Go to Keycloak's sign-in page. */
export async function startSsoLogin() {
  const cfg = await getSsoConfig()
  if (!cfg.enabled) throw new Error(cfg.reason || 'Single sign-on is not available')
  const verifier = randomString(48)
  const state = randomString(16)
  sessionStorage.setItem(PENDING_KEY, JSON.stringify({ verifier, state }))
  const params = new URLSearchParams({
    client_id: cfg.client_id,
    redirect_uri: window.location.origin + CALLBACK_PATH,
    response_type: 'code',
    scope: 'openid profile email',
    code_challenge: await sha256(verifier),
    code_challenge_method: 'S256',
    state,
  })
  window.location.assign(`${endpoint(cfg, 'auth')}?${params}`)
}

function toSession(data) {
  return {
    accessToken: data.access_token,
    refreshToken: data.refresh_token,
    idToken: data.id_token,
    expiresAt: Date.now() + (data.expires_in || 300) * 1000,
  }
}

async function tokenRequest(cfg, form) {
  const resp = await fetch(endpoint(cfg, 'token'), {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({ client_id: cfg.client_id, ...form }),
  })
  const data = await resp.json().catch(() => ({}))
  if (!resp.ok) throw new Error(data.error_description || data.error || `Keycloak answered ${resp.status}`)
  return toSession(data)
}

/** Back from Keycloak: check the state and swap the code for tokens. */
export async function finishSsoLogin(search) {
  const params = new URLSearchParams(search)
  if (params.get('error')) throw new Error(params.get('error_description') || params.get('error'))
  const pending = JSON.parse(sessionStorage.getItem(PENDING_KEY) || 'null')
  sessionStorage.removeItem(PENDING_KEY)
  if (!pending || params.get('state') !== pending.state) {
    throw new Error('This sign-in link is out of date. Please sign in again.')
  }
  const cfg = await getSsoConfig()
  return tokenRequest(cfg, {
    grant_type: 'authorization_code',
    code: params.get('code') || '',
    redirect_uri: window.location.origin + CALLBACK_PATH,
    code_verifier: pending.verifier,
  })
}

/** New access token before the old one expires (15 min in the NEXUS realm). */
export async function refreshSso(session) {
  const cfg = await getSsoConfig()
  return tokenRequest(cfg, { grant_type: 'refresh_token', refresh_token: session.refreshToken })
}

/** Sign out of Keycloak too, then come back to the login page. */
export async function ssoLogout(session) {
  const cfg = await getSsoConfig()
  if (!cfg.enabled) return false
  const params = new URLSearchParams({
    client_id: cfg.client_id,
    post_logout_redirect_uri: window.location.origin + '/login',
    ...(session?.idToken ? { id_token_hint: session.idToken } : {}),
  })
  window.location.assign(`${endpoint(cfg, 'logout')}?${params}`)
  return true
}
