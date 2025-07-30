import io
import os
import re
import csv
import hashlib
import zipfile
from io import BytesIO
from typing import List, Dict, Optional
from urllib.parse import urlparse

import requests
import pandas as pd
from bs4 import BeautifulSoup
from PIL import Image, ImageFilter, ImageStat
import streamlit as st
from datetime import datetime
from zoneinfo import ZoneInfo

# ------------------------------
# Config base (posizioni 1-based)
# ------------------------------
DEFAULT_POSITIONS = [3, 5, 6]  # priorità posizionale in gallery
MAX_IMAGES = 3                 # max immagini per colore

# Esclusioni per filename/URL quando si riempiono buchi
EXCLUDE_PATTERNS = [
    re.compile(r"/1-28\.(jpg|jpeg|png|webp)$", re.IGNORECASE),
    re.compile(r"/146\.(jpg|jpeg|png|webp)$", re.IGNORECASE),
    re.compile(r"/149\.(jpg|jpeg|png|webp)$", re.IGNORECASE),
]

# Parole che identificano frequentemente schede/infografiche
INFO_KEYWORDS = [
    "product-sheet", "productsheet", "datasheet", "data-sheet",
    "spec", "specs", "specification", "specifications",
    "brochure", "leaflet", "flyer", "infographic", "information",
]

# ------------------------------
# HTTP session
# ------------------------------
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
})

# ------------------------------
# Helpers di scraping Tekalab
# ------------------------------
def is_tekalab(url: str) -> bool:
    return "tekalab.com" in urlparse(url).netloc.lower()

def fetch_soup(url: str) -> Optional[BeautifulSoup]:
    try:
        r = SESSION.get(url, timeout=30)
        r.raise_for_status()
        return BeautifulSoup(r.text, "html.parser")
    except Exception as e:
        st.write(f"❌ Errore richiesta: {url} → {e}")
        return None

def find_title_text(soup: BeautifulSoup) -> Optional[str]:
    t = soup.select_one("h1.fusion-title-heading")
    if t and t.text.strip():
        return t.text.strip()
    og = soup.find("meta", property="og:title")
    if og and og.get("content"):
        return og["content"].strip()
    if soup.title and soup.title.text.strip():
        return soup.title.text.strip()
    return None

# ------- Estrazione colore (titolo, testo, slug) -------
CODE_PREFIXES = r"(?:CL|HG|DM|RCF|PPF)"

def _normalize_dashes(s: str) -> str:
    return s.replace("\u2013", "-").replace("\u2014", "-")

def _extract_code_from_url(url: str) -> Optional[str]:
    slug = urlparse(url).path.lower()
    m = re.search(r"/(cl)[-\s]?(\d+[a-z]?)(?:/|[-])", slug, re.IGNORECASE)
    if m:
        return (m.group(1).upper() + "-" + m.group(2).upper())
    return None

def _clean_color_piece(x: str) -> str:
    x = re.split(r"[|•·\(\)\[\]{}]|€|TTC|HT", x, 1)[0]
    return x.strip(" .,:;–—-").strip()

def _extract_from_title(title: str) -> Optional[str]:
    if not title:
        return None
    t = _normalize_dashes(" ".join(title.split()).strip())
    m = re.search(rf"(?:^|[-\s]){CODE_PREFIXES}[-\s]?\d+[A-Z]?\s+(.+)$", t, re.IGNORECASE)
    if m:
        return _clean_color_piece(m.group(1))
    if "-" in t:
        seg = t.split("-")[-1].strip()
        if seg:
            return _clean_color_piece(seg)
    return None

def _extract_from_text(soup: BeautifulSoup, code_hint: Optional[str]) -> Optional[str]:
    text = " ".join(list(soup.stripped_strings))[:40000]
    text = _normalize_dashes(text)
    if code_hint:
        ch = re.escape(code_hint)
        m = re.search(rf"{ch}\s+([A-Z][A-Za-z0-9 .+'/–—\-]*?)\b(?=\s[A-Z][a-z]|[\.\,!;:\)]|\s[0-9€]|$)", text)
        if m:
            return _clean_color_piece(m.group(1))
    m = re.search(rf"{CODE_PREFIXES}[-\s]?\d+[A-Z]?\s+([A-Z][A-Za-z0-9 .+'/–—\-]*?)\b(?=\s[A-Z][a-z]|[\.\,!;:\)]|\s[0-9€]|$)", text)
    if m:
        return _clean_color_piece(m.group(1))
    return None

