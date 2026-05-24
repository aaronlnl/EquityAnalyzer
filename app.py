from flask import Flask, render_template, jsonify
import parse_data

app = Flask(__name__)

data_cache = []

def safe_float(val, default=0.0):
    """Safely converts potential None/null or corrupted strings to clean numeric values."""
    if val is None:
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default

def fetch_screener_data():
    global data_cache
    if data_cache:
        return data_cache

    symbol2cik, _ = parse_data.get_sp500()
    
    # CURRENT: Prototype layout bounded to 10 stocks
    symbols_to_fetch = ["NVDA", "MSFT", "AAPL", "AMZN", "META", "AVGO", "GOOGL", "GOOG", "BRK-B", "TSLA"]
    
    # PRODUCTION: Uncomment to deploy all 500+ stocks
    symbols_to_fetch = list(symbol2cik.keys())

    results = []
    print(f"Fetching data for {len(symbols_to_fetch)} symbols...")
    
    for symbol in symbols_to_fetch:
        try:
            # 1. Leverage unified, event-driven JSON caching layer
            financial_data = parse_data.get_unified_financial_data(symbol) or {}
            
            # Extract unpacked financial data dictionaries safely from the cache entry
            eps_data = financial_data.get("actuals", {}).get("eps") or {}
            market_data = financial_data.get("market_reaction") or {}
            guidance_data = financial_data.get("guidance_comparison") or {}
            
            # Extract nested forward tracking values safely
            qtr_metrics = guidance_data.get("quarter", {})
            year_metrics = guidance_data.get("year", {})
            
            qtr_eps = qtr_metrics.get("EPS", {}).get("surprise_percent", None)
            qtr_rev = qtr_metrics.get("Revenue", {}).get("surprise_percent", None)
            year_eps = year_metrics.get("EPS", {}).get("surprise_percent", None)
            year_rev = year_metrics.get("Revenue", {}).get("surprise_percent", None)
            
            results.append({
                "symbol": symbol,
                "sue": safe_float(eps_data.get("sue")),
                "eps_surprise_percent": safe_float(eps_data.get("surprise_percent")),
                "close_to_open_car": safe_float(market_data.get("close_to_open_car")),
                "open_to_close_car": safe_float(market_data.get("open_to_close_car")),
                "qtr_eps_surprise": float(qtr_eps) if qtr_eps is not None else None,
                "qtr_rev_surprise": float(qtr_rev) if qtr_rev is not None else None,
                "year_eps_surprise": float(year_eps) if year_eps is not None else None,
                "year_rev_surprise": float(year_rev) if year_rev is not None else None
            })
        except Exception as e:
            print(f"Error processing {symbol}: {e}")
            
    data_cache = results
    return results

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/matrix')
def api_matrix():
    data = fetch_screener_data()
    
    season_meta = parse_data.get_latest_earnings_season()
    active_season = f"{season_meta['year']}Q{season_meta['quarter']}"
        
    return jsonify({
        "season": active_season,
        "matrix": data
    })

@app.route('/ticker/<symbol>')
def ticker_profile(symbol):
    return render_template('ticker.html', symbol=symbol)

@app.route('/api/ticker/<symbol>')
@app.route('/api/ticker/<symbol>')
def api_ticker(symbol):
    financial_data = parse_data.get_unified_financial_data(symbol) or {}
    
    return jsonify({
        "symbol": symbol,
        "report_date": financial_data.get("report_date") or "Unknown",
        "timing": financial_data.get("release_timing") or "Unknown",
        "eps_data": financial_data.get("actuals", {}).get("eps") or {},
        "revenue_data": financial_data.get("actuals", {}).get("revenue") or {},
        "market_data": financial_data.get("market_reaction") or {},
        "guidance_data": financial_data.get("guidance_comparison") or {}
    })

if __name__ == '__main__':
    app.run(debug=True, port=5000)