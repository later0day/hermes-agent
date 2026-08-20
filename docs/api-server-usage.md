# API Server: Invocation & Multi-Profile Usage

> **Audience:** Gateway operators and API integrators
> **Source files:** `gateway/platforms/api_server.py`
> **Related:** [Profile-Based Routing](profile-routing.md), [Relay ↔ Connector Contract](relay-connector-contract.md)

The gateway ships a built-in **OpenAI-compatible HTTP server** (the `api_server`
platform). It exposes Chat Completions / Responses / Runs endpoints backed by a
full Hermes agent turn (tools, memory, persona), and supports serving multiple
isolated **profiles** from a single listener via a `/p/<profile>/…` URL prefix.

## Overview

- **Listen address:** `http://0.0.0.0:8643` (loopback: `127.0.0.1:8643`).
- **Model id:** `hermes-agent` for the default profile; under a profile prefix
  `/v1/models` advertises the profile name as the model id (e.g. `xcx`).
- **Auth:** `Authorization: Bearer <API_SERVER_KEY>`, timing-safe comparison
  (`hmac.compare_digest`). Missing/invalid key → `401 gateway_auth_failed`.
- **Startup guard:** `connect()` refuses to start the listener unless a strong
  `API_SERVER_KEY` is configured (rejects missing / placeholder / too-short
  secrets). Generate one with `openssl rand -hex 32`.

## Authentication

`API_SERVER_KEY` is resolved per request from the active profile's secret scope
(`_expected_api_key`, `gateway/platforms/api_server.py`). Store it wherever the
gateway reads secrets for that profile — e.g. `~/.hermes/.env` for the default
profile, `~/.hermes/profiles/<name>/.env` for a named profile.

| Endpoint | Without a key |
| --- | --- |
| `GET /health`, `GET /v1/health` | **200** — public, no auth |
| `GET /v1/models` and all other `/v1/*`, `/api/*` | **401** |

Two endpoints use their **own** authenticator, **not** `API_SERVER_KEY`:

- `POST /api/platforms/{platform}/events` — verified by the target platform
  adapter's own platform-signed bearer (external platforms hold no API key).
- `POST /api/cron/fire` — verified by a NAS-minted JWT (present only when the
  managed-cron feature is available).

## Endpoints

