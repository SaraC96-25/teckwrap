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

st.set_page_config(page_title="Downloader Tekalab PPF", layout="wide")

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "Mozilla/5.0"})

# ---------------------------
# PULIZIA NOME COLORE
# ---------------------------
def clean_color_name(title: str) -> str:
    title = title.strip()

    # Se c'è " – " prendiamo la parte dopo
    if " – " in title:
        title = title.split(" – ")[-1].strip()

    # Rimuovi codici SP-, CL-, ecc.
    title = re.sub(r"\b(?:SP|CL|DM|PPF|CP|DP|XP)[- ]?\d{1,3}\b", "", title, flags=re.IGNORECASE)

    # Rimuovi parole generiche
    generici = ["FLEXISHIELD", "Cosmétique", "PPF", "Film", "couleur", "protection", "Paint", "Protection"]
    pattern_generici = r"\b(" + "|".join(generici) + r")\b"
    title = re.sub(pattern_generici, "", title, flags=re.IGNORECASE)

    # Rimuovi spazi multipli
    title = re.sub(r"\s{2,}", " ", title).strip()
    return title


# ---------------------------
# HEX CALCULATION
# ---------------------------
def rgb_to_hex(rgb):
    r, g, b = rgb
    return "#{:02X}{:02X}{:02X}".format(int(r), int(g), int(b))

def dominant_hex_from_image(img: Image.Image, palette_colors=8):
    img = img.convert("RGB")
    img.thumbnail((300, 300))
    q = img.quantize(colors=palette_colors, method=Image.MEDIANCUT)
    palette = q.getpalette()
    counts = q.getcolors() or []
    best, best_count = None, -1
    for count, idx in counts:
        r, g, b = palette[idx*3: idx*3+3]
        h, l, s = colorsys.rgb_to_hls(r/255, g/255, b/255)
        if l > 0.92 or l < 0.08 or s < 0.15:
            continue
        if count > best_count:
            best, best_count = (r, g, b), count
    return rgb_to_hex(best) if best else "#888888"

def dominant_hex_from_folder(folder_path: str) -> str:
    colors = []
    for f in os.listdir(folder_path):
        if f.lower().endswith((".jpg", ".jpeg", ".png")):
            try:
                with Image.open(os.path.join(folder_path, f)) as img:
                    colors.append(dominant_hex_from_image(img))
            except:
                pass
    if not colors:
        return "#888888"
    return max(set(colors), key=colors.count)


# ---------------------------
# GENERAZIONE CSV
# ---------------------------
def slugify_handle(text: str) -> str:
    t = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    t = re.sub(r"[^a-z0-9]+", "-", t.lower()).strip("-")
    return t

def build_rows_for_color(color_name: str, updated_at_str: str, hex_value: str):
    handle = slugify_handle(color_name)
    base = {
        "ID": "", "Handle": handle, "Command": "MERGE", "Display Name": color_name,
        "Status": "", "Updated At": updated_at_str, "Definition: Handle": "shopify--color-pattern",
        "Definition: Name": "Color"
    }
    rows = [
        {**base, "Top Row": True, "Row #": 1, "Field": "label", "Value": color_name},
        {**base, "Top Row": "", "Row #": 2, "Field": "color", "Value": hex_value},
        {**base, "Top Row": "", "Row #": 3, "Field": "image", "Value": ""},
        {**base, "Top Row": "", "Row #": 4, "Field": "color_taxonomy_reference",
         "Value": "gid://shopify/TaxonomyValue/3"},
        {**base, "Top Row": "", "Row #": 5, "Field": "pattern_taxonomy_reference",
         "Value": "gid://shopify/TaxonomyValue/2874"}
    ]
    return rows

def generate_color_csvs(colors, base_dir):
    tz = ZoneInfo("Europe/Rome")
    updated_at = datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S %z")
    fieldnames = [
        "ID", "Handle", "Command", "Display Name", "Status", "Updated At",
        "Definition: Handle", "Definition: Name", "Top Row", "Row #", "Field", "Value"
    ]
    csv_files = []
    for i in range(0, len(colors), 10):
        chunk = colors[i:i+10]
        rows = []
        for color in chunk:
            folder = os.path.join(base_dir, color)
            hex_val = dominant_hex_from_folder(folder)
            rows.extend(build_rows_for_color(color, updated_at, hex_val))
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
        csv_files.append((f"color-patterns-{i//10+1}.csv", buf.getvalue()))
    return csv_files


