import os
import json
import time
import csv
import io
import threading
import zipfile
import re
import unicodedata
import difflib
from datetime import date as date_cls, timedelta, datetime

import requests
from flask import Flask, request, jsonify, render_template
from google.cloud import storage

GCS_BUCKET_NAME = os.environ.get("GCS_BUCKET_NAME", "debentures-anbima-am")
GCS_PREFIX = os.environ.get("GCS_PREFIX", "anbima_debentures/")
B3_INFO_PREFIX = os.environ.get("B3_INFO_PREFIX", "b3_infos/")
B3_INFO_FILENAME = os.environ.get("B3_INFO_FILENAME", "Debentures.csv")

B3_TRADES_PREFIX = os.environ.get("B3_TRADES_PREFIX", "b3_trades/")
B3_TRADES_FILENAME = os.environ.get("B3_TRADES_FILENAME", "historico-trades.json")

# CVM Filings config. This is fully isolated from ANBIMA/B3 logic.
CVM_LAST_DAYS = int(os.environ.get("CVM_LAST_DAYS", "30"))
CVM_REQUEST_TIMEOUT = int(os.environ.get("CVM_REQUEST_TIMEOUT", "90"))
CVM_USER_AGENT = os.environ.get(
    "CVM_USER_AGENT",
    "Mozilla/5.0 (compatible; CVMFilingsTracker/1.0; +https://dados.cvm.gov.br)",
)
CVM_IPE_ZIP_URL = (
    "https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/IPE/DADOS/"
    "ipe_cia_aberta_{year}.zip"
)

# Fallback live do ENET: o ZIP de Dados Abertos pode atrasar intraday.
CVM_ENET_BASE_URL = "https://www.rad.cvm.gov.br/ENET/frmConsultaExternaCVM.aspx"
CVM_ENET_TIMEOUT = int(os.environ.get("CVM_ENET_TIMEOUT", "45"))
CVM_ENET_LIVE_FALLBACK = os.environ.get("CVM_ENET_LIVE_FALLBACK", "1").lower() not in ("0", "false", "no")

app = Flask(__name__)

# Cache simples em memória.
# CACHE_SECONDS = 3600 => usa cache por 1 hora.
# CACHE_SECONDS = 0 => cache infinito até o container reiniciar.
_history_cache = None
_last_load_time = 0

_volume_cache = None
_volume_last_load_time = 0

_trades_cache = None
_trades_last_load_time = 0
_trades_cache_lock = threading.Lock()

_cvm_cache = {
    "loaded_at": 0,
    "rows": [],
    "errors": [],
    "source_years": [],
}
_cvm_cache_lock = threading.Lock()

CACHE_SECONDS = 3600
TABLE_LIMIT = 30
TRADES_LOOKBACK_DAYS = 10


def get_history_blob_name() -> str:
    prefix = GCS_PREFIX.rstrip("/") + "/"
    return prefix + "historico-anbima.json"


def get_volume_blob_name() -> str:
    prefix = B3_INFO_PREFIX.rstrip("/") + "/"
    return prefix + B3_INFO_FILENAME


def get_trades_blob_name() -> str:
    prefix = B3_TRADES_PREFIX.rstrip("/") + "/"
    return prefix + B3_TRADES_FILENAME


def is_cache_valid(last_load_time):
    """Retorna True se o cache em memória ainda é válido."""
    if CACHE_SECONDS == 0:
        return True

    return time.time() - last_load_time < CACHE_SECONDS


def load_history():
    """Carrega o JSON consolidado do GCS, com cache em memória."""
    global _history_cache, _last_load_time

    if _history_cache is not None and is_cache_valid(_last_load_time):
        print("Usando histórico ANBIMA do cache em memória...")
        return _history_cache

    now = time.time()
    client = storage.Client()
    bucket = client.bucket(GCS_BUCKET_NAME)
    blob_name = get_history_blob_name()
    blob = bucket.blob(blob_name)

    print(f"Carregando histórico de gs://{GCS_BUCKET_NAME}/{blob_name}...")
    t0 = time.time()
    text = blob.download_as_text(encoding="utf-8")
    print(f"Download histórico ANBIMA concluído em {time.time() - t0:.1f}s | tamanho={len(text)/1024/1024:.1f} MB")

    t1 = time.time()
    data = json.loads(text)
    print(f"JSON histórico ANBIMA parseado em {time.time() - t1:.1f}s")

    _history_cache = data
    _last_load_time = now
    return data


def load_volume_map():
    """Carrega o CSV de debêntures do GCS e monta mapa {CODIGO: volume_emissao_original}."""
    global _volume_cache, _volume_last_load_time

    if _volume_cache is not None and is_cache_valid(_volume_last_load_time):
        print("Usando CSV de volumes do cache em memória...")
        return _volume_cache

    now = time.time()
    client = storage.Client()
    bucket = client.bucket(GCS_BUCKET_NAME)
    blob_name = get_volume_blob_name()
    blob = bucket.blob(blob_name)

    print(f"Carregando CSV de volumes de gs://{GCS_BUCKET_NAME}/{blob_name}...")
    t0 = time.time()
    text = blob.download_as_text(encoding="latin1")
    print(f"Download CSV volumes concluído em {time.time() - t0:.1f}s | tamanho={len(text)/1024/1024:.1f} MB")

    reader = csv.DictReader(io.StringIO(text), delimiter=";")

    volume_map = {}
    for row in reader:
        code = (row.get("Código") or "").strip().upper()
        volume = (row.get("Volume Emissão") or "").strip()

        if code:
            volume_map[code] = volume

    _volume_cache = volume_map
    _volume_last_load_time = now
    return volume_map


def parse_number(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)

    s = str(value).strip()
    if s == "":
        return None

    s = s.replace("R$", "").replace("%", "").replace(" ", "")

    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        s = s.replace(".", "").replace(",", ".")
    else:
        s = s.replace(",", "")

    try:
        return float(s)
    except Exception:
        return None


def parse_int(value):
    num = parse_number(value)
    if num is None:
        return None
    try:
        return int(round(num))
    except Exception:
        return None


def parse_iso_date(value):
    if value is None:
        return None

    if isinstance(value, date_cls):
        return value.isoformat()

    s = str(value).strip()
    if not s:
        return None

    if len(s) >= 10 and s[4:5] == "-" and s[7:8] == "-":
        return s[:10]

    s = s.split("T")[0].split(" ")[0]
    s = s.replace("/", "-")

    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d-%m-%y", "%m-%d-%Y", "%m-%d-%y"):
        try:
            dt = datetime.strptime(s, fmt)
            return dt.date().isoformat()
        except Exception:
            pass

    return None


