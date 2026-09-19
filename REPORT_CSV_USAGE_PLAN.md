# Plan: Add LLM-tokens + TTS-characters columns to the report CSV

> **Execute ONLY after the running campaign finishes** — the API rebuild restarts the
> container and drops live calls. Confirm `GET /api/v1/health/active-calls` = 0 first.

## Context (why)
Goal: surface per-call usage in the exported report CSV.

Investigation confirmed the data is **already captured per call** on `workflow_runs.usage_info`
(a JSON column):
- **LLM tokens** — per `processor|||model`, with prompt / completion / total / cache counts.
- **TTS characters** — integer total per `processor|||model`.
- **STT seconds** — **NOT captured** (no STT usage metric exists anywhere in pipecat; capturing it
  would require forking the framework). **Deferred** — this plan ships **LLM tokens + TTS characters only**.

The report builder already SELECTS `usage_info` and already reads `call_duration_seconds` from it, so
this is a **single-file, presentation-only change** — no DB/query/pipeline change. It also works
**retroactively on already-completed runs** (their `usage_info` already exists; no calls need re-running).

## The change (one file)
`api/services/reports/run_report.py` → `build_run_report_csv` — the single source of the CSV column
shape. All three report endpoints delegate to it, so they all gain the columns:
- Workflow report:   `GET /{workflow_id}/report`   — `api/routes/workflow.py:1489`
- Campaign report:   `GET /{campaign_id}/report`   — `api/routes/campaign.py:995`
- Org-usage report:  `GET /usage/runs/report`      — `api/routes/organization_usage.py:442`

Steps:
1. Add two headers — **"LLM Tokens"** and **"TTS Characters"** — right after the existing
   "Call Duration (s)" in `pre_headers` (`run_report.py:36-45`).
2. Per row, `usage = run.usage_info` is already read (`run_report.py:56`). Compute:
   - **LLM Tokens** = sum of `total_tokens` across `usage.get("llm", {})` entries.
   - **TTS Characters** = sum of `usage.get("tts", {}).values()`.
   Write both in the matching row positions (`run_report.py:73-84` region).
3. Add two tiny local sum helpers in `run_report.py` (keeps the reports module self-contained).
   Ready-made equivalents exist at `api/services/integrations/tuner/cost.py` (`_sum_llm_tokens`,
   `_sum_tts_characters`) — mirror them rather than importing an integration's private helpers.
4. Guard for `usage_info` being `None`/empty (older/failed runs) → emit blank/0.

## Deploy (after the campaign)
```
./remote_up.sh --build -- api
```
Code build/restart only — no workflow runs, zero provider credits. The restart drops any in-flight
calls, so run it only once the campaign has completed.

## Verification
1. Download a campaign report CSV (`GET /api/v1/campaign/{id}/report`) → confirm **"LLM Tokens"** and
   **"TTS Characters"** columns appear, populated (non-zero) for completed runs, blank/0 where
   `usage_info` is empty.
2. Cross-check one run: `select usage_info from workflow_runs where id=<run>;` → CSV sums match the JSON
   (LLM `total_tokens`; TTS char total).
3. Existing columns + the dynamic extracted-variable columns are unchanged/still correct.

## Deferred / future
- **STT column** (deferred by decision): would need net-new STT-usage machinery in the pipecat fork —
  a new `STTUsageMetricsData`, a collector/passthrough, and audio-second emission in the base
  `STTService`, then an aggregator branch. Revisit after comparing against Sarvam's own dashboard.
- **Per-call cost ($)** column: reuse `compute_call_cost_cents` (`tuner/cost.py:72-131`) once provider
  rates are configured.
- No pipeline / turn-detection change.
