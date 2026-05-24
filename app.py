from flask import Flask, render_template, jsonify
import parse_data

app = Flask(__name__)

# Simple in-memory cache to prevent re-fetching data on every page reload
data_cache = []

def fetch_screener_data():
    global data_cache
    if data_cache:
        return data_cache

    symbol2cik, _ = parse_data.get_sp500()
    
    # ---------------------------------------------------------
    # CURRENT: Fetching only 10 stocks for prototype speed
    # ---------------------------------------------------------
    symbols_to_fetch = ["NVDA", "MSFT", "AAPL", "AMZN", "META", "AVGO", "GOOGL", "GOOG", "BRK-B", "TSLA"]
    
    # ---------------------------------------------------------
    # PRODUCTION: Uncomment the line below to run all 500+ stocks
    # ---------------------------------------------------------
    # symbols_to_fetch = list(symbol2cik.keys())

    results = []
    print(f"Fetching data for {len(symbols_to_fetch)} symbols...")
    
    for symbol in symbols_to_fetch:
        print(f"Processing {symbol}...")
        try:
            eps_data = parse_data.get_eps(symbol)
            market_data = parse_data.get_stock_market_reaction(symbol)
            guidance_data = parse_data.compare_forward_guidance(symbol)
            
            # Safely extract metrics
            sue = eps_data.get("sue") if eps_data else None
            car = market_data.get("close_to_open_car") if market_data else None
            
            # Extract Q1 EPS guidance surprise % if available
            q_guidance_surprise = None
            if guidance_data and "quarter" in guidance_data and "EPS" in guidance_data["quarter"]:
                q_guidance_surprise = guidance_data["quarter"]["EPS"].get("surprise_percent")
            
            # Only include stocks that actually have data
            if sue is not None and car is not None:
                results.append({
                    "symbol": symbol,
                    "sue": sue,
                    "car": car,
                    "guidance_surprise": q_guidance_surprise or 0  # Default to 0 if no guidance
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
    # Debug mode ensures the server restarts if you change code
    app.run(debug=True, port=5000)