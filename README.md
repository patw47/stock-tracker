# 📈 Stock Tracker — Agent de veille boursière quotidienne

Agent automatisé de veille boursière basé sur **n8n** + **Claude AI** (Anthropic).  
Envoie chaque matin un briefing complet par **Telegram** et **Gmail** pour chaque ticker surveillé.

## Fonctionnalités

- **Analyse différenciée** : prompts adaptés selon que le titre est en portefeuille ou en watchlist
- **Recherche web en temps réel** : Claude utilise `web_search` pour trouver les actualités du jour
- **Règle SKIP** : ignore les tickers sans news récentes (< 7 jours) pour éviter le bruit
- **Telegram** : un message complet par ticker actif (découpage automatique si > 4000 caractères)
- **Gmail** : un email récapitulatif avec résumé, section Portefeuille et section Watchlist

## Structure

```
stock-tracker/
├── workflow.json       # Workflow n8n (import direct)
├── portfolio.json      # Actions détenues
├── watchlist.json      # Actions surveillées
├── start.sh            # Script de démarrage n8n
├── .env                # Variables d'environnement (non versionné)
└── .env.example        # Template des variables requises
```

## Tickers suivis

**Portefeuille** : BBAI · HIMS · HYLN · MMED · PRCH · RGTI · VUZI · XYL

**Watchlist** : SMR · ALTD · OKLO · BLNK · GRO · STIM · PLX

## Installation

### Prérequis
- Node.js 18+ (installé via [nvm](https://github.com/nvm-sh/nvm))
- n8n installé globalement : `npm install -g n8n`

### Configuration

1. Copier le fichier d'environnement :
   ```bash
   cp .env.example .env
   ```

2. Remplir les variables dans `.env` :
   ```
   ANTHROPIC_API_KEY=sk-ant-...
   GMAIL_USER=votre@gmail.com
   GMAIL_PASS=xxxx xxxx xxxx xxxx   # App password Gmail
   TELEGRAM_TOKEN=123456:ABC...
   TELEGRAM_CHAT_ID=1234567890
   ```

3. Lancer n8n :
   ```bash
   ./start.sh
   ```

4. Ouvrir l'interface n8n : `http://localhost:5680`

5. Importer le workflow :
   ```bash
   n8n import:workflow --input=workflow.json
   ```

6. Configurer les credentials dans l'interface n8n :
   - **Anthropic API Key** : HTTP Header Auth (`x-api-key`)
   - **Gmail SMTP** : host `smtp.gmail.com`, port `465`, SSL
   - **Telegram Bot API** : token du bot

7. Activer le workflow dans l'interface.

## Workflow n8n

```
Schedule (14h) → Lire tickers → Préparer requête Claude → Claude API
                                                                ↓
                                                        Extraire briefing
                                                        ├── Agréger pour Email → Gmail
                                                        └── Agréger pour Telegram → Découper → Telegram
```

## Variables d'environnement

| Variable | Description |
|---|---|
| `ANTHROPIC_API_KEY` | Clé API Anthropic |
| `GMAIL_USER` | Adresse Gmail expéditrice |
| `GMAIL_PASS` | Mot de passe d'application Gmail |
| `TELEGRAM_TOKEN` | Token du bot Telegram |
| `TELEGRAM_CHAT_ID` | ID du chat Telegram destinataire |

## Modèle utilisé

`claude-sonnet-4-5` avec l'outil `web_search_20250305` (beta Anthropic).
