from datetime import date, datetime, timedelta
from io import StringIO
from zoneinfo import ZoneInfo
import json
import os
import re
import requests
import statistics

from bs4 import BeautifulSoup
from dotenv import load_dotenv
from google import genai
from markdownify import markdownify as md
import pandas as pd
from pydantic import BaseModel, Field
from typing import Optional, Union
import yfinance as yf


load_dotenv()

ALPHAVANTAGE_API_KEY = os.getenv("ALPHAVANTAGE_API_KEY")
EMAIL = os.getenv("EMAIL")

def get_latest_earnings_season() -> dict[str, Union[int, date]]:
    """
    Calculates the latest active/most recent calendar earnings season based on today's date,
    and returns the corresponding 'end date' boundaries for fiscal calendarization.
    
    Returns:
        dict: A dictionary containing:
            - "year" (int): Year of current earnings season
            - "quarter" (int): Current earnings season (calendar quarter)
            - "start" (date): Earliest end date of financial quarter
            - "end" (date): Latest end date of financial quarter
    """
    today = date.today()
    current_year = today.year
    current_month = today.month
    
    # Determine which Calendar Quarter's earnings season we are currently inside or just finished.    
    if 1 <= current_month <= 3:
        target_quarter = 4
        target_year = current_year - 1
    elif 4 <= current_month <= 6:
        target_quarter = 1
        target_year = current_year
    elif 7 <= current_month <= 9:
        target_quarter = 2
        target_year = current_year
    else:
        target_quarter = 3
        target_year = current_year

    # The standard calendar quarter end dates are:
    # Q1: March 31  |  Q2: June 30  |  Q3: September 30  |  Q4: December 31
    # Allow a +/- 45 day window around those exact dates to catch off-cycle fiscal years.
    
    if target_quarter == 1:
        # Base date: March 31
        start_bound = date(target_year, 2, 15)   # 45 days before March 31
        end_bound = date(target_year, 5, 14)     # 44 days after March 31
    elif target_quarter == 2:
        # Base date: June 30
        start_bound = date(target_year, 5, 15)   # 46 days before June 30
        end_bound = date(target_year, 8, 14)     # 45 days after June 30
    elif target_quarter == 3:
        # Base date: September 30
        start_bound = date(target_year, 8, 15)   # 46 days before Sept 30
        end_bound = date(target_year, 11, 14)    # 45 days after Sept 30
    else:
        # Base date: December 31
        start_bound = date(target_year, 11, 15)  # 46 days before Dec 31
        end_bound = date(target_year + 1, 2, 14) # 45 days after Dec 31

    return {
        "year": target_year,
        "quarter": target_quarter,
        "start": start_bound,
        "end": end_bound
    }

def get_sp500() -> tuple[dict[str, str], dict[str, str]]:
    """
    Scrapes the current S&P 500 company list from Wikipedia and maps symbols to CIKs.
    
    Returns:
        tuple: A tuple containing two dictionaries:
            - symbol2cik (dict): Mapping of ticker symbol to zero-padded CIK string.
            - cik2symbol (dict): Mapping of zero-padded CIK string to ticker symbol.
    """
    url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
    }
    
    response = requests.get(url, headers=headers)
    tables = pd.read_html(StringIO(response.text), match="Symbol")
    df = tables[0]
    
    df['Symbol'] = df['Symbol'].str.replace(".", "-", regex=False)
    padded_ciks = df['CIK'].astype(str).str.zfill(10)

    symbol2cik = dict(zip(df['Symbol'], padded_ciks))
    cik2symbol = dict(zip(padded_ciks, df['Symbol']))

    return symbol2cik, cik2symbol

