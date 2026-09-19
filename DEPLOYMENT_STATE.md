# Production state — Dograh VM

What this branch is, and what the VM holds that git deliberately does not.
Written 2026-09-19 from `65.20.79.190`. Read this before rebuilding the host.

## What `production-baseline` is

A byte-for-byte snapshot of `/opt/dograh/dograh` as it was answering calls on
2026-09-19, verified by comparing git blob hashes against hashes computed on
the VM: **1446 tracked files, no content differences.**

It is an orphan commit. Production was built from a downloaded `dograh-main`
archive that matches no upstream commit (closest is `60a0dba3`, 5 of 8 sampled
files), so parenting it upstream would claim a lineage that does not exist.
Reconciling with upstream is a separate exercise.

The three fixes that previously existed only as patch scripts on the VM are in
this commit — see `801f0036` for the full reasoning on each:

| Fix | Status |
|---|---|
| `EndTaskReason.USER_QUALIFIED` → `END_CALL` | verified on a live call |
| Recorded openings for Q1–Q7 | verified — TTS fallbacks 11 → 2 |
| `ari:answered:` marker for answered-call reporting | **awaiting live verification** |

## What lives only on the VM

Three things are excluded on purpose. Losing the host loses them, so keep the
rescue bundle (below) somewhere durable.

| Path | Why excluded | How to rebuild |
|---|---|---|
| `.env` | every service credential | recreate from the variable list below |
| `certs/local.{crt,key}` | TLS private key | `./generate_certificate.sh` |
| `config/coturn/turnserver.conf` | holds a real `static-auth-secret` | render `deploy/templates/turnserver.remote.conf.template` with `TURN_SECRET` |

`.env` variable names (values never leave the VM):

```
ENABLE_ARI_MANAGER  ENABLE_CAMPAIGN_ORCHESTRATOR  ENABLE_COTURN
ENABLE_TELEMETRY  ENVIRONMENT  FASTAPI_WORKERS  FORCE_TURN_RELAY
MINIO_ROOT_PASSWORD  MINIO_ROOT_USER  OSS_JWT_SECRET  POSTGRES_PASSWORD
PUBLIC_BASE_URL  PUBLIC_HOST  REDIS_PASSWORD  SERVER_IP  TURN_SECRET
```

Asterisk is a **host systemd unit**, not a container. Its real trunk
credentials are in `/etc/asterisk/pjsip.conf` on the host, outside this repo
and outside the deploy directory. `deploy/asterisk/etc/` here carries only
non-secret config; `pjsip.conf` and `ari.conf` are gitignored.

## Runtime shape

```
compose   docker-compose.yaml + docker-compose.override.yaml
api       dograh-local/dograh-api:local   built on the box -- no commit lineage
ui        dograhai/dograh-ui:latest       registry -- no commit lineage
also      postgres (pgvector:pg17), redis:7, minio, coturn
```

Neither image records which commit it came from. That is the gap CI/CD closes:
after it lands, an immutable `<git-sha>` tag answers "what is running?".

`docker-compose.override.yaml` is committed and switches `api` to a local
build. It deliberately defines **no asterisk service** — a container on
`network_mode: host` would contend with the host Asterisk for UDP 5060 and drop
the Unpod registration.

## Database

Alembic is at **`c7a1e4f93b26`**, two revisions behind this branch's head
(`c7a1e4f93b26` → `d4b83a1f6c27` → `f3a1c47b9e02`). The graph is clean: one
root, one head, no dangling parents.

**Do not run `alembic upgrade head` against production as part of adopting this
branch.** `f3a1c47b9e02` migrates `pre_call_fetch_enabled` to a mode enum — a
data migration that deserves its own decision and its own verification call.

## Network and SIP — do not change

Unpod allowlists **one** address, `65.20.85.40`, held as a reserved IP by this
VM. `reserved-ip-src.service` (oneshot + timer) pins egress to it; when it
stalled, egress fell back to `65.20.79.190` and Unpod rejected every REGISTER
(tcpdump: 34 out, 0 in).

The `voicebot` VM claims the same address, so **only one box can hold SIP at a
time**. Starting one silently kills the other's telephony.

## Backups

Rescue bundle, taken before any change:
`C:\Users\sathw\dograh-rescue-backup\rescue-20260918-125024.tar.gz` — database
dump, `.env`, Asterisk config, the seven recordings, certs.

On the VM: `/opt/dograh/backups/` holds per-fix pre-patch copies
(`*.pre-endcall-fix`, `*.pre-allrec`, `*.pre-reporting-fix`) and previous deploy
trees at `/opt/dograh/dograh.old-<timestamp>`.

## Other branches

- `feature/phase-f-fastpath` — Phase F recorded openings, deterministic answer
  fast path, latency telemetry, STT tuning. Measured 3.443 s → 0.919 s on
  fast-path turns. **Not merged, not deployed.**
- `main` — upstream `dograh-hq/dograh` @ `59a4c9b4`, untouched.
