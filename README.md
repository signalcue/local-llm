# Local LLM Gateway

Standalone Docker project that exposes an Anthropic-compatible `/v1/messages` endpoint.

- Returns local Ollama response immediately.
- Sends a best-effort parallel Claude request (optional).
- Logs full prompt + local output + Claude output/failure to Redis.
- Designed for LAN access (`0.0.0.0:8080`) in phase 1.
- Defaults to `qwq:32b` with forced JSON output and low-temperature inference.

## Quick Start

1. Start Ollama natively on host:
   - `ollama serve`
   - `ollama pull qwq:32b`
2. Copy env file:
   - `cp .env.example .env`
3. Start stack:
   - `docker compose up -d --build`
4. Smoke test:
   - `curl -X POST http://localhost:8080/v1/messages -H "content-type: application/json" -d '{"model":"claude-sonnet-4-5-20250929","messages":[{"role":"user","content":"Say hello"}]}'`

## Runtime Defaults

- `MODEL_NAME=qwq:32b`
- `OLLAMA_TEMPERATURE=0.1`
- `OLLAMA_NUM_CTX=8192`
- `OLLAMA_NUM_PREDICT=1024`

Reasoning models can emit `<think>...</think>` blocks. The adapter strips those before returning content so downstream JSON parsers receive clean output.

## Redis Comparison Log

- List key contents:
  - `docker compose exec redis redis-cli LRANGE comparisons 0 -1`
- Each entry contains:
   - `env_name`
   - `analysis_run_id`
  - `timestamp`
  - `prompt` (full)
  - `local_response` (full)
  - `claude_response` (full when available)
  - `claude_error` (if Claude call fails)

Optional request metadata supported for logging:
- JSON body `metadata.analysis_run_id`
- Header `x-analysis-run-id`

## Evaluation Workflow

- Export current comparison logs to JSONL:
   - `./scripts/export-comparisons.sh`
- Extract unique prompts into an eval-ready prompt set:
   - `python3 ./scripts/extract-prompts.py ./evals/comparisons-YYYYMMDD-HHMMSS.jsonl`
- The extracted prompt file can be expanded into the 100-prompt realistic evaluation batch.
- `evals/current-*.jsonl` files are local/generated working artifacts and are intentionally ignored.

## Redis Durability

- Redis uses both AOF (`appendonly yes`) and periodic snapshots.
- Data is persisted to the Docker named volume `redis-data`.
- Safe across reboot/container restart as long as the volume is kept.
- Data loss risk remains if the volume is removed (`docker compose down -v` or `docker volume prune`).
