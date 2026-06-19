# B3 reference rates to GCS Cloud Run Job

Downloads missing B3 reference-rate CSV files for `DI x pré` and `Real x dólar`, then uploads them to Google Cloud Storage.

## Files

- `Dockerfile` - container image definition for Cloud Run Jobs.
- `requirements.txt` - Python dependencies.
- `b3-predi-job.py` - job entrypoint.
- `.env.example` - environment variable example for local runs or deployment configuration.

## Main outputs

- `gs://<GCS_BUCKET_NAME>/<GCS_PREFIX_PRE>taxas_referenciais_YYYY-MM-DD.csv`
- `gs://<GCS_BUCKET_NAME>/<GCS_PREFIX_FX>taxas_referenciais_YYYY-MM-DD.csv`
