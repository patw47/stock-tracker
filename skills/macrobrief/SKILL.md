# SKILL — Macro Brief quotidien

**Marker** : `[MACRO-BRIEF SKILL]`
**Endpoint** : `POST /macro-brief`
**Déclencheur n8n** : Macro Brief Schedule 16h Mon-Fri (Europe/Paris)

## Objectif

Produire chaque jour ouvré à 16h Paris un Market Context Brief indépendant des news tickers.
Le brief est envoyé même si aucun ticker n'a de news.

## Sources de données

| Type | Source | Contenu |
|------|---------|---------|
| Quantitatif | FRED (`get_snapshot()`) | Fed Funds, taux 10Y/2Y, VIX, dollar index, S&P 500 |
| Quantitatif | yfinance (`get_market_closes()`) | IWM close + var. j/j, Crude Oil WTI close + var. j/j |
| Qualitatif | Haiku web_search (`fetch_macro_snapshot()`) | Fed stance, géopolitique, attentes taux, IPOs, secteurs chauds, Fear & Greed, rumeurs |

## Format de sortie (règles absolues)

- Prose française fluide — aucune liste à puces, aucun tableau, aucun titre
- Jamais de heading `#` ni bold `**header**`
- Maximum 5 valeurs numériques dans le brief entier
- 150–300 mots
- Signal-first : ouvrir avec le régime dominant (risk-on / risk-off / neutre)
- Rumeurs et attentes **toujours étiquetées** comme telles
- Conclusion : une phrase régime final

## Contenu obligatoire

Fed stance · taux prévus · dollar · pétrole · VIX · IWM / appétit small caps ·
IPOs notables · secteurs chauds · géopolitique · Fear & Greed · rumeurs notables

Si une rubrique est indisponible, le brief mentionne l'indisponibilité en prose.

## Dégradation douce

- Web search échoue → brief quantitatif FRED seul + mention d'incertitude qualitative
- Toutes les sources FRED échouent → brief avec données de fallback + avertissement
- Jamais de brief vide, jamais de crash

## Payload retourné

```json
{"brief": "Texte du brief en français..."}
```
