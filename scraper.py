"""
Feed de productos para Google Merchant Center desde https://fauna.com.uy (plataforma Luna).

Formato idéntico al catálogo de referencia de El Ganso: un ítem por producto, con
id, title, description, link, image_link, availability, price, brand, product_type,
condition e identifier_exists = no.

Estrategia:
  1. Lee las categorías del menú (Teros, Colibríes, Campo, Aves, Marinos) y recorre sus páginas
     (/campo/1, /campo/2, ...) juntando los links de producto (/p/<nombre>/<id>/<id>) con su precio.
  2. Abre cada producto para sacar imagen, descripción y características (tamaño, peso, material...).
  3. Escribe feed.xml (el feed) y feed.json (datos crudos).

Uso:  pip install requests beautifulsoup4
      python scraper.py
"""
import html
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.parse import urljoin
from xml.sax.saxutils import escape

import requests
from bs4 import BeautifulSoup

# ------------------------------------------------------------ CONFIGURACIÓN ---
BASE = "https://fauna.com.uy"
MARCA = "Fauna Deco"
TIPO = "Escultura"                       # para la descripción: "Escultura Fauna Deco: ..."
TITULO_FEED = "Feed de productos Fauna Deco"
ARCHIVO_XML = "feed.xml"
MONEDA_DEFECTO = "USD"

DELAY = 0.5
WORKERS = 3
TIMEOUT = 25
MAX_PAGINAS_CATEGORIA = 30
MIN_RATIO = 0.7          # si hoy hay menos del 70% de los productos de ayer, no se pisa el feed
GUARDAR_DEBUG = True     # guarda el HTML de un producto en debug/producto.html para revisar la estructura
# -----------------------------------------------------------------------------

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (compatible; ProductFeedBot/1.0)",
    "Accept-Language": "es-UY,es;q=0.9",
})
ESTADOS = {}
INICIO = time.time()

RE_PRODUCTO = re.compile(r"^https?://(?:www\.)?fauna\.com\.uy/p/([^/]+)/(\d+)(?:/(\d+))?/?$")
RE_CATEGORIA = re.compile(r"^https?://(?:www\.)?fauna\.com\.uy/([a-z0-9-]+)/(\d+)/?$")
NO_CATEGORIAS = {"p", "c", "cart", "carrito", "about", "contactanos", "politicas-de-compra-", "index.php"}


def get(url, retries=5):
    for intento in range(retries):
        try:
            r = session.get(url, timeout=TIMEOUT)
            ESTADOS[r.status_code] = ESTADOS.get(r.status_code, 0) + 1
            if r.status_code == 200:
                return r
            if r.status_code == 404:
                return None
            if r.status_code in (429, 403, 503):
                pausa = min(10 * (intento + 1), 60)
                print(f"  HTTP {r.status_code} en {url} -> pausa {pausa}s", flush=True)
                time.sleep(pausa)
                continue
        except requests.RequestException as e:
            ESTADOS["error"] = ESTADOS.get("error", 0) + 1
            print(f"  error {type(e).__name__} en {url}", flush=True)
        time.sleep(2 * (intento + 1))
    return None


def texto(el):
    return re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip() if el else ""


def leer_precio(txt):
    """'USD 2.400' -> (2400.0, 'USD') | '$ 1.250,50' -> (1250.5, 'UYU')"""
    m = re.search(r"(USD|U\$S|US\$|\$)\s*([\d.,]+)", txt or "")
    if not m:
        return None, None
    moneda = "UYU" if m.group(1) == "$" else "USD"
    num = m.group(2).rstrip(".,")
    if "," in num:                                    # 1.250,50 -> 1250.50
        num = num.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"\d{1,3}(\.\d{3})+", num):     # 2.400 -> 2400
        num = num.replace(".", "")
    try:
        return float(num), moneda
    except ValueError:
        return None, None


def titulo_lindo(nombre):
    """'LIBÉLULA DORADA' -> 'Libélula Dorada' (Google penaliza títulos en mayúsculas)."""
    minus = {"de", "del", "y", "con", "en", "la", "el", "a"}
    palabras = nombre.split()
    return " ".join(p.lower() if i and p.lower() in minus else p[:1].upper() + p[1:].lower()
                    for i, p in enumerate(palabras))


# ---------- 1. Recorrer categorías ----------

def categorias():
    r = get(BASE + "/")
    cats = set()
    if r:
        for a in BeautifulSoup(r.text, "html.parser").find_all("a", href=True):
            m = RE_CATEGORIA.match(urljoin(BASE, a["href"]))
            if m and m.group(1) not in NO_CATEGORIAS:
                cats.add(m.group(1))
    return sorted(cats) or ["teros", "colibries", "campo", "aves", "marinos"]


