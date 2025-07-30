import os
import re
import csv
import io
import hashlib
from io import BytesIO
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from PIL import Image
from datetime import datetime
from zoneinfo import ZoneInfo

# =========================
# Configurazione mirata Tekalab
# =========================
# Posizioni 1-based nella gallery (ignora gli elementi .clone)
GALLERY_POSITIONS = [3, 5, 6]  # es. CL-02: ...-01.jpg, 145.jpg, ...-08.jpg
MAX_IMAGES = 3

# Esclusioni (URL da evitare quando si riempiono buchi se mancano posizioni richieste)
EXCLUDE_PATTERNS = [
    re.compile(r"/1-28\.(jpg|jpeg|png|webp)$", re.IGNORECASE),
    re.compile(r"/146\.(jpg|jpeg|png|webp)$", re.IGNORECASE),
    re.compile(r"/149\.(jpg|jpeg|png|webp)$", re.IGNORECASE),
]

CSV_INPUT = "prodotti.csv"

# =========================
# HTTP
# =========================
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
})

# =========================
# Utils
# =========================
def fetch_soup(url: str) -> BeautifulSoup | None:
    try:
        r = SESSION.get(url, timeout=30)
        r.raise_for_status()
        return BeautifulSoup(r.text, "html.parser")
    except Exception as e:
        print(f"❌ Errore richiesta pagina: {url} -> {e}")
        return None

