
import re
import io
import time
import json
import traceback
from urllib.parse import urlparse
import pandas as pd
import requests
from bs4 import BeautifulSoup
from rapidfuzz import fuzz, process
import streamlit as st

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
DEFAULT_HEADERS = {"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.8"}
REQUEST_TIMEOUT = 20

def http_get(url, timeout=REQUEST_TIMEOUT):
    try:
        r = requests.get(url, headers=DEFAULT_HEADERS, timeout=timeout, allow_redirects=True)
        if r.ok:
            return r
    except Exception:
        return None
    return None

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

CORE_PATTERNS = [
    r'Core\\s*Size[^0-9]{0,8}(\\d+(?:\\s*\\d\\/\\d)?)[\\s"]*[xX×]\\s*(\\d+(?:\\s*\\d\\/\\d)?)[\\s"]*[xX×]\\s*(\\d+(?:\\s*\\d\\/\\d)?)\\s*(?:in|")',
    r'(\\d+(?:\\s*\\d\\/\\d)?)[\\s"]*[xX×]\\s*(\\d+(?:\\s*\\d\\/\\d)?)[\\s"]*[xX×]\\s*(\\d+(?:\\s*\\d\\/\\d)?)\\s*(?:in|")',
    r'(\\d+(?:\\.\\d+)?)[\\s"]*[xX×]\\s*(\\d+(?:\\.\\d+)?)[\\s"]*[xX×]\\s*(\\d+(?:\\.\\d+)?)\\s*(?:in|")',
    r'Height[^0-9]{0,6}(\\d+(?:\\.\\d+)?|\\d+\\s*\\d\\/\\d)\\s*(?:in|")[^W]*Width[^0-9]{0,6}(\\d+(?:\\.\\d+)?|\\d+\\s*\\d\\/\\d)\\s*(?:in|")[^D]*Depth[^0-9]{0,6}(\\d+(?:\\.\\d+)?|\\d+\\s*\\d\\/\\d)\\s*(?:in|")',
]

def find_core_size(text: str):
    if not text: return None
    t = re.sub(r"\\s+", " ", text)
    for pat in CORE_PATTERNS:
        m = re.search(pat, t, flags=re.IGNORECASE)
        if m:
            groups = [g for g in m.groups() if g and not str(g).lower().startswith(("t","thick"))]
            if len(groups) >= 3:
                H = frac_to_float(groups[0]); W = frac_to_float(groups[1]); D = frac_to_float(groups[2])
                if all(v > 0 for v in (H,W,D)):
                    return H, W, D
    return None

def first_text(el):
    if not el: return ""
    return el.get_text(" ", strip=True)

def extract_table_keyvals(soup):
    kv = {}
    for tbl in soup.select("table"):
        for tr in tbl.select("tr"):
            tds = tr.find_all(["th","td"])
            if len(tds) >= 2:
                key = first_text(tds[0]).strip().strip(":").lower()
                val = first_text(tds[1]).strip()
                if key and val:
                    kv[key] = val
    return kv

def og_image(soup):
    m = soup.find("meta", property="og:image")
    if m and m.get("content"): return m["content"]
    img = soup.find("img")
    if img and img.get("src"): return img["src"]
    return ""

def jsonld_product(soup):
    try:
        for tag in soup.find_all("script", type="application/ld+json"):
            data = json.loads(tag.text.strip())
            if isinstance(data, dict) and data.get("@type","").lower() == "product":
                return data
            if isinstance(data, list):
                for d in data:
                    if isinstance(d, dict) and d.get("@type","").lower() == "product":
                        return d
    except Exception:
        return None
    return None

def parse_in_out_from_text(text):
    if not text: return (None,None)
    t = re.sub(r"\\s+", " ", text)
    inlet = outlet = None
    m = re.search(r'Inlet[^0-9]{0,10}(\\d+(?:\\.\\d+)?|\\d+\\s*\\d\\/\\d)\\s*(?:in|")', t, re.I)
    if m: inlet = frac_to_float(m.group(1))
    m = re.search(r'Outlet[^0-9]{0,10}(\\d+(?:\\.\\d+)?|\\d+\\s*\\d\\/\\d)\\s*(?:in|")', t, re.I)
    if m: outlet = frac_to_float(m.group(1))
    return inlet, outlet

def collect_xrefs(text):
    refs = set()
    if not text: return []
    for m in re.finditer(r'(?:cross[\\s-]?ref|oem|oe|replaces|equivalent|interchange|xref)[:\\s\\-–]+([A-Z0-9,\\s\\-/\\._]+)', text, re.I):
        chunk = m.group(1)[:240]
        for tok in re.findall(r'[A-Z0-9][A-Z0-9\\-\\._]{3,17}', chunk, re.I):
            if len(tok) >= 4 and not tok.isdigit():
                refs.add(tok.strip(".,:; "))
    return sorted(refs)