def _extract_from_slug(url: str) -> Optional[str]:
    path = urlparse(url).path.lower()
    path = path.strip("/").split("/")
    slug = path[-1] if path else ""
    parts = re.split(r"cl[-]?\d+[a-z]?[-]?", slug)
    if len(parts) >= 2:
        tail = parts[1]
        tail = re.sub(r"\b(\d)-0\b", r"\1.0", tail)   # “2-0” -> “2.0”
        tail = re.sub(r"-\d$", "", tail)              # rimuove un eventuale “-2” finale
        color = _clean_color_piece(tail.replace("-", " ").title())
        return color if color else None
    return None

def get_color_from_page(soup: BeautifulSoup, url: str) -> str:
    title = find_title_text(soup)
    c = _extract_from_title(title) if title else None
    if c:
        return c
    code_hint = _extract_code_from_url(url)
    c = _extract_from_text(soup, code_hint)
    if c:
        return c
    c = _extract_from_slug(url)
    if c:
        return c
    return (title or "").strip()

# ------- Gallery -------
def gallery_fullsize_urls(soup: BeautifulSoup) -> List[str]:
    sel = "div.woocommerce-product-gallery__wrapper div.woocommerce-product-gallery__image:not(.clone) a[href]"
    urls = []
    for a in soup.select(sel):
        href = (a.get("href") or "").strip()
        if href.lower().startswith("http"):
            urls.append(href)
    # dedup preservando ordine
    seen, out = set(), []
    for u in urls:
        if u not in seen:
            seen.add(u); out.append(u)
    return out

def is_info_keyword(url: str) -> bool:
    low = url.lower()
    return any(k in low for k in INFO_KEYWORDS)

def pick_by_positions(urls: List[str], positions: List[int], max_images: int) -> List[str]:
    chosen = []
    # 1) posizioni richieste
    for pos in positions:
        idx = pos - 1
        if 0 <= idx < len(urls):
            u = urls[idx]
            if not is_info_keyword(u):
                chosen.append(u)
        if len(chosen) >= max_images:
            return chosen[:max_images]
    # 2) riempimento con altre immagini non escluse
    def is_excluded(u: str) -> bool:
        return any(p.search(u) for p in EXCLUDE_PATTERNS) or is_info_keyword(u)
    for u in urls:
        if len(chosen) >= max_images:
            break
        if u in chosen or is_excluded(u):
            continue
        chosen.append(u)
    return chosen[:max_images]

# ------- Filtri immagini -------
def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()

def dedup_by_hash_keep_order(blobs: List[bytes]) -> List[bytes]:
    seen, out = set(), []
    for data in blobs:
        try:
            h = sha256_bytes(data)
        except Exception:
            continue
        if h not in seen:
            seen.add(h); out.append(data)
    return out

def looks_like_infographic_bytes(data: bytes) -> bool:
    """
    Heuristica: molto bianco + molti bordi (testo/icone) => probabile scheda/prospetto.
    """
    try:
        with Image.open(BytesIO(data)) as im:
            g = im.convert("L")
            w, h = g.size
            total = max(1, w * h)

            # quota di pixel molto chiari (sfondo bianco)
            hist = g.histogram()
            white_pixels = sum(hist[240:])  # 240..255
            white_ratio = white_pixels / total

            # densità di bordi
            edges = g.filter(ImageFilter.FIND_EDGES)
            mean_edge = ImageStat.Stat(edges).mean[0] / 255.0

            # opzionale: se non quadrata (A4-like) alza la probabilità
            ar = max(w, h) / max(1, min(w, h))
            tall_or_wide = ar > 1.25

            # soglie empiriche (conservative)
            if (white_ratio > 0.60 and mean_edge > 0.12) or (tall_or_wide and white_ratio > 0.50 and mean_edge > 0.10):
                return True
    except Exception:
        return False
    return False

