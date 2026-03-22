# OAuth2 / OIDC and workflow scopes

MiniCloud can act as an **OAuth 2.0 resource server**: callers send an **access token** (usually a JWT) in `Authorization: Bearer …`. The orchestrator verifies the JWT using **JWKS** (public keys from your identity provider) and enforces **scopes** so you can:

- Restrict which **workflows** a client may start.
- Restrict **egress** (outbound HTTP, FTP, SSH, SFTP) independently — e.g. allow transforms but deny external HTTP.

This complements network policies and optional static bearer tokens (`HTTP_INVOCATION_TOKEN` / `SCHEDULE_INVOCATION_TOKEN`).

---

## 1. Where scopes live (recommendation)

**Do not** invent a second database of permissions inside MiniCloud for normal use.

| Approach | Description |
|----------|-------------|
| **Recommended** | Configure your **identity provider** (Keycloak, Microsoft Entra ID, Auth0, Okta, …) to put **scopes or roles** into the access token. Map client roles or groups to strings such as `minicloud:egress:http`. |
| **API gateway** | Some teams terminate OAuth at an API gateway and forward a signed internal header; MiniCloud currently expects a **standard Bearer JWT** on the orchestrator (and the gateway forwards `Authorization`). |
| **Future** | Token introspection (opaque tokens) could be added without changing the scope model. |

The orchestrator reads scopes from JWT claims:

- `scope` — space-separated string (common in OAuth2)
- `scp` — array of strings (some providers)
- `permissions` — optional array (treated like extra scopes)

---

## 2. Environment variables (orchestrator)

| Variable | Meaning |
|----------|---------|
| `OAUTH2_ENABLED` | `true` / `1` — enable JWT validation on workflow entrypoints; **disables** static `HTTP_INVOCATION_TOKEN` / `SCHEDULE_INVOCATION_TOKEN` for those routes (JWT replaces shared secrets). |
| `OAUTH2_JWKS_URI` | HTTPS URL of the JWKS (e.g. `https://idp.example.com/realms/foo/protocol/openid-connect/certs`). **Required** when OAuth is enabled. |
| `OAUTH2_ISSUER` | Expected `iss` claim (strongly recommended). |
| `OAUTH2_AUDIENCE` | Expected `aud` claim (recommended for production). |
| `OAUTH2_SCOPE_PREFIX` | Prefix for built-in scope names; default `minicloud`. |
| `OAUTH2_APPLY_TO_SCHEDULED` | Default `true`. If `false` and `OAUTH2_ENABLED` is `true`, **`POST /invoke/scheduled`** uses only `SCHEDULE_INVOCATION_TOKEN` (no JWT) and **egress scope checks are skipped** on that route — use only if isolated by network policy. |

---

## 3. Scope strings

Default prefix is `minicloud` (configurable via `OAUTH2_SCOPE_PREFIX`).

| Scope | Meaning |
|-------|---------|
| `{prefix}:workflow:run:<workflow_name>` | Allowed to start that workflow (e.g. `minicloud:workflow:run:demo`). |
| `{prefix}:workflow:run:*` | Any workflow. |
| `{prefix}:egress:http` | Allowed to run **`http`** steps. |
| `{prefix}:egress:ftp` | **`ftp`** steps. |
| `{prefix}:egress:ssh` | **`ssh`** steps. |
| `{prefix}:egress:sftp` | **`sftp`** steps. |
| `{prefix}:egress:*` | All egress types. |
| `{prefix}:*` | Wildcard for all MiniCloud checks (use sparingly). |

**Wildcards:** `minicloud:egress:*` matches any egress sub-scope; `minicloud:*` matches everything checked.

**Transforms** (XSLT, Liquid, xml2json, …) do **not** require a separate scope if the caller may run the workflow — only **egress** steps are gated.

---

## 4. Gateway and clients

- The **gateway** forwards the **`Authorization`** header to the orchestrator for `POST /v1/run*`.
- Clients obtain access tokens from your IdP (authorization code, client credentials for machine-to-machine, etc.) and send `Authorization: Bearer <access_token>`.

---

## 5. Scheduled jobs (`POST /invoke/scheduled`)

With default `OAUTH2_APPLY_TO_SCHEDULED=true`, CronJobs should use a **machine-to-machine** OAuth client and request tokens with the same scopes as interactive users.

If you must keep a long-lived shared secret for cron only, set `OAUTH2_APPLY_TO_SCHEDULED=false` and rely on **`SCHEDULE_INVOCATION_TOKEN`** plus **network isolation** — egress scope enforcement will not apply on that route.

---

## 6. Relation to static bearer tokens

- **`OAUTH2_ENABLED=false`** (default): optional **`HTTP_INVOCATION_TOKEN`** / **`SCHEDULE_INVOCATION_TOKEN`** as before.
- **`OAUTH2_ENABLED=true`**: JWT validation is required; static tokens are **not** used for those routes. Use OAuth scopes for fine-grained control.

Back to [README – Environment variables](../README.md#environment-variables).