def get_latest_report_date_with_timing(symbol: str) -> tuple[Optional[date], Optional[date], Optional[str]]:
    """
    Retrieves confirmed reporting dates from Yahoo Finance and the SEC, alongside 
    a precise classification of whether the filing dropped before or after market hours.
    
    Args:
        symbol (str): The stock ticker symbol.
        
    Returns:
        tuple: A tuple containing:
            - end_date (date | None): Latest historical price date from yfinance.
            - report_date (date | None): SEC filing date.
            - release_timing (str | None): "Before Open", "During Hours", "After Close", or None.
    """
    (symbol2cik, _) = get_sp500()
    cik = symbol2cik.get(symbol)
    if not cik:
        return None, None, None

    # Fetch yfinance date boundaries
    end_date = None
    try:
        yf_ticker = yf.Ticker(symbol)
        history_df = yf_ticker.get_earnings_history()
        if history_df is not None and not history_df.empty:
            past_earnings = history_df.index[history_df.index.date <= date.today()]
            if not past_earnings.empty:
                end_date = past_earnings.max().date()
    except Exception as e:
        print(f"Error fetching Yahoo Finance history: {e}")

    # Fetch SEC Submissions containing timestamps
    padded_cik = str(cik).zfill(10)
    headers = {"User-Agent": f"EquityAnalyzer {EMAIL}"}
    url = f"https://data.sec.gov/submissions/CIK{padded_cik}.json"
    
    try:
        response = requests.get(url, headers=headers)
        if response.status_code != 200:
            return end_date, None, None
            
        data = response.json()
        recent = data.get('filings', {}).get('recent', {})
        
        forms = recent.get('form', [])
        items = recent.get('items', [])
        filing_dates = recent.get('filingDate', [])
        acceptance_times = recent.get('acceptanceDateTime', []) # Contains exact ISO timestamp string
        
        target_index = -1
        for i in range(len(forms)):
            if forms[i] == "8-K" and "2.02" in str(items[i]):
                target_index = i
                break
                
        if target_index == -1:
            return end_date, None, None

        report_date = date.fromisoformat(filing_dates[target_index])
        raw_timestamp = acceptance_times[target_index] # Format example: "2026-04-28T16:05:12.000Z"
        
        # Parse UTC timestamp and translate to Eastern Time (Wall Street Time)
        # Note: 'Z' suffix denotes UTC time. We strip/parse it dynamically
        clean_ts = raw_timestamp.replace('Z', '+00:00')
        utc_dt = datetime.fromisoformat(clean_ts)
        est_dt = utc_dt.astimezone(ZoneInfo("America/New_York"))
        
        # Define market hour thresholds for that calendar day
        market_open = est_dt.replace(hour=9, minute=30, second=0, microsecond=0)
        market_close = est_dt.replace(hour=16, minute=0, second=0, microsecond=0)
        
        print(est_dt)

        # Classify timing window
        if est_dt < market_open:
            release_timing = "Before Open"
        elif est_dt > market_close:
            release_timing = "After Close"
        else:
            release_timing = "During Hours"
            
        return end_date, report_date, release_timing

    except Exception as e:
        print(f"Pipeline failure: {e}")
        return end_date, None, None

def get_eps_revenue_estimates(symbol: str, source: str = "YahooFinance") -> tuple[dict[str, Optional[float]], dict[str, Optional[float]]]:
    """
    Fetches analyst consensus estimates for EPS and Revenue.
    
    Args:
        symbol (str): The stock ticker symbol.
        source (str): Data provider to use ("AlphaVantage" or "YahooFinance").
        
    Returns:
        tuple: A tuple containing two dictionaries (eps_dict, revenue_dict).
            Keys for each dictionary:
                - "curr_quarter" (float | None)
                - "next_quarter" (float | None)
                - "next_year" (float | None)
    """
    season = get_latest_earnings_season()
    eps_dict = {"curr_quarter": None, "next_quarter": None, "next_year": None}
    rev_dict = {"curr_quarter": None, "next_quarter": None, "next_year": None}

    # ==========================================
    # 1. Primary Source: Alpha Vantage
    # ==========================================
    if source == "AlphaVantage":
        url = f"https://www.alphavantage.co/query?function=EARNINGS_ESTIMATES&symbol={symbol}&apikey={ALPHAVANTAGE_API_KEY}"
        
        try:
            response = requests.get(url)
            response.raise_for_status()
            data = response.json()
            estimates_list = data.get("estimates", [])
            
            if estimates_list:
                print("Using Alpha Vantage to get estimates")
                quarters, years = [], []
                
                for est in estimates_list:
                    est_date = date.fromisoformat(est["date"])
                    horizon = est.get("horizon", "").lower()
                    
                    parsed_entry = {
                        "date": est_date,
                        "eps": float(est["eps_estimate_average"]) if est.get("eps_estimate_average") else None,
                        "rev": float(est["revenue_estimate_average"]) if est.get("revenue_estimate_average") else None
                    }
                    
                    if horizon == "fiscal quarter":
                        quarters.append(parsed_entry)
                    elif horizon == "fiscal year":
                        years.append(parsed_entry)

                quarters.sort(key=lambda x: x["date"])
                years.sort(key=lambda x: x["date"])

                curr_q_index = None
                for idx, q_est in enumerate(quarters):
                    if season["start"] <= q_est["date"] <= season["end"]:
                        eps_dict["curr_quarter"] = q_est["eps"]
                        rev_dict["curr_quarter"] = q_est["rev"]
                        curr_q_index = idx
                        break
                        
                if curr_q_index is not None and (curr_q_index + 1) < len(quarters):
                    next_q_est = quarters[curr_q_index + 1]
                    eps_dict["next_quarter"] = next_q_est["eps"]
                    rev_dict["next_quarter"] = next_q_est["rev"]

                for y_est in years:
                    if y_est["date"] > season["end"]:
                        eps_dict["next_year"] = y_est["eps"]
                        rev_dict["next_year"] = y_est["rev"]
                        break
                        
                return eps_dict, rev_dict
            
            else:
                print("Alpha Vantage returned no estimates. Falling back...")
        except Exception as e:
            print(f"Alpha Vantage API Connection Error: {e}. Falling back...")

    # ==========================================
    # 2. Fallback / Primary Source: Yahoo Finance
    # ==========================================
    print("Using Yahoo Finance to get estimates")
    yf_ticker = yf.Ticker(symbol)
    already_reported = False
    
    try:
        hist_df = yf_ticker.get_earnings_history()
        if hist_df is not None and not hist_df.empty:
            for index, row in hist_df.iterrows():
                if season["start"] <= index.date() <= season["end"]:
                    val = row.get("epsEstimate")
                    if pd.notna(val):
                        eps_dict["curr_quarter"] = float(val)
                        already_reported = True
                    break
    except Exception as e:
        print(f"Failed to fetch yfinance history: {e}")

    try:
        eps_fwd = yf_ticker.earnings_estimate
        rev_fwd = yf_ticker.revenue_estimate
    except Exception:
        eps_fwd, rev_fwd = None, None

    def get_yf_estimate(df, period):
        if df is not None and not df.empty and period in df.index:
            val = df.loc[period].get("avg")
            return float(val) if pd.notna(val) else None
        return None

    if already_reported:
        rev_dict["curr_quarter"] = None 
        eps_dict["next_quarter"] = get_yf_estimate(eps_fwd, "0q")
        rev_dict["next_quarter"] = get_yf_estimate(rev_fwd, "0q")
    else:
        if eps_dict["curr_quarter"] is None:
            eps_dict["curr_quarter"] = get_yf_estimate(eps_fwd, "0q")
        rev_dict["curr_quarter"] = get_yf_estimate(rev_fwd, "0q")
        
        eps_dict["next_quarter"] = get_yf_estimate(eps_fwd, "+1q")
        rev_dict["next_quarter"] = get_yf_estimate(rev_fwd, "+1q")
        
    eps_dict["next_year"] = get_yf_estimate(eps_fwd, "0y")
    rev_dict["next_year"] = get_yf_estimate(rev_fwd, "0y")

    return eps_dict, rev_dict