def clean_issuer_py(x):
    if not x:
        return ""

    s = str(x)

    while True:
        start = s.find("(*")
        if start == -1:
            break

        end = start + 2
        while end < len(s) and s[end] == "*":
            end += 1

        if end < len(s) and s[end] == ")":
            s = s[:start] + s[end + 1:]
        else:
            break

    return " ".join(s.strip().split())


# ============================================================
# Strict issuer matching helpers - used only by CVM Filings
# ============================================================
def strip_accents_py(value):
    if value is None:
        return ""

    return "".join(
        ch for ch in unicodedata.normalize("NFKD", str(value))
        if not unicodedata.combining(ch)
    )


COMPANY_MATCH_STOPWORDS = {
    # conectores
    "DE", "DA", "DO", "DAS", "DOS", "E", "EM", "NA", "NO", "NAS", "NOS",

    # sufixos jurídicos / societários
    "SA", "S", "A", "SAB", "LTDA", "LTD", "CIA", "COMPANHIA",
    "SOCIEDADE", "ANONIMA", "ANONIMO",

    # termos muito genéricos de empresas
    "PARTICIPACOES", "PARTICIPACAO", "PARTICIPAÇÕES", "PARTICIPAÇÃO",
    "INVESTIMENTOS", "INVESTIMENTO",
    "HOLDING", "HOLDINGS",
    "EMPREENDIMENTOS", "EMPREENDIMENTO",
    "SERVICOS", "SERVIÇOS", "SERVICO", "SERVIÇO",
    "ADMINISTRACAO", "ADMINISTRAÇÃO",
    "GESTAO", "GESTÃO",

    # termos setoriais/genéricos que atrapalham match por emissor
    "SANEAMENTO", "AMBIENTAL", "AGUAS", "ÁGUAS", "AGUA", "ÁGUA",
    "ENERGIA", "ENERGETICA", "ENERGÉTICA",
    "CONCESSIONARIA", "CONCESSIONÁRIA",
    "REGIAO", "REGIÃO", "METROPOLITANA",
}