Registered by `_http_route_table()`. Every native route is also mirrored under
`/p/{profile}/…` (see [Multi-profile](#multi-profile-usage)).

**Health & discovery**
- `GET /health`, `GET /v1/health`, `GET /health/detailed`
- `GET /v1/models`, `GET /api/model/options`
- `GET /v1/capabilities`, `GET /v1/skills`, `GET /v1/toolsets`

**Chat / completions**
- `POST /v1/chat/completions` — OpenAI Chat Completions
- `POST /v1/responses`, `GET /v1/responses/{id}`, `DELETE /v1/responses/{id}`

**Sessions**
- `GET /api/sessions`, `POST /api/sessions`
- `GET|PATCH|DELETE /api/sessions/{id}`
- `GET /api/sessions/{id}/messages`
- `POST /api/sessions/{id}/fork`
- `POST /api/sessions/{id}/chat`, `POST /api/sessions/{id}/chat/stream`
- `POST /api/sessions/{id}/model` (per-session model lock)

**Async runs**
- `POST /v1/runs`, `GET /v1/runs/{id}`, `GET /v1/runs/{id}/events`
- `POST /v1/runs/{id}/approval`, `/steer`, `/stop`

**Jobs** — `GET|POST /api/jobs`, `GET|PATCH|DELETE /api/jobs/{id}`,
`POST /api/jobs/{id}/pause|resume|run`

## Basic usage

```bash
# Health is public
curl http://127.0.0.1:8643/health
# -> {"status": "ok", "platform": "hermes-agent", "version": "0.20.4"}

# Chat completions (authenticated)
curl -H "Authorization: Bearer $API_SERVER_KEY" \
     -H "Content-Type: application/json" \
     -d '{"model":"hermes-agent","messages":[{"role":"user","content":"你好"}]}' \
     http://127.0.0.1:8643/v1/chat/completions
```

Streaming: add `"stream": true` to the body (SSE response on the same route).

### Using an OpenAI SDK

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8643/v1", api_key="<API_SERVER_KEY>")
client.chat.completions.create(
    model="hermes-agent",
    messages=[{"role": "user", "content": "你好"}],
)
```

### Optional request headers

- `X-Hermes-Session-Id` — continue an existing session; history is loaded from
  `state.db` instead of the request body. **Requires** a configured API key
  (else `403`), to prevent unauthenticated history enumeration.
- `X-Hermes-Session-Key` — stable per-channel identifier scoping long-term
  memory (e.g. Honcho) across transcripts. Also requires a configured key.
- `Idempotency-Key` — request idempotency.

## Multi-profile usage

When `gateway.multiplex_profiles: true` (in `~/.hermes/config.yaml`), the same
listener serves multiple isolated profiles. **Select the profile by prefixing
any route with `/p/<profile>`:**

```bash
# Default profile (no prefix)
curl -H "Authorization: Bearer <default_key>" \
     http://127.0.0.1:8643/v1/chat/completions -d '...'

# Named profile (xcx)
curl -H "Authorization: Bearer <xcx_key>" \
     http://127.0.0.1:8643/p/xcx/v1/chat/completions -d '...'
```

The profile-prefix middleware (`_make_profile_prefix_middleware`):

1. Validates the prefix against `profiles_to_serve(...)`. Unknown/unconfigured
   profile → **404** `{"error": "Unknown or unconfigured profile"}`.
2. Enters that profile's runtime scope (`_profile_runtime_scope(get_profile_dir(profile))`),
   so credentials, config, tools, and memory are all scoped to the profile.

Under a prefix, `GET /p/<profile>/v1/models` advertises the **profile name** as
the model id (confirming the scope switch), not `hermes-agent`.

### Each named profile needs its OWN API_SERVER_KEY

Auth is **fail-closed and per-profile** (`_expected_api_key`):

- **Default profile** uses the key from `~/.hermes/.env`.
- **Named profiles do NOT inherit** the default listener's key. Each must define
  its own `API_SERVER_KEY` (≥16 chars) in `~/.hermes/profiles/<name>/.env`,
  otherwise every `/p/<name>/…` request returns **401**. This prevents anyone
  holding the default key from impersonating another profile.

### Enabling a profile for API access

```bash
# Generate a strong secret and add it to the target profile
KEY=$(openssl rand -hex 32)
echo "API_SERVER_KEY=$KEY" >> ~/.hermes/profiles/xcx/.env

# Reload so the profile credential is picked up
hermes gateway restart      # or: /platform resume api_server
```

Then call the profile with **its own** key:

```bash
curl -H "Authorization: Bearer $KEY" \
     -H "Content-Type: application/json" \
     -d '{"model":"hermes-agent","messages":[{"role":"user","content":"..."}]}' \
     http://127.0.0.1:8643/p/xcx/v1/chat/completions
```

The `model` field value is cosmetic; the profile prefix decides which
model/credentials/toolset actually run.

### Verified behavior (xcx)

Cross-tenant isolation observed live after configuring the xcx key:

| Request | Result | Meaning |
| --- | --- | --- |
| `/p/xcx/v1/models` + xcx key | 200 (`id: "xcx"`) | profile scope active |
| `/p/xcx/v1/models` + default key | 401 | keys are not shared across profiles |
| `/v1/models` + default key | 200 | default profile unaffected |
| `/p/nonexistent/v1/models` | 404 | unknown profile rejected |
| `/p/xcx/v1/chat/completions` + xcx key | 200 | real agent turn on xcx credentials |

## Operational notes

- The listener is a long-lived process. Adding/rotating an `API_SERVER_KEY`
  (default or per-profile) requires `hermes gateway restart` or
  `/platform resume api_server` to take effect.
- A rejected `API_SERVER_KEY` at startup is treated as a **non-retryable** fatal
  config error (it will not become valid on its own); fix the key, then resume.
- Never commit or echo real key values; store fingerprints/lengths only.
