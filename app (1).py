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
from urllib.parse import urlparse, parse_qs, urljoin
import streamlit as st

st.set_page_config(page_title="TeckWrap / Partners – Downloader + Color CSV", layout="wide")

# -----------------------
# HTTP session
# -----------------------
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
})

# -----------------------
# Helpers comuni
# -----------------------
def get_color_name(title: str) -> str:
    """Estrae il nome colore da vari formati:
    - 'Matte Coal Black (MT01) Vinyl Wrap' -> 'Matte Coal Black' (Shopify .com)
    - 'MT04 Matte Metallic Matte Cornflower Blue *DISCONTINUED*' -> 'Matte Cornflower Blue' (Shopify .uk)
    - '600 Moon Halo' -> 'Moon Halo' (QZVinyls)
    """
    title = (title or "").strip()
    title = title.split("*")[0].strip()  # rimuove *DISCONTINUED*

    # Caso classico con parentesi (Shopify .com)
    if "(" in title and ")" in title:
        return title.split("(")[0].strip()

    # Caso "CODICE NomeColore" (QZVinyls)
    parts = title.split()
    if parts and any(ch.isdigit() for ch in parts[0]):
        return " ".join(parts[1:]).strip()

    # Caso UK "CODICE Tipo Tipo NomeColore"
    if len(parts) >= 4:
        return " ".join(parts[3:]).strip()

    return title

def find_product_title(soup: BeautifulSoup) -> str | None:
    """Trova il titolo su Shopify (.com/.uk), WooCommerce (.gr, WooMA) e QZVinyls (.fi)."""
    for selector in [
        # Shopify
        "h1.product-title",                # teckwrap.com
        "h1.product__title",               # teckwrap.uk
        # WooCommerce standard / WooMA
        "h1.product_title.entry-title",
        "h1.product_title",
        "h1.entry-title",
        "h2.wooma-summary-item.wooma-product-title",
        "h2.wooma-product-title",
        # QZVinyls
        "h1.ProductName",
    ]:
        t = soup.select_one(selector)
        if t and t.text.strip():
            return t.text.strip()

    # Fallback <meta property="og:title">
    og = soup.find("meta", property="og:title")
    if og and og.get("content"):
        return og["content"].strip()

    # Ultimo fallback: <title>
    if soup.title and soup.title.text.strip():
        return soup.title.text.strip()

    return None

def _abs_url(u: str) -> str:
    if not u:
        return ""
    if u.startswith("//"):
        return "https:" + u
    return u

# ---- larghezza da query ?width= e da suffisso _NNNx.ext (Shopify vecchi temi) ----
SIZE_SUFFIX_RE = re.compile(r'_(\d+)x\.(jpg|jpeg|png|webp)$', re.IGNORECASE)

def width_from_url(u: str) -> int:
    """Estrae la larghezza da ?width=… o dal suffisso _NNNx.ext nel filename."""
    if not u:
        return 0
    try:
        q = parse_qs(urlparse(u).query)
        if "width" in q and q["width"]:
            return int(q["width"][0])
    except:
        pass
    m = SIZE_SUFFIX_RE.search((u or "").split("?")[0])
    if m:
        try:
            return int(m.group(1))
        except:
            pass
    return 0

def width_estimate(u: str) -> int:
    return width_from_url(u)

def _base_key(u: str) -> str:
    """Per confrontare featured vs altre: rimuove query e suffisso _NNNx."""
    if not u:
        return ""
    s = u.split("?")[0]
    s = SIZE_SUFFIX_RE.sub(r'.\2', s)  # toglie _NNNx
    return s

# -----------------------
# Shopify (.com / .uk)
# -----------------------
def candidate_urls_from_img_shopify(img_tag) -> list:
    urls = []
    # srcset / data-srcset
    for attr in ("srcset", "data-srcset"):
        srcset = img_tag.get(attr)
        if srcset:
            for part in srcset.split(","):
                seg = part.strip().split()
                if seg:
                    urls.append(_abs_url(seg[0]))
    # src / data-src
    for attr in ("src", "data-src"):
        src = img_tag.get(attr)
        if src:
            urls.append(_abs_url(src))
    # dedup preservando ordine
    seen, out = set(), []
    for u in urls:
        if u and u not in seen:
            seen.add(u); out.append(u)
    return out

