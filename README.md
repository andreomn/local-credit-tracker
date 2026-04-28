# Local Credit Tracker

Web app to track Brazilian local credit instruments (debentures, trades, CVM filings).

## Features

- Debenture price & yield tracking (ANBIMA)
- B3 trade data (last 10 days)
- CVM filings tracking
- CSV export
- Interactive charts

## Deploy

```bash
gcloud builds submit --tag gcr.io/debenture-tracker/local-credit-tracker

gcloud run deploy local-credit-tracker \
  --image gcr.io/debenture-tracker/local-credit-tracker \
  --region southamerica-east1 \
  --allow-unauthenticated
