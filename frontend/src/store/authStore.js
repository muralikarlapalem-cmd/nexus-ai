import { create } from 'zustand'
import { persist } from 'zustand/middleware'

// `sso` is set when the user signed in through Keycloak:
// { refreshToken, idToken, expiresAt }. Local sign-ins leave it null.
export const useAuthStore = create(
  persist(
    (set) => ({
      token: null,
      user:  null,
      sso:   null,
      login:  (token, user, sso = null) => set({ token, user, sso }),
      setSsoTokens: (session) => set({
        token: session.accessToken,
        sso: { refreshToken: session.refreshToken, idToken: session.idToken, expiresAt: session.expiresAt },
      }),
      logout: () => set({ token: null, user: null, sso: null }),
    }),
    { name: 'nexus-auth' }
  )
)