# ------- HEX dominante -------
def dominant_hex_from_image(img: Image.Image, palette_colors=8) -> str:
    import colorsys
    def rgb_to_hex(rgb):
        r, g, b = rgb
        return "#{:02X}{:02X}{:02X}".format(int(r), int(g), int(b))
    img = img.convert("RGB")
    w, h = img.size
    max_side = 300
    if max(w, h) > max_side:
        scale = max_side / float(max(w, h))
        img = img.resize((int(w*scale), int(h*scale)), Image.BILINEAR)
    q = img.quantize(colors=palette_colors, method=Image.MEDIANCUT)
    palette = q.getpalette()
    counts = q.getcolors() or []
    best, best_count = None, -1
    for count, idx in counts:
        r = palette[idx*3]; g = palette[idx*3+1]; b = palette[idx*3+2]
        h_, l_, s_ = colorsys.rgb_to_hls(r/255.0, g/255.0, b/255.0)
        if 0.08 < l_ < 0.92 and s_ > 0.15 and count > best_count:
            best, best_count = (r, g, b), count
    if best is None:
        for count, idx in counts:
            r = palette[idx*3]; g = palette[idx*3+1]; b = palette[idx*3+2]
            if count > best_count:
                best, best_count = (r, g, b), count
    return "#{:02X}{:02X}{:02X}".format(*best) if best else ""

def dominant_hex_from_blobs(blobs: List[bytes]) -> str:
    candidates = []
    for data in blobs:
        try:
            with Image.open(BytesIO(data)) as im:
                hv = dominant_hex_from_image(im, palette_colors=8)
                if hv:
                    candidates.append(hv.upper())
        except Exception:
            continue
    if not candidates:
        return ""
    freq = {}
    for c in candidates:
        freq[c] = freq.get(c, 0) + 1
    return max(freq.items(), key=lambda x: x[1])[0]

# ------- CSV Maxtrify -------
def slugify_handle(text: str) -> str:
    import unicodedata
    t = unicodedata.normalize("NFKD", text)
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = re.sub(r"[^a-zA-Z0-9\s\-]", "", t)
    t = re.sub(r"\s+", "-", t).strip("-")
    t = re.sub(r"-{2,}", "-", t)
    return t.lower()

def build_rows_for_color(color_name: str, updated_at_str: str, hex_value: str) -> list[dict]:
    base = {
        "ID": "",
        "Handle": slugify_handle(color_name),
        "Command": "MERGE",
        "Display Name": color_name,
        "Status": "",
        "Updated At": updated_at_str,
        "Definition: Handle": "shopify--color-pattern",
        "Definition: Name": "Color",
    }
    rows = []
    rows.append({**base, "Top Row": True, "Row #": 1, "Field": "label", "Value": color_name})
    rows.append({**base, "Top Row": "",   "Row #": 2, "Field": "color", "Value": hex_value})
    rows.append({**base, "Top Row": "",   "Row #": 3, "Field": "image", "Value": ""})
    rows.append({**base, "Top Row": "",   "Row #": 4, "Field": "color_taxonomy_reference",   "Value": "gid://shopify/TaxonomyValue/3"})
    rows.append({**base, "Top Row": "",   "Row #": 5, "Field": "pattern_taxonomy_reference", "Value": "gid://shopify/TaxonomyValue/2874"})
    return rows

def make_color_csv_chunks(colors_hex: Dict[str, str]) -> list[tuple[str, str]]:
    tz = ZoneInfo("Europe/Rome")
    updated_at = datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S %z")
    fieldnames = [
        "ID", "Handle", "Command", "Display Name", "Status", "Updated At",
        "Definition: Handle", "Definition: Name", "Top Row", "Row #", "Field", "Value"
    ]
    colors = sorted(colors_hex.keys())
    chunks = []
    for i in range(0, len(colors), 10):
        chunk = colors[i:i+10]
        rows = []
        for color in chunk:
            rows.extend(build_rows_for_color(color, updated_at, colors_hex[color]))
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)
        name = f"color-patterns-{(i//10)+1:02d}.csv"
        chunks.append((name, buf.getvalue()))
    return chunks

# ------------------------------
# Streamlit UI
# ------------------------------
st.set_page_config(page_title="Tekalab Downloader + Color CSV", layout="wide")
st.title("Tekalab → Immagini per posizione (no infografiche) + CSV Maxtrify")

st.markdown("""
Carica un **CSV** con colonna `url` (solo pagine **tekalab.com**).  
Per ogni pagina:
- estrae il **colore** (titolo → testo → slug; non rimuove “2.0”),
- seleziona immagini per **posizione** (default **3, 5, 6**),
- **ignora schede/infografiche** (filtri su nome + analisi immagine),
- salva **max 3** immagini per colore (dedup hash),
- calcola **HEX** dominante,
- genera CSV **Maxtrify** (10 colori per file),
- scarica **tutto** in **un unico ZIP**.
""")

