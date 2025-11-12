
import re
import io
import time
import pandas as pd
import requests
from bs4 import BeautifulSoup
from rapidfuzz import fuzz, process
import streamlit as st

# -------------------------------
# Helpers
# -------------------------------

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
DEFAULT_HEADERS = {"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.8"}

CORE_SIZE_PATTERNS = [
    r'(\d+(?:\s*\d\/\d)?)(?:["\s]*|in\.?)\s*[xX×]\s*(\d+(?:\s*\d\/\d)?)(?:["\s]*|in\.?)\s*[xX×]\s*(\d+(?:\s*\d\/\d)?)\s*(?:"|in\.?)',
    r'(\d+(?:\.\d+)?)(?:["\s]*|in\.?)\s*[xX×]\s*(\d+(?:\.\d+)?)(?:["\s]*|in\.?)\s*[xX×]\s*(\d+(?:\.\d+)?)\s*(?:in|")',
]

FRACTION_MAP = {
    "1/8": 0.125, "1/4": 0.25, "3/8": 0.375, "1/2": 0.5, "5/8": 0.625,
    "3/4": 0.75, "7/8": 0.875
}

def frac_to_float(tok: str) -> float:
    tok = tok.strip()
    if " " in tok:
        whole, frac = tok.split(" ", 1)
        try:
            num, den = frac.strip().split("/")
            return float(whole) + float(num)/float(den)
        except:
            return float(whole) + FRACTION_MAP.get(frac.strip(), 0.0)
    if "/" in tok:
        try:
            num, den = tok.split("/")
            return float(num)/float(den)
        except:
            return 0.0
    try:
        return float(tok)
    except:
        return 0.0

def find_core_size(text: str):
    if not text:
        return None
    t = re.sub(r"\s+", " ", text)
    for pat in CORE_SIZE_PATTERNS:
        m = re.search(pat, t, flags=re.IGNORECASE)
        if m:
            H = frac_to_float(m.group(1))
            W = frac_to_float(m.group(2))
            D = frac_to_float(m.group(3))
            if all(v > 0 for v in [H,W,D]):
                return H, W, D
    return None

INOUT_PATTERNS = [
    r'Inlet[^0-9]{0,10}(\d+(?:\.\d+)?|\d+\s*\d/\d)\s*(?:in|")',
    r'Outlet[^0-9]{0,10}(\d+(?:\.\d+)?|\d+\s*\d/\d)\s*(?:in|")',
    r'Inlet\s*/\s*Outlet[^0-9]{0,10}(\d+(?:\.\d+)?|\d+\s*\d/\d)\s*(?:in|")[^0-9]{0,8}(\d+(?:\.\d+)?|\d+\s*\d/\d)\s*(?:in|")',
    r'(?:In/Out|I/O)[^0-9]{0,6}(\d+(?:\.\d+)?|\d+\s*\d/\d)\s*(?:in|")[^0-9]{0,8}(\d+(?:\.\d+)?|\d+\s*\d/\d)\s*(?:in|")',
]

def parse_in_out(text: str):
    if not text:
        return None, None
    t = re.sub(r"\s+", " ", text)
    inlet = outlet = None
    m_pair1 = re.search(INOUT_PATTERNS[2], t, flags=re.IGNORECASE)
    m_pair2 = re.search(INOUT_PATTERNS[3], t, flags=re.IGNORECASE)
    if m_pair1:
        return frac_to_float(m_pair1.group(1)), frac_to_float(m_pair1.group(2))
    if m_pair2:
        return frac_to_float(m_pair2.group(1)), frac_to_float(m_pair2.group(2))
    m_in = re.search(INOUT_PATTERNS[0], t, flags=re.IGNORECASE)
    m_out = re.search(INOUT_PATTERNS[1], t, flags=re.IGNORECASE)
    if m_in:
        inlet = frac_to_float(m_in.group(1))
    if m_out:
        outlet = frac_to_float(m_out.group(1))
    return inlet, outlet

XREF_KEYWORDS = r'(?:cross[\s-]?ref|cross[\s-]?reference|oe[\s-]?(?:part)?|oem|replaces|equivalent|interchange|xref)'
PN_TOKEN = r'[A-Z0-9][A-Z0-9\-\._]{3,17}'