def extract_images_from_shopify(soup: BeautifulSoup, max_images=3):
    """Shopify: supporta anche theme con <div class="product-gallery ..."> e <noscript>."""
    # 1) Individua featured <img>
    featured_img = soup.select_one('div.product-gallery__item[data-index="0"] img')
    if not featured_img:
        featured_img = soup.select_one("li.media-viewer__item.is-current-variant img")
    if not featured_img:
        gallery = (soup.find("media-gallery")
                   or soup.find("ul", class_="media-viewer")
                   or soup.find("div", class_="product-gallery"))
        if gallery:
            featured_img = gallery.find("img")

    # 2) URL migliore per featured
    featured_url = None
    if featured_img:
        cand = candidate_urls_from_img_shopify(featured_img)
        if cand:
            featured_url = sorted(cand, key=width_estimate, reverse=True)[0]

    # 3) Scarica tutte le immagini (gallery completa: include anche product-gallery)
    blobs = []
    featured_blob = None
    for selector in ["div.product-gallery img", "media-gallery img", "ul.media-viewer img"]:
        for img in soup.select(selector):
            urls = candidate_urls_from_img_shopify(img)
            if urls:
                best = sorted(urls, key=width_estimate, reverse=True)[0]
                try:
                    r = SESSION.get(best, timeout=20)
                    if r.status_code == 200 and r.content:
                        data = r.content
                        if featured_url and _base_key(best) == _base_key(featured_url):
                            featured_blob = data
                        blobs.append(data)
                except:
                    continue

    # 4) fallback: <noscript> (alcuni temi Shopify)
    if not blobs:
        for ns in soup.select("noscript"):
            try:
                frag = BeautifulSoup(ns.text or "", "html.parser")
            except:
                continue
            for img in frag.find_all("img"):
                urls = candidate_urls_from_img_shopify(img)
                if urls:
                    best = sorted(urls, key=width_estimate, reverse=True)[0]
                    try:
                        r = SESSION.get(best, timeout=20)
                        if r.status_code == 200 and r.content:
                            data = r.content
                            if featured_url and _base_key(best) == _base_key(featured_url):
                                featured_blob = data
                            blobs.append(data)
                    except:
                        continue

    # 5) dedup per hash + ordina per area pixel + featured in testa + limite 3
    return _dedup_order_and_limit(blobs, featured_blob, max_images)

# -----------------------
# WooCommerce (.gr)
# -----------------------
def candidate_urls_from_img_woo(img_tag) -> list:
    """Ordine preferenza: data-large_image > <a href> > srcset > src (+ lazy)."""
    urls = []
    # grande esplicito
    for attr in ("data-large_image", "data-large_image_url"):
        v = img_tag.get(attr)
        if v:
            urls.append(_abs_url(v))
    # href del parent anchor (spesso full-res)
    if img_tag.parent is not None and img_tag.parent.name == "a":
        href = img_tag.parent.get("href")
        if href:
            urls.append(_abs_url(href))
    # srcset / data-srcset
    for attr in ("srcset", "data-srcset"):
        srcset = img_tag.get(attr)
        if srcset:
            for part in srcset.split(","):
                seg = part.strip().split()
                if seg:
                    urls.append(_abs_url(seg[0]))
    # src / data-src (lazy)
    for attr in ("src", "data-src"):
        src = img_tag.get(attr)
        if src:
            urls.append(_abs_url(src))
    # dedup preservando ordine
    out, seen = [], set()
    for u in urls:
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out

def _download_first_ok(urls, timeout=20):
    for url in urls:
        try:
            r = SESSION.get(url, timeout=timeout)
            if r.status_code == 200 and r.content:
                return r.content
        except:
            continue
    return None

def extract_images_from_woocommerce(soup: BeautifulSoup, max_images=3):
    """Featured (prima della gallery) grande + max 2 altre, dedup hash."""
    imgs = soup.select(".woocommerce-product-gallery__image img")
    if not imgs:
        imgs = soup.select("div.woocommerce-product-gallery img")
    if not imgs:
        imgs = soup.select("img.wp-post-image")

    blobs = []
    featured_blob = None
    for i, img in enumerate(imgs):
        candidates = candidate_urls_from_img_woo(img)
        data = _download_first_ok(candidates)
        if not data:
            continue
        if i == 0:
            featured_blob = data
        blobs.append(data)

    return _dedup_order_and_limit(blobs, featured_blob, max_images)

