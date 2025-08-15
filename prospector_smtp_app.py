import re
import time
import random
import ssl
import smtplib
from email.message import EmailMessage
from urllib.parse import urlparse, quote

import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ---------------------- App setup ----------------------
st.set_page_config(page_title="Prospector (SMTP) — GC/Builders/Architects", layout="wide")
st.title("Local Prospector — GC / Builders / Architects (SMTP email)")

# Sender identity (fixed as requested)
FROM_NAME = st.secrets.get("FROM_NAME", "Miami Master Flooring")
FROM_EMAIL = "info@miamimasterflooring.com"  # fixed per your request

# SMTP config (can be stored in Secrets or input in the UI sidebar)
DEFAULT_SMTP = {
    "SMTP_HOST": st.secrets.get("SMTP_HOST", ""),
    "SMTP_PORT": int(st.secrets.get("SMTP_PORT", 587)),
    "SMTP_USER": st.secrets.get("SMTP_USER", ""),
    "SMTP_PASS": st.secrets.get("SMTP_PASS", ""),
    "SMTP_STARTTLS": bool(st.secrets.get("SMTP_STARTTLS", True)),  # True for STARTTLS, False for SSL (465)
}

EMAIL_RE  = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
PHONE_RE  = re.compile(r"\+?1?[\s\-\.\(]?\d{3}[\)\s\-\.\)]?\s?\d{3}\s?[\-\.\s]?\d{4}")
SOCIAL_DOMAINS = (
    "facebook.com","instagram.com","linkedin.com","twitter.com","x.com",
    "youtube.com","yelp.com","angieslist.com","houzz.com","pinterest.com","tiktok.com"
)

if "leads" not in st.session_state:
    st.session_state.leads = pd.DataFrame(columns=["Company","Email","Website","Phone","Source"])

# ---------------------- Robust HTTP session ----------------------
def _session_with_retries():
    s = requests.Session()
    r = Retry(
        total=6, connect=3, read=3, status=3,
        backoff_factor=0.7,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET","POST"]),
        raise_on_status=False,
    )
    s.mount("http://", HTTPAdapter(max_retries=r))
    s.mount("https://", HTTPAdapter(max_retries=r))
    s.headers.update({
        "User-Agent": random.choice([
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.5 Safari/605.1.15",
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123 Safari/537.36",
        ]),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Connection": "keep-alive",
    })
    return s

HTTP = _session_with_retries()

def domain_of(u: str) -> str:
    try:
        return urlparse(u).netloc.lower()
    except Exception:
        return ""

def looks_like_business_site(u: str) -> bool:
    d = domain_of(u)
    if not d:
        return False
    if any(s in d for s in SOCIAL_DOMAINS):
        return False
    return d.endswith(".com") or d.endswith(".net") or d.endswith(".org")

# ---------------------- Search providers ----------------------
def search_bing_api(query: str, key: str, count: int = 20):
    """Bing Web Search API (recommended)."""
    if not key:
        return []
    try:
        endpoint = "https://api.bing.microsoft.com/v7.0/search"
        headers = {"Ocp-Apim-Subscription-Key": key}
        params = {"q": query, "mkt": "en-US", "count": count}
        r = HTTP.get(endpoint, headers=headers, params=params, timeout=20)
        r.raise_for_status()
        data = r.json()
        urls = [v["url"] for v in (data.get("webPages") or {}).get("value", []) if v.get("url")]
        return [u for u in urls if looks_like_business_site(u)][:count]
    except Exception:
        return []

