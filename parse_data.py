from datetime import date
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
from typing import Optional
import yfinance as yf


load_dotenv()

ALPHAVANTAGE_API_KEY = os.getenv("ALPHAVANTAGE_API_KEY")
EMAIL = os.getenv("EMAIL")

def get_latest_earnings_season() -> dict[str, int | date]:
    """
    Calculates the latest active/most recent calendar earnings season based on today's date,
    and returns the corresponding 'end date' boundaries for fiscal calendarization.
    
    Returns:
        dict: A dictionary containing
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

def get_sp500() -> tuple[dict, dict]:
    url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
    }
    
    response = requests.get(url, headers=headers)
    
    # Wrap the text in StringIO to parse the HTML safely
    # We use match="Symbol" to guarantee we grab the right table without needing BeautifulSoup
    tables = pd.read_html(StringIO(response.text), match="Symbol")
    df = tables[0]
    
    df['Symbol'] = df['Symbol'].str.replace(".", "-", regex=False)
    
    padded_ciks = df['CIK'].astype(str).str.zfill(10)

    symbol2cik = dict(zip(df['Symbol'], padded_ciks))
    cik2symbol = dict(zip(padded_ciks, df['Symbol']))

    return symbol2cik, cik2symbol

def get_eps_revenue_estimates(symbol: str, source: str = "YahooFinance") -> tuple[dict, dict]:
    """
    Fetches analyst estimates from Alpha Vantage and groups them into 
    curr_quarter, next_quarter, and next_year based on the current active earnings season window.
    
    Returns:
        tuple: (eps_dict, revenue_dict)
            - Keys for each dictionary:
                "curr_quarter" (float)
                "next_quarter" (float)
                "next_year" (float)
    """
    season = get_latest_earnings_season()

    # Initialize return dictionaries
    eps_dict = {"curr_quarter": None, "next_quarter": None, "next_year": None}
    rev_dict = {"curr_quarter": None, "next_quarter": None, "next_year": None}

    if source == "AlphaVantage":
        
        # Call Alpha Vantage API
        url = f"https://www.alphavantage.co/query?function=EARNINGS_ESTIMATES&symbol={symbol}&apikey={ALPHAVANTAGE_API_KEY}"
        
        try:
            response = requests.get(url)
            response.raise_for_status()
            data = response.json()
        except Exception as e:
            print(f"API Connection Error: {e}")
            return {}, {}
        
        estimates_list = data.get("estimates", [])

    if source == "YahooFinance" or not estimates_list:
        print("Using Yahoo Finance to get estimates")
        yf_ticker = yf.Ticker(symbol)
        already_reported = False
        try:
            hist_df = yf_ticker.get_earnings_history()
            if hist_df is not None and not hist_df.empty:
                for index, row in hist_df.iterrows():
                    # yfinance history index is typically the datetime of the report
                    if season["start"] <= index.date() <= season["end"]:
                        # Handle potential NaN values safely
                        val = row.get("epsEstimate")
                        if pd.notna(val):
                            eps_dict["curr_quarter"] = float(val)
                            already_reported = True
                        break
        except Exception as e:
            print(f"Failed to fetch yfinance history: {e}")

        # 2. Fetch Forward Estimates DataFrames
        try:
            eps_fwd = yf_ticker.earnings_estimate
            rev_fwd = yf_ticker.revenue_estimate
        except Exception:
            eps_fwd, rev_fwd = None, None

        # Helper to safely extract 'avg' from yfinance DataFrames
        def get_yf_estimate(df, period):
            if df is not None and not df.empty and period in df.index:
                val = df.loc[period].get("avg")
                return float(val) if pd.notna(val) else None
            return None

        # 3. Route the forward estimates based on whether the quarter rolled over
        if already_reported:
            # They reported. Yahoo wiped historical revenue. '0q' is now next quarter.
            rev_dict["curr_quarter"] = None 
            
            eps_dict["next_quarter"] = get_yf_estimate(eps_fwd, "0q")
            rev_dict["next_quarter"] = get_yf_estimate(rev_fwd, "0q")
        else:
            # They haven't reported yet. '0q' is still the current quarter.
            if eps_dict["curr_quarter"] is None:
                eps_dict["curr_quarter"] = get_yf_estimate(eps_fwd, "0q")
            rev_dict["curr_quarter"] = get_yf_estimate(rev_fwd, "0q")
            
            eps_dict["next_quarter"] = get_yf_estimate(eps_fwd, "+1q")
            rev_dict["next_quarter"] = get_yf_estimate(rev_fwd, "+1q")
            
        # 4. Extract Next Fiscal Year (Usually '0y' or '+1y' depending on fiscal rollover)
        # We default to '0y' (Current Fiscal Year) as it usually aligns with the next 12-month boundary
        eps_dict["next_year"] = get_yf_estimate(eps_fwd, "0y")
        rev_dict["next_year"] = get_yf_estimate(rev_fwd, "0y")

        return eps_dict, rev_dict
    
    print("Using Alpha Vantage to get estimates")

    # Separate quarter horizons and year horizons for processing
    quarters = []
    years = []
    
    for est in estimates_list:
        est_date = date.fromisoformat(est["date"])
        horizon = est.get("horizon", "").lower()
        
        # Safe float conversion utility
        def clean_val(key):
            val = est.get(key)
            return float(val) if val is not None else None

        parsed_entry = {
            "date": est_date,
            "eps": clean_val("eps_estimate_average"),
            "rev": clean_val("revenue_estimate_average")
        }
        
        if horizon == "fiscal quarter":
            quarters.append(parsed_entry)
        elif horizon == "fiscal year":
            years.append(parsed_entry)

    # Sort chronological elements (oldest dates to newest future dates)
    quarters.sort(key=lambda x: x["date"])
    years.sort(key=lambda x: x["date"])

    # Match current quarter against the calendarized boundary window
    curr_q_index = None
    for idx, q_est in enumerate(quarters):
        if season["start"] <= q_est["date"] <= season["end"]:
            eps_dict["curr_quarter"] = q_est["eps"]
            rev_dict["curr_quarter"] = q_est["rev"]
            curr_q_index = idx
            break
            
    # Extract next quarter relative to the current one
    if curr_q_index is not None and (curr_q_index + 1) < len(quarters):
        next_q_est = quarters[curr_q_index + 1]
        eps_dict["next_quarter"] = next_q_est["eps"]
        rev_dict["next_quarter"] = next_q_est["rev"]

    # Extract next fiscal year
    # Target the upcoming fiscal year whose end date finishes after our reporting season window
    for y_est in years:
        if y_est["date"] > season["end"]:
            eps_dict["next_year"] = y_est["eps"]
            rev_dict["next_year"] = y_est["rev"]
            break

    return eps_dict, rev_dict

def get_revenue(symbol: str) -> dict[str, None]:
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
                "revenue": curr_stmt.get("TotalRevenue", 0),
                "gross_profit": curr_stmt.get("GrossProfit", 0),
                "op_income": curr_stmt.get("OperatingIncome", 0),
            }

    return {}

def get_eps(symbol: str, source: str = "YahooFinance", sue_timeframe: int = 8):
    """
    Fetches the EPS for the current season, including actual, estimates, surprises, 
    and the Standardized Unexpected Earnings (SUE) metric.
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
        # We need at least 2 data points to calculate a sample standard deviation
        if len(historical_surprises) >= 2:
            try:
                std_dev = statistics.stdev(historical_surprises)
                print(historical_surprises, std_dev)
                if std_dev > 0:
                    return current_surprise / std_dev
            except statistics.StatisticsError:
                pass
        return None

    av_success = False
    if source == "AlphaVantage":
        print(f"Using Alpha Vantage to get EPS")
        # Extract past EPS data from Alpha Vantage
        url = f"https://www.alphavantage.co/query?function=EARNINGS&symbol={symbol}&apikey={ALPHAVANTAGE_API_KEY}"

        try:
            response = requests.get(url)
            response.raise_for_status()
            data = response.json()
            
            quarterly_earnings = data.get("quarterlyEarnings", [])

            print(quarterly_earnings)
            
            if quarterly_earnings:
                # Find the index of the quarter that falls into our current season bounds
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
                    av_success = True

        except Exception as e:
            print(f"Alpha Vantage fetch failed: {e}")

    # Fallback: Yahoo Finance
    if not av_success:
        print(f"Using Yahoo Finance to get EPS")
        try:
            yf_ticker = yf.Ticker(symbol)
            hist_df = yf_ticker.get_earnings_history()
            
            if hist_df is not None and not hist_df.empty:
                # yfinance returns history sorted newest to oldest as well
                current_q_idx = None
                
                # Find the target season quarter
                for idx_num, (dt_index, row) in enumerate(hist_df.iterrows()):
                    # Yfinance index is a timestamp, safely extract date
                    if season["start"] <= dt_index.date() <= season["end"]:
                        current_q_idx = idx_num
                        
                        # Populate base metrics
                        result_dict["eps_actual"] = float(row.get("epsActual")) if pd.notna(row.get("epsActual")) else None
                        result_dict["eps_est"] = float(row.get("epsEstimate")) if pd.notna(row.get("epsEstimate")) else None
                        result_dict["surprise"] = float(row.get("epsDifference")) if pd.notna(row.get("epsDifference")) else None
                        result_dict["surprise_percent"] = float(row.get("surprisePercent")) * 100 if pd.notna(row.get("surprisePercent")) else None
                        break
                
                # If we found the target quarter, calculate SUE
                if current_q_idx is not None and result_dict["surprise"] is not None:
                    # Slice the dataframe to grab trailing periods including current
                    past_df = hist_df.iloc[max(0, current_q_idx - sue_timeframe - 1) : current_q_idx]
                    
                    # Drop NaNs to safely extract historical surprises
                    historical_surprises = past_df["epsDifference"].dropna().tolist()
                    
                    result_dict["sue"] = calculate_sue(result_dict["surprise"], historical_surprises)

        except Exception as e:
            print(f"Yahoo Finance fallback failed: {e}")

    return result_dict

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