def get_revenue(symbol: str) -> dict[str, Optional[float]]:
    """
    Fetches actual historical revenue metrics from Yahoo Finance.
    
    Args:
        symbol (str): The stock ticker symbol.
        
    Returns:
        dict: A dictionary containing actual reported metrics for the current season:
            - "revenue" (float)
            - "gross_profit" (float)
            - "op_income" (float)
    """
    try:
        yf_ticker = yf.Ticker(symbol)
    except:
        return {}
    
    season = get_latest_earnings_season()

    # Get income statement from Yahoo Finance
    income_stmt  = yf_ticker.get_income_stmt(freq = "quarterly")
    for stmt_date in income_stmt:
        if season["start"] <= stmt_date.date() <= season["end"]:
            curr_stmt = income_stmt[stmt_date]
            return {
                "revenue": float(curr_stmt.get("TotalRevenue", 0)),
                "gross_profit": float(curr_stmt.get("GrossProfit", 0)),
                "op_income": float(curr_stmt.get("OperatingIncome", 0)),
            }
    return {}

def get_eps(symbol: str, source: str = "YahooFinance", sue_timeframe: int = 8) -> dict[str, Optional[float]]:
    """
    Fetches the EPS for the current season and calculates Standardized Unexpected Earnings (SUE).
    
    Args:
        symbol (str): The stock ticker symbol.
        source (str): Data provider to use ("AlphaVantage" or "YahooFinance").
        sue_timeframe (int): The number of trailing quarters to calculate SUE standard deviation.
        
    Returns:
        dict: A dictionary containing:
            - "eps_actual" (float | None)
            - "eps_est" (float | None)
            - "surprise" (float | None)
            - "surprise_percent" (float | None)
            - "sue" (float | None)
    """
    season = get_latest_earnings_season()
    result_dict = {
        "eps_actual": None,
        "eps_est": None,
        "surprise": None,
        "surprise_percent": None,
        "sue": None
    }

    # Helper function to safely calculate SUE
    def calculate_sue(current_surprise, historical_surprises):
        if len(historical_surprises) >= 2:
            try:
                std_dev = statistics.stdev(historical_surprises)
                if std_dev > 0:
                    return current_surprise / std_dev
            except statistics.StatisticsError:
                pass
        return None

    # ==========================================
    # 1. Primary Source: Alpha Vantage
    # ==========================================
    if source == "AlphaVantage":
        print(f"Using Alpha Vantage to get EPS")
        # Extract past EPS data from Alpha Vantage
        url = f"https://www.alphavantage.co/query?function=EARNINGS&symbol={symbol}&apikey={ALPHAVANTAGE_API_KEY}"

        try:
            response = requests.get(url)
            response.raise_for_status()
            data = response.json()
            quarterly_earnings = data.get("quarterlyEarnings", [])
            
            if quarterly_earnings:
                current_q_idx = None
                for idx, q_report in enumerate(quarterly_earnings):
                    report_date = date.fromisoformat(q_report.get("fiscalDateEnding", "1970-01-01"))
                    if season["start"] <= report_date <= season["end"]:
                        current_q_idx = idx
                        break
                
                if current_q_idx is not None:
                    current_report = quarterly_earnings[current_q_idx]
                    
                    result_dict["eps_actual"] = float(current_report.get("reportedEPS"))
                    result_dict["eps_est"] = float(current_report.get("estimatedEPS"))
                    result_dict["surprise"] = float(current_report.get("surprise"))
                    result_dict["surprise_percent"] = float(current_report.get("surprisePercentage")) # Already normalized with denominator 100
                    
                    # Gather historical surprises for SUE timeframe (slicing from current index forward)
                    # Alpha Vantage data is sorted newest to oldest
                    past_reports = quarterly_earnings[current_q_idx : min(len(quarterly_earnings), current_q_idx + sue_timeframe)]
                    historical_surprises = []
                    for rep in past_reports:
                        val = rep.get("surprise")
                        if val is not None and val != "None":
                            historical_surprises.append(float(val))
                            
                    result_dict["sue"] = calculate_sue(result_dict["surprise"], historical_surprises)
                    return result_dict
                    
            print("Alpha Vantage returned no valid EPS metrics. Falling back...")
        except Exception as e:
            print(f"Alpha Vantage fetch failed: {e}. Falling back...")

    # ==========================================
    # 2. Fallback / Primary Source: Yahoo Finance
    # ==========================================
    print(f"Using Yahoo Finance to get EPS")
    try:
        yf_ticker = yf.Ticker(symbol)
        hist_df = yf_ticker.get_earnings_history()
        
        if hist_df is not None and not hist_df.empty:
            current_q_idx = None
            
            for idx_num, (dt_index, row) in enumerate(hist_df.iterrows()):
                if season["start"] <= dt_index.date() <= season["end"]:
                    current_q_idx = idx_num
                    result_dict["eps_actual"] = float(row.get("epsActual")) if pd.notna(row.get("epsActual")) else None
                    result_dict["eps_est"] = float(row.get("epsEstimate")) if pd.notna(row.get("epsEstimate")) else None
                    result_dict["surprise"] = float(row.get("epsDifference")) if pd.notna(row.get("epsDifference")) else None
                    result_dict["surprise_percent"] = float(row.get("surprisePercent")) * 100 if pd.notna(row.get("surprisePercent")) else None
                    break
            
            if current_q_idx is not None and result_dict["surprise"] is not None:
                past_df = hist_df.iloc[max(0, current_q_idx - sue_timeframe - 1) : current_q_idx]
                historical_surprises = past_df["epsDifference"].dropna().tolist()
                result_dict["sue"] = calculate_sue(result_dict["surprise"], historical_surprises)

    except Exception as e:
        print(f"Yahoo Finance fallback failed: {e}")

    return result_dict