def search_serp_api(query: str, base_url: str, key: str, method: str = "GET",
                    auth_header: str = "X-API-KEY", key_param: str | None = None,
                    count: int = 20):
    """
    Generic SERP API adapter. Works with many providers that return JSON.
    Supports header auth (X-API-KEY) or query param (?api_key=...).
    Expected JSON can be:
      - { "webPages": { "value": [ { "url": ... }, ... ] } }
      - { "results": [ { "url": ... } ] }
      - [ "https://...", ... ]
    """
    if not base_url or not key:
        return []
    try:
        headers = {"User-Agent": HTTP.headers.get("User-Agent")}
        if auth_header:
            headers[auth_header] = key
        params = {"q": query, "count": count}
        if key_param:
            params[key_param] = key

        if method.upper() == "POST":
            r = HTTP.post(base_url, headers=headers, json=params, timeout=25)
        else:
            r = HTTP.get(base_url, headers=headers, params=params, timeout=25)
        r.raise_for_status()
        data = r.json()

        urls = []
        if isinstance(data, dict):
            if "webPages" in data and "value" in data["webPages"]:
                urls = [v.get("url") for v in data["webPages"]["value"] if v.get("url")]
            elif "results" in data and isinstance(data["results"], list):
                for item in data["results"]:
                    u = item.get("url") or item.get("link")
                    if u: urls.append(u)
        elif isinstance(data, list):
            for item in data:
                if isinstance(item, str): urls.append(item)
                elif isinstance(item, dict):
                    u = item.get("url") or item.get("link")
                    if u: urls.append(u)

        urls = [u for u in urls if u and looks_like_business_site(u)]
        return urls[:count]
    except Exception:
        return []

# ---------------------- Extraction ----------------------
def extract_company_info(url: str):
    """Fetch HTML and extract company name, email, phone."""
    try:
        r = HTTP.get(url, timeout=15)
        r.raise_for_status()
        html = r.text
    except Exception:
        return None, None, None

    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)
    emails = EMAIL_RE.findall(text)
    phones = PHONE_RE.findall(text)

    company = None
    if soup.title and soup.title.string:
        company = soup.title.string.split(" | ")[0].split(" – ")[0].strip()[:120]
    if not company:
        h1 = soup.find("h1")
        if h1 and h1.get_text(strip=True):
            company = h1.get_text(strip=True)[:120]

    email = emails[0] if emails else None
    phone = phones[0] if phones else None
    return company, email, phone

def try_candidate_pages(base_url: str):
    root = base_url.rstrip("/")
    return [base_url, f"{root}/contact", f"{root}/contact-us", f"{root}/about", f"{root}/team"]

def upsert_lead(name, email, website, phone, source):
    if not email:
        return
    df = st.session_state.leads
    lowers = set(df["Email"].str.lower())
    if email.lower() in lowers:
        return
    st.session_state.leads.loc[len(df)] = {
        "Company": name or "", "Email": email.strip(), "Website": website,
        "Phone": phone or "", "Source": source
    }

# ---------------------- SMTP email ----------------------
def send_email_smtp(
    smtp_host: str,
    smtp_port: int,
    smtp_user: str,
    smtp_pass: str,
    starttls: bool,
    to_email: str,
    subject: str,
    html: str,
) -> None:
    msg = EmailMessage()
    msg["From"] = f"{FROM_NAME} <{FROM_EMAIL}>"
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.set_content("This is an HTML email. If you see this, your client is showing the plain-text part.")
    msg.add_alternative(html, subtype="html")

    if starttls:  # typical for port 587
        context = ssl.create_default_context()
        with smtplib.SMTP(smtp_host, smtp_port, timeout=20) as server:
            server.ehlo()
            server.starttls(context=context)
            server.ehlo()
            if smtp_user or smtp_pass:
                server.login(smtp_user, smtp_pass)
            server.send_message(msg)
    else:  # SSL (port 465)
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(smtp_host, smtp_port, context=context, timeout=20) as server:
            if smtp_user or smtp_pass:
                server.login(smtp_user, smtp_pass)
            server.send_message(msg)

