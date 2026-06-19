# B3 trades to GCS Cloud Run Job

Fetches B3 trade data, stores daily CSV and JSON files, and rebuilds the compact historical trades JSON in Google Cloud Storage.

## Files

- `Dockerfile` - container image definition for Cloud Run Jobs.
- `requirements.txt` - Python dependencies.
- `b3_trades_cloud_job.py` - job entrypoint.
- `.env.example` - environment variable example for local runs or deployment configuration.

## Main outputs

- `gs://<GCS_BUCKET_NAME>/<GCS_PREFIX>/daily_csv/YYYY-MM-DD.csv`
- `gs://<GCS_BUCKET_NAME>/<GCS_PREFIX>/daily_json/YYYY-MM-DD.json`
- `gs://<GCS_BUCKET_NAME>/<GCS_PREFIX>/historico-trades.json`