def get_stock_market_reaction(symbol: str) -> Optional[dict[str, Optional[float]]]:
    """
    Calculates stock price reaction and cumulative abnormal returns (CAR) 
    relative to the S&P 500 by shifting anchor dates based on the precise 
    intraday execution timing of the SEC 8-K release.
    
    Args:
        symbol (str): The stock ticker symbol.
        
    Returns:
        dict | None: A dictionary containing market reaction metrics, or None if 
        historical price windows cannot be dynamically calculated.
            - "close_to_open_change" (float)
            - "open_to_close_change" (float)
            - "close_to_open_car" (float)
            - "open_to_close_car" (float)
    """
    # Fetch dates along with exact intraday timing window metrics
    _, report_date, release_timing = get_latest_report_date_with_timing(symbol)
    
    if not report_date or not release_timing:
        print(f"Cannot calculate market reaction: Missing execution dates or timing parameters for {symbol}")
        return None

    # Fetch a generic padded 14-day tracking matrix around the filing event 
    start_fetch = report_date - timedelta(days=7)
    end_fetch = report_date + timedelta(days=7)

    try:
        stock_ticker = yf.Ticker(symbol)
        spy_ticker = yf.Ticker("^GSPC")
        
        stock_df = stock_ticker.history(start=start_fetch, end=end_fetch)
        spy_df = spy_ticker.history(start=start_fetch, end=end_fetch)
        
        if stock_df.empty or spy_df.empty:
            print(f"Missing price historical matrices for {symbol} or ^GSPC.")
            return None

        # Generate structural trading dates aligned to reality
        all_trading_days = stock_df.index.date.tolist()
        
        # Determine the exact row index corresponding to the calendar report date
        # If the report date lands on a weekend, find the next available active trading day
        if report_date in all_trading_days:
            t_report_idx = all_trading_days.index(report_date)
        else:
            trading_days_after = [d for d in all_trading_days if d > report_date]
            if not trading_days_after:
                print("Insufficient trailing market boundaries.")
                return None
            t_report_idx = all_trading_days.index(trading_days_after[0])

        # Apply explicit structural shifts based on the timing payload
        if release_timing == "Before Open":
            # Baseline close is the night before. Open/Close reaction happens entirely on report day.
            idx_pre_close = t_report_idx - 1
            idx_post_open_close = t_report_idx
            
        elif release_timing == "After Close":
            # Baseline close is today's close. Open/Close reaction happens tomorrow morning.
            idx_pre_close = t_report_idx
            idx_post_open_close = t_report_idx + 1
            
        else: # "During Hours"
            # Report dropped midday. Baseline close must step back to the previous day's settlement.
            # Intraday velocity spans across the active day.
            idx_pre_close = t_report_idx - 1
            idx_post_open_close = t_report_idx

        # Prevent out-of-bounds structural lookup crashes
        if idx_pre_close < 0 or idx_post_open_close >= len(all_trading_days):
            print("Event boundary conditions exceed historical index capabilities.")
            return None

        # Resolve explicit timestamps 
        t_minus_day = stock_df.index[idx_pre_close]
        t_zero_day = stock_df.index[idx_post_open_close]

        # Extract stock pricing points
        stock_last_close = stock_df.loc[t_minus_day, "Close"]
        stock_first_open = stock_df.loc[t_zero_day, "Open"]
        stock_first_close = stock_df.loc[t_zero_day, "Close"]
        
        spy_last_close = spy_df.loc[t_minus_day, "Close"]
        spy_first_open = spy_df.loc[t_zero_day, "Open"]
        spy_first_close = spy_df.loc[t_zero_day, "Close"]
        
        close_to_open_stock = (stock_first_open - stock_last_close) / stock_last_close
        open_to_close_stock = (stock_first_close - stock_first_open) / stock_first_open
        
        close_to_open_spy = (spy_first_open - spy_last_close) / spy_last_close
        open_to_close_spy = (spy_first_close - spy_first_open) / spy_first_open
        
        close_to_open_car = close_to_open_stock - close_to_open_spy
        open_to_close_car = open_to_close_stock - open_to_close_spy
        
        return {
            "close_to_open_change": float(close_to_open_stock * 100),
            "open_to_close_change": float(open_to_close_stock * 100),
            "close_to_open_car": float(close_to_open_car * 100),
            "open_to_close_car": float(open_to_close_car * 100)
        }

    except Exception as e:
        print(f"Failed to isolate market pricing reaction sequences: {e}")
        return None