# -----------------------
# QZVinyls (.fi)
# -----------------------
def extract_images_from_qzvinyls(soup: BeautifulSoup, page_url: str, max_images=3):
    """QZVinyls: usa gli <a.ProductImage> (href -> 1200x1200).
       Featured = prima .ImageItem (o .is-selected)."""
    # 1) featured anchor
    featured_a = (soup.select_one("div.ProductImageSlider .ImageItem.is-selected a.ProductImage")
                  or soup.select_one("div.ProductImageSlider .ImageItem a.ProductImage"))

    featured_url = None
    if featured_a and featured_a.get("href"):
        featured_url = urljoin(page_url, featured_a["href"])

    # 2) raccogli TUTTE le anchor della slider (e, se serve, delle thumbnails)
    anchors = soup.select("div.ProductImageSlider a.ProductImage")
    if not anchors:
        anchors = soup.select("#ProductThumbnails a.ProductThumbnail")

    urls = []
    for a in anchors:
        href = a.get("href")
        if href:
            urls.append(urljoin(page_url, href))

    # dedup preservando ordine
    seen, urls = set(), [u for u in urls if not (u in seen or seen.add(u))]

    # 3) scarica (href -> versioni 1200x1200)
    blobs = []
    featured_blob = None
    for u in urls:
        try:
            r = SESSION.get(u, timeout=20)
            if r.status_code == 200 and r.content:
                data = r.content
                if featured_url and u.split("?")[0] == featured_url.split("?")[0]:
                    featured_blob = data
                blobs.append(data)
        except:
            continue

    # 4) dedup/ordina/featured/limite
    return _dedup_order_and_limit(blobs, featured_blob, max_images)

# -----------------------
# Download + dedup comuni
# -----------------------
def _dedup_order_and_limit(blobs, featured_blob, max_images):
    if not blobs:
        return []
    # dedup per hash e misura area
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
    # ordina per area
    sorted_imgs = sorted(unique.values(), key=lambda x: x[1], reverse=True)
    images_ordered = [img for img, _ in sorted_imgs]
    # featured in testa
    if featured_blob:
        images_ordered = [featured_blob] + [img for img in images_ordered if img != featured_blob]
    # limita
    return images_ordered[:max_images]

# -----------------------
# Colore HEX (dominante)
# -----------------------
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
        if 0.08 < l < 0.92 and s > 0.15 and count > best_count:
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
# CSV Maxtrify
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
# Dispatcher per dominio
# -----------------------
def extract_images_auto(url, soup, max_images=3):
    host = urlparse(url).netloc.lower()
    if "teckwrap.gr" in host:
        return extract_images_from_woocommerce(soup, max_images=max_images)
    elif "qzvinyls.fi" in host:
        return extract_images_from_qzvinyls(soup, page_url=url, max_images=max_images)
    else:
        return extract_images_from_shopify(soup, max_images=max_images)

# -----------------------
# Pipeline
# -----------------------
def process_urls(urls, work_dir, progress=None, log=None):
    collected_colors = []
    def _log(msg):
        if log: log(msg)
    total = len(urls)
    for idx, url in enumerate(urls):
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

        blobs = extract_images_auto(url, soup, max_images=3)
        if not blobs:
            _log(f"⚠️ Nessuna immagine per {color_name}")
            continue

        for i, blob in enumerate(blobs, start=1):
            fp = os.path.join(color_dir, f"image_{i}.jpg")
            try:
                with open(fp, "wb") as f:
                    f.write(blob)
            except:
                pass

        collected_colors.append(color_name)
        _log(f"✅ {len(blobs)} immagini salvate per {color_name}")

    if progress:
        progress.progress(1.0)
    return collected_colors

def zip_all(base_dir, csv_files):
    """Unico zip con cartelle immagini + CSV in root."""
    bio = io.BytesIO()
    with zipfile.ZipFile(bio, "w", zipfile.ZIP_DEFLATED) as z:
        # immagini
        for root, _, files in os.walk(base_dir):
            for f in files:
                fp = os.path.join(root, f)
                arc = os.path.relpath(fp, base_dir)
                z.write(fp, arc)
        # CSV
        for name, content in csv_files:
            z.writestr(name, content)
    bio.seek(0)
    return bio.read()

# -----------------------
# UI
# -----------------------
st.title("Downloader + Color CSV (Shopify/Woo/QZVinyls) – Maxtrify-ready")

st.markdown('''
Carica un **CSV** con la colonna `url` (o `URL`):  
- Scarica immagini (max 3 con **featured grande**) da **.com / .uk / .gr / .fi**  
- Crea sottocartelle per **colore**  
- Genera CSV **Maxtrify** in blocchi da **10 colori**  
- Scarica **tutto** in un unico ZIP
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

    # trova colonna url
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

        # CSV (max 10 colori per file)
        csv_names, csv_buffers = generate_color_csvs(colors, work_dir)
        csv_files = list(zip(csv_names, csv_buffers))

        # ZIP unico
        all_zip = zip_all(work_dir, csv_files)

        st.success("✅ Completato! Tutti i file sono pronti.")
        st.download_button("⬇️ Scarica tutto (immagini + CSV)",
                           data=all_zip,
                           file_name="package-images-and-csv.zip",
                           mime="application/zip")
        st.write("Colori estratti:", sorted(set(colors)))
