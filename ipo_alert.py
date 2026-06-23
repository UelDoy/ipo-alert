import os
import smtplib
import datetime
import pandas as pd
import re

from io import StringIO
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from playwright.async_api import async_playwright


# ------------------------------------------------------------------
# CONFIG FROM GITHUB SECRETS / ENVIRONMENT
# ------------------------------------------------------------------

GMAIL_USER = os.environ["GMAIL_USER"]
GMAIL_PASS = os.environ["GMAIL_PASS"]

RECIPIENTS = [
    x.strip()
    for x in os.environ["RECIPIENTS"].split(",")
    if x.strip()
]

print(f"Sender: {GMAIL_USER}")
print(f"Recipients: {RECIPIENTS}")

# ------------------------------------------------------------------
# SETTINGS
# ------------------------------------------------------------------

FILTER_DAYS = int(os.environ.get("FILTER_DAYS", 3))

URLS = {
    "Mainboard": "https://www.chittorgarh.com/report/ipo-subscription-status-live-bidding-data-bse-nse/21/mainboard/?year=2026",
    "SME": "https://www.chittorgarh.com/report/ipo-subscription-status-live-bidding-data-bse-nse/21/sme/?year=2026",
}

GMP_URL = "https://www.investorgain.com/report/ipo-gmp-live/331/all/"

# ------------------------------------------------------------------
# HELPERS
# ------------------------------------------------------------------

def get_now_ist() -> datetime.datetime:
    """
    Returns current IST time as a tz-naive datetime (UTC+5:30).
    Avoids tz-aware vs tz-naive conflicts with pandas.
    """
    return datetime.datetime.utcnow() + datetime.timedelta(hours=5, minutes=30)


