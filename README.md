# 📈 Stock Tracker — Agent de veille boursière quotidienne

Agent automatisé de veille boursière basé sur **n8n** + **Claude AI** (Anthropic).  
Envoie chaque matin un briefing complet par **Telegram** et **Gmail** pour chaque ticker surveillé.

## Fonctionnalités

- **Analyse différenciée** : prompts adaptés selon que le titre est en portefeuille ou en watchlist
- **Recherche web obligatoire** : Claude utilise `web_search` avant toute décision (SKIP uniquement après recherche)
- **Règle SKIP** : ignore les tickers sans résultats pertinents après recherche web
- **Telegram** : un message par ticker actif (découpage automatique si > 4 000 caractères)
- **Gmail** : un email récapitulatif avec section Portefeuille et section Watchlist
- **Démarrage automatique** : n8n géré par PM2, redémarre automatiquement au boot WSL

## Tickers suivis

**Portefeuille** : BBAI · HIMS · HYLN · MMED · PRCH · RGTI · VUZI · XYL

**Watchlist** : SMR · ALTD · OKLO · BLNK · GRO · STIM · PLX

## Structure

```
stock-tracker/
├── workflow.json          # Workflow n8n (import direct)
├── ecosystem.config.js    # Configuration PM2 (port, timezone, data folder)
├── start.sh               # Script de démarrage manuel
├── portfolio.json         # Actions détenues
├── watchlist.json         # Actions surveillées
├── .env                   # Variables d'environnement (non versionné)
├── .env.example           # Template des variables requises
└── .n8n/                  # Base de données n8n (non versionnée)
```

## Installation

### Prérequis

- WSL2 avec systemd activé (`/etc/wsl.conf` : `systemd=true`)
- Node.js 18+ via [nvm](https://github.com/nvm-sh/nvm)
- n8n et PM2 installés globalement :
  ```bash
  npm install -g n8n pm2
  ```

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

3. Démarrer n8n via PM2 :
   ```bash
   pm2 start ecosystem.config.js
   pm2 save
   ```

4. Activer le démarrage automatique au boot WSL (une seule fois, nécessite sudo) :
   ```bash
   sudo env PATH=$PATH:/home/<user>/.nvm/versions/node/<version>/bin \
     pm2 startup systemd -u <user> --hp /home/<user>
   ```

5. Ouvrir l'interface n8n : `http://localhost:5680`  
   Login : `admin / stockwatcher2026`

6. Importer le workflow :
   ```bash
   N8N_USER_FOLDER=/home/<user>/stock-tracker \
     n8n import:workflow --input=workflow.json
   ```

7. Configurer les credentials dans l'interface n8n :
   - **Anthropic API Key** : HTTP Header Auth (`x-api-key`)
   - **Gmail SMTP** : host `smtp.gmail.com`, port `465`, SSL
   - **Telegram Bot API** : token du bot

8. Activer le workflow dans l'interface (toggle en haut à droite).

## Workflow n8n

```
Schedule (10h Paris) → Lire tickers → Préparer requête Claude → Claude API (web_search)
                                                                        ↓
                                                               Extraire briefing
                                                               ├── Agréger Email → Gmail
                                                               └── Agréger Telegram → Découper → Telegram
```

Durée d'exécution estimée : **4 à 6 minutes** (15 tickers × ~20 s par appel Claude avec web_search).

## Variables PM2 (`ecosystem.config.js`)

| Variable | Valeur |
|---|---|
| `N8N_PORT` | `5680` |
| `N8N_USER_FOLDER` | `/home/<user>/stock-tracker` |
| `N8N_BASIC_AUTH_USER` | `admin` |
| `GENERIC_TIMEZONE` | `Europe/Paris` |

## Variables d'environnement (`.env`)

| Variable | Description |
|---|---|
| `ANTHROPIC_API_KEY` | Clé API Anthropic |
| `GMAIL_USER` | Adresse Gmail expéditrice |
| `GMAIL_PASS` | Mot de passe d'application Gmail |
| `TELEGRAM_TOKEN` | Token du bot Telegram |
| `TELEGRAM_CHAT_ID` | ID du chat Telegram destinataire |

## Modèle utilisé

`claude-sonnet-4-5` avec l'outil `web_search_20250305` (beta Anthropic) — max 5 recherches par ticker, 2 048 tokens de réponse.