def parse_northern(soup, text):
    out = {}
    data = jsonld_product(soup)
    if data:
        out["sku"] = data.get("sku","") or data.get("mpn","")
        out["title"] = data.get("name","")
        img = data.get("image")
        if img and not isinstance(img, list): out["image"] = img
        elif isinstance(img, list) and img: out["image"] = img[0]
    kv = extract_table_keyvals(soup)
    for k,v in kv.items():
        if "core size" in k:
            cs = find_core_size(v)
            if cs: out["core"] = cs
    H = kv.get("height","") or kv.get("core height","")
    W = kv.get("width","") or kv.get("core width","")
    D = kv.get("depth","") or kv.get("thickness","") or kv.get("core thickness","")
    if H and W and D:
        try_vals = [frac_to_float(H), frac_to_float(W), frac_to_float(D)]
        if all(v > 0 for v in try_vals):
            out["core"] = tuple(try_vals)
    if "core" not in out:
        cs = find_core_size(text)
        if cs: out["core"] = cs
    inlet = kv.get("inlet","") or kv.get("inlet diameter","")
    outlet = kv.get("outlet","") or kv.get("outlet diameter","")
    def numify(x): 
        try: return frac_to_float(re.search(r'(\\d+(?:\\.\\d+)?|\\d+\\s*\\d\\/\\d)', x).group(1))
        except: return None
    ii = numify(inlet) if inlet else None
    oo = numify(outlet) if outlet else None
    if ii is None or oo is None:
        i2,o2 = parse_in_out_from_text(text); ii = ii or i2; oo = oo or o2
    if ii: out["inlet"] = ii
    if oo: out["outlet"] = oo
    xrefs = []
    for k,v in kv.items():
        if any(key in k for key in ["xref","cross","oem","oe","replaces"]):
            xrefs.extend(collect_xrefs(v))
    if not xrefs: xrefs = collect_xrefs(text)
    if xrefs: out["xrefs"] = ", ".join(sorted(set(xrefs)))
    out["image"] = out.get("image") or og_image(soup)
    out["title"] = out.get("title") or (soup.title.get_text(strip=True) if soup.title else "")
    return out

def parse_active(soup, text):
    out = {}
    data = jsonld_product(soup)
    if data:
        out["sku"] = data.get("sku","") or data.get("mpn","")
        out["title"] = data.get("name","")
        img = data.get("image")
        if img and not isinstance(img, list): out["image"] = img
        elif isinstance(img, list) and img: out["image"] = img[0]
    kv = extract_table_keyvals(soup)
    for k,v in kv.items():
        if "core size" in k or "core dimension" in k or (k.strip() in ["size","dimensions"]):
            cs = find_core_size(v)
            if cs: out["core"] = cs
    if "core" not in out:
        cs = find_core_size(text)
        if cs: out["core"] = cs
    inlet = kv.get("inlet","") or kv.get("inlet diameter","")
    outlet = kv.get("outlet","") or kv.get("outlet diameter","")
    def numify(x): 
        try: return frac_to_float(re.search(r'(\\d+(?:\\.\\d+)?|\\d+\\s*\\d\\/\\d)', x).group(1))
        except: return None
    ii = numify(inlet) if inlet else None
    oo = numify(outlet) if outlet else None
    if ii is None or oo is None:
        i2,o2 = parse_in_out_from_text(text); ii = ii or i2; oo = oo or o2
    if ii: out["inlet"] = ii
    if oo: out["outlet"] = oo
    xrefs = []
    for k,v in kv.items():
        if any(key in k for key in ["xref","cross","oem","oe","replaces"]):
            xrefs.extend(collect_xrefs(v))
    if not xrefs: xrefs = collect_xrefs(text)
    if xrefs: out["xrefs"] = ", ".join(sorted(set(xrefs)))
    out["image"] = out.get("image") or og_image(soup)
    out["title"] = out.get("title") or (soup.title.get_text(strip=True) if soup.title else "")
    return out

def parse_vendor_page(url):
    resp = http_get(url)
    if not resp: return None
    soup = BeautifulSoup(resp.text, "lxml")
    text = soup.get_text(" ", strip=True)
    host = urlparse(resp.url).netloc.lower()
    if "northernradiator" in host:
        data = parse_northern(soup, text)
    elif "activeradiator" in host:
        data = parse_active(soup, text)
    else:
        data = {}
        data["core"] = find_core_size(text)
        data["image"] = og_image(soup)
        data["title"] = soup.title.get_text(strip=True) if soup.title else ""
        ii,oo = parse_in_out_from_text(text)
        if ii: data["inlet"] = ii
        if oo: data["outlet"] = oo
        xrefs = collect_xrefs(text)
        if xrefs: data["xrefs"] = ", ".join(xrefs)
    data["url"] = resp.url
    data["snippet"] = (text[:300] + "...") if len(text) > 300 else text
    return data

