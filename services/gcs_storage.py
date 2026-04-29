from google.cloud import storage


def build_blob_name(prefix: str, filename: str) -> str:
    return prefix.rstrip("/") + "/" + filename


def download_gcs_text(bucket_name: str, blob_name: str, encoding: str) -> str:
    client = storage.Client()
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(blob_name)
    return blob.download_as_text(encoding=encoding)
