import os
import re
import base64
import asyncio
from datetime import datetime
from pathlib import Path

from google.cloud import storage
from playwright.async_api import async_playwright


# ============================================================
# Config
# ============================================================

GCS_BUCKET_NAME = os.environ.get("GCS_BUCKET_NAME", "debentures-anbima-am")

# Mantém PRE x DI exatamente no prefixo já usado
GCS_PREFIX_PRE = os.environ.get("GCS_PREFIX_PRE", "B3-predi/")

# Nova pasta para Real x dólar
GCS_PREFIX_FX = os.environ.get("GCS_PREFIX_FX", "b3-fx/")

MAX_DOWNLOADS = int(os.environ.get("MAX_DOWNLOADS", "10"))

B3_URL = (
    "https://www.b3.com.br/pt_br/market-data-e-indices/servicos-de-dados/"
    "market-data/consultas/mercado-de-derivativos/precos-referenciais/"
    "taxas-referenciais-bm-fbovespa/"
)

LOCAL_DIR = Path("/tmp/b3_reference_rates")
LOCAL_DIR.mkdir(parents=True, exist_ok=True)


# ============================================================
# Helpers
# ============================================================

def normalize_prefix(prefix: str) -> str:
    return prefix.strip("/").strip() + "/"


def parse_date_br(date_text: str) -> datetime:
    return datetime.strptime(date_text, "%d/%m/%Y")


def date_to_filename(date_text: str) -> str:
    dt = parse_date_br(date_text)
    return f"taxas_referenciais_{dt.strftime('%Y-%m-%d')}.csv"


def decode_b3_response_to_csv(body: bytes) -> bytes:
    """
    A B3 retorna o CSV em base64. Esta função decodifica para CSV legível.
    Se por algum motivo vier CSV normal, retorna o body original.
    """
    text = body.decode("utf-8", errors="ignore").strip()

    try:
        decoded = base64.b64decode(text)
        if b";" in decoded[:500] or b"," in decoded[:500]:
            return decoded
        return body
    except Exception:
        return body


def gcs_blob_name_for_date(prefix: str, date_text: str) -> str:
    return normalize_prefix(prefix) + date_to_filename(date_text)


def list_existing_files(bucket, prefix: str) -> set[str]:
    prefix = normalize_prefix(prefix)
    blobs = bucket.list_blobs(prefix=prefix)
    return {blob.name for blob in blobs if blob.name.endswith(".csv")}


def upload_to_gcs(bucket, blob_name: str, local_path: Path):
    blob = bucket.blob(blob_name)
    blob.upload_from_filename(str(local_path), content_type="text/csv")
    print(f"Uploaded to gs://{GCS_BUCKET_NAME}/{blob_name}")


async def close_popups(page):
    for txt in ["Aceitar", "ACEITAR", "Concordo", "OK", "Ok"]:
        try:
            btn = page.get_by_text(txt, exact=False).first
            if await btn.count() > 0 and await btn.is_visible():
                await btn.click(timeout=3000)
                await page.wait_for_timeout(1500)
                print("Closed cookie/popup.")
                break
        except Exception:
            pass


async def scroll_page(page):
    # Rola até o histórico de arquivos e força o conteúdo dinâmico/iframe carregar
    for _ in range(8):
        await page.mouse.wheel(0, 1200)
        await page.wait_for_timeout(1000)


