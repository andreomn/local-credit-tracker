import os

GCS_BUCKET_NAME = os.environ.get("GCS_BUCKET_NAME", "debentures-anbima-am")
GCS_PREFIX = os.environ.get("GCS_PREFIX", "anbima_debentures/")
B3_INFO_PREFIX = os.environ.get("B3_INFO_PREFIX", "b3_infos/")
B3_INFO_FILENAME = os.environ.get("B3_INFO_FILENAME", "Debentures.csv")

B3_TRADES_PREFIX = os.environ.get("B3_TRADES_PREFIX", "b3_trades/")
B3_TRADES_FILENAME = os.environ.get("B3_TRADES_FILENAME", "historico-trades.json")

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

CVM_ENET_BASE_URL = "https://www.rad.cvm.gov.br/ENET/frmConsultaExternaCVM.aspx"
CVM_ENET_TIMEOUT = int(os.environ.get("CVM_ENET_TIMEOUT", "45"))
CVM_ENET_LIVE_FALLBACK = os.environ.get("CVM_ENET_LIVE_FALLBACK", "1").lower() not in ("0", "false", "no")
