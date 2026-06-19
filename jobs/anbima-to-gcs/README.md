# ANBIMA to GCS Cloud Run Job

Downloads ANBIMA debenture data and CRI/CRA data, uploads the source files to Google Cloud Storage, and rebuilds the consolidated ANBIMA history JSON.

## Files

- `Dockerfile` - container image definition for Cloud Run Jobs.
- `requirements.txt` - Python dependencies.
- `anbima_to_gcs.py` - job entrypoint.
- `.env.example` - environment variable example for local runs or deployment configuration.

## Main outputs

- `gs://<GCS_BUCKET_NAME>/<GCS_PREFIX>dYYmmmDD.xls`
- `gs://<GCS_BUCKET_NAME>/<CRICRA_GCS_PREFIX>dYYmmmDD.xlsx`
- `gs://<GCS_BUCKET_NAME>/<GCS_PREFIX>historico-anbima.json`