def get_latest_8k_press_release(cik: str, source: str, force_parse: bool):
    """
    Downloads the primary 8-K document, locates the Exhibit 99.1 URL,
    and returns the cleaned text of the earnings press release.
    """
    padded_cik = str(cik).zfill(10)
    stripped_cik = str(cik).lstrip('0') # The Archives URL uses unpadded CIKs
    headers = {"User-Agent": f"EquityAnalyzer {EMAIL}"}
    
    # Fetch Submissions and find the latest 8-K with Item 2.02
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
    
    target_index = -1
    for i in range(len(forms)):
        if forms[i] == "8-K" and "2.02" in items[i]:
            target_index = i
            break
            
    if target_index == -1:
        print("No recent 8-K with Item 2.02 (Earnings Release) found.")
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
        # Case A: The "99.1" text itself is wrapped in an <a> tag (e.g., CRL)
        parent_a = element.find_parent('a')
        if parent_a and parent_a.has_attr('href'):
            exhibit_href = parent_a['href']
            break
            
        # Case B: "99.1" is plain text in a table cell, but the link is in the same row (e.g., COR, ADBE)
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
    safe_filename = str(padded_cik) + exhibit_href.split('/')[-1] # Prevents issues if href contains subdirectories
    
    with open(f"data/{safe_filename}.md", "w", encoding="utf-8") as f:
        f.write(markdown_text)

    # Process via Gemini API
    if source == "Gemini":
        cache_file = "data/extractions.json"
        cache_data = {}
        
        # Load the existing master JSON cache if it exists
        if os.path.exists(cache_file):
            try:
                with open(cache_file, "r", encoding="utf-8") as f:
                    cache_data = json.load(f)
            except json.JSONDecodeError:
                print("Cache file corrupted or empty. Creating a new one.")
                cache_data = {}

        # Check if we already have this extraction and shouldn't force parse
        if safe_filename in cache_data and not force_parse:
            print(f"Record found in cache for {safe_filename}. Skipping API call.")
            return cache_data[safe_filename]

        if force_parse and safe_filename in cache_data:
            print(f"Force parse enabled. Overwriting existing record for {safe_filename}...")
        else:
            print("Sending Markdown to Gemini for guidance extraction...")
        
        # Initialize the new SDK Client
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            print("Error: GEMINI_API_KEY environment variable not found.")
            return None
            
        client = genai.Client(api_key=api_key)
        
        # The prompt for extraction task
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
            # Call the model using the new Structured Output config format
            response = client.models.generate_content(
                model="gemma-4-26b-a4b-it",
                contents=prompt,
                config={
                    "response_mime_type": "application/json",
                    "response_schema": ForwardGuidance,
                }
            )
            
            # Validate and parse the response strictly into our Pydantic model
            guidance_obj = ForwardGuidance.model_validate_json(response.text)
            
            # Convert the Pydantic object back to a standard dictionary to append metadata
            guidance_data = guidance_obj.model_dump()
            
            # Append requested metadata
            guidance_data["CIK"] = cik
            guidance_data["safe_filename"] = safe_filename
            guidance_data["source"] = "Gemini"
            
            # Update the cache dictionary
            cache_data[safe_filename] = guidance_data
            
            # Write the updated dictionary back to the master JSON file
            with open(cache_file, "w", encoding="utf-8") as f:
                json.dump(cache_data, f, indent=4)
                
            print(f"Extraction successfully saved to {cache_file}")
            return guidance_data
            
        except Exception as e:
            print(f"Gemini API Error during extraction or schema validation: {e}")
            return None
            
    # If source is not Gemini, return the raw markdown
    return markdown_text



