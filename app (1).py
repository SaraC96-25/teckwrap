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
import streamlit as st

# -----------------------
# CONFIG
# -----------------------
st.set_page_config(page_title="Tekalab Downloader + Color CSV", layout="wide")
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "Mozilla/5.0"})

# -----------------------
# FUNZIONI HELPER
# -----------------------
def clean_color_name(title: str) -> str:
    """
    Estrae solo il nome colore ignorando FLEXISHIELD, SP-07, CL-08 ecc.
    """
    title = title.strip()

    # Se contiene un trattino lungo – prendi solo l'ultima parte
    if " – " in title:
        title = title.split(" – ")[-1]

    parts = title.split()

    # Se ultima parte è un codice tipo SP-07, rimuovila
    if len(parts) >= 2 and re.match(r"^[A-Z]{1,3}-?\d{1,3}$", parts[-1]):
        color_name = " ".join(parts[:-1])
    else:
        color_name = " ".join(parts)

    # Se inizia con FLEXISHIELD lo rimuove
    if color_name.lower().startswith("flexishield"):
        color_name = " ".join(color_name.split()[1:])

    return color_name.strip()

def slugify_handle(text: str) -> str:
    t = unicodedata.normalize("NFKD", text)
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = t.lower()
    t = re.sub(r"[^a-z0-9\s\-]", "", t)
    t = re.sub(r"\s+", "-", t).strip("-")
    t = re.sub(r"-{2,}", "-", t)
    return t

def dominant_hex_from_image(img: Image.Image, palette_colors=8):
    img = img.convert("RGB")
    w, h = img.size
    max_side = 300
    if max(w, h) > max_side:
        scale = max_side / float(max(w, h))
        img = img.resize((int(w * scale), int(h * scale)), Image.BILINEAR)
    q = img.quantize(colors=palette_colors, method=Image.MEDIANCUT)
    palette = q.getpalette()
    counts = q.getcolors() or []
    best = None
    best_count = -1
    for count, idx in counts:
        r, g, b = palette[idx * 3], palette[idx * 3 + 1], palette[idx * 3 + 2]
        h, l, s = colorsys.rgb_to_hls(r / 255.0, g / 255.0, b / 255.0)
        if l > 0.92 or l < 0.08 or s < 0.15:
            continue
        if count > best_count:
            best = (r, g, b)
            best_count = count
    if best is None:
        for count, idx in counts:
            r, g, b = palette[idx * 3], palette[idx * 3 + 1], palette[idx * 3 + 2]
            if count > best_count:
                best = (r, g, b)
                best_count = count
    if best:
        return "#{:02X}{:02X}{:02X}".format(*best)
    return ""

def dominant_hex_from_folder(folder_path: str) -> str:
    candidates = []
    for name in os.listdir(folder_path):
        if not name.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
            continue
        fp = os.path.join(folder_path, name)
        try:
            with Image.open(fp) as im:
                hexv = dominant_hex_from_image(im)
                if hexv:
                    candidates.append(hexv)
        except:
            continue
    if not candidates:
        return ""
    freq = {}
    for c in candidates:
        freq[c] = freq.get(c, 0) + 1
    return max(freq.items(), key=lambda x: x[1])[0]

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
    rows.append({**base, "Top Row": "", "Row #": 2, "Field": "color", "Value": hex_value})
    rows.append({**base, "Top Row": "", "Row #": 3, "Field": "image", "Value": ""})
    rows.append({**base, "Top Row": "", "Row #": 4, "Field": "color_taxonomy_reference", "Value": "gid://shopify/TaxonomyValue/3"})
    rows.append({**base, "Top Row": "", "Row #": 5, "Field": "pattern_taxonomy_reference", "Value": "gid://shopify/TaxonomyValue/2874"})
    return rows

