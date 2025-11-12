
import re
import io
import time
import json
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

# Core size patterns (supports fractions and decimals)
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

# Inlet/Outlet diameter extraction (inches)
INOUT_PATTERNS = [
    r'Inlet[^0-9]{0,10}(\d+(?:\.\d+)?|\d+\s*\d/\d)\s*(?:in|")',
    r'Outlet[^0-9]{0,10}(\d+(?:\.\d+)?|\d+\s*\d/\d)\s*(?:in|")',
    r'Inlet\s*/\s*Outlet[^0-9]{0,10}(\d+(?:\.\d+)?|\d+\s*\d/\d)\s*(?:in|")[^0-9]{0,8}(\d+(?:\.\d+)?|\d+\s*\d/\d)\s*(?:in|")',
    r'(?:In/Out|I/O)[^0-9]{0,6}(\d+(?:\.\d+)?|\d+\s*\d/\d)\s*(?:in|")[^0-9]{0,8}(\d+(?:\.\d+)?|\d+\s*\d/\d)\s*(?:in|")',
    r'\b(inlet|outlet)\b[^0-9]{0,6}(\d+(?:\.\d+)?|\d+\s*\d/\d)\s*(?:in|")'
]

def parse_in_out(text: str):
    if not text:
        return None, None
    t = re.sub(r"\s+", " ", text)
    inlet = outlet = None
    # Try paired first
    for pat in INOUT_PATTERNS[:2]:
        pass
    m_pair1 = re.search(INOUT_PATTERNS[2], t, flags=re.IGNORECASE)
    m_pair2 = re.search(INOUT_PATTERNS[3], t, flags=re.IGNORECASE)
    if m_pair1:
        inlet = frac_to_float(m_pair1.group(1))
        outlet = frac_to_float(m_pair1.group(2))
        return inlet, outlet
    if m_pair2:
        inlet = frac_to_float(m_pair2.group(1))
        outlet = frac_to_float(m_pair2.group(2))
        return inlet, outlet
    # Try individual
    m_in = re.search(INOUT_PATTERNS[0], t, flags=re.IGNORECASE)
    m_out = re.search(INOUT_PATTERNS[1], t, flags=re.IGNORECASE)
    if m_in:
        inlet = frac_to_float(m_in.group(1))
    if m_out:
        outlet = frac_to_float(m_out.group(1))
    return inlet, outlet

# Cross-reference extraction
# Look for lines with keywords and collect alphanumeric, dash/underscore part numbers (length 5-18)
XREF_KEYWORDS = r'(?:cross[\s-]?ref|cross[\s-]?reference|oe[\s-]?(?:part)?|oem|replaces|equivalent|interchange|xref)'
PN_TOKEN = r'[A-Z0-9][A-Z0-9\-\._]{3,17}'

def extract_cross_refs(text: str):
    if not text:
        return []
    t = text
    refs = set()
    # capture after keywords
    for m in re.finditer(XREF_KEYWORDS + r'[^:]{0,10}[:\-–]\s*([A-Z0-9,\s\-/\._]+)', t, flags=re.IGNORECASE):
        chunk = m.group(1)[:200]
        for tok in re.findall(PN_TOKEN, chunk, flags=re.IGNORECASE):
            # Filter out obvious non-PNs
            if len(tok) >= 4 and not tok.isdigit():
                refs.add(tok.strip(".,:; "))
    # also any PN-like tokens near "Kenworth", "Peterbilt", etc.
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
    except Exception as e:
        return None
    return None

