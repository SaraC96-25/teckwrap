import os
import re
import csv
import io
import zipfile
import unicodedata
import colorsys
import hashlib
import requests
import tempfile
import pandas as pd
from io import BytesIO
from bs4 import BeautifulSoup
from PIL import Image
from datetime import datetime
from zoneinfo import ZoneInfo
from urllib.parse import urlparse, parse_qs
import streamlit as st

st.set_page_config(page_title="TeckWrap Downloader + Color CSV", layout="wide")

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
def get_color_name(title: str) -> str:
    title = (title or "").strip()
    title = title.split("*")[0].strip()  # remove *DISCONTINUED*
    if "(" in title and ")" in title:
        return title.split("(")[0].strip()
    parts = title.split()
    if len(parts) >= 4:
        return " ".join(parts[3:]).strip()
    return title

def find_product_title(soup: BeautifulSoup) -> str | None:
    t = soup.find("h1", class_="product-title")
    if t and t.text.strip():
        return t.text.strip()
    t = soup.find("h1", class_="product__title")
    if t and t.text.strip():
        return t.text.strip()
    return None

def _abs_url(u: str) -> str:
    if not u:
        return ""
    if u.startswith("//"):
        return "https:" + u
    if u.startswith("/"):
        return "https://teckwrap.com" + u
    return u

def candidate_urls_from_img(img_tag) -> list:
    urls = []
    srcset = img_tag.get("srcset")
    if srcset:
        for part in srcset.split(","):
            seg = part.strip().split()
            if seg:
                urls.append(_abs_url(seg[0]))
    src = img_tag.get("src")
    if src:
        urls.append(_abs_url(src))
    return list(dict.fromkeys(urls))

def width_estimate(url: str) -> int:
    try:
        q = parse_qs(urlparse(url).query)
        if "width" in q and q["width"]:
            return int(q["width"][0])
    except:
        pass
    return 0

def extract_images_from_page(soup: BeautifulSoup, max_images=3):
    """Estrae immagini garantendo featured image grande come prima"""
    # 1. Individua featured image
    featured_img = soup.select_one("li.media-viewer__item.is-current-variant img")
    if not featured_img:
        gallery = soup.find("media-gallery") or soup.find("ul", class_="media-viewer")
        if gallery:
            featured_img = gallery.find("img")

    # 2. Trova URL migliore per featured image
    featured_url = None
    if featured_img:
        candidates = candidate_urls_from_img(featured_img)
        if candidates:
            candidates_sorted = sorted(candidates, key=width_estimate, reverse=True)
            featured_url = candidates_sorted[0]

    # 3. Scarica tutte le immagini
    blobs = []
    featured_blob = None
    for selector in ["media-gallery img", "ul.media-viewer img"]:
        for img in soup.select(selector):
            for url in candidate_urls_from_img(img):
                try:
                    r = SESSION.get(url, timeout=20)
                    if r.status_code == 200 and r.content:
                        data = r.content
                        # se corrisponde alla featured (stessa base url)
                        if featured_url and url.split("?")[0] == featured_url.split("?")[0]:
                            featured_blob = data
                        blobs.append(data)
                except:
                    continue

    if not blobs:
        return []

    # 4. Deduplica per hash e ordina per area pixel
    unique = {}
    for data in blobs:
        try:
            im = Image.open(BytesIO(data))
            area = im.size[0] * im.size[1]
            h = hashlib.sha256(data).hexdigest()
            if h not in unique or area > unique[h][1]:
                unique[h] = (data, area)
        except:
            continue

    sorted_imgs = sorted(unique.values(), key=lambda x: x[1], reverse=True)
    images_ordered = [img for img,_ in sorted_imgs]

    # 5. Metti featured image in testa
    if featured_blob:
        images_ordered = [featured_blob] + [img for img in images_ordered if img != featured_blob]

    # 6. Limita a max_images
    return images_ordered[:max_images]

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
        if 0.08 < l < 0.92 and s > 0.15:
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

def process_urls(df_urls, work_dir, progress=None, log=None):
    collected_colors = []
    def _log(msg):
        if log: log(msg)
    total = len(df_urls)
    for idx, url in enumerate(df_urls):
        if progress:
            progress.progress((idx)/max(total,1))
        try:
            r = SESSION.get(url, timeout=30)
            soup = BeautifulSoup(r.text, "html.parser")
        except:
            _log(f"❌ Errore caricamento pagina: {url}")
            continue
        title = find_product_title(soup)
        if not title:
            _log(f"❌ Titolo non trovato: {url}")
            continue
        color_name = get_color_name(title)
        color_dir = os.path.join(work_dir, color_name)
        os.makedirs(color_dir, exist_ok=True)
        blobs = extract_images_from_page(soup, max_images=3)
        if not blobs:
            _log(f"⚠️ Nessuna immagine per {color_name}")
            continue
        for i, blob in enumerate(blobs, start=1):
            fp = os.path.join(color_dir, f"image_{i}.jpg")
            with open(fp, "wb") as f:
                f.write(blob)
        collected_colors.append(color_name)
        _log(f"✅ {len(blobs)} immagini salvate per {color_name}")
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
# UI
# -----------------------
st.title("TeckWrap – Downloader + Color CSV (Maxtrify-ready)")

st.markdown('''
Carica un **CSV** con la colonna `url` (o `URL`): per ogni pagina prodotto verranno scaricate le immagini
(max 3 con la featured image grande), create le **sottocartelle colore**, calcolato un **HEX** coerente e generati i CSV
**in blocchi da 10 colori** per Maxtrify.
''')

csv_file = st.file_uploader("Carica prodotti.csv (colonna url o URL)", type=["csv"])

run = st.button("Esegui workflow")

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
        st.error("Nessun URL trovato nella colonna url.")
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

        # genera CSV (max 10 colori per file)
        csv_names, csv_buffers = generate_color_csvs(colors, work_dir)

        # pacchetto zip con cartelle immagini
        st.success("Completato.")
        zbytes = zip_folder(work_dir)
        st.download_button("Scarica immagini (zip)", data=zbytes, file_name="teckwrap-images.zip", mime="application/zip")

        for name, content in zip(csv_names, csv_buffers):
            st.download_button(f"Scarica {name}", data=content, file_name=name, mime="text/csv")

        st.write("Colori estratti:", sorted(set(colors)))