def clean_columns(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.columns = [
        str(c).replace("▲", "").replace("▼", "").strip()
        for c in df.columns
    ]
    return df


def normalize_name(name) -> str:
    """Keep only alphanumeric characters and lowercase."""
    return re.sub(r"[^a-zA-Z0-9]", "", str(name)).lower()


def safe_val(row, col):
    """Return '-' if the value is missing or NaN, otherwise the value."""
    val = row.get(col, "-")
    if pd.isna(val):
        return "-"
    return val


def parse_date_series(series: pd.Series) -> pd.Series:
    """
    Parses full dates from subscription tables (e.g. '19-Jun-2026').
    """
    s = series.astype(str).str.strip()
    return pd.to_datetime(s, errors="coerce", dayfirst=True)


# ------------------------------------------------------------------
# SUBSCRIPTION SCRAPER
# ------------------------------------------------------------------

async def scrape_subscription(label, url):
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        print(f"Fetching {label} data...")

        await page.goto(
            url,
            wait_until="networkidle",
            timeout=60000
        )

        try:
            await page.wait_for_selector(
                "table tbody tr",
                timeout=25000
            )
        except:
            print(f"No rows found for {label}")
            await browser.close()
            return None

        table_html = await page.inner_html("table")
        await browser.close()
        return table_html


# ------------------------------------------------------------------
# GMP SCRAPER
# ------------------------------------------------------------------

async def scrape_gmp():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        print("Fetching GMP data...")

        await page.goto(
            GMP_URL,
            wait_until="networkidle",
            timeout=60000
        )

        try:
            await page.wait_for_function(
                """() => {
                    const rows = document.querySelectorAll('table tbody tr');
                    return rows.length > 1 ||
                           (rows.length === 1 && !rows[0].innerText.includes('No data'));
                }""",
                timeout=30000
            )
        except:
            print("No GMP data loaded.")
            await browser.close()
            return None

        table_html = await page.inner_html("table")
        await browser.close()
        return table_html


# ------------------------------------------------------------------
# LOAD SUBSCRIPTION DATA
# ------------------------------------------------------------------

async def get_subscription_data():
    results = {}

    for label, url in URLS.items():
        html = await scrape_subscription(label, url)

        if not html:
            continue

        df = pd.read_html(StringIO("<table>" + html + "</table>"))[0]
        df = df.dropna(how="all").reset_index(drop=True)
        df.insert(0, "Type", label)

        results[label] = df

    if not results:
        return pd.DataFrame()

    combined_df = pd.concat(results.values(), ignore_index=True)
    return combined_df


# ------------------------------------------------------------------
# FILTER IPOS CLOSING IN THE NEXT N DAYS
# ------------------------------------------------------------------

def filter_upcoming_ipos(subscription_df, days=FILTER_DAYS):
    if subscription_df.empty:
        return pd.DataFrame()

    work_df = clean_columns(subscription_df)

    date_col = next(
        (
            c for c in work_df.columns
            if "Closing" in str(c) or "Close" in str(c)
        ),
        None
    )

    if not date_col:
        return pd.DataFrame()

    work_df["parsed_date"] = parse_date_series(work_df[date_col])

    today = get_now_ist().replace(hour=0, minute=0, second=0, microsecond=0)
    end_date = today + datetime.timedelta(days=days, hours=23, minutes=59, seconds=59)

    mask = (
        (work_df["parsed_date"] >= today) &
        (work_df["parsed_date"] <= end_date)
    )

    result_df = work_df.loc[mask].copy()
    result_df = result_df.drop(columns=["parsed_date"], errors="ignore")

    return result_df.reset_index(drop=True)


# ------------------------------------------------------------------
# LOAD GMP DATA
# ------------------------------------------------------------------

async def get_gmp_data():
    html = await scrape_gmp()

    if not html:
        return pd.DataFrame()

    gmp_df = pd.read_html(StringIO("<table>" + html + "</table>"))[0]
    gmp_df = gmp_df.dropna(how="all").reset_index(drop=True)

    # Remove placeholder rows
    if len(gmp_df.columns) > 0:
        gmp_df = gmp_df[
            ~gmp_df.iloc[:, 0].astype(str).str.contains("No data", na=False)
        ]

    # Strip embedded "GMP" text from within cell values
    gmp_df = gmp_df.apply(
        lambda col: (
            col.astype(str)
            .str.split(pat=r"(?i)GMP", n=1, regex=True)
            .str[0]
            .str.strip()
        )
        if col.dtype == "object"
        else col
    )

    # Remove rows containing @
    gmp_df = gmp_df[
        ~gmp_df.astype(str).apply(
            lambda row: row.str.contains("@", regex=False, na=False).any(),
            axis=1
        )
    ]

    gmp_df = clean_columns(gmp_df)
    return gmp_df.reset_index(drop=True)


# ------------------------------------------------------------------
# FILTER GMP DATA TO THE SAME DATE WINDOW
# ------------------------------------------------------------------

def filter_gmp_upcoming(gmp_df, days=FILTER_DAYS):
    if gmp_df.empty:
        return pd.DataFrame()

    work_df = clean_columns(gmp_df)

    date_col = next(
        (
            c for c in work_df.columns
            if "Close" in str(c)
        ),
        None
    )

    if not date_col:
        return work_df.reset_index(drop=True)

    today = get_now_ist().replace(hour=0, minute=0, second=0, microsecond=0)
    current_year = today.year
    end_date = today + datetime.timedelta(days=days, hours=23, minutes=59, seconds=59)

    # GMP dates are always short format ("19-Jun"), so always append year
    work_df["parsed_date"] = pd.to_datetime(
        work_df[date_col].astype(str).str.strip() + f"-{current_year}",
        format="%d-%b-%Y",
        errors="coerce"
    )

    valid_dates_df = work_df.dropna(subset=["parsed_date"]).copy()

    mask = (
        (valid_dates_df["parsed_date"] >= today) &
        (valid_dates_df["parsed_date"] <= end_date)
    )

    result_df = valid_dates_df.loc[mask].copy()
    result_df = result_df.drop(columns=["parsed_date"], errors="ignore")

    return result_df.reset_index(drop=True)


# ------------------------------------------------------------------
# MERGE SUBSCRIPTION + GMP
# ------------------------------------------------------------------
def build_summary(subscription_df, gmp_df):
    if subscription_df.empty or gmp_df.empty:
        return pd.DataFrame()

    sub_work = clean_columns(subscription_df)
    gmp_work = clean_columns(gmp_df)

    merged_rows = []

    for _, g_row in gmp_work.iterrows():

        # GMP side
        g_name_norm = normalize_name(g_row.get("Name", ""))
        g_prefix = g_name_norm[:5]

        g_close_raw = str(
            g_row.get("Close", "")
        ).strip()

        for _, s_row in sub_work.iterrows():

            # Subscription side
            s_name_norm = normalize_name(
                s_row.get("Company", "")
            )
            s_prefix = s_name_norm[:5]

            s_close_raw = str(
                s_row.get("Closing Date", "")
            ).strip()

            # EXACTLY SAME LOGIC AS COLAB:
            # first 5 chars match + GMP date contained in subscription date
            if (
                g_prefix == s_prefix
                and g_close_raw in s_close_raw
            ):
                merged_rows.append({
                    "Type": safe_val(s_row, "Type"),
                    "IPO Name": safe_val(s_row, "Company"),
                    "Close": safe_val(s_row, "Closing Date"),

                    "QIB": safe_val(s_row, "QIB (x)"),
                    "sNII": safe_val(s_row, "sNII (x)"),
                    "bNII": safe_val(s_row, "bNII (x)"),
                    "NII": safe_val(s_row, "NII (x)"),
                    "Retail": safe_val(s_row, "Retail (x)"),
                    "Emp": safe_val(s_row, "Employee (x)"),
                    "SH": safe_val(s_row, "Shareholder (x)"),
                    "Total": safe_val(s_row, "Total (x)"),

                    "GMP": safe_val(g_row, "GMP"),
                    "Price": safe_val(g_row, "Price (₹)")
                })

                break

    summary_df = pd.DataFrame(merged_rows)

    # DEBUG OUTPUT
    print(f"Subscription rows: {len(sub_work)}")
    print(f"GMP rows: {len(gmp_work)}")
    print(f"Merged rows: {len(summary_df)}")

    return summary_df

# ------------------------------------------------------------------
# EMAIL
# ------------------------------------------------------------------

def send_email(df_summary):
    now_ist = get_now_ist()
    generated_str = now_ist.strftime("%d-%b-%Y %I:%M %p IST")

    if df_summary.empty:
        html_content = f"""
        <html>
        <body>
            <h2>IPO Alert</h2>
            <p>Generated: {generated_str}</p>
            <p>No IPOs found in the selected window.</p>
        </body>
        </html>
        """
        subject = "IPO Alert - No IPOs Found"
    else:
        html_table = df_summary.to_html(index=False, classes="table", border=0)
        html_content = f"""
        <html>
          <head>
            <style>
              .table {{ font-family: 'Segoe UI', Arial, sans-serif; border-collapse: collapse; width: 100%; font-size: 11px; }}
              .table td, .table th {{ border: 1px solid #ddd; padding: 5px; text-align: center; }}
              .table tr:nth-child(even) {{ background-color: #f9f9f9; }}
              .table th {{ background-color: #1a3a5c; color: white; font-weight: bold; }}
              h2 {{ font-family: Arial, sans-serif; color: #1a3a5c; }}
            </style>
          </head>
          <body>
            <h2>IPO Alert</h2>
            <p><small>Generated: {generated_str}</small></p>
            <p>IPOs closing in the selected window:</p>
            {html_table}
          </body>
        </html>
        """
        subject = f"IPO Alert - {len(df_summary)} IPO(s)"

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_USER, GMAIL_PASS)

        for recipient in RECIPIENTS:
            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"] = GMAIL_USER
            msg["To"] = recipient
            msg.attach(MIMEText(html_content, "html"))

            try:
                server.sendmail(GMAIL_USER, recipient, msg.as_string())
                print(f"Email sent to {recipient}")
            except Exception as e:
                print(f"Failed to send to {recipient}: {e}")


