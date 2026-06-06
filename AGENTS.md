# Project Rules for Codex

## Architecture

This repository contains an existing Warren/news pipeline and a new EOD anomaly detection layer.

The Warren/news pipeline is the context layer.
The EOD anomaly layer is the trigger layer.

They are complementary and must not replace each other.

## Hard rules

- Do not break the existing Warren/news workflow.
- No LLM calls in the anomaly detection critical path.
- Warren is called only after anomaly filtering, beta gate and dedup.
- MacroSnapshot must be calculated once per run and reused.
- Missing or ambiguous ticker data must be flagged, never silently accepted.
- Technical analysis must use the `market_intelligence` S0 foundation, not the legacy n8n ticker list.
- Implement only one sprint per Codex run.
- Add or update tests for every sprint.


## Testing

Run:

```bash
pytest -q
Do not add live API calls to unit tests.
Mock yfinance, Twelve Data and external services.

---

## 5. `.codex/config.toml` + subagents

### `.codex/config.toml`

```toml
[agents]
max_threads = 4
max_depth = 1
job_max_runtime_seconds = 1800