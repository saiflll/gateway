# nyxgtw

nyxgtw is an AI gateway stack that deliberately separates two routing responsibilities:

```text
OpenAI-compatible client
        ↓ :4000/v1
LiteLLM (logical-model routing, fallback, cooldown, one retry)
        ↓ http://9router:20128/v1
9Router (provider translation, OAuth and account rotation)
        ├─→ Headroom (:8787, internal context compression)
        └─→ provider accounts
```

The first phase uses the upstream `decolua/9router:latest` image unchanged. The controller is an explicit integration point for paid-model failure reports; automatically intercepting 9Router's execution path and stopping account fallback requires a later, reviewed upstream/custom-image patch.

## Quick start

Prerequisites are Docker Engine with the Compose v2 plugin, outbound access to the three image registries, and provider/OAuth accounts that you configure in the 9Router dashboard. **The Compose stack does not create provider accounts for you.**

```bash
cp .env.example .env
nano .env                    # replace every placeholder
docker compose pull
docker compose up -d --build
docker compose ps
docker compose logs -f
```

Or use the guarded helper, which rejects unchanged placeholder secrets and validates Compose before starting:

```bash
./scripts/nyxgtw start
./scripts/nyxgtw check
./scripts/nyxgtw logs
```

Use `http://SERVER:4000/v1` as the public OpenAI-compatible API and supply `LITELLM_MASTER_KEY` as its bearer token. Available logical model names are `free`, `coder`, `smart`, `fast`, and `ultra`. The concrete 9Router model aliases in `litellm-config.yaml` are initial examples; adjust them to models enabled by your configured provider accounts.

After startup, open the locally bound 9Router dashboard through an SSH tunnel when deploying remotely:

```bash
ssh -L 20128:127.0.0.1:20128 user@SERVER
```

Then visit `http://127.0.0.1:20128`, add/login provider accounts, and verify that `/v1/models` exposes the concrete IDs used in `litellm-config.yaml`. A first API smoke test is:

```bash
curl http://SERVER:4000/v1/chat/completions \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"model":"coder","messages":[{"role":"user","content":"Reply only: nyxgtw-ok"}]}'
```

## What is complete and what remains

Ready now: Compose networking, persistent 9Router data, LiteLLM logical routing, local-only administration ports, controller TTL persistence, full-volume backup/restore, and internal dependency checks.

Still required from the operator:

1. replace all placeholder secrets in `.env`;
2. configure provider/OAuth accounts in 9Router;
3. align concrete model IDs in `litellm-config.yaml` with `/v1/models`;
4. put TLS/reverse proxy and firewall/rate limiting in front of public port 4000;
5. run a real completion and inspect 9Router logs to prove Headroom compression with the selected model/provider.

Still intentionally deferred in code: automatic interception of an upstream paid-model 402 inside 9Router, stopping account rotation immediately, and filtering disabled models in direct/combo execution paths. In this phase `/report-failure` works, but an operator or external error hook must call it. Therefore the full paid-model flow is **not automatic yet**; it requires a reviewed custom 9Router build or an accepted upstream patch.

## Ports and exposure

| Service | Container | Host | Purpose |
|---|---:|---:|---|
| LiteLLM | 4000 | `0.0.0.0:4000` | public API (`/v1`) |
| 9Router | 20128 | `127.0.0.1:20128` | local dashboard/debug; containers use `http://9router:20128/v1` |
| Headroom | 8787 | not published | internal compression; 9Router uses `http://headroom:8787` |
| model-controller | 4010 | `127.0.0.1:4010` | health and model TTL control |

Keep port 4000 behind a firewall or TLS reverse proxy in production. Change all four values in `.env`; never commit that file. 9Router provider credentials and OAuth accounts remain managed by 9Router and persist in the `9router-data` volume.

## Paid-model controller

The controller classifies a failure only when HTTP status 402 is paired with a payment/credit semantic signature. It then calls 9Router's existing disabled-model API and stores only the expiry registry in SQLite. It does **not** treat arbitrary 402 responses as model failures. A background sweep runs every minute; an authenticated manual sweep is also available.

```bash
curl -X POST http://127.0.0.1:4010/report-failure \
  -H "X-Controller-Token: $CONTROLLER_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"provider_alias":"kilocode","model_id":"google/gemini-2.5-pro","status":402,"error":"Paid Model - Credits Required","ttl_seconds":21600}'

curl -X POST http://127.0.0.1:4010/sweep \
  -H "X-Controller-Token: $CONTROLLER_TOKEN"
curl http://127.0.0.1:4010/health
```

`/report-failure` and `/sweep` require `X-Controller-Token`; `/health` is intentionally unauthenticated for health checks. The reporting call must be made by an error hook or operator in phase one. End-to-end interception, direct-request disabled checks, combo filtering, and immediate cessation of account fallback belong in the later 9Router source patch—not as runtime modifications to the upstream image.

## Operations

Validate configuration before launch:

```bash
docker compose config
python -m compileall controller
```

Headroom is a fail-open 9Router feature: 9Router waits for it at initial startup, while normal 9Router behavior allows AI calls to proceed when compression later becomes unavailable. Compose restarts unhealthy services; no source-level circuit breaker is injected in this phase.

Check the actual internal routes from the controller container (not from the host):

```bash
curl http://127.0.0.1:4010/health/dependencies \
  -H "X-Controller-Token: $CONTROLLER_TOKEN"
```

This checks that the controller can reach both 9Router's model API and Headroom's health API over the Compose network. It complements container restart healthchecks; it does not claim that a provider account is configured. Confirm Headroom compression in 9Router request logs during an actual completion.

## Persistent backup, restore, and controller registry transfer

9Router's complete `/app/data` directory and the controller SQLite database live in named volumes. The helper performs a **consistent offline snapshot**: it stops all data writers, archives both volumes, and starts services again. This includes 9Router-managed accounts, configuration, and any database files stored beneath `/app/data`.

```bash
./scripts/nyxgtw-data export backups/nyxgtw-data.tar.gz
./scripts/nyxgtw-data import backups/nyxgtw-data.tar.gz
```

Import replaces current persistent data, so take a fresh export first. Archive files can contain provider credentials; encrypt them and never commit them. The helper requires Docker and downloads `alpine:3.22` when it is not present locally.

For a portable JSON transfer of only the controller TTL registry (not 9Router accounts), use:

```bash
curl http://127.0.0.1:4010/registry/export \
  -H "X-Controller-Token: $CONTROLLER_TOKEN" > disabled-models.json

curl -X POST http://127.0.0.1:4010/registry/import \
  -H "X-Controller-Token: $CONTROLLER_TOKEN" \
  -H 'Content-Type: application/json' \
  --data-binary @disabled-models.json
```

The JSON import restores TTL bookkeeping. A full volume restore is the correct operation when 9Router's own disabled-model state must be restored too.

## GitHub authentication

Do not put a GitHub token in `.env`: Compose passes that file to application services and it is unrelated to deployment. Authenticate GitHub CLI on the host with either `gh auth login` (recommended interactive setup) or a short-lived environment variable such as `GH_TOKEN`; a Codex plugin is not required. Use the minimum repository permissions needed to push a branch and open a pull request.
