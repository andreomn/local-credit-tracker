# Backend jobs map

This repository contains three backend Cloud Run Jobs under [`jobs/`](../jobs/). The table below maps each job's GitHub path to its Cloud Run Job name, purpose, outputs, and runtime configuration.

## Summary

| Job | GitHub path | Cloud Run Job name |
| --- | --- | --- |
| ANBIMA to GCS | `jobs/anbima-to-gcs/anbima-to-gcs` | `anbima-to-gcs` |
| B3 reference rates to GCS | `jobs/b3-predi-to-gcs/b3-predi-to-gcs` | `b3-predi-to-gcs` |
| B3 trades to GCS | `jobs/b3-trades-to-gcs` | `b3-trades-to-gcs` |

## ANBIMA to GCS

- **GitHub path:** `jobs/anbima-to-gcs/anbima-to-gcs`
- **Cloud Run Job name:** `anbima-to-gcs`
- **What it does:**
  - Downloads the latest ANBIMA secondary-market debenture spreadsheet available in the recent lookback window.
  - Uploads the raw `.xls` file to Google Cloud Storage.
  - Downloads ANBIMA CRI/CRA CSV data, converts it into a normalized `.xlsx`, and uploads it to Google Cloud Storage.
  - Rebuilds the consolidated ANBIMA history JSON from stored debenture and CRI/CRA files.
- **Main output files:**
  - `gs://<GCS_BUCKET_NAME>/<GCS_PREFIX>dYYmmmDD.xls`
  - `gs://<GCS_BUCKET_NAME>/<CRICRA_GCS_PREFIX>dYYmmmDD.xlsx`
  - `gs://<GCS_BUCKET_NAME>/<GCS_PREFIX>historico-anbima.json`
- **Required environment variables:**
  - `GCS_BUCKET_NAME` - target Google Cloud Storage bucket.
  - `GCS_PREFIX` - destination prefix for ANBIMA debenture `.xls` files and `historico-anbima.json`.
  - `CRICRA_GCS_PREFIX` - destination prefix for normalized CRI/CRA `.xlsx` files.

## B3 reference rates to GCS

- **GitHub path:** `jobs/b3-predi-to-gcs/b3-predi-to-gcs`
- **Cloud Run Job name:** `b3-predi-to-gcs`
- **What it does:**
  - Uses Playwright to open the B3 reference rates page.
  - Downloads missing CSV files for the default `DI x pré` product.
  - Selects `Real x dólar` and downloads missing FX reference-rate CSV files.
  - Uploads newly downloaded files to Google Cloud Storage.
- **Main output files:**
  - `gs://<GCS_BUCKET_NAME>/<GCS_PREFIX_PRE>taxas_referenciais_YYYY-MM-DD.csv`
  - `gs://<GCS_BUCKET_NAME>/<GCS_PREFIX_FX>taxas_referenciais_YYYY-MM-DD.csv`
- **Required environment variables:**
  - `GCS_BUCKET_NAME` - target Google Cloud Storage bucket.
  - `GCS_PREFIX_PRE` - destination prefix for `DI x pré` CSV files.
  - `GCS_PREFIX_FX` - destination prefix for `Real x dólar` CSV files.
  - `MAX_DOWNLOADS` - maximum number of missing files to download per product in one run.

## B3 trades to GCS

- **GitHub path:** `jobs/b3-trades-to-gcs`
- **Cloud Run Job name:** `b3-trades-to-gcs`
- **What it does:**
  - Calls the B3 BDI table API for a requested date or the current São Paulo date.
  - Ensures recent business-day trade data is present in Google Cloud Storage.
  - Writes daily CSV and JSON files for fetched trades.
  - Rebuilds the compact historical trades JSON from stored daily CSV files.
- **Main output files:**
  - `gs://<GCS_BUCKET_NAME>/<GCS_PREFIX>/daily_csv/YYYY-MM-DD.csv`
  - `gs://<GCS_BUCKET_NAME>/<GCS_PREFIX>/daily_json/YYYY-MM-DD.json`
  - `gs://<GCS_BUCKET_NAME>/<GCS_PREFIX>/historico-trades.json`
- **Required environment variables:**
  - `GCS_BUCKET_NAME` - target Google Cloud Storage bucket.
  - `GCS_PREFIX` - destination prefix for B3 trade outputs.
  - `TABLE_ENDPOINT` - B3 table endpoint to query.
  - `TARGET_DATE` - optional ISO date (`YYYY-MM-DD`) to anchor the run; defaults to the current date in `America/Sao_Paulo`.
  - `PAGE_SIZE` - B3 API page size.
  - `CONNECT_TIMEOUT` - HTTP connect timeout in seconds.
  - `READ_TIMEOUT` - HTTP read timeout in seconds.
  - `MAX_RETRIES` - maximum retry attempts for each API page.
  - `SLEEP_BETWEEN_PAGES` - delay between paginated API requests.
  - `RECENT_BUSINESS_DAYS_TO_ENSURE` - number of recent business days the job checks and backfills.
