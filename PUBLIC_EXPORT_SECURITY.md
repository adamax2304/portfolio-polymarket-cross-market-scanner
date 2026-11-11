# Public export security report

Source: `C:\Users\adama\Documents\_SKLAD_proektov\polymarket_cross_market_scanner`
Status: research / paper
Files copied: 122
Files omitted: 9

Excluded runtime data includes secrets, environment files, credentials, session files, databases, logs, caches, archives, generated artifacts, and files above 20 MB.

## Omitted paths

- `.env | policy-excluded`
- `.env.example | policy-excluded`
- `main.py | OpenAI-or-OpenRouter-key`
- `PROJECT_LOGIC.md | OpenAI-or-OpenRouter-key`
- `start_bots.ps1 | OpenAI-or-OpenRouter-key`
- `polymarket_cross_scanner\cross_live_executor.py | provider-secret`
- `_archived\dead_arb_bots\cross_live_executor.py | provider-secret`
- `_archived\dead_arb_bots\strict_fok\executor.py | provider-secret`
- `_archived\dead_arb_bots\maker_first\executor.py | provider-secret`