def extract_cross_refs(text: str):
    if not text:
        return []
    t = text
    refs = set()
    for m in re.finditer(XREF_KEYWORDS + r'[^:]{0,10}[:\-–]\s*([A-Z0-9,\s\-/\._]+)', t, flags=re.IGNORECASE):
        chunk = m.group(1)[:200]
        for tok in re.findall(PN_TOKEN, chunk, flags=re.IGNORECASE):
            if len(tok) >= 4 and not tok.isdigit():
                refs.add(tok.strip(".,:; "))
    for m in re.finditer(r'(Kenworth|Peterbilt|Freightliner|Mack|Volvo)[^\.]{0,100}', t, flags=re.IGNORECASE):
        chunk = m.group(0)
        for tok in re.findall(PN_TOKEN, chunk, flags=re.IGNORECASE):
            if len(tok) >= 4 and not tok.isdigit():
                refs.add(tok.strip(".,:; "))
    return sorted(refs)

def extract_image(soup: BeautifulSoup):
    og = soup.find("meta", property="og:image")
    if og and og.get("content"):
        return og["content"]
    img = soup.find("img")
    if img and img.get("src"):
        return img["src"]
    return None

def get(url, timeout=25):
    try:
        resp = requests.get(url, headers=DEFAULT_HEADERS, timeout=timeout, allow_redirects=True)
        if resp.ok:
            return resp
    except Exception:
        return None
    return None

def parse_product_page(url):
    resp = get(url)
    if not resp:
        return None
    soup = BeautifulSoup(resp.text, "lxml")
    text = soup.get_text(" ", strip=True)
    return {
        "fetched_url": resp.url,
        "title": soup.title.get_text(strip=True) if soup.title else "",
        "core_size_hwd_in": find_core_size(text),
        "inlet_in": parse_in_out(text)[0],
        "outlet_in": parse_in_out(text)[1],
        "cross_refs": ", ".join(extract_cross_refs(text)) or "",
        "image": extract_image(soup)
    }

def site_search(search_url_tmpl: str, query: str, max_candidates=5):
    if not search_url_tmpl:
        return []
    url = search_url_tmpl.format(q=requests.utils.quote(query))
    resp = get(url)
    if not resp:
        return []
    soup = BeautifulSoup(resp.text, "lxml")
    links = []
    for a in soup.select("a[href]"):
        href = a.get("href","")
        text = a.get_text(" ", strip=True)
        if not href:
            continue
        if href.startswith("//"):
            href = "https:" + href
        elif href.startswith("/"):
            from urllib.parse import urlparse, urljoin
            base = urlparse(url)
            href = urljoin(f"{base.scheme}://{base.netloc}", href)
        links.append((href, text))
    out = []
    for href, text in links:
        if any(k in text.lower() for k in ["charge", "cooler", "radiator", "intercooler", "cac"]):
            out.append({"href": href, "text": text})
        if len(out) >= max_candidates:
            break
    return out

# -------------------------------
# UI
# -------------------------------

st.set_page_config(page_title="CAC Matcher – Safer UI", layout="wide")
st.title("Charge Air Cooler Matcher – Northern & Active (Safer UI)")
st.caption("Run button always visible. Manual column mapping included.")

