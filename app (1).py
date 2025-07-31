import os
import re
import csv
import io
import zipfile
import unicodedata
import colorsys
import requests
import tempfile
import pandas as pd
from io import BytesIO
from bs4 import BeautifulSoup
from PIL import Image
from datetime import datetime
from zoneinfo import ZoneInfo
import streamlit as st

# -----------------------
# Configurazione Streamlit
# -----------------------
st.set_page_config(page_title="Downloader Tekalab + CSV colori", layout="wide")

# -----------------------
# HTTP session
# -----------------------
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
})

# -----------------------
# Helpers
# -----------------------

CODE_PREFIXES = r"(?:CL|SP)"

def _normalize_dashes(t: str) -> str:
    return t.replace("–", "-").replace("—", "-")

def _clean_color_piece(piece: str) -> str:
    # Rimuove eventuali codici rimasti tipo SP-05 o CL-02
    piece = re.sub(rf"{CODE_PREFIXES}[-\s]?\d+[A-Z]?", "", piece, flags=re.IGNORECASE).strip()
    return piece

def _extract_from_title(title: str):
    if not title:
        return None

    t = _normalize_dashes(" ".join(title.split()).strip())

    # Escludiamo parole inutili
    REMOVE_WORDS = [
        "FLEXISHIELD", "Cosmétique", "PPF", "Film", "de", "protection",
        "couleur", "violet", "pourpre", "noir", "profond", "aurore", "boréal"
    ]
    for w in REMOVE_WORDS:
        t = re.sub(rf"\b{w}\b", "", t, flags=re.IGNORECASE).strip()

    # Cerca colore DOPO il codice
    m = re.search(rf"(?:^|[-\s]){CODE_PREFIXES}[-\s]?\d+[A-Z]?\s+(.+)$", t, re.IGNORECASE)
    if m:
        return _clean_color_piece(m.group(1))

    # Cerca colore PRIMA del codice
    m = re.search(rf"(.+?)\s+{CODE_PREFIXES}[-\s]?\d+[A-Z]?$", t, re.IGNORECASE)
    if m:
        return _clean_color_piece(m.group(1))

    # Fallback: ultima parte dopo un trattino
    if "-" in t:
        seg = t.split("-")[-1].strip()
        if seg:
            return _clean_color_piece(seg)

    return t.strip()

def _abs_url(u: str, base_url: str) -> str:
    if not u:
        return ""
    if u.startswith("//"):
        return "https:" + u
    if u.startswith("/"):
        base = re.match(r"^(https?://[^/]+)", base_url)
        return base.group(1) + u if base else u
    return u

def rgb_to_hex(rgb):
    r, g, b = rgb
    return "#{:02X}{:02X}{:02X}".format(int(r), int(g), int(b))

def dominant_hex_from_image(img: Image.Image, palette_colors=8):
    img = img.convert("RGB")
    w, h = img.size
    max_side = 300
    if max(w, h) > max_side:
        scale = max_side / float(max(w, h))
        img = img.resize((int(w*scale), int(h*scale)), Image.BILINEAR)
    q = img.quantize(colors=palette_colors, method=Image.MEDIANCUT)
    palette = q.getpalette()
    counts = q.getcolors() or []
    best = None
    best_count = -1
    for count, idx in counts:
        r = palette[idx*3]; g = palette[idx*3+1]; b = palette[idx*3+2]
        h, l, s = colorsys.rgb_to_hls(r/255.0, g/255.0, b/255.0)
        if l > 0.92 or l < 0.08 or s < 0.15:
            continue
        if count > best_count:
            best = (r, g, b); best_count = count
    if best is None:
        for count, idx in counts:
            r = palette[idx*3]; g = palette[idx*3+1]; b = palette[idx*3+2]
            if count > best_count:
                best = (r, g, b); best_count = count
    return rgb_to_hex(best) if best else ""

def dominant_hex_from_folder(folder_path: str) -> str:
    candidates = []
    if not os.path.isdir(folder_path):
        return ""
    for name in sorted(os.listdir(folder_path)):
        if not name.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
            continue
        fp = os.path.join(folder_path, name)
        try:
            with Image.open(fp) as im:
                hexv = dominant_hex_from_image(im, palette_colors=8)
                if hexv:
                    candidates.append(hexv.upper())
        except:
            continue
    if not candidates:
        return ""
    freq = {}
    for c in candidates:
        freq[c] = freq.get(c, 0) + 1
    return max(freq.items(), key=lambda x: x[1])[0]

# -----------------------
# Immagini
# -----------------------
def extract_images_from_tekalab(soup: BeautifulSoup, page_url: str, max_images=3):
    images = []
    seen_hashes = set()

    gallery = soup.find("div", class_="fusion-woo-product-images")
    if not gallery:
        return []

    for img_tag in gallery.find_all("img"):
        src = img_tag.get("data-large_image") or img_tag.get("src")
        if not src:
            continue

        # Filtra le immagini indesiderate (Product Sheet, Clear Coat)
        alt_text = (img_tag.get("alt") or "").lower()
        if "product sheet" in alt_text or "clear coat" in alt_text:
            continue

        url = _abs_url(src.split("?")[0], page_url)
        try:
            r = SESSION.get(url, timeout=30)
            if r.status_code == 200 and r.content:
                h = hash(r.content)
                if h not in seen_hashes:
                    seen_hashes.add(h)
                    images.append(r.content)
                    if len(images) >= max_images:
                        break
        except:
            continue
    return images

