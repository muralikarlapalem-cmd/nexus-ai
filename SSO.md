# Single sign-on with Keycloak

NEXUS AI can sign people in through **Keycloak**, the identity server that already runs in
docker-compose. The login page then shows **Sign in with Keycloak** above the usual
username/password form. Local NEXUS accounts keep working.

| | |
|---|---|
| Keycloak admin console | http://localhost:8080/admin (user `KC_ADMIN`, password `KC_ADMIN_PASSWORD` from `.env`) |
| NEXUS realm | `nexus` |
| Web app client | `nexus-frontend` (public client, PKCE) |
| Roles | `nexus-admin`, `nexus-agent`, `nexus-analyst`; anyone else is a plain user |

---

## 1. One-time setup (Command Prompt, project folder)

1. Make sure Keycloak is running: `docker compose up -d keycloak`, then wait until
   http://localhost:8080 opens (about a minute on first start).
2. Create the realm, roles and web-app client:
   ```
   docker compose exec api python -m app.cli sso-setup
   ```
   It's safe to run again; it repairs anything missing. If the web app isn't at
   `http://localhost:5173`, add `--app-url http://<host>:<port>`.
3. Add people (each command asks for the password twice):
   ```
   docker compose exec api python -m app.cli sso-user priya --email priya@example.com --role admin --name "Priya Sharma"
   docker compose exec api python -m app.cli sso-user ravi --email ravi@example.com --role agent
   ```
   Run it again with another `--role` to change someone's role, adding `--keep-password` to
   leave the password alone. You can also manage users in the Keycloak admin console: give them
   one of the `nexus-*` realm roles.
4. Reload http://localhost:5173: **Sign in with Keycloak** appears on the login page.

---

## 2. Where to sign in

Start at the NEXUS app, http://localhost:5173, and click **Sign in with Keycloak**. Never sign
in to NEXUS at http://localhost:8080/admin: that is the Keycloak **admin console** (the `master`
realm), where only `admin` exists. Its sign-in page looks the same, so check the address bar:
NEXUS users sign in on a page whose address contains **`/realms/nexus/`**.

---

## 3. What happens when someone signs in

1. **Sign in with Keycloak** sends the browser to Keycloak's sign-in page, with a one-time PKCE
   challenge (no client secret is ever in the browser).
2. After the password, Keycloak sends the browser back to `/auth/callback` with a one-time code.
   The web app swaps it, plus the PKCE proof, for tokens.
3. The web app calls the NEXUS API with the Keycloak access token. The API checks the signature
   (the realm's public keys), expiry, issuer (the `nexus` realm) and audience (`nexus-backend`).
4. **First sign-in:** NEXUS creates the account. If a local account already has the same
   e-mail and Keycloak has verified that e-mail, the two are linked instead.
5. **Role:** on every request the NEXUS role follows the person's Keycloak roles, so a change in
   Keycloak applies at their next request. Changes are recorded in the audit log.
6. **Staying signed in:** access tokens last 15 minutes; the web app renews them in the
   background shortly before they expire.
7. **Sign out** (the arrow next to your name) also ends the Keycloak session, so the next sign-in
   asks for the password again.

---

## 4. Settings (`.env`)

| Variable | Default | What |
|---|---|---|
| `KEYCLOAK_URL` | `http://keycloak:8080` | Keycloak inside Docker (the API uses it) |
| `KEYCLOAK_PUBLIC_URL` | `http://localhost:8080` | Keycloak as the browser sees it. Tokens carry this address. |
| `KEYCLOAK_REALM` | `nexus` | |
| `KEYCLOAK_CLIENT_ID` | `nexus-backend` | The audience the API requires in tokens |
| `KEYCLOAK_FRONTEND_CLIENT_ID` | `nexus-frontend` | The web app's client |
| `SSO_ENABLED` | `true` | `false` hides the button |
| `KC_ADMIN`, `KC_ADMIN_PASSWORD` | | Keycloak admin; used by `sso-setup` and `sso-user` only |

`KEYCLOAK_CLIENT_SECRET` isn't used by sign-in.

---

## 5. If something doesn't work

Start with `docker compose exec api python -m app.cli sso-check <username> --test-password`: it
shows whether the user exists, is enabled or locked, has a password and a role, and whether the
password you type is accepted.

| Symptom | Fix |
|---|---|
| A NEXUS user gets "Invalid username or password" with the right password | They're on the admin console's sign-in page. Start from NEXUS → **Sign in with Keycloak**; the address must contain `/realms/nexus/` |
| "Invalid username or password" after many wrong tries | 30 wrong passwords lock the account for a while (even the right one is refused): `sso-check <username> --unlock` |
| `sso-setup` / `sso-user` say "Could not sign in to Keycloak as 'admin' (401)" | Keycloak sets the admin password only on its first start; a later change to `KC_ADMIN_PASSWORD` in `.env` doesn't reach it. Sign in to the admin console with the password that works, reset it (realm **master** → Users → admin → Credentials, Temporary off) to the one in `.env`, then `docker compose up -d --force-recreate api` |
| No **Sign in with Keycloak** button | The text under the sign-in box says why. Usually the realm doesn't exist yet: run `sso-setup`. |
| Keycloak says **Invalid parameter: redirect_uri** | The app runs at another address: `sso-setup --app-url http://<that address>` |
| Signed in at Keycloak, then "Invalid Keycloak token: unexpected issuer" | You opened Keycloak by another address than `KEYCLOAK_PUBLIC_URL`. Use http://localhost:8080 or change the setting. |
| "A NEXUS account already uses this e-mail" | Mark the e-mail as verified for that user in Keycloak, or use another e-mail |
| Someone has the wrong role | Change their `nexus-*` role in Keycloak (or `sso-user ... --role`); it applies at their next request |

A guide for all the quick wins, with sign-in and troubleshooting: [NEXUS AI Quick Wins guide](https://claude.ai/artifact/8EyypkcQ2XKmhW4UvUg18a).

---

## 6. Before using this in production

Keycloak runs with `start-dev` here: HTTP, development settings. For production run it with
`start`, HTTPS and its own database, set `KEYCLOAK_PUBLIC_URL` to its HTTPS address, and run
`sso-setup --app-url https://<your NEXUS address>`. Local sign-in is refused when
`APP_ENV=production`, so everyone then signs in through Keycloak.
