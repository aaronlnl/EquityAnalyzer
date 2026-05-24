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
    # symbols_to_fetch = list(symbol2cik.keys())

    results = []
    print(f"Fetching data for {len(symbols_to_fetch)} symbols...")
    
    for symbol in symbols_to_fetch:
        try:
            eps_data = parse_data.get_eps(symbol) or {}
            market_data = parse_data.get_stock_market_reaction(symbol) or {}
            guidance_data = parse_data.compare_forward_guidance(symbol) or {}
            
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
                "qtr_eps_surprise": safe_float(qtr_eps),
                "qtr_rev_surprise": safe_float(qtr_rev),
                "year_eps_surprise": safe_float(year_eps),
                "year_rev_surprise": safe_float(year_rev)
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
    return jsonify(data)

@app.route('/ticker/<symbol>')
def ticker_profile(symbol):
    return render_template('ticker.html', symbol=symbol)

@app.route('/api/ticker/<symbol>')
def api_ticker(symbol):
    eps_data = parse_data.get_eps(symbol)
    market_data = parse_data.get_stock_market_reaction(symbol)
    guidance_data = parse_data.compare_forward_guidance(symbol)
    _, report_date, timing = parse_data.get_latest_report_date_with_timing(symbol)
    
    return jsonify({
        "symbol": symbol,
        "report_date": str(report_date) if report_date else "Unknown",
        "timing": timing or "Unknown",
        "eps_data": eps_data,
        "market_data": market_data,
        "guidance_data": guidance_data
    })

if __name__ == '__main__':
    app.run(debug=True, port=5000)