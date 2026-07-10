# Connecting Gmail (and Drive) in Admin

This is the "just go connect your Gmail in Admin" flow, modelled on how Aspora
People connects a user's Google Calendar. It is deliberately **simple**: a
signed-in user opens Admin → Integrations and clicks **Connect Gmail**. There is
no account-linking table, no owner re-pointing, and no cross-identity
resolution.

## The model

1. **API access is a SEPARATE OAuth from sign-in.** Signing in (Google SSO, Okta
   SSO, ...) authenticates *who you are*. Connecting Gmail is a second, distinct
   OAuth consent that grants this app access to *a mailbox*. They are not the
   same grant and do not have to be the same Google account.

2. **Connect tokens bind to the SESSION's own user id.** The Gmail/Drive OAuth
   tokens are stored keyed to the app's own user record — the session identity
   (`google:<sub>`, `okta:<sub>`, `sso:<sub>`, ...), exactly the id the rest of
   the app already scopes matters, the processed-ledger, and the sync cursor to.
   `google_connection.connected_owner_user_id` is **provider-agnostic**: any
   authenticated session with a non-empty user id may connect, and the tokens
   land under *that* id. An empty/whitespace user id refuses to connect and is
   never treated as a wildcard (the ownerless contract).

3. **The connected mailbox email is METADATA, never the owner key.** After
   consent the callback verifies the returned Google **ID token** (via
   `google_identity.verify_google_id_token`) and captures the connected mailbox
   address. That address is stored as **display metadata** only
   (`connection-meta.json`, the analog of Aspora People's `external_user_id`)
   and used to enforce the domain gate. It is *never* used to derive the token
   owner. Connecting a different mailbox than the one you signed in with is
   fine, expected, and changes nothing about your tenant — "Connected as
   <email>" simply shows the other address. The tenant never moves.

4. **Fail closed on identity.** If the token exchange returns no ID token, or the
   ID token cannot be verified, the connect is **rejected** and **nothing is
   written** (no tokens, no metadata). The identity gate runs *before* any
   persistence.

## Deliberate semantics: SSO sessions own their own tenant (LOUDLY)

**An SSO-signed-in user gets a brand-new, self-contained tenant keyed to their
SSO id, with NO continuity to any pre-existing `google:<sub>` data.**

Concretely: if a person previously used the app signed in with Google
(`google:1234`) and accumulated matters there, then later signs in through Okta
(`okta:abcd`) and connects Gmail, their Gmail, matters, ledger, and cursor all
live under `okta:abcd`. The old `google:1234` matters are **not** visible from
the Okta session. **There is no bridging.** This is by design — it is the direct
consequence of keeping tokens keyed to the session's own id and never moving the
tenant. Do not build account-linking to "reunite" the two identities; that was
the abandoned over-engineered approach. Each identity is its own island.

## The domain gate

The connected mailbox is checked against the **same allowlist that sign-in
uses** (`http_auth.google_email_allowed`, backed by `NDA_ALLOWED_EMAIL_DOMAINS`
/ `NDA_ALLOWED_EMAILS`):

- **Allowlist unset/empty → any mailbox may connect.** This preserves the
  default (Render) behavior; an unconfigured allowlist does **not** fail closed.
- **Allowlist set + connected email in an allowed domain/list → connects.**
- **Allowlist set + connected email not allowed → rejected**, nothing written,
  with a clear message naming the rejected address.

## Disconnect

Disconnect removes only the **session's own** tokens (and, on a full "all"
disconnect, its connection metadata). No other owner's tokens are ever touched,
because tenancy never moved in the first place — there is no link to clear.

## Deployment prerequisites (EKS)

The connect flow requires the Google OAuth app to be configured via these
environment variables (names only; values are provisioned per environment):

- `NDA_GOOGLE_OAUTH_CLIENT_ID`
- `NDA_GOOGLE_OAUTH_CLIENT_SECRET`
- `NDA_GMAIL_OAUTH_REDIRECT_URI`

(The Google OAuth consent screen must also list the connect scopes: `openid`,
`email`, the Gmail read/send scopes, and `drive.file`.)