# Pydantic Schemas for Structured JSON output
class GuidanceMetrics(BaseModel):
    revenue: Optional[float] = Field(default=None, description="Actual monetary amount for revenue.")
    revenue_range_min: Optional[float] = Field(default=None, description="Minimum bound for revenue guidance range.")
    revenue_range_max: Optional[float] = Field(default=None, description="Maximum bound for revenue guidance range.")
    revenue_percent: Optional[float] = Field(default=None, description="Growth or decay percentage for revenue.")
    revenue_percent_range_min: Optional[float] = Field(default=None, description="Minimum bound for revenue percentage range **with respect to last year/current quarter**.")
    revenue_percent_range_max: Optional[float] = Field(default=None, description="Maximum bound for revenue percentage range **with respect to last year/current quarter**.")
    eps: Optional[float] = Field(default=None, description="Actual EPS amount.")
    eps_range_min: Optional[float] = Field(default=None, description="Minimum bound for EPS range.")
    eps_range_max: Optional[float] = Field(default=None, description="Maximum bound for EPS range.")

class ForwardGuidance(BaseModel):
    next_quarter: GuidanceMetrics = Field(description="Guidance specifically for the upcoming next quarter. Leave nested fields null if not provided.")
    current_year: GuidanceMetrics = Field(description="Guidance specifically for the full current fiscal year. Leave nested fields null if not provided.")


def extract_guidance_with_gemini(markdown_text: str, cik: str, safe_filename: str, report_date: date, parse: str = "Auto", n_retry: int = 3) -> Optional[dict]:
    """
    Extracts forward guidance from earnings text using the Gemini API and saves it to a JSON cache.
    
    Args:
        markdown_text (str): The markdown text of the 8-K press release.
        cik (str): The central index key of the company.
        safe_filename (str): A secure filename used as the key in the JSON cache.
        report_date (date): The report date of the 8-K press release.
        parse (str): Parsing mode - "None" (skip API), "Auto" (default; use API if not cached), or "Force" (overwrite cache).
        n_retry (int): Number of retries in case of errors in extraction, default = 3
        
    Returns:
        dict: A dictionary containing the extracted forward guidance, or None if extraction fails/is skipped.
    """
    if parse == "None":
        return None

    cache_file = "data/extractions.json"
    cache_data = {}
    
    # Load cache
    if os.path.exists(cache_file):
        try:
            with open(cache_file, "r", encoding="utf-8") as f:
                cache_data = json.load(f)
        except json.JSONDecodeError:
            print("Cache file corrupted or empty. Starting fresh.")
            cache_data = {}

    if parse == "Auto" and safe_filename in cache_data:
        print(f"Record found in cache for {safe_filename}. Skipping API call.")
        return cache_data[safe_filename]

    if parse == "Force" and safe_filename in cache_data:
        print(f"Force parse enabled. Overwriting existing record for {safe_filename}...")
    else:
        print("Sending Markdown to Gemini for guidance extraction...")
    
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("Error: GEMINI_API_KEY environment variable not found.")
        return None
        
    client = genai.Client(api_key=api_key)
    
    prompt = f"""
    You are a financial data extraction assistant. Extract forward guidance for revenue and EPS from the following earnings press release.
    
    CRITICAL INSTRUCTIONS:
    1. Extract explicitly stated numbers for forward guidance.
    2. Carefully separate the guidance into two time horizons: "next_quarter" (the immediate upcoming quarter) and "current_year" (the full fiscal year).
    3. DO NOT perform any calculations or assumptions. If the data is not there, leave it as null.
    4. If a single number is provided, fill in the base field and leave the range fields empty.
    5. If a range is provided, fill in the min/max fields and leave the base field empty.
    6. For revenue value, the unit is dollars, so convert the million/billion back to dollars by adding trailing zeros where appropriate
    
    Press Release Markdown:
    {markdown_text}
    """
    
    for attempt in range(n_retry + 1):
        try:
            response = client.models.generate_content(
                model="gemma-4-26b-a4b-it",
                contents=prompt,
                config={
                    "response_mime_type": "application/json",
                    "response_schema": ForwardGuidance,
                }
            )
            
            guidance_obj = ForwardGuidance.model_validate_json(response.text)
            guidance_data = guidance_obj.model_dump()
            
            guidance_data["CIK"] = cik
            guidance_data["safe_filename"] = safe_filename
            guidance_data["source"] = "Gemini"
            guidance_data["report_date"] = report_date.strftime("%Y-%m-%d")
            
            cache_data[safe_filename] = guidance_data
            
            os.makedirs("data", exist_ok=True)
            with open(cache_file, "w", encoding="utf-8") as f:
                json.dump(cache_data, f, indent=4)
                
            print(f"Extraction successfully saved to {cache_file}")
            return guidance_data
            
        except Exception as e:
            print(f"Gemini API Error (Attempt {attempt + 1}/{n_retry + 1}): {e}")
            if attempt < n_retry:
                print("Retrying...")
            else:
                print("Max retries reached. Failing.")
                return None