# -----------------------
# CSV generator
# -----------------------
def slugify_handle(text: str) -> str:
    t = unicodedata.normalize("NFKD", text)
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = t.lower()
    t = re.sub(r"[^a-z0-9\s\-]", "", t)
    t = re.sub(r"\s+", "-", t).strip("-")
    t = re.sub(r"-{2,}", "-", t)
    return t

def build_rows_for_color(color_name: str, updated_at_str: str, hex_value: str):
    handle = slugify_handle(color_name)
    base = {
        "ID": "",
        "Handle": handle,
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

def generate_color_csvs(colors, base_dir):
    colors = sorted(set(c for c in colors if c.strip()))
    if not colors:
        return [], []
    tz = ZoneInfo("Europe/Rome")
    updated_at = datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S %z")
    fieldnames = [
        "ID", "Handle", "Command", "Display Name", "Status", "Updated At",
        "Definition: Handle", "Definition: Name", "Top Row", "Row #", "Field", "Value"
    ]
    chunk_size = 10
    csv_buffers = []
    csv_names = []
    for i in range(0, len(colors), chunk_size):
        chunk = colors[i:i+chunk_size]
        rows = []
        for color in chunk:
            folder = os.path.join(base_dir, color)
            hex_val = dominant_hex_from_folder(folder) or ""
            rows.extend(build_rows_for_color(color, updated_at, hex_val))
        idx = (i // chunk_size) + 1
        name = f"color-patterns-{idx:02d}.csv"
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)
        csv_buffers.append(buf.getvalue())
        csv_names.append(name)
    return csv_names, csv_buffers

# -----------------------
# Main workflow
# -----------------------
def process_urls(df_urls, work_dir, progress=None, log=None):
    collected_colors = []
    total = len(df_urls)
    for idx, url in enumerate(df_urls):
        if progress:
            progress.progress((idx) / max(total, 1))

        try:
            r = SESSION.get(url, timeout=30)
            soup = BeautifulSoup(r.text, "html.parser")
        except Exception:
            if log: log(f"❌ Errore caricamento pagina: {url}")
            continue

        # Estrai titolo colore
        h1 = soup.find("h1", class_="fusion-title-heading")
        color_name = _extract_from_title(h1.text if h1 else "")
        if not color_name:
            if log: log(f"⚠️ Nessun nome colore trovato: {url}")
            continue

        # Scarica immagini
        blobs = extract_images_from_tekalab(soup, url, max_images=3)
        if not blobs:
            if log: log(f"⚠️ Nessuna immagine valida trovata per {color_name}")
            continue

        color_dir = os.path.join(work_dir, color_name)
        os.makedirs(color_dir, exist_ok=True)
        for i, blob in enumerate(blobs, start=1):
            fp = os.path.join(color_dir, f"image_{i}.jpg")
            with open(fp, "wb") as f:
                f.write(blob)

        collected_colors.append(color_name)
        if log: log(f"✅ {color_name} ({len(blobs)} immagini)")

    if progress:
        progress.progress(1.0)
    return collected_colors

def zip_folder(base_dir) -> bytes:
    bio = io.BytesIO()
    with zipfile.ZipFile(bio, "w", zipfile.ZIP_DEFLATED) as z:
        for root, _, files in os.walk(base_dir):
            for f in files:
                fp = os.path.join(root, f)
                arc = os.path.relpath(fp, base_dir)
                z.write(fp, arc)
    bio.seek(0)
    return bio.read()

# -----------------------
# UI Streamlit
# -----------------------
st.title("Downloader Tekalab – Immagini + CSV colori")

csv_file = st.file_uploader("Carica prodotti.csv (colonna url o URL)", type=["csv"])
run = st.button("Esegui")

if run:
    if not csv_file:
        st.error("Carica prima un file CSV.")
        st.stop()

    try:
        df = pd.read_csv(csv_file)
    except Exception as e:
        st.error(f"Errore lettura CSV: {e}")
        st.stop()

    url_col = None
    for c in df.columns:
        if c.strip().lower() == "url":
            url_col = c
            break
    if not url_col:
        st.error("Il CSV deve contenere una colonna 'url' (o 'URL').")
        st.stop()

    urls = [u for u in df[url_col].astype(str).tolist() if u.strip()]
    if not urls:
        st.error("Nessun URL trovato nel CSV.")
        st.stop()

    with tempfile.TemporaryDirectory() as work_dir:
        st.info("Inizio download immagini e analisi…")
        prog = st.progress(0)
        log_area = st.empty()
        logs = []

        def log(msg):
            logs.append(msg)
            log_area.write("\n".join(logs[-20:]))

        colors = process_urls(urls, work_dir, progress=prog, log=log)

        if not colors:
            st.warning("Nessun colore valido processato.")
            st.stop()

        csv_names, csv_buffers = generate_color_csvs(colors, work_dir)

        zbytes = zip_folder(work_dir)
        st.download_button("⬇️ Scarica immagini (zip)", data=zbytes, file_name="immagini.zip", mime="application/zip")

        for name, content in zip(csv_names, csv_buffers):
            st.download_button(f"⬇️ Scarica {name}", data=content, file_name=name, mime="text/csv")

        st.success(f"✅ Completato! {len(colors)} colori processati")