# ------------------------------------------------------------------
# MAIN
# ------------------------------------------------------------------

async def main():
    print("Loading subscription data...")
    subscription_df = await get_subscription_data()

    print(f"Filtering IPOs closing in next {FILTER_DAYS} days...")
    filtered_subscription_df = filter_upcoming_ipos(
        subscription_df,
        days=FILTER_DAYS
    )

    print("Loading GMP data...")
    gmp_df = await get_gmp_data()
    filtered_gmp_df = filter_gmp_upcoming(
        gmp_df,
        days=FILTER_DAYS
    )

    # ================= DEBUG =================
    print("\n========== SETTINGS ==========")
    print("FILTER_DAYS =", FILTER_DAYS)
    print("Current IST =", get_now_ist())

    print("\n========== SUBSCRIPTION DATA ==========")
    print("Total scraped:", len(subscription_df))
    print("After filter:", len(filtered_subscription_df))

    if not filtered_subscription_df.empty:
        cols = [c for c in ["Company", "Closing Date"] if c in filtered_subscription_df.columns]
        if cols:
            print(filtered_subscription_df[cols].to_string())
        else:
            print("Expected columns not found in subscription data:", list(filtered_subscription_df.columns))

    print("\n========== GMP DATA ==========")
    print("Total scraped:", len(gmp_df))
    print("After filter:", len(filtered_gmp_df))

    if not filtered_gmp_df.empty:
        cols = [c for c in ["Name", "Close"] if c in filtered_gmp_df.columns]
        if cols:
            print(filtered_gmp_df[cols].to_string())
        else:
            print("Expected columns not found in GMP data:", list(filtered_gmp_df.columns))
    # =========================================

    print("Building summary...")
    summary_df = build_summary(filtered_subscription_df, filtered_gmp_df)

    print(f"Rows in summary: {len(summary_df)}")

    send_email(summary_df)

    print("Completed successfully.")

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())