def productos_de_listado(html_pagina, categoria):
    """Devuelve {url_producto: {...}} con nombre, precio y pre-orden según la tarjeta del listado."""
    sopa = BeautifulSoup(html_pagina, "html.parser")
    encontrados = {}
    for a in sopa.find_all("a", href=True):
        url = urljoin(BASE, a["href"]).split("?")[0].split("#")[0]
        m = RE_PRODUCTO.match(url)
        if not m:
            continue
        t = texto(a)
        precio, moneda = leer_precio(t)
        nombre = ""
        img = a.find("img")
        if img and img.get("alt"):
            nombre = img["alt"].strip()
        if not nombre:   # la tarjeta repite el nombre: "ABEJA DORADA ABEJA DORADA ABEJA DORADA USD 120"
            antes = re.split(r"(USD|U\$S|US\$|\$)\s*[\d.]", t)[0].replace("Este producto es pre-orden", " ")
            partes = antes.split()
            for n in range(1, len(partes) + 1):
                if " ".join(partes[:n]) == " ".join(partes[n:2 * n]):
                    nombre = " ".join(partes[:n])
                    break
            nombre = nombre or " ".join(partes)
        dato = encontrados.setdefault(url, {"url": url, "id": m.group(2), "categoria": categoria})
        dato["nombre"] = dato.get("nombre") or nombre
        if precio and not dato.get("precio"):
            dato["precio"], dato["moneda"] = precio, moneda
        if "pre-orden" in t.lower() or "preorden" in t.lower():
            dato["preorden"] = True
        if img and not dato.get("imagen_listado"):
            src = img.get("data-src") or img.get("src") or ""
            if "/cdn/" in src:
                dato["imagen_listado"] = urljoin(BASE, src)
    return encontrados


def recorrer_catalogo():
    productos = {}
    for cat in categorias():
        for pag in range(1, MAX_PAGINAS_CATEGORIA + 1):
            r = get(f"{BASE}/{cat}/{pag}")
            if not r:
                break
            nuevos = productos_de_listado(r.text, cat)
            agregados = 0
            for url, d in nuevos.items():
                if url not in productos:
                    productos[url] = d
                    agregados += 1
                else:   # completar datos que falten, sin cambiar la categoría original
                    for k, v in d.items():
                        if k != "categoria":
                            productos[url].setdefault(k, v)
            print(f"  /{cat}/{pag}: {len(nuevos)} productos ({agregados} nuevos)", flush=True)
            time.sleep(DELAY)
            if not nuevos or (pag > 1 and agregados == 0):
                break
    return list(productos.values())


# ---------- 2. Detalle de cada producto ----------

TEXTO_GLOBAL = "Esculturas sustentables de animales promoviendo"


def json_ld_producto(sopa):
    for s in sopa.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(s.string or "")
        except (ValueError, TypeError):
            continue
        for d in (data if isinstance(data, list) else data.get("@graph", [data])):
            if isinstance(d, dict) and str(d.get("@type", "")).lower() == "product":
                return d
    return {}


def leer_detalle(producto):
    r = get(producto["url"])
    time.sleep(DELAY)
    if not r:
        return producto
    if GUARDAR_DEBUG and not DEBUG["guardado"]:
        import os
        os.makedirs("debug", exist_ok=True)
        with open("debug/producto.html", "w", encoding="utf-8") as f:
            f.write(r.text)
        DEBUG["guardado"] = True
    sopa = BeautifulSoup(r.text, "html.parser")
    ld = json_ld_producto(sopa)

    # Imagen: JSON-LD > og:image (si no es el logo) > imágenes del CDN de la tienda
    imagenes = []
    ld_img = ld.get("image")
    for i in (ld_img if isinstance(ld_img, list) else [ld_img] if ld_img else []):
        imagenes.append(i if isinstance(i, str) else i.get("url", ""))
    og = sopa.find("meta", property="og:image")
    if og and og.get("content") and "metaLogo" not in og["content"]:
        imagenes.append(og["content"])
    for img in sopa.find_all("img"):
        src = img.get("data-src") or img.get("data-zoom-image") or img.get("src") or ""
        if "/cdn/files/" in src and "logo" not in src.lower():
            imagenes.append(urljoin(BASE, src))
    vistos = set()
    producto["imagenes"] = [i for i in imagenes if i and not (i in vistos or vistos.add(i))]

    # Descripción: JSON-LD > bloque con "descrip" en la clase/id > meta description propia del producto
    desc = (ld.get("description") or "").strip()
    if not desc:
        bloque = sopa.find(attrs={"class": re.compile("descrip", re.I)}) or \
                 sopa.find(attrs={"id": re.compile("descrip", re.I)})
        desc = texto(bloque)
    if not desc:
        md = sopa.find("meta", attrs={"name": "description"})
        if md and md.get("content") and TEXTO_GLOBAL not in md["content"]:
            desc = md["content"].strip()
    producto["descripcion"] = html.unescape(desc)

    # Características "Clave: valor" (Tamaño, Peso, Material, Medidas...) en el texto del producto
    caracteristicas = {}
    fuente = sopa.find("main") or sopa.body or sopa
    for linea in fuente.get_text("\n").split("\n"):
        linea = re.sub(r"\s+", " ", linea).strip(" :-•·*")
        m = re.match(r"^(Tamaño|Tamaños|Medidas?|Dimensiones|Peso|Material(?:es)?|Terminación|Acabado|"
                     r"Color(?:es)?|Uso|Origen|Procedencia|Técnica)\s*:\s*(.{1,150})$", linea, re.I)
        if m:
            caracteristicas.setdefault(m.group(1).capitalize(), m.group(2).strip())
    producto["caracteristicas"] = caracteristicas

    # Precio y stock desde JSON-LD si existen (más confiables que el listado)
    oferta = ld.get("offers") or {}
    oferta = oferta[0] if isinstance(oferta, list) and oferta else oferta
    if isinstance(oferta, dict):
        try:
            if oferta.get("price"):
                producto["precio"] = float(str(oferta["price"]).replace(",", "."))
                producto["moneda"] = oferta.get("priceCurrency") or producto.get("moneda")
        except ValueError:
            pass
        disp = str(oferta.get("availability", "")).lower()
        if "outofstock" in disp or "soldout" in disp:
            producto["agotado"] = True
        elif "preorder" in disp:
            producto["preorden"] = True
    if not producto.get("precio"):
        p, mon = leer_precio(texto(sopa.find(attrs={"class": re.compile("price|precio", re.I)})))
        if p:
            producto["precio"], producto["moneda"] = p, mon
    return producto