# ---------------------- UI: Sidebar (providers + SMTP) ----------------------
with st.sidebar:
    st.subheader("Search provider")
    provider = st.selectbox("Choose", ["Bing API (recommended)", "Generic SERP API"], index=0)

    if provider == "Bing API (recommended)":
        BING_API_KEY = st.secrets.get("BING_API_KEY", st.text_input("BING_API_KEY (or add to Secrets)", type="password"))
        SERP_BASE_URL = ""
        SERP_KEY = ""
        SERP_METHOD = "GET"
        SERP_AUTH_HEADER = "X-API-KEY"
        SERP_KEY_PARAM = ""
    else:
        SERP_BASE_URL = st.text_input("SERP Base URL (JSON endpoint)")
        SERP_KEY = st.secrets.get("SERP_API_KEY", st.text_input("SERP API Key (or add to Secrets)", type="password"))
        SERP_METHOD = st.selectbox("SERP HTTP Method", ["GET","POST"], index=0)
        SERP_AUTH_HEADER = st.text_input("Auth Header (blank if using query param)", value="X-API-KEY")
        SERP_KEY_PARAM = st.text_input("Key Query Param (e.g., api_key)", value="")

    st.markdown("---")
    st.subheader("SMTP settings")
    SMTP_HOST = st.text_input("SMTP_HOST", value=DEFAULT_SMTP["SMTP_HOST"])
    SMTP_PORT = st.number_input("SMTP_PORT", value=DEFAULT_SMTP["SMTP_PORT"], step=1)
    SMTP_USER = st.text_input("SMTP_USER (often your full email)", value=DEFAULT_SMTP["SMTP_USER"])
    SMTP_PASS = st.text_input("SMTP_PASS", value=DEFAULT_SMTP["SMTP_PASS"], type="password")
    SMTP_STARTTLS = st.checkbox("Use STARTTLS (587)", value=DEFAULT_SMTP["SMTP_STARTTLS"])

# ---------------------- Tabs ----------------------
tab_search, tab_results, tab_email, tab_export = st.tabs(["Search", "Results", "Email", "Export/Import"])

