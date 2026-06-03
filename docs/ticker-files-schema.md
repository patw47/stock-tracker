# Ticker Files Schema

Location and structure of portfolio and watchlist files.

| Property | Portfolio | Watchlist |
|----------|-----------|-----------|
| **Relative path** | `portfolio.json` | `watchlist.json` |
| **Top-level key** | `tickers` (array) | `tickers` (array) |
| **Entry format** | Object | Object |
| **Entry fields** | `symbol`, `name`, `sector` | `symbol`, `name`, `sector` |
| **Example entry** | `{ "symbol": "BBAI", "name": "BigBear.ai", "sector": "IA défense" }` | `{ "symbol": "SMR", "name": "NuScale Power", "sector": "Nucléaire SMR" }` |
| **Entry count** | 8 tickers | 8 tickers |
| **Additional fields** | `updated_at` (metadata) | `updated_at` (metadata) |

## Usage in n8n Code node

Extract symbols from both files:

```javascript
// Read both files
const portfolio = JSON.parse(readFileSync('./portfolio.json', 'utf8'));
const watchlist = JSON.parse(readFileSync('./watchlist.json', 'utf8'));

// Extract ticker symbols
const tickers = [
  ...portfolio.tickers.map(t => t.symbol),
  ...watchlist.tickers.map(t => t.symbol)
];
```

Result: array of 16 strings (8 portfolio + 8 watchlist).

## n8n node update

Node `Read Tickers` (`node-read-watchlist`) updated to read from files at runtime.
Absolute host paths: `/opt/apps/stock-tracker/portfolio.json` and `/opt/apps/stock-tracker/watchlist.json`.
Output shape unchanged: `{ symbol, sector, status }` per item (status derived from source file).