def generate_color_csvs(colors, base_dir):
    colors = sorted(set(colors))
    tz = ZoneInfo("Europe/Rome")
    updated_at = datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S %z")
    csv_files = []
    chunk_size = 10
    fieldnames = ["ID", "Handle", "Command", "Display Name", "Status", "Updated At",
                  "Definition: Handle", "Definition: Name", "Top Row", "Row #", "Field", "Value"]

    for i in range(0, len(colors), chunk_size):
        chunk = colors[i:i + chunk_size]
        rows = []
        for color in chunk:
            folder = os.path.join(base_dir, color)
            hexv = dominant_hex_from_folder(folder)
            rows.extend(build_rows_for_color(color, updated_at, hexv))

        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
        csv_files.append((f"color-patterns-{i//chunk_size+1:02}.csv", buf.getvalue()))
    return csv_files

def extract_images_from_tekalab(soup: BeautifulSoup, positions: list[int]):
    gallery = soup.find("div", class_="woocommerce-product-gallery__wrapper")
    if not gallery:
        return []
    all_images = gallery.find_all("img")
    selected = []
    for idx in positions:
        if idx < len(all_images):
            url = all_images[idx].get("data-large_image") or all_images[idx].get("src")
            if url and "product-sheet" not in url.lower() and "cosmetic-paint-protection-film" not in url.lower():
                selected.append(url)
    return selected

def download_image(url):
    try:
        r = SESSION.get(url, timeout=20)
        if r.status_code == 200:
            return r.content
    except:
        return None

# -----------------------
# MAIN STREAMLIT APP
# -----------------------
st.title("Tekalab – Downloader + Color CSV")

csv_file = st.file_uploader("Carica CSV con colonna url", type=["csv"])
positions_str = st.text_input("Posizioni immagini (es: 0,1,2)", "0,1,2")
run = st.button("Avvia processo")

if run:
    if not csv_file:
        st.error("Carica prima un CSV!")
        st.stop()

    try:
        df = pd.read_csv(csv_file)
    except:
        st.error("Errore nel leggere il CSV")
        st.stop()

    if "url" not in [c.lower() for c in df.columns]:
        st.error("Colonna 'url' non trovata nel CSV")
        st.stop()

    url_col = [c for c in df.columns if c.lower() == "url"][0]
    urls = df[url_col].dropna().tolist()

    posizioni = [int(x.strip()) for x in positions_str.split(",") if x.strip().isdigit()]

    with tempfile.TemporaryDirectory() as work_dir:
        colors = []
        prog = st.progress(0)
        log_area = st.empty()
        logs = []

        for i, url in enumerate(urls):
            try:
                r = SESSION.get(url, timeout=30)
                soup = BeautifulSoup(r.text, "html.parser")
            except:
                logs.append(f"❌ Errore su {url}")
                continue

            title_tag = soup.find("h1", class_="fusion-title-heading")
            if not title_tag:
                logs.append(f"❌ Titolo non trovato: {url}")
                continue

            color_name = clean_color_name(title_tag.text)
            color_dir = os.path.join(work_dir, color_name)
            os.makedirs(color_dir, exist_ok=True)

            images = extract_images_from_tekalab(soup, posizioni)
            saved = 0
            for j, img_url in enumerate(images):
                img_bytes = download_image(img_url)
                if img_bytes:
                    fp = os.path.join(color_dir, f"image_{j + 1}.jpg")
                    with open(fp, "wb") as f:
                        f.write(img_bytes)
                    saved += 1

            if saved:
                colors.append(color_name)
                logs.append(f"✅ {saved} immagini scaricate per {color_name}")
            else:
                logs.append(f"⚠️ Nessuna immagine per {color_name}")

            prog.progress((i + 1) / len(urls))
            log_area.write("\n".join(logs[-15:]))

        # ZIP immagini
        zip_bytes = io.BytesIO()
        with zipfile.ZipFile(zip_bytes, "w", zipfile.ZIP_DEFLATED) as z:
            for root, dirs, files in os.walk(work_dir):
                for file in files:
                    filepath = os.path.join(root, file)
                    arcname = os.path.relpath(filepath, work_dir)
                    z.write(filepath, arcname)
        zip_bytes.seek(0)

        st.download_button("📦 Scarica immagini (zip)", data=zip_bytes.getvalue(), file_name="tekalab-images.zip")

        # CSV colori
        csv_files = generate_color_csvs(colors, work_dir)
        for name, content in csv_files:
            st.download_button(f"📄 Scarica {name}", data=content, file_name=name, mime="text/csv")
