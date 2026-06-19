from __future__ import annotations

import csv
import io
import json
import os
import re
import time
from datetime import datetime, timedelta, date as date_cls
from zoneinfo import ZoneInfo

import requests
from google.cloud import storage

GCS_BUCKET_NAME = os.environ.get("GCS_BUCKET_NAME", "debentures-anbima-am")
GCS_PREFIX = os.environ.get("GCS_PREFIX", "b3_trades")
TABLE_ENDPOINT = os.environ.get("TABLE_ENDPOINT", "Trade")

SAO_PAULO_TZ = ZoneInfo("America/Sao_Paulo")
TARGET_DATE = os.environ.get("TARGET_DATE")

PAGE_SIZE = int(os.environ.get("PAGE_SIZE", "1000"))
CONNECT_TIMEOUT = int(os.environ.get("CONNECT_TIMEOUT", "20"))
READ_TIMEOUT = int(os.environ.get("READ_TIMEOUT", "180"))
MAX_RETRIES = int(os.environ.get("MAX_RETRIES", "5"))
SLEEP_BETWEEN_PAGES = float(os.environ.get("SLEEP_BETWEEN_PAGES", "0.4"))

RECENT_BUSINESS_DAYS_TO_ENSURE = int(
    os.environ.get("RECENT_BUSINESS_DAYS_TO_ENSURE", "5")
)

BASE_URL = (
    "https://arquivos.b3.com.br/bdi/table/"
    "{table_endpoint}/{date}/{date}/{page}/{page_size}"
)

HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://arquivos.b3.com.br",
    "Referer": "https://arquivos.b3.com.br/bdi/tabelas?lang=pt-BR",
}


def daily_csv_prefix() -> str:
    return f"{GCS_PREFIX.rstrip('/')}/daily_csv/"


def daily_json_prefix() -> str:
    return f"{GCS_PREFIX.rstrip('/')}/daily_json/"


def daily_csv_blob_name(date_str: str) -> str:
    return f"{daily_csv_prefix()}{date_str}.csv"


def daily_json_blob_name(date_str: str) -> str:
    return f"{daily_json_prefix()}{date_str}.json"


def historico_blob_name() -> str:
    return f"{GCS_PREFIX.rstrip('/')}/historico-trades.json"


def log(msg: str) -> None:
    print(msg, flush=True)


def resolve_target_date() -> str:
    if TARGET_DATE:
        return TARGET_DATE
    return datetime.now(SAO_PAULO_TZ).date().isoformat()


def parse_any_date_to_iso(value: str | None) -> str | None:
    if value is None:
        return None

    s = str(value).strip()
    if not s:
        return None

    if len(s) >= 10 and s[4:5] == "-" and s[7:8] == "-":
        return s[:10]

    m = re.fullmatch(r"(\d{2})/(\d{2})/(\d{4})", s)
    if m:
        dd, mm, yyyy = m.groups()
        return f"{yyyy}-{mm}-{dd}"

    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", s)
    if m:
        mm, dd, yyyy = m.groups()
        return f"{yyyy}-{int(mm):02d}-{int(dd):02d}"

    return None


def format_date_mmddyyyy(value: str | None) -> str | None:
    iso = parse_any_date_to_iso(value)
    if not iso:
        return value
    yyyy, mm, dd = iso.split("-")
    return f"{mm}-{dd}-{yyyy}"


def normalize_row_dates_to_iso(row: dict) -> dict:
    new_row = {}
    for k, v in row.items():
        parsed = parse_any_date_to_iso(v)
        new_row[k] = parsed if parsed else v
    return new_row


def get_column_names(columns_meta: list[dict]) -> list[str]:
    names: list[str] = []
    for i, col in enumerate(columns_meta):
        if isinstance(col, dict):
            name = col.get("name") or f"col_{i}"
        else:
            name = f"col_{i}"
        name = str(name).strip() if name is not None else f"col_{i}"
        if not name:
            name = f"col_{i}"
        names.append(name)
    return names


def normalize_label(s: str | None) -> str:
    if s is None:
        return ""
    return " ".join(str(s).strip().lower().split())


def previous_business_day(d: date_cls) -> date_cls:
    d = d - timedelta(days=1)
    while d.weekday() >= 5:
        d = d - timedelta(days=1)
    return d


def get_recent_business_dates(end_date_str: str, count: int) -> list[str]:
    current = date_cls.fromisoformat(end_date_str)
    dates: list[str] = []

    if current.weekday() >= 5:
        while current.weekday() >= 5:
            current = previous_business_day(current + timedelta(days=1))

    while len(dates) < count:
        if current.weekday() < 5:
            dates.append(current.isoformat())
        current = previous_business_day(current)

    return dates


