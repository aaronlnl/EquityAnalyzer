from datetime import date, timedelta
from io import StringIO
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


def extract_guidance_with_gemini(markdown_text: str, cik: str, safe_filename: str, parse: str = "Auto") -> Optional[dict]:
    """
    Extracts forward guidance from earnings text using the Gemini API and saves it to a JSON cache.
    
    Args:
        markdown_text (str): The markdown text of the 8-K press release.
        cik (str): The central index key of the company.
        safe_filename (str): A secure filename used as the key in the JSON cache.
        parse (str): Parsing mode - "None" (skip API), "Auto" (use API if not cached), or "Force" (overwrite cache).
        
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
    
    Press Release Markdown:
    {markdown_text}
    """
    
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
        
        cache_data[safe_filename] = guidance_data
        
        os.makedirs("data", exist_ok=True)
        with open(cache_file, "w", encoding="utf-8") as f:
            json.dump(cache_data, f, indent=4)
            
        print(f"Extraction successfully saved to {cache_file}")
        return guidance_data
        
    except Exception as e:
        print(f"Gemini API Error during extraction or schema validation: {e}")
        return None

def get_latest_8k_press_release(cik: str, parse: str = "Auto") -> Union[dict, str, None]:
    """
    Downloads the primary 8-K document, validates the filing date against the active earnings season, 
    locates Exhibit 99.1, saves it as Markdown, and conditionally extracts forward guidance via Gemini.
    
    Args:
        cik (str): The SEC Central Index Key for the company.
        parse (str): Parsing mode for Gemini extraction ("None", "Auto", "Force"). Defaults to "Auto".
        
    Returns:
        dict | str | None: Returns a dictionary of guidance if extracted, the raw markdown string if parse="None", 
        or None if the document couldn't be retrieved or wasn't within the active season.
    """
    padded_cik = str(cik).zfill(10)
    stripped_cik = str(cik).lstrip('0')
    headers = {"User-Agent": f"EquityAnalyzer {EMAIL}"}
    
    url = f"https://data.sec.gov/submissions/CIK{padded_cik}.json"
    print("Fetching submissions...")
    
    response = requests.get(url, headers=headers)
    if response.status_code != 200:
        print(f"Error fetching submissions: {response.status_code}")
        return None
        
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
        return None

    # --- Date Window Validation ---
    report_date = date.fromisoformat(filing_dates[target_index])
    season = get_latest_earnings_season()
    
    # 8-K filing dates naturally fall immediately following the end of the quarter.
    # Allowing a window of 45 days after the fiscal end period bounds to capture late or off-cycle filers.
    report_window_start = season["start"]
    report_window_end = season["end"] + timedelta(days=45)
    
    if not (report_window_start <= report_date <= report_window_end):
        print(f"Skipped: Report filing date ({report_date}) falls outside the active earnings season window.")
        return None
        
    accession_no_dashes = accessions[target_index].replace('-', '')
    primary_doc_filename = primary_docs[target_index]
    
    # Set the base directory URL for this specific SEC filing
    base_archive_url = f"https://www.sec.gov/Archives/edgar/data/{stripped_cik}/{accession_no_dashes}/"
    primary_url = f"{base_archive_url}{primary_doc_filename}"
    
    # Fetch Primary Document HTML and locate Exhibit 99.1
    print(f"Parsing primary document: {primary_url}")
    primary_response = requests.get(primary_url, headers=headers)
    
    if primary_response.status_code != 200:
        print("Failed to download the primary 8-K document.")
        return None
        
    soup = BeautifulSoup(primary_response.content, 'html.parser')
    exhibit_href = None
    
    # Find all text nodes that contain "99.1"
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
        return None
        
    # Fetch and Tidy the Exhibit 99.1 HTML
    exhibit_url = f"{base_archive_url}{exhibit_href}"
    print(f"Downloading Exhibit 99.1 from: {exhibit_url}")
    
    exhibit_response = requests.get(exhibit_url, headers=headers)
    if exhibit_response.status_code != 200:
        print("Failed to download Exhibit 99.1.")
        return None
        
    markdown_text = md(exhibit_response.text)
    
    # Ensure the data directory exists and save the file safely
    os.makedirs("data", exist_ok=True)
    safe_filename = str(padded_cik) + exhibit_href.split('/')[-1]
    
    with open(f"data/{safe_filename}.md", "w", encoding="utf-8") as f:
        f.write(markdown_text)

    # Trigger Gemini extraction process based on parse variable
    if parse in ["Auto", "Force"]:
        return extract_guidance_with_gemini(markdown_text, cik, safe_filename, parse)
    
    return markdown_text

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
    pr_text = get_latest_8k_press_release("0001045810")
    print(pr_text)
    get_sp500()
    