def get_latest_8k_press_release(cik: str, parse: str = "Auto") -> dict[str, Optional[Union[str, dict]]]:
    """
    Downloads the primary 8-K document, validates the filing date against the active earnings season, 
    locates Exhibit 99.1, saves it as Markdown, and conditionally extracts forward guidance via Gemini.
    
    Args:
        cik (str): The SEC Central Index Key for the company.
        parse (str): Parsing mode for Gemini extraction ("None", "Auto", "Force"). Defaults to "Auto".
        
    Returns:
        dict: A dictionary containing "markdown" and "guidance" keys, 
        where the values could be None if the document couldn't be retrieved or wasn't within the active season.
    """
    padded_cik = str(cik).zfill(10)
    stripped_cik = str(cik).lstrip('0')
    headers = {"User-Agent": f"EquityAnalyzer {EMAIL}"}
    
    url = f"https://data.sec.gov/submissions/CIK{padded_cik}.json"
    print("Fetching submissions...")
    
    response = requests.get(url, headers=headers)
    if response.status_code != 200:
        print(f"Error fetching submissions: {response.status_code}")
        return {
            "markdown": None,
            "guidance": None
        }
        
    data = response.json()
    recent_filings = data.get('filings', {}).get('recent', {})
    
    forms = recent_filings.get('form', [])
    items = recent_filings.get('items', [])
    accessions = recent_filings.get('accessionNumber', [])
    primary_docs = recent_filings.get('primaryDocument', [])
    filing_dates = recent_filings.get('filingDate', [])
    
    target_index = -1
    for i in range(len(forms)):
        if forms[i] == "8-K" and "2.02" in items[i]:
            target_index = i
            break
            
    if target_index == -1:
        print("No recent 8-K with Item 2.02 (Earnings Release) found.")
        return {
            "markdown": None,
            "guidance": None
        }

    # --- Date Window Validation ---
    report_date = date.fromisoformat(filing_dates[target_index])
    season = get_latest_earnings_season()
    
    # 8-K filing dates naturally fall immediately following the end of the quarter.
    # Allowing a window of 45 days after the fiscal end period bounds to capture late or off-cycle filers.
    report_window_start = season["start"]
    report_window_end = season["end"] + timedelta(days=45)
    
    if not (report_window_start <= report_date <= report_window_end):
        print(f"Skipped: Report filing date ({report_date}) falls outside the active earnings season window.")
        return {
            "markdown": None,
            "guidance": None
        }
        
    accession_no_dashes = accessions[target_index].replace('-', '')
    primary_doc_filename = primary_docs[target_index]
    
    base_archive_url = f"https://www.sec.gov/Archives/edgar/data/{stripped_cik}/{accession_no_dashes}/"
    primary_url = f"{base_archive_url}{primary_doc_filename}"
    
    print(f"Parsing primary document: {primary_url}")
    primary_response = requests.get(primary_url, headers=headers)
    
    if primary_response.status_code != 200:
        print("Failed to download the primary 8-K document.")
        return {
            "markdown": None,
            "guidance": None
        }
        
    soup = BeautifulSoup(primary_response.content, 'html.parser')
    exhibit_href = None
    
    for element in soup.find_all(string=re.compile(r'99\.1')):
        parent_a = element.find_parent('a')
        if parent_a and parent_a.has_attr('href'):
            exhibit_href = parent_a['href']
            break
            
        parent_tr = element.find_parent('tr')
        if parent_tr:
            row_a = parent_tr.find('a', href=True)
            if row_a:
                exhibit_href = row_a['href']
                break
                
    if not exhibit_href:
        print("Could not find a valid hyperlink for Exhibit 99.1 in the document.")
        return {
            "markdown": None,
            "guidance": None
        }
        
    exhibit_url = f"{base_archive_url}{exhibit_href}"
    print(f"Downloading Exhibit 99.1 from: {exhibit_url}")
    
    exhibit_response = requests.get(exhibit_url, headers=headers)
    if exhibit_response.status_code != 200:
        print("Failed to download Exhibit 99.1.")
        return {
            "markdown": None,
            "guidance": None
        }
        
    markdown_text = md(exhibit_response.text)
    
    os.makedirs("data", exist_ok=True)
    safe_filename = f"{str(padded_cik)}_{report_date.strftime('%Y-%m-%d')}_{exhibit_href.split('/')[-1]}"
    
    with open(f"data/{safe_filename}.md", "w", encoding="utf-8") as f:
        f.write(markdown_text)

    # Trigger Gemini extraction process based on parse variable
    guidance_data = None
    if parse in ["Auto", "Force"]:
        guidance_data = extract_guidance_with_gemini(markdown_text, cik, safe_filename, report_date, parse)
    
    return {
        "markdown": markdown_text,
        "guidance": guidance_data
    }

