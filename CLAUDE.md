# Claude Code instructions for stock-tracker

## 🤖 Instructions pour les agents dispatchés

Si le fichier `.sprint.md` existe dans le répertoire courant, **le lire en premier**
avant toute action. Il contient le périmètre exact du Sprint à implémenter, les
dépendances, et le hors-scope. Utiliser les subagents `code-mapper`, `tester`, `reviewer`
avant de modifier des fichiers.


## Project context

This repository contains:
- an existing Warren/news pipeline
- a new EOD anomaly detection layer under `market_intelligence/`

The Warren/news pipeline is the context layer.
The EOD anomaly layer is the trigger layer.

## Hard rules

- Do not break the existing Warren/news workflow.
- Do not rewrite Sprint 0 unless explicitly requested.
- S0 is already implemented and must be consumed by later sprints.
- No LLM calls in the anomaly detection critical path.
- Warren is called only after anomaly filtering, beta gate and dedup.
- Technical analysis must use `market_intelligence`, not the legacy n8n ticker list.
- Implement only one sprint per run.
- Add or update tests for every sprint.

## Test command

```bash
pytest -q