def is_tekalab(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return "tekalab.com" in host

def find_title_text(soup: BeautifulSoup) -> str | None:
    t = soup.select_one("h1.fusion-title-heading")
    if t and t.text.strip():
        return t.text.strip()
    og = soup.find("meta", property="og:title")
    if og and og.get("content"):
        return og["content"].strip()
    if soup.title and soup.title.text.strip():
        return soup.title.text.strip()
    return None

def get_color_from_title(title: str) -> str:
    """
    'FLEXISHIELD Cosmétique PPF CL-02 Racing Red' -> 'Racing Red'
    Regola: prendi il testo dopo 'PPF <CODICE> ' se presente; altrimenti le ultime 2 parole.
    """
    t = (title or "").strip()
    m = re.search(r"PPF\s+[A-Z0-9\-]+\s+(.+)$", t, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    parts = t.split()
    if len(parts) >= 2:
        return " ".join(parts[-2:]).strip()
    return t

def gallery_fullsize_urls(soup: BeautifulSoup) -> list[str]:
    """
    Link full-size ordinati:
    div.woocommerce-product-gallery__wrapper > div.woocommerce-product-gallery__image:not(.clone) a[href]
    """
    urls = []
    for a in soup.select(
        "div.woocommerce-product-gallery__wrapper div.woocommerce-product-gallery__image:not(.clone) a[href]"
    ):
        href = (a.get("href") or "").strip()
        if href.lower().startswith("http"):
            urls.append(href)

    # dedup preservando ordine
    seen = set()
    out = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out

def pick_by_positions(urls: list[str], positions: list[int], max_images: int) -> list[str]:
    """Sceglie per posizioni 1-based; se mancano, riempie con i successivi non esclusi."""
    chosen = []
    # 1) posizioni richieste
    for pos in positions:
        idx = pos - 1
        if 0 <= idx < len(urls):
            chosen.append(urls[idx])
        if len(chosen) >= max_images:
            return chosen[:max_images]

    # 2) riempimento
    def is_excluded(u: str) -> bool:
        return any(p.search(u) for p in EXCLUDE_PATTERNS)

    for u in urls:
        if len(chosen) >= max_images:
            break
        if u in chosen or is_excluded(u):
            continue
        chosen.append(u)

    return chosen[:max_images]

def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()

def dedup_by_hash_keep_order(blobs: list[bytes]) -> list[bytes]:
    seen = set()
    out = []
    for data in blobs:
        try:
            h = sha256_bytes(data)
        except Exception:
            continue
        if h not in seen:
            seen.add(h)
            out.append(data)
    return out

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
    best = None
    best_count = -1
    for count, idx in counts:
        r = palette[idx*3]; g = palette[idx*3+1]; b = palette[idx*3+2]
        h_, l_, s_ = colorsys.rgb_to_hls(r/255.0, g/255.0, b/255.0)
        if 0.08 < l_ < 0.92 and s_ > 0.15 and count > best_count:
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
                hv = dominant_hex_from_image(im, palette_colors=8)
                if hv:
                    candidates.append(hv.upper())
        except Exception:
            continue
    if not candidates:
        return ""
    # moda semplice
    freq = {}
    for c in candidates:
        freq[c] = freq.get(c, 0) + 1
    return max(freq.items(), key=lambda x: x[1])[0]

# =========================
# CSV Maxtrify (come last-color-patterns.csv)
# =========================
def slugify_handle(text: str) -> str:
    import unicodedata
    t = unicodedata.normalize("NFKD", text)
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = re.sub(r"[^a-zA-Z0-9\s\-]", "", t)
    t = re.sub(r"\s+", "-", t).strip("-")
    t = re.sub(r"-{2,}", "-", t)
    return t.lower()

def build_rows_for_color(color_name: str, updated_at_str: str, hex_value: str) -> list[dict]:
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

def write_chunked_color_csvs(colors: list[str], base_dir: str) -> list[str]:
    colors = sorted(set(c for c in colors if c.strip()))
    if not colors:
        return []
    tz = ZoneInfo("Europe/Rome")
    updated_at = datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S %z")

    fieldnames = [
        "ID", "Handle", "Command", "Display Name", "Status", "Updated At",
        "Definition: Handle", "Definition: Name", "Top Row", "Row #", "Field", "Value"
    ]

    os.makedirs(base_dir, exist_ok=True)
    out_files = []
    chunk_size = 10
    for i in range(0, len(colors), chunk_size):
        chunk = colors[i:i+chunk_size]
        rows = []
        for color in chunk:
            folder = os.path.join(base_dir, color)
            hex_val = dominant_hex_from_folder(folder) or ""
            rows.extend(build_rows_for_color(color, updated_at, hex_val))
        idx = (i // chunk_size) + 1
        csv_name = os.path.join(base_dir, f"color-patterns-{idx:02d}.csv")
        with open(csv_name, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            w.writerows(rows)
        out_files.append(csv_name)
    return out_files

# =========================
# Core: 1 URL Tekalab -> cartella colore + 3 immagini
# =========================
def process_tekalab_url(url: str, base_dir: str) -> str | None:
    if not is_tekalab(url):
        print(f"⚠️ URL ignorato (non tekalab.com): {url}")
        return None

    soup = fetch_soup(url)
    if not soup:
        return None

    title = find_title_text(soup)
    if not title:
        print(f"❌ Titolo non trovato: {url}")
        return None

    color = get_color_from_title(title)
    color_dir = os.path.join(base_dir, color)
    os.makedirs(color_dir, exist_ok=True)

    urls = gallery_fullsize_urls(soup)
    if not urls:
        print(f"⚠️ Nessuna immagine trovata in gallery: {url}")
        return None

    wanted = pick_by_positions(urls, GALLERY_POSITIONS, MAX_IMAGES)
    if not wanted:
        print(f"⚠️ Impossibile selezionare immagini per {color}")
        return None

    blobs = []
    for u in wanted:
        try:
            r = SESSION.get(u, timeout=30)
            if r.status_code == 200 and r.content:
                blobs.append(r.content)
        except Exception:
            continue

    blobs = dedup_by_hash_keep_order(blobs)
    if not blobs:
        print(f"⚠️ Nessuna immagine scaricata per {color}")
        return None

    # salva file
    for i, data in enumerate(blobs[:MAX_IMAGES], start=1):
        fp = os.path.join(color_dir, f"image_{i}.jpg")
        try:
            with open(fp, "wb") as f:
                f.write(data)
        except Exception:
            pass

    print(f"✅ {len(blobs[:MAX_IMAGES])} immagini salvate per {color}")
    return color

# =========================
# Batch: legge prodotti.csv e crea CSV colori
# =========================
def run_batch(csv_input: str, output_root: str):
    if not os.path.exists(csv_input):
        print(f"❌ CSV non trovato: {csv_input}")
        return

    with open(csv_input, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        urls = [row.get("url", "").strip() for row in reader if row.get("url", "").strip()]

    if not urls:
        print("⚠️ Nessun URL valido nel CSV.")
        return

    colors = []
    for u in urls:
        c = process_tekalab_url(u, output_root)
        if c:
            colors.append(c)

    if not colors:
        print("⚠️ Nessun colore processato.")
        return

    csv_files = write_chunked_color_csvs(colors, output_root)
    print("🎉 Completato.")
    if csv_files:
        for name in csv_files:
            print(f"   • CSV generato: {name}")

# =========================
# Main
# =========================
if __name__ == "__main__":
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    OUTPUT_DIR = BASE_DIR  # sottocartelle colore e CSV nello stesso path dello script
    run_batch(os.path.join(BASE_DIR, CSV_INPUT), OUTPUT_DIR)
