import os
import re
import csv
import io
import zipfile
import unicodedata
import requests
import tempfile
from bs4 import BeautifulSoup
from PIL import Image
from datetime import datetime
from zoneinfo import ZoneInfo
import streamlit as st

# ----------------------
# Config Streamlit
# ----------------------
st.set_page_config(page_title="Tekalab Downloader", layout="wide")
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "Mozilla/5.0"})

# ----------------------
# Helpers
# ----------------------
def clean_color_name(title: str) -> str:
    """
    Estrae il nome colore dal titolo del prodotto.
    """
    title = title.strip()
    # Prende solo la parte dopo l'ultimo trattino o spazio
    if " – " in title:
        name = title.split(" – ")[-1]
    else:
        parts = title.split()
        name = parts[-2] + " " + parts[-1] if len(parts) > 1 else parts[-1]
    return name.strip()

def find_title(soup: BeautifulSoup) -> str:
    h1 = soup.find("h1", class_="fusion-title-heading")
    return h1.get_text(strip=True) if h1 else None

def filter_invalid_images(url: str, alt_text: str = "") -> bool:
    """
    Filtra immagini che devono essere escluse (product sheet, placeholder, ecc.)
    """
    invalid_keywords = ["product sheet", "cosmetic paint protection film", "clear coat"]
    filename = url.lower()
    alt = alt_text.lower()
    return not any(k in filename or k in alt for k in invalid_keywords)

def extract_images(soup: BeautifulSoup, positions: list[int]):
    """
    Estrae immagini dalle posizioni specificate nella galleria.
    """
    gallery = soup.find("div", class_="woocommerce-product-gallery__wrapper")
    if not gallery:
        return []
    imgs = gallery.find_all("img", recursive=True)
    urls = []
    for idx, img in enumerate(imgs, start=1):
        url = img.get("data-large_image") or img.get("data-src") or img.get("src")
        alt = img.get("alt") or ""
        if url and filter_invalid_images(url, alt):
            if idx in positions:
                urls.append(url)
    return urls

def download_images(color_name: str, urls: list[str], base_dir: str):
    """
    Scarica le immagini selezionate in una cartella.
    """
    folder = os.path.join(base_dir, color_name)
    os.makedirs(folder, exist_ok=True)
    for i, url in enumerate(urls[:3]):
        if url.startswith("//"):
            url = "https:" + url
        try:
            r = SESSION.get(url, timeout=30)
            if r.status_code == 200:
                with open(os.path.join(folder, f"image_{i+1}.jpg"), "wb") as f:
                    f.write(r.content)
        except:
            continue

def zip_folder(base_dir):
    """
    Crea un archivio zip di tutte le cartelle immagini.
    """
    bio = io.BytesIO()
    with zipfile.ZipFile(bio, "w", zipfile.ZIP_DEFLATED) as z:
        for root, _, files in os.walk(base_dir):
            for f in files:
                fp = os.path.join(root, f)
                arc = os.path.relpath(fp, base_dir)
                z.write(fp, arc)
    bio.seek(0)
    return bio.read()

# ----------------------
# UI Streamlit
# ----------------------
st.title("Tekalab Downloader – Versione Posizioni Immagini")

uploaded_file = st.file_uploader("Carica CSV (colonna url)", type=["csv"])
positions_input = st.text_input(
    "Posizioni immagini da scaricare (es. 1,3,5)", value="1,2,3"
)

if st.button("Esegui download"):
    if not uploaded_file:
        st.error("Carica un CSV valido.")
        st.stop()

    try:
        df = csv.DictReader(io.StringIO(uploaded_file.getvalue().decode("utf-8")))
    except Exception as e:
        st.error(f"Errore lettura CSV: {e}")
        st.stop()

    try:
        positions = [int(x.strip()) for x in positions_input.split(",") if x.strip().isdigit()]
    except:
        st.error("Inserisci posizioni valide (es. 1,3,5).")
        st.stop()

    with tempfile.TemporaryDirectory() as work_dir:
        logs = []
        for row in df:
            url = row.get("url") or row.get("URL")
            if not url:
                continue
            try:
                r = SESSION.get(url.strip(), timeout=30)
                soup = BeautifulSoup(r.text, "html.parser")
                title = find_title(soup)
                if not title:
                    logs.append(f"❌ Titolo non trovato per {url}")
                    continue
                color = clean_color_name(title)
                img_urls = extract_images(soup, positions)
                if not img_urls:
                    logs.append(f"⚠️ Nessuna immagine valida trovata per {color}")
                    continue
                download_images(color, img_urls, work_dir)
                logs.append(f"✅ {len(img_urls)} immagini salvate per {color}")
            except Exception as e:
                logs.append(f"❌ Errore {url}: {e}")
        # Download ZIP
        zip_data = zip_folder(work_dir)
        st.download_button(
            "Scarica tutte le immagini (zip)",
            data=zip_data,
            file_name="tekalab-immagini.zip",
            mime="application/zip",
        )

        st.text("\n".join(logs))