async def select_product(page, product_name: str | None):
    """
    Se product_name for None, mantém o produto default da página, que é DI x pré.
    Se product_name for 'Real x dólar', seleciona no dropdown e clica em BUSCAR.
    """
    if not product_name:
        print("Keeping default B3 product, expected: DI x pré")
        return

    print(f"Selecting B3 product: {product_name}")

    # A tabela/dropdown fica em iframe. Procuramos em todos os frames.
    selected = False

    for frame_i, frame in enumerate(page.frames):
        try:
            # tenta abrir dropdown pelo texto default/placeholder
            candidates = [
                frame.get_by_text("Selecione um produto", exact=False).first,
                frame.get_by_text("DI x pré", exact=False).first,
                frame.locator("select").first,
            ]

            for locator in candidates:
                try:
                    if await locator.count() > 0 and await locator.is_visible():
                        tag_name = await locator.evaluate("el => el.tagName.toLowerCase()")

                        if tag_name == "select":
                            await locator.select_option(label=product_name)
                        else:
                            await locator.click(timeout=5000, force=True)
                            await page.wait_for_timeout(1500)
                            option = frame.get_by_text(product_name, exact=False).first
                            await option.click(timeout=5000, force=True)

                        selected = True
                        print(f"Product selected in frame {frame_i}: {product_name}")
                        break
                except Exception:
                    continue

            if selected:
                break

        except Exception:
            pass

    if not selected:
        raise RuntimeError(f"Could not select product: {product_name}")

    # Clica em BUSCAR no frame que tiver o botão
    searched = False

    for frame_i, frame in enumerate(page.frames):
        try:
            buscar = frame.get_by_text("BUSCAR", exact=False).first
            if await buscar.count() > 0 and await buscar.is_visible():
                await buscar.click(timeout=8000, force=True)
                searched = True
                print(f"Clicked BUSCAR in frame {frame_i}")
                break
        except Exception:
            pass

    if not searched:
        # fallback na página principal
        try:
            buscar = page.get_by_text("BUSCAR", exact=False).first
            await buscar.click(timeout=8000, force=True)
            searched = True
            print("Clicked BUSCAR in main page fallback")
        except Exception:
            pass

    if not searched:
        raise RuntimeError(f"Could not click BUSCAR after selecting product: {product_name}")

    await page.wait_for_timeout(7000)


async def find_date_links(page):
    """
    Procura links com datas dd/mm/yyyy em todos os frames.
    """
    candidates = []

    for frame_i, frame in enumerate(page.frames):
        try:
            links = await frame.locator("a").evaluate_all(
                """
                els => els.map((el, idx) => {
                    const text = (el.innerText || el.textContent || '').trim();
                    const rect = el.getBoundingClientRect();
                    return {
                        idx,
                        text,
                        href: el.href || '',
                        visible: !!(rect.width || rect.height),
                        x: rect.x,
                        y: rect.y
                    };
                }).filter(e => e.visible && /\\b\\d{2}\\/\\d{2}\\/\\d{4}\\b/.test(e.text))
                """
            )

            for link in links:
                link["frame_i"] = frame_i
                candidates.append(link)

        except Exception:
            pass

    return candidates