def compare_forward_guidance(symbol: str) -> dict[str, dict]:
    """
    Compares the company's forward guidance (from 8-K extraction) against the market 
    consensus (from Yahoo Finance / Alpha Vantage) for both Revenue and EPS.
    
    If the company provides revenue guidance as a percentage, it calculates the implied 
    absolute revenue based on historical performance (YoY) for accurate comparison.
    
    Args:
        symbol (str): The stock ticker symbol.
        
    Returns:
        dict: A nested dictionary structured by 'quarter' and 'year', containing 
        'Revenue' and 'EPS' comparisons. Inner keys only exist if guidance was provided.
    """
    result = {
        "quarter": {},
        "year": {}
    }
    
    # Map Symbol to CIK
    symbol2cik, _ = get_sp500()
    cik = symbol2cik.get(symbol)
    if not cik:
        print(f"CIK not found for {symbol}.")
        return result

    padded_cik = str(cik).zfill(10)
        
    # Get Active Season and SEC Report Date
    _, report_date, _ = get_latest_report_date_with_timing(symbol)
    if not report_date:
        print(f"Could not resolve a valid recent report date for {symbol}.")
        return result

    # Retrieve Extraction Data (Cache check first, then API fallback)
    guidance_data = None
    cache_file = "data/extractions.json"
    report_date_str = report_date.strftime("%Y-%m-%d")
    
    if os.path.exists(cache_file):
        try:
            with open(cache_file, "r", encoding="utf-8") as f:
                cache = json.load(f)
            
            # Search cache for matching CIK and Report Date
            for key, entry in cache.items():
                if entry.get("CIK") == padded_cik and entry.get("report_date") == report_date_str:
                    guidance_data = entry
                    print("Loaded guidance from extractions.json cache.")
                    break
        except json.JSONDecodeError:
            pass

    # Fallback to fetching and parsing if not in cache
    if not guidance_data:
        print("Guidance not found in cache for current season. Triggering parser...")
        pr_data = get_latest_8k_press_release(cik, parse="Auto")
        if pr_data and pr_data.get("guidance"):
            guidance_data = pr_data["guidance"]
            
    if not guidance_data:
        print("No guidance data extracted or found.")
        return result

    # Fetch Market Consensus
    eps_est, rev_est = get_eps_revenue_estimates(symbol)
    yf_ticker = yf.Ticker(symbol)

    # Helper function to extract single value or compute midpoint of a range
    def get_val_or_midpoint(metrics: dict, base_key: str) -> Optional[float]:
        val = metrics.get(base_key)
        if val is not None:
            return float(val)
            
        min_val = metrics.get(f"{base_key}_range_min")
        max_val = metrics.get(f"{base_key}_range_max")
        
        if min_val is not None and max_val is not None:
            return (float(min_val) + float(max_val)) / 2.0
        elif min_val is not None: 
            return float(min_val)
        elif max_val is not None: 
            return float(max_val)
            
        return None

    # Process Next Quarter Guidance
    q_guidance = guidance_data.get("next_quarter", {})
    if any(v is not None for v in q_guidance.values()):
        
        # Quarter Revenue
        q_rev_guidance = get_val_or_midpoint(q_guidance, "revenue")
        
        # If absolute revenue isn't provided, try deriving it from percentage guidance
        if q_rev_guidance is None:
            q_rev_pct = get_val_or_midpoint(q_guidance, "revenue_percent")
            if q_rev_pct is not None:
                try:
                    q_inc = yf_ticker.get_income_stmt(freq="quarterly")
                    # Index 3 typically corresponds to the reported quarter from exactly 1 year ago
                    # relative to the *upcoming* (next) quarter
                    if q_inc.shape[1] > 3:
                        last_year_q_rev = float(q_inc.iloc[:, 3].get("TotalRevenue", 0))
                        q_rev_guidance = last_year_q_rev * (1 + (q_rev_pct / 100.0))
                except Exception:
                    pass
                    
        if q_rev_guidance is not None:
            q_rev_cons = rev_est.get("next_quarter")
            if q_rev_cons:
                surprise = q_rev_guidance - q_rev_cons
                result["quarter"]["Revenue"] = {
                    "guidance": q_rev_guidance,
                    "consensus": q_rev_cons,
                    "surprise": surprise,
                    "surprise_percent": (surprise / q_rev_cons) * 100
                }
                
        # Quarter EPS
        q_eps_guidance = get_val_or_midpoint(q_guidance, "eps")
        if q_eps_guidance is not None:
            q_eps_cons = eps_est.get("next_quarter")
            if q_eps_cons is not None:
                surprise = q_eps_guidance - q_eps_cons
                # Use absolute consensus in denominator to preserve correct positive/negative growth logic
                denom = abs(q_eps_cons) if q_eps_cons != 0 else 1 
                result["quarter"]["EPS"] = {
                    "guidance": q_eps_guidance,
                    "consensus": q_eps_cons,
                    "surprise": surprise,
                    "surprise_percent": (surprise / denom) * 100
                }

    # Process Current Year Guidance
    y_guidance = guidance_data.get("current_year", {})
    if any(v is not None for v in y_guidance.values()):
        
        # Year Revenue
        y_rev_guidance = get_val_or_midpoint(y_guidance, "revenue")
        
        if y_rev_guidance is None:
            y_rev_pct = get_val_or_midpoint(y_guidance, "revenue_percent")
            if y_rev_pct is not None:
                try:
                    y_inc = yf_ticker.get_income_stmt(freq="yearly")
                    # Index 0 is the most recently completed fiscal year
                    if y_inc.shape[1] > 0:
                        last_year_rev = float(y_inc.iloc[:, 0].get("TotalRevenue", 0))
                        y_rev_guidance = last_year_rev * (1 + (y_rev_pct / 100.0))
                except Exception:
                    pass

        if y_rev_guidance is not None:
            y_rev_cons = rev_est.get("next_year")
            if y_rev_cons:
                surprise = y_rev_guidance - y_rev_cons
                result["year"]["Revenue"] = {
                    "guidance": y_rev_guidance,
                    "consensus": y_rev_cons,
                    "surprise": surprise,
                    "surprise_percent": (surprise / y_rev_cons) * 100
                }
                
        # Year EPS
        y_eps_guidance = get_val_or_midpoint(y_guidance, "eps")
        if y_eps_guidance is not None:
            y_eps_cons = eps_est.get("next_year")
            if y_eps_cons is not None:
                surprise = y_eps_guidance - y_eps_cons
                denom = abs(y_eps_cons) if y_eps_cons != 0 else 1
                result["year"]["EPS"] = {
                    "guidance": y_eps_guidance,
                    "consensus": y_eps_cons,
                    "surprise": surprise,
                    "surprise_percent": (surprise / denom) * 100
                }

    return result

