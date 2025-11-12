
import re
import io
import time
import traceback
import pandas as pd
import requests
from bs4 import BeautifulSoup
from rapidfuzz import fuzz, process
import streamlit as st

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
DEFAULT_HEADERS = {"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.8"}

REQUEST_TIMEOUT = 15

# ---------------- Parsers ----------------

CORE_SIZE_PATTERNS = [
    # Plain H x W x D (fractions or decimals)
    r'(\d+(?:\s*\d\/\d)?)[\s"]*[xX×]\s*(\d+(?:\s*\d\/\d)?)[\s"]*[xX×]\s*(\d+(?:\s*\d\/\d)?)\s*(?:in|")',
    r'(\d+(?:\.\d+)?)[\s"]*[xX×]\s*(\d+(?:\.\d+)?)[\s"]*[xX×]\s*(\d+(?:\.\d+)?)\s*(?:in|")',
    # Labelled: Height: 37.25", Width: 33.375", Depth: 2"
    r'Height[^0-9]{0,6}(\d+(?:\.\d+)?|\d+\s*\d/\d)\s*(?:in|")[^H]*Width[^0-9]{0,6}(\d+(?:\.\d+)?|\d+\s*\d/\d)\s*(?:in|")[^D]*Depth[^0-9]{0,6}(\d+(?:\.\d+)?|\d+\s*\d/\d)\s*(?:in|")',
    # H: 37.25 W: 33.375 T: 2 (or Thick: 2)
    r'\bH[^0-9]{0,3}(\d+(?:\.\d+)?|\d+\s*\d/\d)\b[^W]{0,20}\bW[^0-9]{0,3}(\d+(?:\.\d+)?|\d+\s*\d/\d)\b[^T]{0,20}\b(T|Thick(?:ness)?)\b[^0-9]{0,3}(\d+(?:\.\d+)?|\d+\s*\d/\d)',
    # Size: 37.25 x 33.375 x 2
    r'Size[^0-9]{0,6}(\d+(?:\.\d+)?|\d+\s*\d/\d)\s*[xX×]\s*(\d+(?:\.\d+)?|\d+\s*\d/\d)\s*[xX×]\s*(\d+(?:\.\d+)?|\d+\s*\d/\d)',
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
            # Some patterns capture 4 groups (H W label D)
            groups = [g for g in m.groups() if g and not g.lower().startswith(("t","thick"))]
            if len(groups) >= 3:
                H = frac_to_float(groups[0])
                W = frac_to_float(groups[1])
                D = frac_to_float(groups[2])
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

def http_get(url, timeout=REQUEST_TIMEOUT):
    try:
        resp = requests.get(url, headers=DEFAULT_HEADERS, timeout=timeout, allow_redirects=True)
        if resp.ok:
            return resp
    except Exception:
        return None
    return None

def parse_product_page(url):
    resp = http_get(url)
    if not resp:
        return None
    soup = BeautifulSoup(resp.text, "lxml")
    text = soup.get_text(" ", strip=True)
    snippet = (text[:300] + "...") if len(text) > 300 else text
    return {
        "fetched_url": resp.url,
        "title": soup.title.get_text(strip=True) if soup.title else "",
        "core_size_hwd_in": find_core_size(text),
        "inlet_in": parse_in_out(text)[0],
        "outlet_in": parse_in_out(text)[1],
        "cross_refs": ", ".join(extract_cross_refs(text)) or "",
        "image": extract_image(soup),
        "snippet": snippet
    }

def site_search(search_url_tmpl: str, query: str, base_url_for_rel=None, max_candidates=5):
    urls = []
    if not search_url_tmpl:
        return urls
    resp = http_get(search_url_tmpl.format(q=requests.utils.quote(query)))
    if not resp:
        return urls
    soup = BeautifulSoup(resp.text, "lxml")
    for a in soup.select("a[href]"):
        href = a.get("href","")
        text = a.get_text(" ", strip=True)
        if not href:
            continue
        if href.startswith("//"):
            href = "https:" + href
        elif href.startswith("/"):
            from urllib.parse import urlparse, urljoin
            base = urlparse(resp.url if base_url_for_rel is None else base_url_for_rel)
            href = urljoin(f"{base.scheme}://{base.netloc}", href)
        if any(k in text.lower() for k in ["charge", "cooler", "radiator", "intercooler", "cac"]):
            urls.append({"href": href, "text": text})
        if len(urls) >= max_candidates:
            break
    return urls

# ---------------- UI ----------------

st.set_page_config(page_title="CAC Matcher – Anti-Zero", layout="wide")
st.title("Charge Air Cooler Matcher – Northern & Active (Anti-Zero Matches)")

with st.sidebar:
    st.header("Controls")
    northern_search = st.text_input("Northern search URL", value="https://www.northernradiator.com/search?search={q}")
    active_search = st.text_input("Active search URL", value="https://www.activeradiator.com/?s={q}")
    max_rows = st.number_input("Max rows to process", min_value=1, max_value=2000, value=15, step=1)
    evidence_threshold = st.selectbox("Minimum evidence to accept candidate", ["size_or_xref", "title_only", "accept_first"])
    show_candidates = st.checkbox("Include candidate URLs in output", value=True)
    requests_per_min = st.slider("Requests per minute", 5, 60, 20)
    sleep_sec = max(1, 60 // requests_per_min)

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

    your_part_col = st.selectbox("Your Part No column *", options=cols, index=cols.index(autosuggest("Your Part No") or autosuggest("RSH PN") or cols[0]))
    appl_col = st.selectbox("Application / Truck Model (optional)", options=["<None>"] + cols, index=0)
    northern_col = st.selectbox("Northern URL (optional)", options=["<None>"] + cols, index=0)
    active_col = st.selectbox("Active URL (optional)", options=["<None>"] + cols, index=0)

    run = st.button("Run Matching", type="primary")

    if run:
        results = []
        n = min(len(df), int(max_rows))
        progress = st.progress(0)
        status = st.empty()

        for i in range(n):
            row = df.iloc[i]
            part_no = str(row[your_part_col]).strip()
            appl = "" if appl_col == "<None>" else str(row[appl_col]).strip()
            n_url = "" if northern_col == "<None>" else str(row[northern_col]).strip()
            a_url = "" if active_col == "<None>" else str(row[active_col]).strip()

            entry = {"Your Part No": part_no, "Application / Truck Model": appl}

            def process_vendor(vendor, direct_url, search_tmpl):
                info = None
                candidates = []
                # Try direct URL
                if direct_url.lower().startswith("http"):
                    tmp = parse_product_page(direct_url)
                    time.sleep(sleep_sec)
                    if tmp:
                        info = tmp
                # If nothing, try search
                if not info:
                    q = " ".join([part_no, appl]).strip()
                    candidates = site_search(search_tmpl, q, max_candidates=5)
                    # Try up to 5 candidates until threshold met
                    for c in candidates:
                        tmp = parse_product_page(c["href"])
                        time.sleep(sleep_sec)
                        if not tmp:
                            continue
                        ok = False
                        if evidence_threshold == "size_or_xref":
                            ok = bool(tmp.get("core_size_hwd_in") or tmp.get("cross_refs"))
                        elif evidence_threshold == "title_only":
                            ok = bool(tmp.get("title"))
                        elif evidence_threshold == "accept_first":
                            ok = True
                        if ok:
                            info = tmp
                            break
                return info, candidates

            n_info, n_cands = process_vendor("Northern", n_url, northern_search)
            a_info, a_cands = process_vendor("Active", a_url, active_search)

            def upd(prefix, info, cands):
                if info:
                    entry.update({
                        f"{prefix} URL (final)": info.get("fetched_url", ""),
                        f"{prefix} Title": info.get("title", ""),
                        f"{prefix} Core Size (H×W×D, in)": info.get("core_size_hwd_in", ""),
                        f"{prefix} Inlet (in)": info.get("inlet_in", ""),
                        f"{prefix} Outlet (in)": info.get("outlet_in", ""),
                        f"{prefix} Cross-Refs": info.get("cross_refs", ""),
                        f"{prefix} Image": info.get("image", ""),
                        f"{prefix} Snippet": info.get("snippet", ""),
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
                        f"{prefix} Snippet": "",
                    })
                if show_candidates:
                    entry[f"{prefix} Candidates"] = " | ".join([c["href"] for c in cands]) if cands else ""

            upd("Northern", n_info, n_cands)
            upd("Active", a_info, a_cands)
            results.append(entry)

            progress.progress(int((i+1)/n * 100))
            status.write(f"Processed {i+1}/{n}: {part_no}")

        out_df = pd.DataFrame(results)
        st.subheader("Results")
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
    st.info("Upload your Excel to begin. If you see few matches, set 'Minimum evidence' to 'accept_first' to capture candidate URLs.")
