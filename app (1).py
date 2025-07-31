import os
import re
import csv
import io
import zipfile
import unicodedata
import hashlib
import tempfile
from datetime import datetime
from zoneinfo import ZoneInfo
from urllib.parse import urlparse, parse_qs
from io import BytesIO

import requests
import pandas as pd
from bs4 import BeautifulSoup
from PIL import Image
import streamlit as st

st.set_page_config(page_title="CoverStyl Downloader + CSV", layout="wide")

# -----------------------
# HTTP session
# -----------------------
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X X) AppleWebKit/537.36 (KHTML, like Gecko) Chrome Safari"
})

# -----------------------
# Helpers
# -----------------------
def slugify_handle(text: str) -> str:
    t = unicodedata.normalize("NFKD", text)
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = t.lower()
    t = re.sub(r"[^a-z0-9\s\-]", "", t)
    t = re.sub(r"\s+", "-", t).strip("-")
    return re.sub(r"-{2,}", "-", t)

def rgb_to_hex(rgb):
    return "#{:02X}{:02X}{:02X}".format(int(rgb[0]), int(rgb[1]), int(rgb[2]))

def dominant_hex_from_image(img: Image.Image):
    img = img.convert("RGB")
    img = img.resize((150, 150))
    colors = img.getcolors(150*150)
    if not colors:
        return "#000000"
    colors.sort(reverse=True)
    return rgb_to_hex(colors[0][1])

def dominant_hex_from_folder(folder_path: str) -> str:
    for name in os.listdir(folder_path):
        if name.lower().endswith((".jpg", ".jpeg", ".png")):
            with Image.open(os.path.join(folder_path, name)) as im:
                return dominant_hex_from_image(im)
    return "#000000"

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
    return [
        {**base, "Top Row": True, "Row #": 1, "Field": "label", "Value": color_name},
        {**base, "Top Row": "",   "Row #": 2, "Field": "color", "Value": hex_value},
        {**base, "Top Row": "",   "Row #": 3, "Field": "image", "Value": ""},
        {**base, "Top Row": "",   "Row #": 4, "Field": "color_taxonomy_reference",   "Value": "gid://shopify/TaxonomyValue/3"},
        {**base, "Top Row": "",   "Row #": 5, "Field": "pattern_taxonomy_reference", "Value": "gid://shopify/TaxonomyValue/2874"}
    ]

def generate_color_csvs(colors, base_dir):
    colors = sorted(set(colors))
    if not colors:
        return [], []
    tz = ZoneInfo("Europe/Rome")
    updated_at = datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S %z")
    fieldnames = ["ID","Handle","Command","Display Name","Status","Updated At",
                  "Definition: Handle","Definition: Name","Top Row","Row #","Field","Value"]
    chunk_size = 10
    csv_names, csv_buffers = [], []
    for i in range(0, len(colors), chunk_size):
        chunk = colors[i:i+chunk_size]
        rows = []
        for c in chunk:
            folder = os.path.join(base_dir, c)
            hex_val = dominant_hex_from_folder(folder)
            rows.extend(build_rows_for_color(c, updated_at, hex_val))
        name = f"color-patterns-{(i//chunk_size)+1:02d}.csv"
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
        csv_names.append(name)
        csv_buffers.append(buf.getvalue())
    return csv_names, csv_buffers

def zip_all(base_dir, csv_names, csv_buffers):
    bio = io.BytesIO()
    with zipfile.ZipFile(bio, "w", zipfile.ZIP_DEFLATED) as z:
        for root, _, files in os.walk(base_dir):
            for f in files:
                fp = os.path.join(root, f)
                z.write(fp, os.path.relpath(fp, base_dir))
        for name, buf in zip(csv_names, csv_buffers):
            z.writestr(name, buf)
    bio.seek(0)
    return bio.read()

# -----------------------
# Image extraction for CoverStyl
# -----------------------
def process_urls(urls, work_dir, progress=None, log=None):
    colors = []
    total = len(urls)
    for idx, url in enumerate(urls):
        if progress:
            progress.progress((idx+1)/total)
        try:
            r = SESSION.get(url, timeout=20)
            soup = BeautifulSoup(r.text, "html.parser")
        except Exception as e:
            if log: log(f"❌ Errore caricamento {url}")
            continue

        # Titolo -> colore
        title_tag = soup.find("h1", class_="t5")
        if not title_tag:
            if log: log(f"❌ Titolo non trovato: {url}")
            continue
        color_name = title_tag.get_text(strip=True)

        # Cartella colore
        folder = os.path.join(work_dir, color_name)
        os.makedirs(folder, exist_ok=True)

        # Immagine principale
        img_div = soup.find("div", class_="gallery_Image__7KTqk")
        if not img_div:
            if log: log(f"⚠️ Nessuna immagine trovata per {color_name}")
            continue
        img_tag = img_div.find("img")
        if not img_tag:
            if log: log(f"⚠️ Nessuna immagine trovata per {color_name}")
            continue

        # Usa srcset per la migliore risoluzione
        srcset = img_tag.get("srcset") or img_tag.get("src")
        if " " in srcset:
            src = srcset.split(",")[-1].strip().split(" ")[0]
        else:
            src = srcset
        if src.startswith("//"):
            src = "https:" + src
        elif src.startswith("/"):
            src = "https://coverstyl.com" + src

        try:
            img_bytes = SESSION.get(src, timeout=20).content
            fp = os.path.join(folder, "image_1.jpg")
            with open(fp, "wb") as f:
                f.write(img_bytes)
            colors.append(color_name)
            if log: log(f"✅ Immagine salvata per {color_name}")
        except:
            if log: log(f"⚠️ Download fallito per {color_name}")

    return colors


# -----------------------
# UI Streamlit
# -----------------------
st.title("CoverStyl Downloader + CSV")

csv_file = st.file_uploader("Carica CSV con colonna url", type=["csv"])
run = st.button("Esegui")

if run:
    if not csv_file:
        st.error("Carica prima un CSV!")
        st.stop()

    try:
        df = pd.read_csv(csv_file)
    except Exception as e:
        st.error(f"Errore lettura CSV: {e}")
        st.stop()

    url_col = next((c for c in df.columns if c.strip().lower() == "url"), None)
    if not url_col:
        st.error("Il CSV deve contenere una colonna 'url'")
        st.stop()

    urls = [u for u in df[url_col].astype(str).tolist() if u.strip()]
    if not urls:
        st.error("Nessun URL valido nel CSV")
        st.stop()

    with tempfile.TemporaryDirectory() as work_dir:
        prog = st.progress(0)
        log_box = st.empty()
        logs = []
        def log(msg):
            logs.append(msg)
            log_box.write("\n".join(logs[-20:]))

        colors = process_urls(urls, work_dir, progress=prog, log=log)

        if not colors:
            st.warning("Nessun colore scaricato.")
            st.stop()

        csv_names, csv_buffers = generate_color_csvs(colors, work_dir)
        zip_data = zip_all(work_dir, csv_names, csv_buffers)

        st.success("Completato!")
        st.download_button("⬇️ Scarica immagini + CSV (zip)", data=zip_data,
                           file_name="coverstyl.zip", mime="application/zip")
        st.write("Colori scaricati:", colors)
