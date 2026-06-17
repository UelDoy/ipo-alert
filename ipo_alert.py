import os
import smtplib
import datetime
import pandas as pd

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

FILTER_DAYS = 3

URLS = {
    "Mainboard": "https://www.chittorgarh.com/report/ipo-subscription-status-live-bidding-data-bse-nse/21/mainboard/?year=2026",
    "SME": "https://www.chittorgarh.com/report/ipo-subscription-status-live-bidding-data-bse-nse/21/sme/?year=2026",
}

GMP_URL = "https://www.investorgain.com/report/ipo-gmp-live/331/nonzero/"

# ------------------------------------------------------------------
# HELPERS
# ------------------------------------------------------------------

def clean_columns(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.columns = [
        str(c).replace("▲", "").replace("▼", "").strip()
        for c in df.columns
    ]
    return df


def normalize_ipo_name(value) -> str:
    return (
        str(value)
        .lower()
        .split(" (")[0]
        .split(" bse")[0]
        .split(" nse")[0]
        .strip()
    )


def parse_date_series(series: pd.Series) -> pd.Series:
    """
    Try to parse dates from subscription/GMP tables.
    Supports both full dates (e.g. 25-Jun-2026) and dates without year
    (e.g. 25-Jun, which gets the current year appended).
    """
    s = series.astype(str).str.strip()

    parsed = pd.to_datetime(s, errors="coerce", dayfirst=True)

    if parsed.notna().any():
        return parsed

    current_year = datetime.datetime.now().year
    parsed = pd.to_datetime(
        s + f"-{current_year}",
        errors="coerce",
        format="%d-%b-%Y"
    )
    return parsed


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

    today = datetime.datetime.now().replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0
    )
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

    work_df["parsed_date"] = parse_date_series(work_df[date_col])

    today = datetime.datetime.now().replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0
    )
    end_date = today + datetime.timedelta(days=days, hours=23, minutes=59, seconds=59)

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
        g_name = normalize_ipo_name(g_row.get("Name", ""))

        for _, s_row in sub_work.iterrows():
            s_name = str(s_row.get("Company", "")).lower().strip()

            if g_name in s_name or s_name in g_name:
                merged_rows.append({
                    "Type": s_row.get("Type", "-"),
                    "IPO Name": s_row.get("Company", "-"),
                    "Close": s_row.get("Closing Date", "-"),
                    "QIB": s_row.get("QIB (x)", "-"),
                    "sNII": s_row.get("sNII (x)", "-"),
                    "bNII": s_row.get("bNII (x)", "-"),
                    "NII": s_row.get("NII (x)", "-"),
                    "Retail": s_row.get("Retail (x)", "-"),
                    "Emp": s_row.get("Employee (x)", "-"),
                    "SH": s_row.get("Shareholder (x)", "-"),
                    "Total": s_row.get("Total (x)", "-"),
                    "GMP": g_row.get("GMP", "-"),
                    "Price": g_row.get("Price (₹)", "-"),
                })
                break

    return pd.DataFrame(merged_rows)


# ------------------------------------------------------------------
# EMAIL
# ------------------------------------------------------------------

def send_email(df_summary):
    if df_summary.empty:
        html_content = f"""
        <html>
        <body>
            <h2>IPO Alert</h2>
            <p>No IPOs found in the selected window.</p>
            <p>Generated: {datetime.datetime.now()}</p>
        </body>
        </html>
        """
        subject = "IPO Alert - No IPOs Found"
    else:
        html_table = df_summary.to_html(index=False, border=0)
        html_content = f"""
        <html>
        <body>
            <h2>IPO Alert</h2>
            <p>IPOs closing in the selected window:</p>
            {html_table}
            <p><small>Generated: {datetime.datetime.now()}</small></p>
        </body>
        </html>
        """
        subject = f"IPO Alert - {len(df_summary)} IPO(s)"

    for recipient in RECIPIENTS:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = GMAIL_USER
        msg["To"] = recipient

        msg.attach(MIMEText(html_content, "html"))

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL_USER, GMAIL_PASS)
            server.sendmail(GMAIL_USER, recipient, msg.as_string())

        print(f"Email sent to {recipient}")


# ------------------------------------------------------------------
# MAIN
# ------------------------------------------------------------------

async def main():
    print("Loading subscription data...")
    subscription_df = await get_subscription_data()

    print(f"Filtering IPOs closing in next {FILTER_DAYS} days...")
    filtered_subscription_df = filter_upcoming_ipos(subscription_df, days=FILTER_DAYS)

    print("Loading GMP data...")
    gmp_df = await get_gmp_data()
    filtered_gmp_df = filter_gmp_upcoming(gmp_df, days=FILTER_DAYS)

    print("Building summary...")
    summary_df = build_summary(filtered_subscription_df, filtered_gmp_df)

    print(f"Rows in summary: {len(summary_df)}")

    send_email(summary_df)

    print("Completed successfully.")


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())