if __name__ == "__main__":
    symbol = input("Enter symbol:")
    print("---EPS and Revenue Estimates---")
    eps_est_dict, rev_est_dict = get_eps_revenue_estimates(symbol)
    print("EPS Estimates:")
    print(f"Current quarter: {eps_est_dict['curr_quarter']}")
    print(f"Next quarter: {eps_est_dict['next_quarter']}")
    print(f"Next year: {eps_est_dict['next_year']}")
    print("Revenue Estimates:")
    print(f"Current quarter: {rev_est_dict['curr_quarter']}")
    print(f"Next quarter: {rev_est_dict['next_quarter']}")
    print(f"Next year: {rev_est_dict['next_year']}")
    print("---Revenue---")
    rev_dict = get_revenue(symbol)
    print("Actual Revenue:")
    print(f"Total Revenue: {rev_dict['revenue']}")
    print(f"Gross Profit: {rev_dict['gross_profit']}")
    print(f"Operating Income: {rev_dict['op_income']}")
    print("---EPS---")
    eps_dict = get_eps(symbol)
    print("Actual EPS:")
    print(f"EPS (Actual): {eps_dict['eps_actual']}")
    print(f"EPS (Estimate): {eps_dict['eps_est']}")
    print(f"Surprise: {eps_dict['surprise']} ( {eps_dict['surprise_percent']}% )")
    print(f"SUE: {eps_dict['sue']}")
    print("---Press Release---")
    (symbol2cik, _) = get_sp500()
    pr_text = get_latest_8k_press_release(symbol2cik[symbol])
    print(pr_text)
    print(compare_forward_guidance(symbol))
    print("---Stock Market Reaction---")
    print(get_latest_report_date_with_timing(symbol))
    stock = get_stock_market_reaction(symbol)
    print(f"Close-to-open change: {stock['close_to_open_change']}%")
    print(f"Open-to-close change: {stock['open_to_close_change']}%")
    print(f"Close-to-open CAR: {stock['close_to_open_car']}%")
    print(f"Open-to-close CAR: {stock['open_to_close_car']}%")
    