def parse_product_page(url):
    resp = get(url)
    if not resp:
        return None
    soup = BeautifulSoup(resp.text, "lxml")
    text = soup.get_text(" ", strip=True)
    core = find_core_size(text)
    inlet, outlet = parse_in_out(text)
    xrefs = extract_cross_refs(text)
    img = extract_image(soup)
    title = soup.title.get_text(strip=True) if soup.title else ""
    return {
        "fetched_url": resp.url,
        "title": title,
        "core_size_hwd_in": core,
        "inlet_in": inlet,
        "outlet_in": outlet,
        "cross_refs": ", ".join(xrefs) if xrefs else "",
        "image": img
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
            try:
                from urllib.parse import urlparse, urljoin
                base = urlparse(url)
                href = urljoin(f"{base.scheme}://{base.netloc}", href)
            except:
                pass
        links.append((href, text))
    # Filter and score
    seen = set()
    out = []
    for href, text in links:
        key = (href, text)
        if href not in seen and any(k in text.lower() for k in ["charge", "cooler", "radiator", "intercooler", "cac"]):
            seen.add(href)
            out.append({"href": href, "text": text})
        if len(out) >= max_candidates:
            break
    return out

def best_guess_from_candidates(cands, query_terms):
    scored = []
    for c in cands:
        score = fuzz.token_set_ratio(" ".join(query_terms), c["text"])
        scored.append((score, c))
    scored.sort(reverse=True, key=lambda x: x[0])
    return [c for score, c in scored]

# -------------------------------
# Streamlit UI
# -------------------------------

st.set_page_config(page_title="CAC Matcher – Northern & Active (Enhanced)", layout="wide")

st.title("Charge Air Cooler Matcher – Northern & Active (Enhanced)")
st.caption("Uploads your Excel and pulls core size, inlet/outlet diameters, cross-references, image, and titles from vendor pages.")

with st.sidebar:
    st.header("Settings")
    st.markdown("Add exact product URLs in Excel columns **'Northern URL'** and **'Active URL'** for best accuracy.")
    northern_search = st.text_input(
        "Northern Search URL template",
        value="https://www.northernradiator.com/search?search={q}",
        help="Use {q} where the query should go."
    )
    active_search = st.text_input(
        "Active Radiator Search URL template",
        value="https://www.activeradiator.com/?s={q}",
        help="Use {q} where the query should go."
    )
    max_candidates = st.slider("Max search candidates per site", 1, 10, 5)
    rate_limit = st.slider("Requests per minute (polite scraping)", 5, 60, 20)
    sleep_sec = max(1, 60 // rate_limit)

uploaded = st.file_uploader("Upload your Excel (Top CACs.xlsx)", type=["xlsx"])

template_cols = ["Your Part No", "Application / Truck Model", "Northern URL", "Active URL"]

if uploaded is not None:
    try:
        df = pd.read_excel(uploaded)
    except Exception as e:
        st.error(f"Failed to read Excel: {e}")
        st.stop()

    st.subheader("Input Preview")
    st.dataframe(df.head(20), use_container_width=True)

    # Column auto-map
    colmap = {}
    for need in template_cols:
        best = process.extractOne(need, df.columns, scorer=fuzz.WRatio)
        if best and best[1] >= 85:
            colmap[need] = best[0]
        else:
            colmap[need] = None

    if not colmap["Your Part No"]:
        st.error("Please include a 'Your Part No' column (or a very similar name).")
        st.stop()

    st.markdown("**Detected column mapping:**")
    st.json(colmap)

    if st.button("Run Matching", type="primary"):
        results = []
        for _, row in df.iterrows():
            part_no = str(row.get(colmap.get("Your Part No", ""), "")).strip()
            appl = str(row.get(colmap.get("Application / Truck Model", ""), "")).strip()
            n_url = str(row.get(colmap.get("Northern URL", ""), "")).strip() if colmap.get("Northern URL") else ""
            a_url = str(row.get(colmap.get("Active URL", ""), "")).strip() if colmap.get("Active URL") else ""

            entry = {"Your Part No": part_no, "Application / Truck Model": appl}

            # Northern flow
            n_info = None
            if n_url.lower().startswith("http"):
                n_info = parse_product_page(n_url)
                time.sleep(sleep_sec)
            else:
                q_terms = [part_no] + ([appl] if appl else [])
                cands = best_guess_from_candidates(site_search(northern_search, " ".join(q_terms), max_candidates=max_candidates), q_terms)
                for c in cands[:3]:
                    tmp = parse_product_page(c["href"])
                    time.sleep(sleep_sec)
                    if tmp and (tmp.get("core_size_hwd_in") or tmp.get("cross_refs")):
                        n_info = tmp
                        break

            # Active flow
            a_info = None
            if a_url.lower().startswith("http"):
                a_info = parse_product_page(a_url)
                time.sleep(sleep_sec)
            else:
                q_terms = [part_no] + ([appl] if appl else [])
                cands = best_guess_from_candidates(site_search(active_search, " ".join(q_terms), max_candidates=max_candidates), q_terms)
                for c in cands[:3]:
                    tmp = parse_product_page(c["href"])
                    time.sleep(sleep_sec)
                    if tmp and (tmp.get("core_size_hwd_in") or tmp.get("cross_refs")):
                        a_info = tmp
                        break

            # Assemble output
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

        # Download button
        out = io.BytesIO()
        with pd.ExcelWriter(out, engine="openpyxl") as writer:
            out_df.to_excel(writer, index=False, sheet_name="Matches")
        st.download_button(
            "Download Excel Results",
            data=out.getvalue(),
            file_name="Top CACs - USA Matches.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )

        st.success("Done. Review blanks and consider adding direct product URLs for exact matches.")
else:
    st.info("Upload your Excel to begin. Include at least 'Your Part No'. Optional: 'Application / Truck Model', 'Northern URL', 'Active URL'.")