# ---------------------------
# DOWNLOAD IMMAGINI TEKALAB
# ---------------------------
def is_excluded_image(img_tag):
    """Esclude immagini tipo 'PRODUCT SHEET' o 'COSMETIC PPF' """
    alt = img_tag.get("alt", "").lower()
    src = img_tag.get("src", "").lower()
    exclude_keywords = ["product sheet", "cosmetic paint protection film", "cosmetic ppf"]
    return any(kw in alt or kw in src for kw in exclude_keywords)

def extract_images_from_tekalab(soup, positions):
    wrapper = soup.find("div", class_="woocommerce-product-gallery__wrapper")
    if not wrapper:
        return []
    items = wrapper.find_all("div", class_="woocommerce-product-gallery__image")
    selected = []
    for pos in positions:
        if pos < len(items):
            img_tag = items[pos].find("img")
            if img_tag and not is_excluded_image(img_tag):
                if img_tag.get("data-src"):
                    selected.append(img_tag["data-src"])
                elif img_tag.get("src"):
                    selected.append(img_tag["src"])
    return selected


def process_urls(urls, positions, progress, log, base_dir):
    colors = []
    for idx, url in enumerate(urls):
        progress.progress((idx+1) / len(urls))
        try:
            r = SESSION.get(url, timeout=30)
            soup = BeautifulSoup(r.text, "html.parser")
        except Exception as e:
            log(f"❌ Errore connessione: {url}")
            continue

        h1 = soup.find("h1")
        if not h1:
            log(f"❌ Titolo non trovato: {url}")
            continue

        color_name = clean_color_name(h1.text)
        if not color_name:
            log(f"⚠️ Nome colore vuoto: {url}")
            continue

        folder = os.path.join(base_dir, color_name)
        os.makedirs(folder, exist_ok=True)

        img_urls = extract_images_from_tekalab(soup, positions)
        if not img_urls:
            log(f"⚠️ Nessuna immagine trovata per {color_name}")
            continue

        for i, img_url in enumerate(img_urls):
            try:
                img_data = SESSION.get(img_url).content
                with open(os.path.join(folder, f"image_{i+1}.jpg"), "wb") as f:
                    f.write(img_data)
            except:
                pass

        colors.append(color_name)
        log(f"✅ {len(img_urls)} immagini salvate per {color_name}")

    return list(set(colors))


def build_final_zip(base_dir, colors):
    bio = io.BytesIO()
    with zipfile.ZipFile(bio, "w") as z:
        # Aggiungi immagini
        for color in colors:
            folder = os.path.join(base_dir, color)
            for root, _, files in os.walk(folder):
                for f in files:
                    z.write(os.path.join(root, f), os.path.join(color, f))

        # Aggiungi CSV
        csv_files = generate_color_csvs(colors, base_dir)
        for name, content in csv_files:
            z.writestr(name, content)

    bio.seek(0)
    return bio.read()


# ---------------------------
# UI STREAMLIT
# ---------------------------
st.title("Downloader Tekalab – immagini + CSV colori")

uploaded = st.file_uploader("Carica CSV con colonna url", type=["csv"])
positions = st.multiselect("Seleziona posizioni immagini da scaricare",
                            [0, 1, 2, 3, 4, 5], default=[0, 1, 2])

if st.button("Avvia"):
    if not uploaded:
        st.error("Carica prima un file CSV!")
        st.stop()

    df = pd.read_csv(uploaded)
    if "url" not in [c.lower() for c in df.columns]:
        st.error("Il CSV deve avere colonna 'url'")
        st.stop()

    urls = df[df.columns[0]].dropna().tolist()

    log_box = st.empty()
    prog = st.progress(0)

    logs = []
    def log(msg):
        logs.append(msg)
        log_box.text("\n".join(logs[-10:]))

    base_dir = tempfile.mkdtemp()
    colors = process_urls(urls, positions, prog, log, base_dir)

    if not colors:
        st.warning("Nessun colore processato")
        st.stop()

    final_zip = build_final_zip(base_dir, colors)
    st.download_button("📦 Scarica immagini + CSV (zip)", data=final_zip,
                       file_name="tekalab-immagini-colori.zip", mime="application/zip")