with tab_search:
    st.subheader("Find GC / Builders / Architects near you")
    col1, col2 = st.columns(2)
    with col1:
        location = st.text_input("City / Area", value="Miami, FL")
        radius_phrase = st.select_slider("Radius phrase", ["5 miles","10 miles","25 miles","50 miles"], value="25 miles")
    with col2:
        categories = st.multiselect("Categories", ["General Contractors","Builders","Architects"],
                                    default=["General Contractors","Builders","Architects"])
        rate_delay = st.slider("Delay between requests (sec)", 0.0, 3.0, 1.0, 0.1)
    max_sites = st.slider("Max sites (total)", 10, 200, 60, 10)

    if st.button("Search & Extract"):
        queries = []
        if "General Contractors" in categories:
            queries.append(f'General Contractors "{location}" site:.com OR site:.net OR site:.org "{radius_phrase}"')
        if "Builders" in categories:
            queries.append(f'Home Builders "{location}" site:.com OR site:.net OR site:.org "{radius_phrase}"')
        if "Architects" in categories:
            queries.append(f'Architecture Firms "{location}" site:.com OR site:.net OR site:.org "{radius_phrase}"')

        per_q = max(10, max_sites // max(len(queries), 1))
        all_urls = []

        for q in queries:
            if provider.startswith("Bing API"):
                urls = search_bing_api(q, key=BING_API_KEY, count=per_q)
            else:
                urls = search_serp_api(
                    q, base_url=SERP_BASE_URL, key=SERRP_KEY if (SERRP_KEY:=SERP_KEY) else "", method=SERP_METHOD,
                    auth_header=(SERP_AUTH_HEADER or None),
                    key_param=(SERP_KEY_PARAM or None),
                    count=per_q
                )
            all_urls += urls
            time.sleep(rate_delay or 1.0)

        # Deduplicate by domain
        by_domain = {}
        for u in all_urls:
            d = domain_of(u) or u
            if d not in by_domain:
                by_domain[d] = u

        urls = list(by_domain.values())[:max_sites]
        st.write(f"Unique candidate sites: **{len(urls)}**")

        added = 0
        for base in urls:
            for target in try_candidate_pages(base):
                name, email, phone = extract_company_info(target)
                if email:
                    upsert_lead(name, email, base, phone, source=("bing" if provider.startswith("Bing") else "serp"))
                    added += 1
                    break
                time.sleep(rate_delay or 1.0)
        st.success(f"Added {added} contacts. Check **Results** tab.")

with tab_results:
    st.subheader("Leads")
    df = st.session_state.leads.copy()
    if df.empty:
        st.info("No leads yet. Run a search first.")
    else:
        st.dataframe(df, use_container_width=True, hide_index=True)
        st.caption(f"Total leads: {len(df)}")

with tab_email:
    st.subheader("Email campaign (SMTP)")
    st.caption(f"Sender: {FROM_EMAIL} (fixed). Configure SMTP in the sidebar.")

    colA, colB = st.columns(2)
    with colA:
        subject = st.text_input("Subject", "Flooring Installations for Your Upcoming Projects")
        greeting = st.text_input("Greeting", "Dear Team,")
        body = st.text_area(
            "Body (HTML allowed)",
            value=(
                "<p>We specialize in high-quality flooring installations for commercial and residential projects in your area.</p>"
                "<ul><li>Luxury vinyl plank (LVP)</li><li>Waterproof flooring</li>"
                "<li>Custom tile & stone</li><li>10-year craftsmanship warranty</li></ul>"
                "<p>Could we schedule a brief call next week?</p>"
            ),
            height=180,
        )
    with colB:
        signature = st.text_area(
            "Signature (HTML allowed)",
            value=(
                f"<p>Best regards,<br>{FROM_NAME}<br>{FROM_EMAIL}<br>(305) 000-0000<br>"
                "<a href='https://www.miamimasterflooring.com' target='_blank'>www.miamimasterflooring.com</a></p>"
                "<p style='font-size:12px;color:#666'>If you prefer not to receive these emails, reply with 'unsubscribe'.</p>"
            ),
            height=180,
        )
        daily_cap = st.number_input("Daily send cap", min_value=10, max_value=500, value=100, step=10)

    emails = st.session_state.leads["Email"].dropna().tolist() if not st.session_state.leads.empty else []
    preview = st.selectbox("Preview recipient", options=(emails[:50] or ["no-data"]))

    def render_html(greeting, body, signature):
        return f"{greeting}<br/>{body}{signature}"

    c1, c2 = st.columns(2)
    if c1.button("Send test to preview"):
        if preview and preview != "no-data":
            try:
                send_email_smtp(
                    SMTP_HOST, int(SMTP_PORT), SMTP_USER, SMTP_PASS, SMTP_STARTTLS,
                    preview, subject, render_html(greeting, body, signature)
                )
                st.success(f"Sent to {preview}")
            except Exception as e:
                st.error(f"Send failed: {e}")

    if c2.button("Send campaign now (up to cap)"):
        sent = 0
        for e in emails:
            if sent >= daily_cap:
                break
            try:
                send_email_smtp(
                    SMTP_HOST, int(SMTP_PORT), SMTP_USER, SMTP_PASS, SMTP_STARTTLS,
                    e, subject, render_html(greeting, body, signature)
                )
                sent += 1
                time.sleep(0.3)
            except Exception:
                continue
        st.success(f"Sent {sent} emails.")

with tab_export:
    st.subheader("Export / Import")
    df = st.session_state.leads.copy()
    colX, colY = st.columns(2)
    with colX:
        if not df.empty:
            st.download_button(
                "Download leads.csv", data=df.to_csv(index=False),
                file_name="leads.csv", mime="text/csv"
            )
    with colY:
        up = st.file_uploader("Import leads.csv", type=["csv"])
        if up is not None:
            try:
                new = pd.read_csv(up)
                rename = {c: c.strip().title() for c in new.columns}
                new.rename(columns=rename, inplace=True)
                existing = set(st.session_state.leads["Email"].str.lower())
                imported = 0
                for _, row in new.iterrows():
                    email = str(row.get("Email","") or "").strip()
                    if not email or not EMAIL_RE.match(email): continue
                    if email.lower() in existing: continue
                    st.session_state.leads.loc[len(st.session_state.leads)] = {
                        "Company": str(row.get("Company","") or "")[:120],
                        "Email": email,
                        "Website": str(row.get("Website","") or ""),
                        "Phone": str(row.get("Phone","") or ""),
                        "Source": "import",
                    }
                    existing.add(email.lower())
                    imported += 1
                st.success(f"Imported {imported} leads.")
            except Exception as e:
                st.error(f"Import failed: {e}")