if __name__ == "__main__":
    # symbol = input("Enter symbol:")
    # print("---EPS and Revenue Estimates---")
    # eps_est_dict, rev_est_dict = get_eps_revenue_estimates(symbol)
    # print("EPS Estimates:")
    # print(f"Current quarter: {eps_est_dict['curr_quarter']}")
    # print(f"Next quarter: {eps_est_dict['next_quarter']}")
    # print(f"Next year: {eps_est_dict['next_year']}")
    # print("Revenue Estimates:")
    # print(f"Current quarter: {rev_est_dict['curr_quarter']}")
    # print(f"Next quarter: {rev_est_dict['next_quarter']}")
    # print(f"Next year: {rev_est_dict['next_year']}")
    # print("---Revenue---")
    # rev_dict = get_revenue(symbol)
    # print("Actual Revenue:")
    # print(f"Total Revenue: {rev_dict['revenue']}")
    # print(f"Gross Profit: {rev_dict['gross_profit']}")
    # print(f"Operating Income: {rev_dict['op_income']}")
    # print("---EPS---")
    # eps_dict = get_eps(symbol)
    # print("Actual EPS:")
    # print(f"EPS (Actual): {eps_dict['eps_actual']}")
    # print(f"EPS (Estimate): {eps_dict['eps_est']}")
    # print(f"Surprise: {eps_dict['surprise']} ( {eps_dict['surprise_percent']}% )")
    # print(f"SUE: {eps_dict['sue']}")
    # print("---Press Release---")
    # pr_text = get_latest_8k_press_release("0001045810", "Gemini", False)
    # print(pr_text)
    get_sp500()
    