def fetch_page(session: requests.Session, target_date: str, page: int) -> dict:
    url = BASE_URL.format(
        table_endpoint=TABLE_ENDPOINT,
        date=target_date,
        page=page,
        page_size=PAGE_SIZE,
    )
    last_err = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = session.post(url, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT))
            response.raise_for_status()
            return response.json()
        except Exception as e:
            last_err = e
            wait_s = min(2 ** attempt, 20)
            log(f"Page {page} failed on attempt {attempt}/{MAX_RETRIES}: {e}")
            if attempt < MAX_RETRIES:
                log(f"Waiting {wait_s}s before retry...")
                time.sleep(wait_s)

    raise RuntimeError(
        f"Failed to download page {page} after {MAX_RETRIES} attempts"
    ) from last_err


def date_has_rows(session: requests.Session, target_date: str) -> bool:
    payload = fetch_page(session, target_date, 1)
    table = payload.get("table", {})
    values = table.get("values", [])
    log(f"Probe {target_date}: {len(values)} row(s) on page 1")
    return len(values) > 0


def fetch_all_rows(session: requests.Session, target_date: str) -> tuple[list[dict], list[dict]]:
    all_rows_raw: list[list] = []
    columns_meta: list[dict] | None = None
    page = 1

    while True:
        payload = fetch_page(session, target_date, page)
        table = payload["table"]

        if columns_meta is None:
            columns_meta = table["columns"]

        values = table.get("values", [])
        log(f"Page {page}: {len(values)} rows for {target_date}")

        if not values:
            break

        all_rows_raw.extend(values)

        if len(values) < PAGE_SIZE:
            break

        page += 1
        time.sleep(SLEEP_BETWEEN_PAGES)

    if columns_meta is None:
        raise RuntimeError(f"The B3 API returned no column metadata for {target_date}.")

    col_names = get_column_names(columns_meta)
    row_dicts = [dict(zip(col_names, row)) for row in all_rows_raw]

    return columns_meta, row_dicts


CSV_LABELS = [
    "Data negócio",
    "Instrumento financeiro",
    "Emissor",
    "Código IF",
    "Quantidade negociada",
    "Preço negócio",
    "Volume financeiro (R$)",
    "Taxa negócio",
    "Origem negócio",
    "Horário negócio",
    "Data negócio",
    "Cód. identificador do negócio",
    "Código ISIN",
    "Data liquidação",
    "Situação negócio",
]


def build_friendly_to_technical_map(columns_meta: list[dict]) -> dict[str, list[str]]:
    mapping: dict[str, list[str]] = {}

    for i, col in enumerate(columns_meta):
        if not isinstance(col, dict):
            continue

        tech = col.get("name") or f"col_{i}"
        friendly = (
            col.get("friendlyNamePt")
            or col.get("friendlyName")
            or col.get("label")
            or col.get("title")
        )

        key = normalize_label(friendly)
        if not key:
            continue

        mapping.setdefault(key, []).append(tech)

    return mapping


def get_value_by_friendly_label(
    row: dict,
    friendly_map: dict[str, list[str]],
    label: str,
    occurrence: int = 1,
):
    key = normalize_label(label)
    tech_names = friendly_map.get(key, [])

    if len(tech_names) < occurrence:
        return None

    tech_name = tech_names[occurrence - 1]
    return row.get(tech_name)


def build_csv_bytes(columns_meta: list[dict], row_dicts: list[dict]) -> bytes:
    friendly_map = build_friendly_to_technical_map(columns_meta)

    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=",")
    writer.writerow(CSV_LABELS)

    for row in row_dicts:
        out_row = [
            format_date_mmddyyyy(get_value_by_friendly_label(row, friendly_map, "Data negócio", 1)),
            get_value_by_friendly_label(row, friendly_map, "Instrumento financeiro", 1),
            get_value_by_friendly_label(row, friendly_map, "Emissor", 1),
            get_value_by_friendly_label(row, friendly_map, "Código IF", 1),
            get_value_by_friendly_label(row, friendly_map, "Quantidade negociada", 1),
            get_value_by_friendly_label(row, friendly_map, "Preço negócio", 1),
            get_value_by_friendly_label(row, friendly_map, "Volume financeiro (R$)", 1),
            get_value_by_friendly_label(row, friendly_map, "Taxa negócio", 1),
            get_value_by_friendly_label(row, friendly_map, "Origem negócio", 1),
            get_value_by_friendly_label(row, friendly_map, "Horário negócio", 1),
            format_date_mmddyyyy(get_value_by_friendly_label(row, friendly_map, "Data negócio", 2)),
            get_value_by_friendly_label(row, friendly_map, "Cód. identificador do negócio", 1),
            get_value_by_friendly_label(row, friendly_map, "Código ISIN", 1),
            format_date_mmddyyyy(get_value_by_friendly_label(row, friendly_map, "Data liquidação", 1)),
            get_value_by_friendly_label(row, friendly_map, "Situação negócio", 1),
        ]
        writer.writerow(out_row)

    return ("\ufeff" + buffer.getvalue()).encode("utf-8")


