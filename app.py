from flask import Flask

from services import app_service as legacy

app = Flask(__name__)


@app.route("/")
def index():
    return legacy.index()


@app.route("/data")
def data_route():
    return legacy.data_route()


@app.route("/issuers")
def issuers_route():
    return legacy.issuers_route()


@app.route("/issuer_data")
@app.route("/issuer-data")
def issuer_data_route():
    return legacy.issuer_data_route()


@app.route("/tables")
def tables():
    return legacy.tables()


@app.route("/all_data")
def all_data_route():
    return legacy.all_data_route()




# Backward-compatible kebab-case aliases used by existing clients/load balancers.
@app.route("/all-data")
def all_data_route_alias():
    return legacy.all_data_route()


@app.route("/warm-trades-cache")
def warm_trades_cache_route_alias():
    return legacy.warm_trades_cache_route()

@app.route("/trades")
def trades_route():
    return legacy.trades_route()


@app.route("/warm_trades_cache")
def warm_trades_cache_route():
    return legacy.warm_trades_cache_route()


@app.route("/cvm/company-list")
def cvm_company_list_route():
    return legacy.cvm_company_list_route()


@app.route("/cvm/filings")
def cvm_filings_route():
    return legacy.cvm_filings_route()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int("8080"), debug=False)