with st.sidebar:
    st.header("Search Settings")
    northern_search = st.text_input("Northern search URL", value="https://www.northernradiator.com/search?search={q}")
    active_search = st.text_input("Active search URL", value="https://www.activeradiator.com/?s={q}")
    max_candidates = st.slider("Max candidates per site", 1, 10, 5)
    rate_limit = st.slider("Requests per minute", 5, 60, 20)
    sleep_sec = max(1, 60 // rate_limit)

uploaded = st.file_uploader("Upload your Excel (Top CACs.xlsx)", type=["xlsx"])

if uploaded is not None:
    df = pd.read_excel(uploaded)
    st.subheader("Input Preview")
    st.dataframe(df.head(20), use_container_width=True)

    st.subheader("Column Mapping")
    cols = list(df.columns)

    def autosuggest(name):
        best = process.extractOne(name, cols, scorer=fuzz.WRatio)
        return best[0] if best and best[1] >= 80 else None

    your_part_suggest = autosuggest("Your Part No") or autosuggest("RSH PN") or cols[0]
    appl_suggest = autosuggest("Application / Truck Model")
    northern_suggest = autosuggest("Northern URL")
    active_suggest = autosuggest("Active URL")

    your_part_col = st.selectbox("Your Part No column *", options=cols, index=cols.index(your_part_suggest))
    appl_col = st.selectbox("Application / Truck Model (optional)", options=["<None>"] + cols, index=(["<None>"] + cols).index(appl_suggest) if appl_suggest else 0)
    northern_col = st.selectbox("Northern URL (optional)", options=["<None>"] + cols, index=(["<None>"] + cols).index(northern_suggest) if northern_suggest else 0)
    active_col = st.selectbox("Active URL (optional)", options=["<None>"] + cols, index=(["<None>"] + cols).index(active_suggest) if active_suggest else 0)

    run = st.button("Run Matching", type="primary")

    if run:
        results = []
        for _, row in df.iterrows():
            part_no = str(row[your_part_col]).strip()
            appl = "" if appl_col == "<None>" else str(row[appl_col]).strip()
            n_url = "" if northern_col == "<None>" else str(row[northern_col]).strip()
            a_url = "" if active_col == "<None>" else str[row[active_col]].strip() if active_col != "<None>" else ""

            entry = {"Your Part No": part_no, "Application / Truck Model": appl}

            # Northern
            n_info = None
            if n_url.lower().startswith("http"):
                n_info = parse_product_page(n_url)
                time.sleep(sleep_sec)
            else:
                query = " ".join([part_no, appl]).strip()
                cands = site_search(northern_search, query, max_candidates=max_candidates)
                for c in cands[:3]:
                    tmp = parse_product_page(c["href"])
                    time.sleep(sleep_sec)
                    if tmp and (tmp.get("core_size_hwd_in") or tmp.get("cross_refs")):
                        n_info = tmp
                        break

            # Active
            a_info = None
            if a_url.lower().startswith("http"):
                a_info = parse_product_page(a_url)
                time.sleep(sleep_sec)
            else:
                query = " ".join([part_no, appl]).strip()
                cands = site_search(active_search, query, max_candidates=max_candidates)
                for c in cands[:3]:
                    tmp = parse_product_page(c["href"])
                    time.sleep(sleep_sec)
                    if tmp and (tmp.get("core_size_hwd_in") or tmp.get("cross_refs")):
                        a_info = tmp
                        break

            def upd(prefix, info):
                if info:
                    entry.update({
                        f"{prefix} URL (final)": info.get("fetched_url", ""),
                        f"{prefix} Title": info.get("title", ""),
                        f"{prefix} Core Size (H×W×D, in)": info.get("core_size_hwd_in", ""),
                        f"{prefix} Inlet (in)": info.get("inlet_in", ""),
                        f"{prefix} Outlet (in)": info.get("outlet_in", ""),
                        f"{prefix} Cross-Refs": info.get("cross_refs", ""),
                        f"{prefix} Image": info.get("image", ""),
                    })
                else:
                    entry.update({
                        f"{prefix} URL (final)": "",
                        f"{prefix} Title": "",
                        f"{prefix} Core Size (H×W×D, in)": "",
                        f"{prefix} Inlet (in)": "",
                        f"{prefix} Outlet (in)": "",
                        f"{prefix} Cross-Refs": "",
                        f"{prefix} Image": "",
                    })

            upd("Northern", n_info)
            upd("Active", a_info)
            results.append(entry)

        out_df = pd.DataFrame(results)
        st.subheader("Matched Results")
        st.dataframe(out_df, use_container_width=True)

        out = io.BytesIO()
        with pd.ExcelWriter(out, engine="openpyxl") as writer:
            out_df.to_excel(writer, index=False, sheet_name="Matches")
        st.download_button(
            "Download Excel Results",
            data=out.getvalue(),
            file_name="Top CACs - USA Matches.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
else:
    st.info("Upload your Excel to begin. The Run button will appear below the column mapping once your file is loaded.")