def build_daily_json_bytes(
    target_date: str,
    table_endpoint: str,
    columns_meta: list[dict],
    row_dicts: list[dict],
) -> bytes:
    payload = {
        "date": target_date,
        "table_endpoint": table_endpoint,
        "row_count": len(row_dicts),
        "columns": columns_meta,
        "rows": row_dicts,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")


def upload_bytes(
    client: storage.Client,
    bucket_name: str,
    blob_name: str,
    data: bytes,
    content_type: str,
) -> None:
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(blob_name)
    blob.upload_from_string(data, content_type=content_type)
    log(f"Uploaded gs://{bucket_name}/{blob_name}")


def blob_exists(client: storage.Client, bucket_name: str, blob_name: str) -> bool:
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(blob_name)
    return blob.exists()


def list_csv_blobs(client: storage.Client, bucket_name: str) -> list[storage.Blob]:
    blobs = list(client.list_blobs(bucket_name, prefix=daily_csv_prefix()))
    return [b for b in blobs if b.name.lower().endswith(".csv")]


def make_unique_headers(headers: list[str]) -> list[str]:
    seen: dict[str, int] = {}
    unique: list[str] = []

    for h in headers:
        key = h.strip() if isinstance(h, str) else h
        if key in seen:
            seen[key] += 1
            unique.append(f"{key}__{seen[key]}")
        else:
            seen[key] = 1
            unique.append(key)

    return unique


def detect_csv_delimiter(sample: str) -> str:
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;")
        return dialect.delimiter
    except Exception:
        return ";" if sample.count(";") > sample.count(",") else ","


def read_csv_blob_rows(
    client: storage.Client,
    bucket_name: str,
    blob_name: str,
) -> tuple[list[str], list[dict]]:
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(blob_name)

    size_mb = (blob.size or 0) / (1024 * 1024)
    log(f"--- Reading file: {blob_name} ({size_mb:.2f} MB) ---")

    start_time = time.time()
    raw = blob.download_as_bytes()

    text = None
    used_encoding = None
    for encoding in ("utf-8-sig", "utf-8", "latin1"):
        try:
            text = raw.decode(encoding)
            used_encoding = encoding
            break
        except Exception:
            continue

    if text is None:
        raise RuntimeError(f"Could not decode CSV blob: {blob_name}")

    sample = text[:5000]
    delimiter = detect_csv_delimiter(sample)
    log(f"{blob_name}: encoding={used_encoding}, delimiter='{delimiter}'")

    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    rows_raw = list(reader)

    if not rows_raw:
        log(f"{blob_name}: file is empty")
        return [], []

    original_headers = [
        str(h).replace("\ufeff", "").strip() if h is not None else ""
        for h in rows_raw[0]
    ]
    unique_headers = make_unique_headers(original_headers)

    total_rows = max(len(rows_raw) - 1, 0)
    log(f"{blob_name}: {total_rows} data row(s) detected")

    rows = []
    for i, raw_row in enumerate(rows_raw[1:], start=1):
        if not any(str(x).strip() for x in raw_row):
            continue

        if len(raw_row) < len(unique_headers):
            raw_row = raw_row + [""] * (len(unique_headers) - len(raw_row))

        row_dict = {
            unique_headers[j]: raw_row[j].strip() if isinstance(raw_row[j], str) else raw_row[j]
            for j in range(len(unique_headers))
        }
        rows.append(row_dict)

        if i % 5000 == 0:
            elapsed = time.time() - start_time
            log(f"{blob_name}: processed {i}/{total_rows} row(s) in {elapsed:.1f}s")

    elapsed_total = time.time() - start_time
    log(f"{blob_name}: DONE ({len(rows)} parsed row(s) in {elapsed_total:.1f}s)")

    return unique_headers, rows


def rebuild_historico_from_daily_csvs(
    client: storage.Client,
    bucket_name: str,
    historico_name: str,
) -> None:
    """
    Recria apenas o JSON principal:
      gs://<bucket>/<GCS_PREFIX>/historico-trades.json

    Estrutura:
      {
        "YYYY-MM-DD": [
          {...trade row...},
          {...trade row...}
        ]
      }

    Não cria mais:
      - historico-trades-by-ticker.json
      - historico-trades-by-issuer.json
    """
    blobs = list_csv_blobs(client, bucket_name)
    log(f"Found {len(blobs)} daily CSV file(s) under {daily_csv_prefix()}")

    historico: dict[str, list[dict]] = {}

    daily_blobs = []
    for blob in blobs:
        file_date_match = re.search(r"(\d{4}-\d{2}-\d{2})\.csv$", blob.name)
        if not file_date_match:
            log(f"Skipping non-daily CSV file: gs://{bucket_name}/{blob.name}")
            continue
        daily_blobs.append((blob, file_date_match.group(1)))

    total_files = len(daily_blobs)
    total_rows_added = 0
    start_all = time.time()

    for idx, (blob, file_date) in enumerate(sorted(daily_blobs, key=lambda x: x[0].name), start=1):
        log(f"Processing file {idx}/{total_files}: {blob.name}")
        _, rows = read_csv_blob_rows(client, bucket_name, blob.name)

        if not rows:
            log(f"Skipping empty file: gs://{bucket_name}/{blob.name}")
            continue

        included = 0
        for row_idx, row in enumerate(rows, start=1):
            normalized_row = normalize_row_dates_to_iso(row)
            historico.setdefault(file_date, []).append(normalized_row)

            included += 1
            total_rows_added += 1

            if row_idx % 10000 == 0:
                log(
                    f"{blob.name}: historico grouping progress "
                    f"{row_idx}/{len(rows)} row(s)"
                )

        log(
            f"Included {included} row(s) from gs://{bucket_name}/{blob.name} "
            f"into historico grouped by date {file_date}"
        )

    compact_kwargs = {"ensure_ascii": False, "separators": (",", ":")}

    upload_bytes(
        client=client,
        bucket_name=bucket_name,
        blob_name=historico_name,
        data=json.dumps(historico, **compact_kwargs).encode("utf-8"),
        content_type="application/json",
    )

    elapsed_all = time.time() - start_all
    log(
        f"Historical rebuild DONE: {total_rows_added} total row(s), "
        f"{len(historico)} date bucket(s), {elapsed_all:.1f}s"
    )


def ensure_recent_business_days_in_bucket(
    client: storage.Client,
    session: requests.Session,
    requested_date: str,
) -> list[str]:
    target_dates = get_recent_business_dates(
        requested_date,
        RECENT_BUSINESS_DAYS_TO_ENSURE,
    )
    log(f"Recent business dates to ensure: {target_dates}")

    saved_dates: list[str] = []

    for target_date in target_dates:
        csv_name = daily_csv_blob_name(target_date)
        json_name = daily_json_blob_name(target_date)

        csv_exists = blob_exists(client, GCS_BUCKET_NAME, csv_name)
        json_exists = blob_exists(client, GCS_BUCKET_NAME, json_name)

        if csv_exists and json_exists:
            log(f"Already present in bucket: {target_date}")
            saved_dates.append(target_date)
            continue

        log(f"Missing file(s) for {target_date}. Querying B3...")
        if not date_has_rows(session, target_date):
            log(f"No B3 data found for {target_date}. Skipping.")
            continue

        columns_meta, row_dicts = fetch_all_rows(session, target_date)
        log(f"Fetched {len(row_dicts)} row(s) for {target_date}")

        if not row_dicts:
            log(f"No rows returned for {target_date}. Skipping save.")
            continue

        csv_bytes = build_csv_bytes(columns_meta, row_dicts)
        upload_bytes(
            client=client,
            bucket_name=GCS_BUCKET_NAME,
            blob_name=csv_name,
            data=csv_bytes,
            content_type="text/csv",
        )

        daily_json_bytes = build_daily_json_bytes(
            target_date=target_date,
            table_endpoint=TABLE_ENDPOINT,
            columns_meta=columns_meta,
            row_dicts=row_dicts,
        )
        upload_bytes(
            client=client,
            bucket_name=GCS_BUCKET_NAME,
            blob_name=json_name,
            data=daily_json_bytes,
            content_type="application/json",
        )

        saved_dates.append(target_date)

    return saved_dates


def main() -> None:
    requested_date = resolve_target_date()

    log("Starting B3 trade ingestion job...")
    log(f"Bucket: {GCS_BUCKET_NAME}")
    log(f"Prefix: {GCS_PREFIX}")
    log(f"Table endpoint: {TABLE_ENDPOINT}")
    log(f"Requested date: {requested_date}")
    log(f"Page size: {PAGE_SIZE}")
    log(f"Recent business days to ensure: {RECENT_BUSINESS_DAYS_TO_ENSURE}")

    session = requests.Session()
    session.headers.update(HEADERS)
    client = storage.Client()

    ensured_dates = ensure_recent_business_days_in_bucket(
        client=client,
        session=session,
        requested_date=requested_date,
    )
    log(f"Dates ensured in bucket: {ensured_dates}")

    rebuild_historico_from_daily_csvs(
        client=client,
        bucket_name=GCS_BUCKET_NAME,
        historico_name=historico_blob_name(),
    )

    log("Job finished successfully.")


if __name__ == "__main__":
    main()