DEBUG = {"guardado": False}


# ---------- 3. Feed en el formato de El Ganso ----------

CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def generar_xml(productos):
    items = []
    for p in productos:
        if not p.get("precio") or not p.get("nombre"):
            continue
        imagen = (p.get("imagenes") or [None])[0] or p.get("imagen_listado")
        if not imagen:
            continue
        nombre = titulo_lindo(p["nombre"])
        categoria = titulo_lindo(p["categoria"].replace("-", " ")).replace("Colibries", "Colibríes")

        if p.get("agotado"):
            disp = "out_of_stock"
        elif p.get("preorden"):
            disp = "preorder"
        else:
            disp = "in_stock"

        desc = f"{TIPO} {MARCA}: {nombre}. Colección {categoria}."
        if disp == "preorder":
            desc += " Producto en pre-orden."
        if p.get("descripcion"):
            desc += "\n\n" + p["descripcion"]
        if p.get("caracteristicas"):
            desc += "\n\nCaracterísticas del producto:\n" + "\n".join(
                f"- {k}: {v}" for k, v in p["caracteristicas"].items())

        moneda = p.get("moneda") or MONEDA_DEFECTO
        campos = [
            ("g:id", p["id"]),
            ("g:title", nombre[:150]),
            ("g:description", desc[:5000]),
            ("g:link", p["url"]),
            ("g:image_link", imagen),
            ("g:availability", disp),
            ("g:price", f"{p['precio']:.2f} {moneda}"),
            ("g:brand", MARCA),
            ("g:product_type", categoria),
            ("g:condition", "new"),
            ("g:identifier_exists", "no"),
        ]
        lineas = [f"      <{k}>{escape(CTRL.sub('', str(v)))}</{k}>" for k, v in campos if v]
        items.append("    <item>\n" + "\n".join(lineas) + "\n    </item>")

    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss xmlns:g="http://base.google.com/ns/1.0" version="2.0">\n'
        "  <channel>\n"
        f"    <title>{escape(TITULO_FEED)}</title>\n"
        f"    <link>{BASE}</link>\n"
        "    <description>Catalogo completo de productos</description>\n"
        + "\n".join(items) +
        "\n  </channel>\n</rss>\n"
    )
    with open(ARCHIVO_XML, "w", encoding="utf-8") as f:
        f.write(xml)
    print(f"{ARCHIVO_XML}: {len(items)} productos")


# ---------- 4. Main ----------

def main():
    try:
        with open("feed.json", encoding="utf-8") as f:
            previos = json.load(f).get("total", 0)
    except (OSError, ValueError):
        previos = 0

    productos = recorrer_catalogo()
    print(f"Listados: {len(productos)} productos únicos", flush=True)
    if not productos or (previos and len(productos) < MIN_RATIO * previos):
        raise SystemExit(f"ERROR: solo {len(productos)} productos (antes {previos}). No se actualiza el feed.")

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        productos = list(pool.map(leer_detalle, productos))

    with open("feed.json", "w", encoding="utf-8") as f:
        json.dump({"generado": datetime.now(timezone.utc).isoformat(), "total": len(productos),
                   "productos": productos}, f, ensure_ascii=False, indent=2)
    generar_xml(productos)
    print(f"Estados HTTP: {ESTADOS} | listo en {time.time() - INICIO:.0f}s", flush=True)


if __name__ == "__main__":
    main()