st.set_page_config(page_title="CAC Matcher – Vendor Parsers", layout="wide")
st.title("Charge Air Cooler Matcher – Northern & Active (Vendor Parsers)")

with st.sidebar:
    st.header("Controls")
    max_rows = st.number_input("Max rows", min_value=1, max_value=5000, value=20, step=1)
    accept_without_sizes = st.checkbox("Accept match even if core size missing", value=True)

st.subheader("Debug URL Tester")
test_url = st.text_input("Paste a Northern/Active product URL to test parsing")
if st.button("Test Parse URL"):
    if test_url.strip().lower().startswith("http"):
        data = parse_vendor_page(test_url.strip())
        if data:
            st.write("**Parsed fields:**", data)
        else:
            st.error("Failed to fetch or parse the URL.")
    else:
        st.warning("Please paste a valid http(s) URL.")

st.subheader("Batch Excel Matcher")
uploaded = st.file_uploader("Upload Excel (columns: Your Part No, Northern URL, Active URL; Application optional)", type=["xlsx"])

if uploaded is not None:
    df = pd.read_excel(uploaded)
    st.write("Input preview:")
    st.dataframe(df.head(20), use_container_width=True)

    cols = list(df.columns)
    def suggest(name):
        best = process.extractOne(name, cols, scorer=fuzz.WRatio)
        return best[0] if best and best[1] >= 80 else None

    your_part_col = st.selectbox("Your Part No column *", cols, index=cols.index(suggest("Your Part No") or suggest("RSH PN") or cols[0]))
    northern_col = st.selectbox("Northern URL column (optional)", ["<None>"] + cols, index=(["<None>"]+cols).index(suggest("Northern URL")) if suggest("Northern URL") else 0)
    active_col = st.selectbox("Active URL column (optional)", ["<None>"] + cols, index=(["<None>"]+cols).index(suggest("Active URL")) if suggest("Active URL") else 0)
    appl_col = st.selectbox("Application / Truck Model (optional)", ["<None>"] + cols, index=(["<None>"]+cols).index(suggest("Application / Truck Model")) if suggest("Application / Truck Model") else 0)

    if st.button("Run Matching", type="primary"):
        results = []
        n = min(len(df), int(max_rows))
        prog = st.progress(0); status = st.empty()

        for i in range(n):
            row = df.iloc[i]
            part = str(row[your_part_col]).strip()
            nurl = "" if northern_col == "<None>" else str(row[northern_col]).strip()
            aurl = "" if active_col == "<None>" else str(row[active_col]).strip()

            entry = {"Your Part No": part}

            picked = None
            for url in [nurl, aurl]:
                if url.lower().startswith("http"):
                    picked = url; break

            if picked:
                data = parse_vendor_page(picked)
                if data:
                    entry.update({
                        "Final URL": data.get("url",""),
                        "Title": data.get("title",""),
                        "Core Size (H×W×D, in)": data.get("core",""),
                        "Inlet (in)": data.get("inlet",""),
                        "Outlet (in)": data.get("outlet",""),
                        "Cross-Refs": data.get("xrefs",""),
                        "Image": data.get("image",""),
                        "Snippet": data.get("snippet","")
                    })
                elif accept_without_sizes:
                    entry.update({"Final URL": picked, "Title":"", "Core Size (H×W×D, in)":"", "Image":"", "Cross-Refs":"", "Snippet":""})
                else:
                    entry.update({"Final URL": "", "Title":"", "Core Size (H×W×D, in)":"", "Image":"", "Cross-Refs":"", "Snippet":""})
            else:
                entry.update({"Final URL": "", "Title":"", "Core Size (H×W×D, in)":"", "Image":"", "Cross-Refs":"", "Snippet":""})

            results.append(entry)
            prog.progress(int((i+1)/n*100)); status.write(f"Processed {i+1}/{n}: {part}")

        out_df = pd.DataFrame(results)
        st.subheader("Results")
        st.dataframe(out_df, use_container_width=True)

        out = io.BytesIO()
        with pd.ExcelWriter(out, engine="openpyxl") as w:
            out_df.to_excel(w, index=False, sheet_name="Matches")
        st.download_button("Download Excel Results", out.getvalue(), "Top CACs - USA Matches.xlsx",
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