async def fetch_b3_curve_missing_files(prefix: str, product_name: str | None, existing_files: set[str]):
    """
    Baixa arquivos faltantes para o produto indicado.

    product_name=None: mantém produto default DI x pré.
    product_name='Real x dólar': seleciona Real x dólar.
    """
    downloaded = []
    captured_responses = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--window-size=1920,1600",
            ],
        )

        context = await browser.new_context(
            accept_downloads=True,
            locale="pt-BR",
            viewport={"width": 1920, "height": 1600},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )

        page = await context.new_page()

        async def on_response(response):
            try:
                headers = response.headers
                content_type = headers.get("content-type", "").lower()
                content_disposition = headers.get("content-disposition", "").lower()

                looks_like_file = (
                    response.status == 200
                    and (
                        "attachment" in content_disposition
                        or "octet-stream" in content_type
                        or "csv" in content_type
                        or "text/plain" in content_type
                        or "application" in content_type
                    )
                )

                if looks_like_file:
                    body = await response.body()
                    if len(body) > 500:
                        captured_responses.append({"url": response.url, "headers": headers, "body": body})
            except Exception:
                pass

        page.on("response", on_response)

        print("Opening B3 page...")
        await page.goto(B3_URL, wait_until="domcontentloaded", timeout=120000)
        await page.wait_for_timeout(12000)

        await close_popups(page)
        await scroll_page(page)

        # Seleciona o produto apenas para FX; PRE x DI fica como default.
        if product_name:
            await select_product(page, product_name)
            await scroll_page(page)

        candidates = await find_date_links(page)

        print(f"Date links found for prefix {prefix}: {len(candidates)}")
        for i, c in enumerate(candidates[:20]):
            print(i, "| frame", c["frame_i"], "|", c["text"], "|", c.get("href", ""))

        if not candidates:
            raise RuntimeError(f"No B3 date links found for prefix {prefix} / product {product_name}")

        selected = []
        seen_dates = set()

        # Datas únicas, mais recentes primeiro, conforme aparecem no site
        for c in candidates:
            m = re.search(r"\d{2}/\d{2}/\d{4}", c["text"])
            if not m:
                continue

            date_text = m.group(0)
            if date_text in seen_dates:
                continue

            seen_dates.add(date_text)
            blob_name = gcs_blob_name_for_date(prefix, date_text)

            if blob_name in existing_files:
                print(f"Already exists, skipping {date_text}: {blob_name}")
                continue

            selected.append(c)

            if len(selected) >= MAX_DOWNLOADS:
                break

        print(f"Missing files selected for {prefix}: {len(selected)}")

        for c in selected:
            frame = page.frames[c["frame_i"]]
            date_text = re.search(r"\d{2}/\d{2}/\d{4}", c["text"]).group(0)
            blob_name = gcs_blob_name_for_date(prefix, date_text)

            print(f"Downloading {product_name or 'DI x pré'} file for {date_text}...")

            before = len(captured_responses)
            link = frame.locator("a").nth(c["idx"])

            try:
                await link.click(timeout=30000, force=True)
            except Exception:
                await link.evaluate("(el) => el.click()")

            await page.wait_for_timeout(12000)

            new_responses = captured_responses[before:]

            if not new_responses:
                print(f"FAIL: no file response captured for {date_text}")
                continue

            best = max(new_responses, key=lambda x: len(x["body"]))
            csv_bytes = decode_b3_response_to_csv(best["body"])

            filename = date_to_filename(date_text)
            local_prefix = normalize_prefix(prefix).strip("/").replace("/", "_")
            local_path = LOCAL_DIR / f"{local_prefix}_{filename}"

            with open(local_path, "wb") as f:
                f.write(csv_bytes)

            downloaded.append(
                {
                    "date": date_text,
                    "blob_name": blob_name,
                    "local_path": local_path,
                    "url": best["url"],
                    "size": len(csv_bytes),
                }
            )

            print(f"OK {date_text}: {local_path} ({len(csv_bytes)} bytes)")
            await page.wait_for_timeout(2000)

        await browser.close()

    return downloaded


async def run_all_downloads(bucket):
    all_downloaded = []

    # ============================================================
    # 1) PRE x DI - fluxo existente preservado
    # ============================================================
    print("\n========================================")
    print("Downloading PRE x DI into B3-predi/")
    print("========================================")

    existing_pre = list_existing_files(bucket, GCS_PREFIX_PRE)
    print(f"Existing PRE CSV files: {len(existing_pre)}")

    downloaded_pre = await fetch_b3_curve_missing_files(
        prefix=GCS_PREFIX_PRE,
        product_name=None,
        existing_files=existing_pre,
    )

    all_downloaded.extend(downloaded_pre)

    # ============================================================
    # 2) Real x dólar - novo fluxo FX
    # ============================================================
    print("\n========================================")
    print("Downloading Real x dólar into b3-fx/")
    print("========================================")

    existing_fx = list_existing_files(bucket, GCS_PREFIX_FX)
    print(f"Existing FX CSV files: {len(existing_fx)}")

    downloaded_fx = await fetch_b3_curve_missing_files(
        prefix=GCS_PREFIX_FX,
        product_name="Real x dólar",
        existing_files=existing_fx,
    )

    all_downloaded.extend(downloaded_fx)

    return all_downloaded


def main():
    print("========================================")
    print("B3 reference rates download job")
    print("========================================")
    print(f"Bucket: {GCS_BUCKET_NAME}")
    print(f"PRE prefix: {normalize_prefix(GCS_PREFIX_PRE)}")
    print(f"FX prefix: {normalize_prefix(GCS_PREFIX_FX)}")
    print(f"Max downloads per product: {MAX_DOWNLOADS}")

    storage_client = storage.Client()
    bucket = storage_client.bucket(GCS_BUCKET_NAME)

    downloaded = asyncio.run(run_all_downloads(bucket))

    if not downloaded:
        print("No new files to upload.")
        return

    for item in downloaded:
        upload_to_gcs(bucket, item["blob_name"], item["local_path"])

    print("========================================")
    print(f"Done. Uploaded {len(downloaded)} new file(s).")
    print("========================================")


if __name__ == "__main__":
    main()
