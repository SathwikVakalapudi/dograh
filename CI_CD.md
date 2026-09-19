# CI/CD

`production-baseline` is the production source of truth. `main` tracks upstream
Dograh and is not deployed.

```
push / PR ──▶ CI ──▶ image ghcr.io/<owner>/dograh-api:<sha>
                          │
                          └─▶ CD (manual + approval) ──▶ VM ──▶ health ──▶ done
                                                                  │
                                                                  └─ fail ─▶ rollback
```

## CI — `.github/workflows/ci.yml`

Runs on pull requests and pushes to `production-baseline`.

| Job | What it proves |
|---|---|
| **Tests** | 1297 tests against real pgvector + redis. Node 22 is installed, which matters: two SDK tests skip silently without it |
| **Migrations** | exactly one alembic head, and a fresh database upgrades to it |
| **Docker image** | builds, bakes `GIT_SHA`, and **verifies the image reports that sha** |

The image job needs both others, so nothing publishes from a red build. Pull
requests build but do not publish — a fork PR only gets a read-only token.

Images are tagged `:<full-sha>` (what CD deploys) and `:baseline` (a
convenience pointer, never deployed from).

## CD — `.github/workflows/cd.yml`

**Manual only.** Actions ▸ CD ▸ Run workflow ▸ paste the 40-character commit.

Two gates before anything is touched:

1. **verify** — the image must already exist in GHCR and report that commit. A
   reviewer is never asked to approve a deploy that cannot work.
2. **production environment** — the job then pauses for a required reviewer.

### What the deploy does, in order

`deploy/cd/deploy.sh`, run on the VM:

1. **Live-call check.** Recreating the api container tears down its websocket to
   Asterisk and drops calls in progress. Refuses to deploy while any call is up
   unless `--force`. Asterisk is asked directly; `/health/active-calls` would
   need `DOGRAH_DEVOPS_SECRET`, which this host does not set.
2. **Record the rollback point** — the currently running image reference.
3. **`pg_dumpall`** to `/opt/dograh/backups/pre-deploy-<ts>.sql.gz`. The
   entrypoint runs `alembic upgrade head`, and migrations are forward-only.
4. **Pull first.** A pull failure leaves the running container untouched.
5. **Recreate only `api`** — `up -d --no-deps api`.
6. **Health check** — `/api/v1/health` must return 200 **and** report the
   deployed `build_sha`, within 180s. Checking only for 200 would let a failed
   rollback look green, because the old container answers 200 perfectly well.
7. **Roll back automatically** if that fails, then re-verify the rolled-back
   commit. If the rollback is also unhealthy it stops and says so rather than
   leaving you guessing.
8. **Post-deploy**: container states, SIP registration count, alembic revision.

### Only `api` is replaced

postgres, redis, minio, coturn, nginx and ui keep running. The database, Redis
and the recordings live in volumes on those containers, and this is a phone
system — restarting them costs live calls for nothing.

The script never runs `docker compose down`, never passes `-v`, never prunes
images (rollback needs the previous one), and never writes to `.env`, `certs/`,
`config/coturn/` or `/etc/asterisk`.

### Why a separate compose file

Production runs `docker-compose.yaml` + `docker-compose.override.yaml`, and
that override pins a locally-built image with `build:` and
`pull_policy: never` — both wrong for deploying a registry image. The deploy
writes `docker-compose.deploy.yaml` and passes compose files explicitly, which
also stops `override.yaml` being auto-loaded. `ARQ_WORKERS` is carried across
because that override is the only place it is set.

## Rollback

Automatic on a failed health check. Manually:

```bash
ssh root@<host> /opt/dograh/deploy.sh rollback
```

Or deploy an earlier commit through the normal CD flow — every build stays in
GHCR under its own sha.

**What is running right now:**

```bash
curl -s http://<host>:8000/api/v1/health | python3 -m json.tool
# build_sha is the exact commit
cat /opt/dograh/deploy-state/current_sha
```

## Required GitHub secrets

Settings ▸ Secrets and variables ▸ Actions.

| Secret | What | Notes |
|---|---|---|
| `DOGRAH_HOST` | VM address | |
| `DOGRAH_USER` | ssh user | |
| `DOGRAH_SSH_KEY` | **private** deploy key | full PEM including header/footer |
| `DOGRAH_KNOWN_HOSTS` | `ssh-keyscan -H <host>` output | pins host identity; without it the job would trust whatever answers on that address |
| `GHCR_PULL_TOKEN` | PAT, **`read:packages` only** | lets the VM pull; revoked with `docker logout` after each deploy |

No registry secrets are needed for CI — it publishes with the built-in
`GITHUB_TOKEN`.

Secrets are written to files, never echoed and never passed as command
arguments. GitHub also masks them in logs, but that only helps if they reach
the log in the first place.

## One-time setup

**GitHub**

1. Settings ▸ Environments ▸ **New environment** ▸ `production`
2. Tick **Required reviewers**, add yourself. *This is the approval gate; without
   it CD deploys as soon as it is dispatched.*
3. Add the five secrets above.
4. Optional: Settings ▸ Branches ▸ set `production-baseline` as default so PRs
   target it.

**VM**

1. Create a deploy key and authorise it:
   ```bash
   ssh-keygen -t ed25519 -f dograh_deploy -C "github-actions-cd"
   ssh-copy-id -i dograh_deploy.pub root@<host>     # public half
   ```
   Put the **private** half in `DOGRAH_SSH_KEY`.
2. `ssh-keyscan -H <host>` → `DOGRAH_KNOWN_HOSTS`.
3. Nothing else. The script creates `/opt/dograh/deploy-state` itself and leaves
   `.env`, `certs/` and the Asterisk configuration alone.

## Things this deliberately does not do

- **No automatic deploy on merge.** Publishing an image is automatic; putting it
  on the phone system is not.
- **No Kubernetes, no new infrastructure.** Same Docker Compose stack.
- **No SIP or network changes.** Unpod allowlists one address, `65.20.85.40`,
  held as a reserved IP by this VM. Nothing here touches SIP credentials,
  Asterisk config, the reserved IP, or the other `voicebot` VM.
- **No phone calls.**
- **No UI deploys.** `ui` still runs `dograhai/dograh-ui:latest` and has no
  commit lineage. Worth fixing, but it is a separate change.

## Known gaps

- **The first deploy switches api from a locally-built image to a registry
  one.** Behaviour should be identical — the baseline builds the same source —
  but it is a container replacement and deserves a quiet window.
- **Migrations are not gated.** The entrypoint runs `alembic upgrade head`
  unconditionally. Today the baseline head equals the production revision
  (`c7a1e4f93b26`), so a deploy is migration-neutral; a future migration will
  apply on deploy, which is why step 3 takes the dump.
- **`ui` is unversioned** (above).