uploaded = st.file_uploader("Carica prodotti.csv (colonna url)", type=["csv"])
pos_str = st.text_input("Posizioni gallery (1-based, separate da virgola)", value="3,5,6")
run = st.button("Esegui")

def parse_positions(s: str) -> List[int]:
    out = []
    for part in s.split(","):
        try:
            n = int(part.strip())
            if n > 0:
                out.append(n)
        except:
            pass
    return out or DEFAULT_POSITIONS

if run:
    if not uploaded:
        st.error("Carica prima un CSV.")
        st.stop()

    try:
        df = pd.read_csv(uploaded)
    except Exception as e:
        st.error(f"Errore lettura CSV: {e}")
        st.stop()

    # trova colonna url
    url_col = None
    for c in df.columns:
        if c.strip().lower() == "url":
            url_col = c
            break
    if not url_col:
        st.error("Il CSV deve avere una colonna 'url'.")
        st.stop()

    urls = [u for u in df[url_col].astype(str).tolist() if u.strip()]
    urls = [u for u in urls if is_tekalab(u)]
    if not urls:
        st.warning("Nessun URL tekalab.com valido nel CSV.")
        st.stop()

    positions = parse_positions(pos_str)

    progress = st.progress(0)
    log_box = st.empty()
    logs = []
    def log(msg):
        logs.append(msg)
        log_box.write("\n".join(logs[-25:]))

    colors_map: Dict[str, List[bytes]] = {}   # colore -> blobs
    colors_hex: Dict[str, str] = {}           # colore -> hex

    total = len(urls)
    for idx, url in enumerate(urls, start=1):
        progress.progress((idx-1)/max(total,1))
        soup = fetch_soup(url)
        if not soup:
            log(f"❌ Pagina non caricata: {url}")
            continue

        color = get_color_from_page(soup, url)
        if not color:
            log(f"❌ Colore non trovato: {url}")
            continue

        all_urls = gallery_fullsize_urls(soup)
        if not all_urls:
            log(f"⚠️ Nessuna immagine in gallery per {color}")
            continue

        # Selezione prioritaria per posizioni, con esclusioni base
        preferred = pick_by_positions(all_urls, positions, MAX_IMAGES)
        # Coda di fallback (tutte le altre non già scelte)
        fallback = [u for u in all_urls if u not in preferred and not is_info_keyword(u)]

        blobs: List[bytes] = []
        seen_hashes = set()

        def try_add(u: str):
            nonlocal blobs, seen_hashes
            try:
                r = SESSION.get(u, timeout=30)
                if r.status_code == 200 and r.content:
                    data = r.content
                    if looks_like_infographic_bytes(data):
                        return False
                    h = sha256_bytes(data)
                    if h in seen_hashes:
                        return False
                    seen_hashes.add(h)
                    blobs.append(data)
                    return True
            except:
                return False
            return False

        # prova posizioni richieste
        for u in preferred:
            if len(blobs) >= MAX_IMAGES:
                break
            try_add(u)

        # riempimento se necessario
        for u in fallback:
            if len(blobs) >= MAX_IMAGES:
                break
            try_add(u)

        if not blobs:
            log(f"⚠️ Nessuna immagine valida per {color}")
            continue

        # tronca a MAX_IMAGES e salva info
        blobs = blobs[:MAX_IMAGES]
        colors_map[color] = blobs
        colors_hex[color] = dominant_hex_from_blobs(blobs)
        log(f"✅ {len(blobs)} immagini per {color}")

    progress.progress(1.0)

    if not colors_map:
        st.warning("Nessun colore processato.")
        st.stop()

    # CSV (blocchi da 10)
    csv_chunks = make_color_csv_chunks(colors_hex)

    # ZIP unico: cartelle colore + CSV in root
    zip_bytes = io.BytesIO()
    with zipfile.ZipFile(zip_bytes, "w", zipfile.ZIP_DEFLATED) as z:
        for color, blobs in colors_map.items():
            for i, data in enumerate(blobs, start=1):
                arcname = os.path.join(color, f"image_{i}.jpg")
                z.writestr(arcname, data)
        for name, content in csv_chunks:
            z.writestr(name, content)
    zip_bytes.seek(0)

    st.success("Completato!")
    st.download_button(
        "⬇️ Scarica tutto (immagini + CSV)",
        data=zip_bytes.getvalue(),
        file_name="tekalab-package.zip",
        mime="application/zip",
    )

    st.write("Colori estratti:", sorted(colors_map.keys()))