def normalize_company_for_match(value):
    """
    Normalização forte para matching de emissores:
    - remove acentos
    - padroniza S.A. / S/A / SA
    - remove pontuação
    - coloca tudo em caixa alta

    Importante: esta função não faz fuzzy amplo. Ela só normaliza detalhes pequenos.
    """
    s = clean_issuer_py(value or "")
    s = strip_accents_py(s).upper()

    # Padroniza formas societárias antes de remover pontuação.
    s = re.sub(r"\bS\s*/\s*A\b", " SA ", s)
    s = re.sub(r"\bS\.?\s*A\.?\b", " SA ", s)
    s = re.sub(r"\bC\.?I\.?A\.?\b", " CIA ", s)

    # Remove pontuação e normaliza espaços.
    s = re.sub(r"[^A-Z0-9]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()

    return s


def normalize_text_search(x):
    return clean_issuer_py(x).strip().lower()


def company_match_tokens(value):
    norm = normalize_company_for_match(value)

    if not norm:
        return []

    raw_tokens = norm.split()

    tokens = [
        t for t in raw_tokens
        if t not in COMPANY_MATCH_STOPWORDS and len(t) > 1
    ]

    # Fallback: se tudo foi removido como genérico, usa tokens originais
    # sem conectores/sufixos muito básicos.
    if not tokens:
        tokens = [
            t for t in raw_tokens
            if t not in {"DE", "DA", "DO", "DAS", "DOS", "E", "SA", "S", "A"} and len(t) > 1
        ]

    return tokens


def company_token_close(a, b):
    """
    Match de token conservador.
    Exato é aceito.
    Fuzzy só para tokens longos, mesma primeira letra e similaridade muito alta.
    Isso evita AEGEA bater com EZTEC, BTG bater com nomes aleatórios etc.
    """
    if not a or not b:
        return False

    if a == b:
        return True

    if len(a) < 5 or len(b) < 5:
        return False

    if a[0] != b[0]:
        return False

    ratio = difflib.SequenceMatcher(None, a, b).ratio()
    return ratio >= 0.92


def issuer_name_matches(query, issuer_name):
    """
    Matching mais restritivo para emissores.

    Exemplos que passam:
    - HAPVIDA PARTICIPACOES E INVESTIMENTOS S.A.
      x HAPVIDA PARTICIPACOES E INVESTIMENTOS S/A

    - AEGEA SANEAMENTO E PARTICIPACOES S/A
      x AEGEA SANEAMENTO E PARTICIPAÇÕES S/A

    - IGUA SANEAMENTO S.A
      x IGUA RIO DE JANEIRO

    - BRK AMBIENTAL PARTICIPACOES S/A
      x BRK AMBIENTAL - REGIAO METROPOLITANA DE MACEIO S.A.

    Exemplos que devem deixar de passar:
    - AEGEA x EZTEC
    - BTG x empresas sem token BTG real
    """
    q_norm = normalize_company_for_match(query)
    c_norm = normalize_company_for_match(issuer_name)

    if not q_norm or not c_norm:
        return False

    if q_norm == c_norm:
        return True

    q_tokens = company_match_tokens(query)
    c_tokens = company_match_tokens(issuer_name)

    if not q_tokens or not c_tokens:
        return False

    # Regra principal: o primeiro token relevante, normalmente a "marca",
    # precisa bater de forma exata ou quase exata.
    first_token_matches = any(company_token_close(q_tokens[0], ct) for ct in c_tokens)

    if not first_token_matches:
        return False

    matched_count = 0

    for qt in q_tokens:
        if any(company_token_close(qt, ct) for ct in c_tokens):
            matched_count += 1

    coverage = matched_count / len(q_tokens)

    # Query curta tipo "AEGEA", "IGUA", "BRK", "BTG":
    # exige token real no nome da empresa.
    if len(q_tokens) == 1:
        return matched_count == 1

    # Query com mais tokens:
    # exige cobertura alta, mas permite que termos genéricos/cidade/sufixos mudem.
    if coverage >= 0.75:
        return True

    if matched_count >= 2 and coverage >= 0.60:
        return True

    return False


def normalize_trade_record(record, fallback_date=None):
    """
    Estrutura real do historico-trades.json:
    {
      "YYYY-MM-DD": [
        {
          "Data negócio": "...",
          "Código IF": "...",
          "Quantidade negociada": "...",
          "Preço negócio": "...",
          "Volume financeiro (R$)": "...",
          "Taxa negócio": "...",
          "Cód. identificador do negócio": "...",
          "Código ISIN": "...",
          ...
        }
      ]
    }
    """
    code = (record.get("Código IF") or "").strip().upper()

    trade_date = (
        parse_iso_date(record.get("Data negócio"))
        or parse_iso_date(record.get("Data negócio__2"))
        or parse_iso_date(record.get("Data Negócio"))
        or parse_iso_date(record.get("tradeDate"))
        or parse_iso_date(record.get("Data negociação"))
        or parse_iso_date(fallback_date)
    )

    quantity = parse_int(record.get("Quantidade negociada"))
    price = parse_number(record.get("Preço negócio"))
    rate = parse_number(record.get("Taxa negócio"))
    financial_volume = parse_number(record.get("Volume financeiro (R$)"))

    if financial_volume is None and quantity is not None and price is not None:
        financial_volume = quantity * price

    isin = record.get("Código ISIN")
    cod_identificador = record.get("Cód. identificador do negócio")

    return {
        "code": code,
        "date": trade_date,
        "quantity": quantity,
        "price": price,
        "rate": rate,
        "financial_volume": financial_volume,
        "financial_volume_k": (financial_volume / 1000.0) if financial_volume is not None else None,
        "isin": (isin or "").strip() if isinstance(isin, str) else isin,
        "cod_identificador": (
            (cod_identificador or "").strip()
            if isinstance(cod_identificador, str)
            else cod_identificador
        ),
    }


def load_trades_history():
    """Carrega o historico-trades.json inteiro do GCS com cache em memória."""
    global _trades_cache, _trades_last_load_time

    if _trades_cache is not None and is_cache_valid(_trades_last_load_time):
        print("Usando trades do cache em memória...")
        return _trades_cache

    with _trades_cache_lock:
        # Double-check depois de adquirir o lock, para evitar downloads duplicados
        # se duas requests chegarem ao mesmo tempo.
        if _trades_cache is not None and is_cache_valid(_trades_last_load_time):
            print("Usando trades do cache em memória...")
            return _trades_cache

        now = time.time()
        client = storage.Client()
        bucket = client.bucket(GCS_BUCKET_NAME)
        blob_name = get_trades_blob_name()
        blob = bucket.blob(blob_name)

        print(f"Carregando trades de gs://{GCS_BUCKET_NAME}/{blob_name}...")
        t0 = time.time()
        text = blob.download_as_text(encoding="utf-8")
        print(
            f"Download trades concluído em {time.time() - t0:.1f}s | "
            f"tamanho={len(text)/1024/1024:.1f} MB"
        )

        t1 = time.time()
        historico = json.loads(text)
        print(f"JSON trades parseado em {time.time() - t1:.1f}s")

        if not isinstance(historico, dict):
            historico = {}

        _trades_cache = historico
        _trades_last_load_time = now

        return historico


def extract_trades_for_codes(codes):
    """
    Filtra o histórico de trades por um conjunto de tickers.
    O JSON é agrupado por data, não por ticker.
    """
    safe_codes = {str(c or "").strip().upper() for c in codes if str(c or "").strip()}
    if not safe_codes:
        return []

    t0 = time.time()
    historico = load_trades_history()
    print(f"load_trades_history levou {time.time() - t0:.1f}s")

    rows = []
    scanned = 0

    t1 = time.time()
    for date_key, day_rows in historico.items():
        if not isinstance(day_rows, list):
            continue

        for record in day_rows:
            scanned += 1
            if not isinstance(record, dict):
                continue

            code_if = (record.get("Código IF") or "").strip().upper()
            if code_if not in safe_codes:
                continue

            normalized = normalize_trade_record(record, fallback_date=date_key)
            if normalized["date"]:
                rows.append(normalized)

    print(
        f"Filtro trades levou {time.time() - t1:.1f}s | "
        f"registros_lidos={scanned:,} | encontrados={len(rows):,}"
    )
    print(f"Trades encontrados para {len(safe_codes)} ticker(s): {len(rows)}")
    return rows


def load_trades_for_code(code: str):
    safe_code = (code or "").strip().upper()
    if not safe_code:
        return []

    print(f"Filtrando trades para ticker {safe_code}...")
    return extract_trades_for_codes({safe_code})


def find_codes_for_issuer(issuer_query: str):
    """
    Retorna todos os tickers cujo emissor bate com a busca informada.

    IMPORTANTE:
    Esta função é usada pela aba B3 Trades quando a busca é por emissor.
    Por isso, ela foi mantida com a lógica antiga de substring,
    para não alterar o comportamento da consulta de trades.
    """
    q_norm = normalize_text_search(issuer_query)
    if not q_norm:
        return []

    hist = load_history()
    codes = set()

    for code, series in hist.items():
        for p in series:
            issuer_norm = normalize_text_search(p.get("issuer"))
            if issuer_norm and q_norm in issuer_norm:
                codes.add((code or "").strip().upper())
                break

    return sorted(c for c in codes if c)


def filter_recent_identified_trades(rows):
    """
    Mantém apenas os trades com cod identificador nos últimos N dias disponíveis
    e remove duplicatas por Cód. identificador do negócio.
    """
    dated_rows = [r for r in rows if r.get("date")]
    if not dated_rows:
        return [], None

    latest_date_str = max(r["date"] for r in dated_rows)
    latest_dt = date_cls.fromisoformat(latest_date_str)
    min_dt = latest_dt - timedelta(days=TRADES_LOOKBACK_DAYS - 1)

    filtered = []
    for row in dated_rows:
        dt = date_cls.fromisoformat(row["date"])
        cod_id = row.get("cod_identificador")
        cod_id_str = str(cod_id).strip() if cod_id is not None else ""
        if min_dt <= dt <= latest_dt and cod_id_str and cod_id_str != "-":
            filtered.append(row)

    filtered.sort(
        key=lambda x: (
            x.get("date") or "",
            x.get("code") or "",
            x.get("cod_identificador") or "",
            x.get("quantity") or 0,
        ),
        reverse=True,
    )

    deduped = []
    seen_cod_ids = set()
    for row in filtered:
        cod_id = row.get("cod_identificador")
        cod_id_str = str(cod_id).strip() if cod_id is not None else ""
        if not cod_id_str or cod_id_str in seen_cod_ids:
            continue
        seen_cod_ids.add(cod_id_str)
        deduped.append(row)

    return deduped, latest_date_str


def build_latest_rows(limit=None):
    """
    Monta linhas consolidadas para a última data disponível, por ticker/indexador.
    Usado nas tabelas Top 30 e na aba All Data.
    """
    hist = load_history()
    volume_map = load_volume_map()

    latest_date_str = None
    for series in hist.values():
        for p in series:
            d = p.get("date")
            if not d:
                continue
            if latest_date_str is None or d > latest_date_str:
                latest_date_str = d

    if latest_date_str is None:
        return [], None

    latest_dt = date_cls.fromisoformat(latest_date_str)
    target_prev_dt = latest_dt - timedelta(days=7)

    result_map = {}

    for code, series in hist.items():
        by_idx = {}
        for p in series:
            idx = p.get("index")
            dstr = p.get("date")
            if not idx or not dstr:
                continue
            by_idx.setdefault(idx, []).append(p)

        for idx, recs in by_idx.items():
            current_list = [r for r in recs if r.get("date") == latest_date_str]
            if not current_list:
                continue
            curr = current_list[0]

            prev = None
            best_days = None
            for r in recs:
                dstr = r.get("date")
                if not dstr or dstr == latest_date_str:
                    continue
                dt = date_cls.fromisoformat(dstr)
                if dt > latest_dt:
                    continue
                diff = abs((dt - target_prev_dt).days)
                if best_days is None or diff < best_days:
                    best_days = diff
                    prev = r

            taxa = curr.get("taxa_indicativa")
            brl = curr.get("brl_cents")
            prev_taxa = prev.get("taxa_indicativa") if prev else None
            prev_brl = prev.get("brl_cents") if prev else None

            wow_spread_bps = None
            if taxa is not None and prev_taxa is not None:
                try:
                    wow_spread_bps = (taxa - prev_taxa) * 100.0
                except TypeError:
                    wow_spread_bps = None

            wow_change_pct = None
            if brl is not None and prev_brl not in (None, 0):
                try:
                    wow_change_pct = (brl - prev_brl) / prev_brl * 100.0
                except Exception:
                    wow_change_pct = None

            result_map[(code, idx)] = {
                "code": code,
                "index": idx,
                "maturity_date": curr.get("maturity_date"),
                "issuer": clean_issuer_py(curr.get("issuer")),
                "taxa_indicativa": taxa,
                "brl_cents": brl,
                "wow_spread_bps": wow_spread_bps,
                "wow_change_pct": wow_change_pct,
                "volume_emissao": volume_map.get((code or "").upper(), ""),
            }

    rows = list(result_map.values())
    if limit is not None:
        return rows[:limit], latest_date_str
    return rows, latest_date_str


@app.route("/")
def index():
    return render_template("legacy_index.html")

@app.route("/dashboard", endpoint="page_dashboard")
def dashboard_page():
    return render_template("dashboard.html")


@app.route("/debentures", endpoint="page_debentures")
def debentures_page():
    return render_template("debentures.html")


@app.route("/trades-page", endpoint="page_trades")
def trades_page():
    return render_template("trades.html")


@app.route("/cvm-page", endpoint="page_cvm")
def cvm_page():
    return render_template("cvm.html")


@app.route("/data")
def data_route():
    code = request.args.get("code", "").strip()
    if not code:
        return jsonify([])

    hist = load_history()
    serie = hist.get(code, [])
    serie = sorted(serie, key=lambda x: x["date"])
    return jsonify(serie)


@app.route("/issuers")
def issuers_route():
    hist = load_history()

    issuers = set()

    for series in hist.values():
        for p in series:
            issuer = clean_issuer_py(p.get("issuer"))
            if issuer:
                issuers.add(issuer)

    return jsonify(sorted(issuers))


@app.route("/issuer-data")
def issuer_data_route():
    issuer_query = request.args.get("issuer", "").strip()
    if not issuer_query:
        return jsonify([])

    hist = load_history()

    q_norm = normalize_text_search(issuer_query)
    rows = []

    for code, series in hist.items():
        for p in series:
            issuer = p.get("issuer")
            issuer_norm = normalize_text_search(issuer)

            if not issuer_norm:
                continue

            # Busca flexível antiga: permite digitar parte do nome do emissor.
            # Mantida sem alteração para não afetar o Debenture Tracker.
            if q_norm not in issuer_norm:
                continue

            row = dict(p)
            row["code"] = code
            row["issuer"] = clean_issuer_py(issuer)
            rows.append(row)

    rows.sort(key=lambda x: ((x.get("code") or ""), (x.get("date") or "")))

    return jsonify(rows)


@app.route("/tables")
def tables():
    rows, latest_date_str = build_latest_rows()

    if latest_date_str is None:
        return jsonify(
            {
                "top_cdi_spread": [],
                "top_ipca_spread": [],
                "bottom_brl_cents": [],
                "reference_date": None,
            }
        )

    cdi_list = [
        v for v in rows
        if v["index"] == "CDI" and v["taxa_indicativa"] is not None
    ]
    ipca_list = [
        v for v in rows
        if v["index"] == "IPCA" and v["taxa_indicativa"] is not None
    ]
    brl_list = [
        v for v in rows
        if v["brl_cents"] is not None
    ]

    cdi_list.sort(key=lambda x: x["taxa_indicativa"], reverse=True)
    ipca_list.sort(key=lambda x: x["taxa_indicativa"], reverse=True)
    brl_list.sort(key=lambda x: x["brl_cents"])

    return jsonify(
        {
            "top_cdi_spread": cdi_list[:TABLE_LIMIT],
            "top_ipca_spread": ipca_list[:TABLE_LIMIT],
            "bottom_brl_cents": brl_list[:TABLE_LIMIT],
            "reference_date": latest_date_str,
        }
    )


@app.route("/all-data")
def all_data_route():
    index = request.args.get("index", "CDI").strip().upper()
    if index not in {"CDI", "IPCA"}:
        index = "CDI"

    rows, latest_date_str = build_latest_rows()

    filtered = [
        r for r in rows
        if r.get("index") == index
    ]

    filtered.sort(
        key=lambda x: (
            x.get("taxa_indicativa") is None,
            -(x.get("taxa_indicativa") or 0),
            (x.get("code") or "")
        )
    )

    return jsonify(
        {
            "rows": filtered,
            "reference_date": latest_date_str,
            "index": index,
        }
    )


@app.route("/trades")
def trades_route():
    code = request.args.get("code", "").strip().upper()
    issuer_query = request.args.get("issuer", "").strip()

    if not code and not issuer_query:
        return jsonify({"rows": [], "reference_date": None})

    try:
        if issuer_query:
            codes = find_codes_for_issuer(issuer_query)
            if not codes:
                print(f"Nenhuma debênture encontrada para emissor: {issuer_query}")
                return jsonify(
                    {
                        "rows": [],
                        "reference_date": None,
                        "issuer": issuer_query,
                        "codes": [],
                    }
                )

            rows = extract_trades_for_codes(codes)
            query_desc = f"emissor {issuer_query} ({len(codes)} ticker(s))"
        else:
            codes = [code]
            rows = load_trades_for_code(code)
            query_desc = f"ticker {code}"
    except Exception as e:
        print(f"Erro ao carregar trades para {issuer_query or code}: {e}")
        return jsonify({"rows": [], "reference_date": None, "error": str(e)}), 500

    if not rows:
        print(f"Nenhum trade encontrado para {query_desc}")
        return jsonify(
            {
                "rows": [],
                "reference_date": None,
                "issuer": issuer_query or None,
                "codes": codes,
            }
        )

    deduped, latest_date_str = filter_recent_identified_trades(rows)

    print(
        f"Trades retornados para {query_desc}: total_encontrado={len(rows)}, "
        f"deduplicados_ultimos_{TRADES_LOOKBACK_DAYS}_dias={len(deduped)}, "
        f"latest_date={latest_date_str}"
    )

    return jsonify(
        {
            "reference_date": latest_date_str,
            "rows": deduped,
            "issuer": issuer_query or None,
            "codes": codes,
        }
    )


@app.route("/warm-trades-cache")
def warm_trades_cache_route():
    """
    Endpoint chamado automaticamente quando o site abre.
    Ele força o carregamento do historico-trades.json para o cache em memória,
    sem executar nenhuma busca ainda.
    """
    try:
        t0 = time.time()
        historico = load_trades_history()

        total_dates = len(historico)
        total_rows = 0

        for day_rows in historico.values():
            if isinstance(day_rows, list):
                total_rows += len(day_rows)

        elapsed = time.time() - t0

        print(
            f"Warm cache trades concluído em {elapsed:.1f}s | "
            f"datas={total_dates:,} | registros={total_rows:,}"
        )

        return jsonify(
            {
                "ok": True,
                "elapsed_seconds": elapsed,
                "dates": total_dates,
                "rows": total_rows,
                "cached": True,
            }
        )

    except Exception as e:
        print(f"Erro ao aquecer cache de trades: {e}")
        return jsonify(
            {
                "ok": False,
                "error": str(e),
                "cached": False,
            }
        ), 500


# =============================
# CVM Filings - isolated logic
# =============================
def cvm_strip_accents(value):
    if value is None:
        return ""
    return "".join(
        ch for ch in unicodedata.normalize("NFKD", str(value))
        if not unicodedata.combining(ch)
    )


def cvm_norm(value):
    value = cvm_strip_accents(value or "").upper()
    value = value.replace("S/A", "SA").replace("S.A.", "SA").replace(" S.A", " SA")
    value = re.sub(r"[^A-Z0-9]+", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def cvm_only_digits(value):
    return re.sub(r"\D+", "", value or "")


def cvm_normalize_col(col):
    col = cvm_norm(col)
    col = re.sub(r"[^A-Z0-9]+", "_", col).strip("_")
    return col


def cvm_parse_date(value):
    if not value:
        return None

    value = str(value).strip()

    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d/%m/%y", "%Y/%m/%d"):
        try:
            return datetime.strptime(value[:10], fmt).date()
        except ValueError:
            pass

    return None


def cvm_first_present(row, candidates):
    for c in candidates:
        if c in row and str(row[c]).strip():
            return str(row[c]).strip()
    return ""


def cvm_normalize_row(raw):
    return {cvm_normalize_col(k): (v or "").strip() for k, v in raw.items()}


def cvm_detect_csv_from_zip(zip_bytes):
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        csv_names = [name for name in zf.namelist() if name.lower().endswith(".csv")]

        if not csv_names:
            raise RuntimeError("ZIP da CVM não contém CSV.")

        csv_names.sort(key=lambda n: zf.getinfo(n).file_size, reverse=True)
        name = csv_names[0]

        return name, zf.read(name)


def cvm_decode_csv(csv_bytes):
    for enc in ("utf-8-sig", "latin1", "cp1252"):
        try:
            return csv_bytes.decode(enc)
        except UnicodeDecodeError:
            continue

    return csv_bytes.decode("latin1", errors="replace")


def cvm_years_needed(days):
    today = date_cls.today()
    start = today - timedelta(days=days)
    years = list(range(start.year, today.year + 1))
    return sorted(set(years), reverse=True)


def cvm_read_year(year):
    url = CVM_IPE_ZIP_URL.format(year=year)

    print(f"Baixando CVM IPE {year}...")

    resp = requests.get(
        url,
        headers={"User-Agent": CVM_USER_AGENT},
        timeout=CVM_REQUEST_TIMEOUT,
    )

    if resp.status_code == 404:
        return [], f"Arquivo CVM IPE {year} ainda não disponível."

    resp.raise_for_status()

    csv_name, csv_bytes = cvm_detect_csv_from_zip(resp.content)
    text = cvm_decode_csv(csv_bytes)

    sample = text[:5000]

    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=";,\t,")
    except csv.Error:
        dialect = csv.excel
        dialect.delimiter = ";"

    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    rows = [cvm_normalize_row(r) for r in reader]

    print(f"CVM IPE {year}: {len(rows):,} linhas carregadas de {csv_name}")

    return rows, None


def cvm_load_source_rows(days=CVM_LAST_DAYS, force=False):
    now = time.time()

    if (
        not force
        and _cvm_cache["rows"]
        and is_cache_valid(float(_cvm_cache["loaded_at"]))
    ):
        print("Usando CVM IPE do cache em memória...")
        return _cvm_cache["rows"], _cvm_cache["errors"], _cvm_cache["source_years"], _cvm_cache["loaded_at"]

    with _cvm_cache_lock:
        if (
            not force
            and _cvm_cache["rows"]
            and is_cache_valid(float(_cvm_cache["loaded_at"]))
        ):
            print("Usando CVM IPE do cache em memória...")
            return _cvm_cache["rows"], _cvm_cache["errors"], _cvm_cache["source_years"], _cvm_cache["loaded_at"]

        all_rows = []
        errors = []
        years = cvm_years_needed(max(days, 1))

        for year in years:
            try:
                rows, warning = cvm_read_year(year)
                all_rows.extend(rows)
                if warning:
                    errors.append(warning)
            except Exception as exc:
                err = f"Erro ao carregar CVM IPE {year}: {exc}"
                print(err)
                errors.append(err)

        _cvm_cache.update({
            "loaded_at": now,
            "rows": all_rows,
            "errors": errors,
            "source_years": years,
        })

        return all_rows, errors, years, now


# FIX: inclui os nomes reais das colunas do IPE da CVM, como DT_RECEB e DENOM_CIA.
def cvm_delivery_date(row):
    value = cvm_first_present(row, [
        "DT_RECEB",
        "DATA_RECEB",
        "DATA_RECEBIMENTO",
        "DT_RECEBIMENTO",
        "DATA_ENTREGA",
        "DT_ENTREGA",
        "DATA_ENVIO",
        "DT_ENVIO",
        "DATA_RECEBIMENTO_DOCUMENTO",
        "DT_RECEBIMENTO_DOCUMENTO",
    ])

    return cvm_parse_date(value)


# FIX: inclui DENOM_CIA, nome real usual no dataset IPE.
def cvm_company_name(row):
    return cvm_first_present(row, [
        "DENOM_CIA",
        "DENOM_SOCIAL",
        "NOME_COMPANHIA",
        "NOME_EMPRESARIAL",
        "EMPRESA",
    ])


def cvm_company_name_proper(value):
    if not value:
        return ""

    s = " ".join(str(value).strip().split())
    if not s:
        return ""

    lower_words = {
        "e", "de", "da", "do", "das", "dos", "di", "du", "del",
        "la", "le", "van", "von", "y"
    }

    upper_words = {
        "S.A.", "S/A", "SA", "S.A", "S", "A", "CVM", "B3"
    }

    parts = s.split(" ")
    result = []

    for i, word in enumerate(parts):
        raw = word.strip()
        if not raw:
            continue

        letters_only = re.sub(r"[^A-Za-zÀ-ÿ]", "", raw)
        upper_raw = raw.upper()
        upper_letters = letters_only.upper()

        if upper_raw in upper_words or upper_letters in {"SA", "CVM", "B3"}:
            if upper_letters == "SA":
                result.append("S.A.")
            else:
                result.append(upper_raw)
            continue

        # Preserve tokens that are mostly numbers/codes.
        if letters_only == "":
            result.append(raw)
            continue

        lower_raw = raw.lower()

        if i > 0 and lower_raw in lower_words:
            result.append(lower_raw)
            continue

        # Title-case each hyphen-separated chunk.
        hyphen_parts = raw.split("-")
        hyphen_out = []
        for hp in hyphen_parts:
            hp_clean = hp.lower()
            if not hp_clean:
                hyphen_out.append(hp)
            else:
                hyphen_out.append(hp_clean[:1].upper() + hp_clean[1:])
        result.append("-".join(hyphen_out))

    return " ".join(result)


# FIX: inclui CNPJ_CIA e CD_CVM, nomes reais usuais no dataset IPE.
def cvm_company_fields(row):
    return [
        cvm_company_name(row),
        cvm_first_present(row, ["CNPJ_CIA", "CNPJ_COMPANHIA", "CNPJ"]),
        cvm_first_present(row, ["CD_CVM", "CODIGO_CVM", "COD_CVM"]),
    ]


def cvm_match_score(query, company_name):
    q = cvm_norm(query)
    c = cvm_norm(company_name)

    if not q or not c:
        return 0

    if q == c:
        return 100

    # Usa a mesma lógica conservadora do ANBIMA/B3.
    if issuer_name_matches(query, company_name):
        q_tokens = company_match_tokens(query)
        c_tokens = company_match_tokens(company_name)

        if not q_tokens or not c_tokens:
            return 0

        matched_count = 0

        for qt in q_tokens:
            if any(company_token_close(qt, ct) for ct in c_tokens):
                matched_count += 1

        coverage = matched_count / len(q_tokens)

        # Se a string normalizada inteira aparece, é um match muito forte.
        if q in c or c in q:
            return 95

        # Match por tokens relevantes.
        return max(80, int(75 + coverage * 20))

    return 0


def cvm_find_matching_companies(source_rows, issuer_query, max_matches=5):
    """
    Acha nomes CVM próximos ao emissor pesquisado.
    Esta lógica é separada do issuer-list da ANBIMA/B3.
    """
    q_digits = cvm_only_digits(issuer_query)

    candidates = {}

    for row in source_rows:
        company = cvm_company_name(row)
        if not company:
            continue

        cnpj = cvm_first_present(row, ["CNPJ_CIA", "CNPJ_COMPANHIA", "CNPJ"])
        cvm_code = cvm_first_present(row, ["CD_CVM", "CODIGO_CVM", "COD_CVM"])

        company_digits = cvm_only_digits(" ".join([company, cnpj, cvm_code]))

        if q_digits and q_digits in company_digits:
            score = 100
        else:
            score = cvm_match_score(issuer_query, company)

        key = cvm_norm(company)

        if score >= 80:
            prev = candidates.get(key)
            if prev is None or score > prev["score"]:
                candidates[key] = {
                    "company": company,
                    "score": score,
                    "cnpj": cnpj,
                    "cvm_code": cvm_code,
                }

    matches = sorted(candidates.values(), key=lambda x: x["score"], reverse=True)

    return matches[:max_matches]


def cvm_row_matches_company(row, matched_companies):
    if not matched_companies:
        return False

    row_company_norm = cvm_norm(cvm_company_name(row))
    matched_norms = {cvm_norm(m["company"]) for m in matched_companies}

    return row_company_norm in matched_norms


def cvm_extract_sequence_from_text(value):
    if not value:
        return ""

    value = str(value)

    patterns = [
        r"numProtocolo=(\d+)",
        r"numSequencia=(\d+)",
        r"NumeroProtocoloEntrega=(\d+)",
        r"NumeroSequencialDocumento=(\d+)",
    ]

    for pattern in patterns:
        m = re.search(pattern, value, flags=re.IGNORECASE)
        if m:
            return m.group(1)

    return ""


# FIX: inclui LINK_DOC, nome real usual do dataset IPE.
def cvm_existing_download_link(row):
    return cvm_first_present(row, [
        "LINK_DOC",
        "LINK_DOCUMENTO",
        "LINK_DOWNLOAD",
        "LINK_DOC_DOWNLOAD",
        "URL_DOCUMENTO",
        "URL",
    ])


def cvm_online_sequence_number(row):
    download_link = cvm_existing_download_link(row)
    seq = cvm_extract_sequence_from_text(download_link)

    if seq:
        return seq

    seq = cvm_first_present(row, [
        "NUMERO_SEQUENCIAL_DOCUMENTO",
        "NUM_SEQUENCIAL_DOCUMENTO",
        "NUM_SEQUENCIAL",
        "NUM_SEQUENCIA",
        "NUM_SEQ_DOCUMENTO",
        "ID_DOCUMENTO",
        "ID_DOC",
        "ID_DOC_RAD",
        "NUM_DOC",
    ])

    seq_digits = cvm_only_digits(seq)

    if seq_digits:
        return seq_digits

    return ""


# FIX: se LINK_DOC já vier como URL completo, usa ele direto.
def cvm_online_link(row):
    existing = cvm_existing_download_link(row)

    if existing and existing.lower().startswith("http"):
        return existing

    seq = cvm_online_sequence_number(row)

    if not seq:
        return ""

    return (
        "https://www.rad.cvm.gov.br/ENET/"
        f"frmExibirArquivoIPEExterno.aspx?NumeroProtocoloEntrega={seq}"
    )


def cvm_download_link(row):
    existing = cvm_existing_download_link(row)

    if existing:
        return existing

    seq = cvm_online_sequence_number(row)

    if seq:
        return (
            "https://www.rad.cvm.gov.br/ENET/"
            f"frmDownloadDocumento.aspx?Tela=ext&numSequencia={seq}"
        )

    return ""


# FIX: inclui os nomes reais de colunas do dataset IPE: CATEG_DOC, TIPO_DOC, ESPECIE_DOC, DT_REFER, SIT_DOC.
def cvm_transform_row(row):
    delivered = cvm_delivery_date(row)

    return {
        "delivery_date": delivered.isoformat() if delivered else "",
        "company": cvm_company_name_proper(cvm_company_name(row)),
        "cnpj": cvm_first_present(row, ["CNPJ_CIA", "CNPJ_COMPANHIA", "CNPJ"]),
        "cvm_code": cvm_first_present(row, ["CD_CVM", "CODIGO_CVM", "COD_CVM"]),
        "category": cvm_first_present(row, ["CATEG_DOC", "CATEGORIA", "CATEGORIA_DOCUMENTO"]),
        "type": cvm_first_present(row, ["TIPO_DOC", "TIPO", "TIPO_DOCUMENTO"]),
        "species": cvm_first_present(row, ["ESPECIE_DOC", "ESPECIE"]),
        "reference_date": cvm_first_present(row, ["DT_REFER", "DATA_REFERENCIA", "DT_REFERENCIA", "DATA_REF", "DT_REF"]),
        "status": cvm_first_present(row, ["SIT_DOC", "STATUS", "SITUACAO", "SITUACAO_DOCUMENTO"]),
        "modality": cvm_first_present(row, ["MODALIDADE", "TIPO_ENTREGA"]),
        "subject": cvm_first_present(row, ["ASSUNTO", "DESCRICAO", "OBSERVACAO"]),
        "version": cvm_first_present(row, ["VERSAO", "V"]),
        "online_link": cvm_online_link(row),
        "download_link": cvm_download_link(row),
    }



def cvm_strip_html(value):
    if value is None:
        return ""
    text = str(value)
    text = re.sub(r"<br\s*/?>", " ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    text = text.replace("&quot;", '"').replace("&#39;", "'").replace("&#039;", "'")
    return re.sub(r"\s+", " ", text).strip()


def cvm_parse_delivery_datetime(value):
    s = cvm_strip_html(value)
    if not s:
        return None
    for fmt in ("%d/%m/%Y %H:%M", "%d/%m/%Y %H:%M:%S", "%d/%m/%Y", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s[:len(datetime.now().strftime(fmt))], fmt)
        except Exception:
            pass
    d = cvm_parse_date(s)
    return datetime.combine(d, datetime.min.time()) if d else None


def cvm_extract_rows_from_enet_response(obj):
    if obj is None:
        return []
    if isinstance(obj, str):
        txt = obj.strip()
        if not txt:
            return []
        try:
            return cvm_extract_rows_from_enet_response(json.loads(txt))
        except Exception:
            pass
        trs = re.findall(r"<tr[^>]*>(.*?)</tr>", txt, flags=re.I | re.S)
        out = []
        for tr in trs:
            cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, flags=re.I | re.S)
            if len(cells) >= 7:
                out.append(cells)
        return out
    if isinstance(obj, dict):
        if "d" in obj:
            return cvm_extract_rows_from_enet_response(obj["d"])
        for k in ("aaData", "data", "rows", "lista", "documentos", "lstDocumentos", "Table"):
            if k in obj:
                rows = cvm_extract_rows_from_enet_response(obj[k])
                if rows:
                    return rows
        for v in obj.values():
            rows = cvm_extract_rows_from_enet_response(v)
            if rows:
                return rows
        return []
    if isinstance(obj, list):
        if not obj:
            return []
        if all(isinstance(x, (str, int, float, type(None))) for x in obj):
            return [obj]
        out = []
        for item in obj:
            if isinstance(item, dict):
                out.append(item)
            else:
                out.extend(cvm_extract_rows_from_enet_response(item))
        return out
    return []


def cvm_enet_payloads(search_term, start_date, end_date):
    de = start_date.strftime("%d/%m/%Y")
    ate = end_date.strftime("%d/%m/%Y")
    base = {
        "dataDe": de,
        "dataAte": ate,
        "empresa": search_term or "",
        "setorAtividade": "-1",
        "categoriaEmissor": "-1",
        "situacaoEmissor": "-1",
        "tipoParticipante": "-1",
        "dataReferencia": "",
        "categoria": "",
        "periodo": "0",
        "horaIni": "",
        "horaFim": "",
        "palavraChave": "",
        "ultimaDtRef": "false",
        "tipoEmpresa": "0",
        "iDisplayStart": 0,
        "iDisplayLength": 1000,
        "sEcho": 1,
    }
    p1 = dict(base)
    p1["categoria"] = "EST_3"
    p1["periodo"] = "1"
    p2 = dict(base)
    p2["dataEntregaDe"] = de
    p2["dataEntregaAte"] = ate
    return [base, p1, p2]


def cvm_company_search_terms(issuer, matched_companies=None):
    terms = []
    def add(x):
        x = str(x or "").strip()
        if x and x not in terms:
            terms.append(x)
    add(issuer)
    for m in matched_companies or []:
        add(m.get("company"))
        code_digits = cvm_only_digits(m.get("cvm_code") or "")
        if code_digits:
            add(code_digits)
            if len(code_digits) >= 2:
                add(code_digits[:-1].zfill(5) + "-" + code_digits[-1])
    if "" not in terms:
        terms.append("")
    return terms

def cvm_enet_row_to_final(row, issuer_query):
    if isinstance(row, dict):
        norm = cvm_normalize_row(row)
        company = cvm_first_present(norm, ["EMPRESA", "DENOM_CIA", "DENOM_SOCIAL", "NOME_COMPANHIA"])
        delivery = cvm_first_present(norm, ["DATA_ENTREGA", "DT_ENTREGA", "DATA_RECEB", "DT_RECEB", "DATA_RECEBIMENTO", "DT_RECEBIMENTO"])
        category = cvm_first_present(norm, ["CATEGORIA", "CATEG_DOC", "CATEGORIA_DOCUMENTO"])
        typ = cvm_first_present(norm, ["TIPO", "TIPO_DOC", "TIPO_DOCUMENTO"])
        species = cvm_first_present(norm, ["ESPECIE", "ESPECIE_DOC"])
        ref_date = cvm_first_present(norm, ["DATA_REFERENCIA", "DT_REFERENCIA", "DT_REFER", "DATA_REF"])
        status = cvm_first_present(norm, ["STATUS", "SIT_DOC", "SITUACAO"])
        modality = cvm_first_present(norm, ["MODALIDADE", "TIPO_ENTREGA"])
        subject = cvm_first_present(norm, ["ASSUNTO", "DESCRICAO", "OBSERVACAO"])
        cnpj = cvm_first_present(norm, ["CNPJ_CIA", "CNPJ_COMPANHIA", "CNPJ"])
        cvm_code = cvm_first_present(norm, ["CD_CVM", "CODIGO_CVM", "COD_CVM", "CODIGO"])
        online_link = cvm_online_link(norm)
    else:
        cells = [cvm_strip_html(x) for x in row] + [""] * 12
        cvm_code, company, category, typ, species, ref_date, delivery, status, version, modality, actions, subject = cells[:12]
        cnpj = ""
        raw_join = " ".join(str(x) for x in row)
        online_link = ""
        m = re.search(r"href=[\"']([^\"']+)[\"']", raw_join, flags=re.I)
        if m:
            href = m.group(1).replace("&amp;", "&")
            online_link = href if href.lower().startswith("http") else "https://www.rad.cvm.gov.br/ENET/" + href.lstrip("/")
        if not online_link:
            seq = cvm_extract_sequence_from_text(raw_join)
            if seq:
                online_link = "https://www.rad.cvm.gov.br/ENET/frmExibirArquivoIPEExterno.aspx?NumeroProtocoloEntrega=" + seq
    delivered_dt = cvm_parse_delivery_datetime(delivery)
    if not company or not delivered_dt:
        return None
    if not issuer_name_matches(issuer_query, company) and cvm_norm(issuer_query) not in cvm_norm(company):
        return None
    return {
        "delivery_date": delivered_dt.date().isoformat(),
        "company": cvm_company_name_proper(company),
        "cnpj": cnpj,
        "cvm_code": cvm_code,
        "category": category,
        "type": typ,
        "species": species,
        "reference_date": ref_date,
        "status": status,
        "modality": modality,
        "subject": subject,
        "version": "",
        "online_link": online_link,
        "download_link": online_link,
        "source": "ENET live",
    }


def cvm_fetch_enet_live_filings(issuer, days, matched_companies=None):
    if not CVM_ENET_LIVE_FALLBACK:
        return [], []
    end_date = date_cls.today()
    start_date = end_date - timedelta(days=max(days, 1))
    endpoints = [
        CVM_ENET_BASE_URL + "/ListarDocumentos",
        CVM_ENET_BASE_URL + "/ConsultarDocumentos",
        CVM_ENET_BASE_URL + "/PesquisarDocumentos",
    ]
    errors = []
    search_terms = cvm_company_search_terms(issuer, matched_companies)
    session = requests.Session()
    session.headers.update({
        "User-Agent": CVM_USER_AGENT,
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Content-Type": "application/json; charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": CVM_ENET_BASE_URL,
    })
    try:
        session.get(CVM_ENET_BASE_URL, timeout=CVM_ENET_TIMEOUT)
    except Exception as exc:
        errors.append(f"ENET página inicial falhou: {exc}")
    for endpoint in endpoints:
        for search_term in search_terms:
            for payload in cvm_enet_payloads(search_term, start_date, end_date):
                try:
                    resp = session.post(endpoint, data=json.dumps(payload), timeout=CVM_ENET_TIMEOUT)
                    if resp.status_code >= 400:
                        errors.append(f"ENET {endpoint.rsplit('/', 1)[-1]} HTTP {resp.status_code}")
                        continue
                    try:
                        obj = resp.json()
                    except Exception:
                        obj = resp.text
                    final_rows = []
                    for raw in cvm_extract_rows_from_enet_response(obj):
                        final = cvm_enet_row_to_final(raw, issuer)
                        if final:
                            final_rows.append(final)
                    if final_rows:
                        print(f"ENET live retornou {len(final_rows)} linha(s) para {issuer} via {endpoint} | termo={search_term or '[lista geral]'}")
                        return final_rows, errors
                except Exception as exc:
                    errors.append(f"ENET {endpoint.rsplit('/', 1)[-1]} falhou: {exc}")
    return [], errors


def cvm_merge_final_rows(rows):
    dedup = {}
    for r in rows:
        key = (r.get("delivery_date") or "", cvm_norm(r.get("company") or ""), cvm_norm(r.get("category") or ""), cvm_norm(r.get("type") or ""), cvm_norm(r.get("species") or ""), cvm_norm(r.get("subject") or ""), r.get("reference_date") or "")
        if key not in dedup or (not dedup[key].get("online_link") and r.get("online_link")):
            dedup[key] = r
    out = list(dedup.values())
    out.sort(key=lambda r: (r.get("delivery_date") or "", r.get("company") or ""), reverse=True)
    return out


@app.route("/cvm-company-list")
def cvm_company_list_route():
    try:
        source_rows, errors, years, loaded_at = cvm_load_source_rows(days=CVM_LAST_DAYS, force=False)
        companies = sorted({cvm_company_name(row) for row in source_rows if cvm_company_name(row)})
        return jsonify(companies)
    except Exception as e:
        print(f"Erro ao carregar lista de companhias CVM: {e}")
        return jsonify([])


@app.route("/cvm-filings")
def cvm_filings_route():
    issuer = request.args.get("issuer", "").strip()
    days = int(request.args.get("days", str(CVM_LAST_DAYS)))
    force = request.args.get("force", "0").lower() in ("1", "true", "yes")

    # FIX: Refresh CVM limpa explicitamente o cache antes de baixar novamente.
    if force:
        _cvm_cache.update({
            "loaded_at": 0,
            "rows": [],
            "errors": [],
            "source_years": [],
        })

    if not issuer:
        return jsonify({
            "rows": [],
            "issuer": "",
            "matched_companies": [],
            "days": days,
            "errors": ["Missing issuer"],
            "source_years": [],
            "loaded_at": None,
        })

    try:
        source_rows, errors, years, loaded_at = cvm_load_source_rows(days=days, force=force)
        matched_companies = cvm_find_matching_companies(source_rows, issuer)

        start_date = date_cls.today() - timedelta(days=days)
        filtered = []

        for row in source_rows:
            delivered = cvm_delivery_date(row)

            if not delivered or delivered < start_date:
                continue

            if not cvm_row_matches_company(row, matched_companies):
                continue

            filtered.append(cvm_transform_row(row))

        live_rows, live_errors = cvm_fetch_enet_live_filings(issuer, days, matched_companies)
        if live_errors:
            errors.extend(live_errors[:5])

        filtered = cvm_merge_final_rows(filtered + live_rows)

        return jsonify({
            "rows": filtered,
            "issuer": issuer,
            "matched_companies": matched_companies,
            "days": days,
            "errors": errors,
            "source_years": years,
            "loaded_at": datetime.fromtimestamp(float(loaded_at)).isoformat(timespec="seconds") if loaded_at else None,
        })

    except Exception as e:
        print(f"Erro ao carregar CVM filings para {issuer}: {e}")
        return jsonify({
            "rows": [],
            "issuer": issuer,
            "matched_companies": [],
            "days": days,
            "errors": [str(e)],
            "source_years": [],
            "loaded_at": None,
        }), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
