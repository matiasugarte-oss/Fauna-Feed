"""
Feed de productos de Fauna Deco (https://fauna.com.uy, plataforma Luna) para Google Merchant Center.

No necesita el feed nativo: cada página de producto trae escondidos los datos internos de la
plataforma (SKU, stock, pre-orden, precio, precio anterior, descripción y fotos). El scraper:
  1. Recorre las categorías y subcategorías del menú (Teros > Chicos, Aves > Búho...) y junta los
     links de producto. También sigue los "También te puede interesar" para no perder ninguno.
  2. Abre cada producto y lee esos datos internos.
  3. Escribe feed.xml (mismos campos que el feed nativo, corregidos) y feed.json (datos crudos).

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
TITULO_FEED = "Feed de productos Fauna Deco"
ARCHIVO_XML = "feed.xml"
PREORDEN_COMO = "preorder"     # cómo informar a Google los productos sin stock que aceptan pre-orden
                               # ("preorder" o "in_stock", que es lo que hace el feed nativo)
TITULOS_EN_MAYUSCULAS = False  # True = deja "BÚHO - 60 x 25 cm" como viene

DELAY = 0.5
WORKERS = 3
TIMEOUT = 25
MAX_PAGINAS = 30
MIN_RATIO = 0.7                # si hoy hay menos del 70% de los productos de ayer, no se pisa el feed
# -----------------------------------------------------------------------------

session = requests.Session()
session.headers.update({"User-Agent": "Mozilla/5.0 (compatible; ProductFeedBot/1.0)",
                        "Accept-Language": "es-UY,es;q=0.9"})
ESTADOS = {}
INICIO = time.time()

RE_PRODUCTO = re.compile(r"^https?://(?:www\.)?fauna\.com\.uy/p/([^/?#]+)/(\d+)/(\d+)/?$")
RE_CATEGORIA = re.compile(r"^https?://(?:www\.)?fauna\.com\.uy/([a-z0-9-]+)(?:/([a-z0-9-]+))?/(\d+)/?$")
NO_CATEGORIAS = {"p", "c", "cart", "carrito", "about", "contactanos"}


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


def links_producto(html_pagina):
    urls = set()
    for href in re.findall(r'href=["\']([^"\']+)["\']', html_pagina):
        url = urljoin(BASE, html.unescape(href)).split("?")[0].split("#")[0]
        if RE_PRODUCTO.match(url):
            urls.add(url.rstrip("/"))
    return urls


# ---------- 1. Categorías -> links de producto ----------

def nombre_lindo(slug):
    t = slug.replace("-", " ").strip()
    fijos = {"colibries": "Colibríes", "tematicos": "Temáticos", "libelula": "Libélula", "yacare": "Yacaré",
             "aguila": "Águila", "buho": "Búho", "chaja": "Chajá", "faisan": "Faisán", "nandu": "Ñandú",
             "pajaros": "Pájaros", "tucan": "Tucán", "delfin": "Delfín", "tiburon": "Tiburón",
             "espatula": "Espátula"}
    return fijos.get(slug, t[:1].upper() + t[1:])


def recorrer_categorias():
    """Devuelve {url_producto: 'Aves > Búho'} recorriendo categorías y subcategorías del menú."""
    r = get(BASE + "/")
    cats = set()
    if r:
        for href in re.findall(r'href=["\']([^"\']+)["\']', r.text):
            m = RE_CATEGORIA.match(urljoin(BASE, html.unescape(href)))
            if m and m.group(1) not in NO_CATEGORIAS:
                cats.add((m.group(1), m.group(2)))
    if not cats:
        cats = {(c, None) for c in ("teros", "colibries", "campo", "aves", "marinos")}

    categoria_de = {}
    # primero las generales y después las subcategorías, para que gane la más específica
    for cat, sub in sorted(cats, key=lambda x: (x[1] is not None, x)):
        ruta = f"{cat}/{sub}" if sub else cat
        nombre = nombre_lindo(cat) + (f" > {nombre_lindo(sub)}" if sub else "")
        vistos_cat = set()
        for pag in range(1, MAX_PAGINAS + 1):
            r = get(f"{BASE}/{ruta}/{pag}")
            if not r:
                break
            urls = links_producto(r.text) - vistos_cat
            time.sleep(DELAY)
            if not urls:
                break
            vistos_cat |= urls
            for u in urls:
                if sub or u not in categoria_de:
                    categoria_de[u] = nombre
        print(f"  /{ruta}: {len(vistos_cat)} productos", flush=True)
    return categoria_de


# ---------- 2. Datos internos de cada producto ----------

def buscar_json(texto, condicion):
    """Busca en el HTML estructuras JSON ([...] o {...}) que cumplan la condición."""
    dec = json.JSONDecoder()
    for fuente in (texto, html.unescape(texto)):
        for m in re.finditer(r'[\[{]\s*\{?\s*"id"\s*:', fuente):
            try:
                obj, _ = dec.raw_decode(fuente, m.start())
            except ValueError:
                continue
            if condicion(obj):
                return obj
    return None


def html_a_texto(desc_html):
    t = re.sub(r"<\s*(br|/p|/li|/div|/h\d)[^>]*>", "\n", desc_html or "", flags=re.I)
    t = html.unescape(re.sub(r"<[^>]+>", " ", t)).replace("\xa0", " ")
    parrafos = [re.sub(r"\s+", " ", p).strip() for p in t.split("\n")]
    return "\n\n".join(p for p in parrafos if p)


def tamanos_de_pagina(sopa):
    """Texto que la página muestra bajo 'Tamaño' (ej. '60 x 25 cm - Peso 8kg')."""
    lineas = [l.strip() for l in sopa.get_text("\n").split("\n") if l.strip()]
    for i, l in enumerate(lineas):
        if l.lower() in ("tamaño", "tamaños", "medida", "medidas"):
            salida = []
            for siguiente in lineas[i + 1:i + 12]:
                if siguiente.lower() in ("guía de talles", "guia de talles", "cantidad", "-", "+",
                                         "agregar al carrito"):
                    break
                salida.append(siguiente)
            return salida
    return []


def leer_producto(url):
    r = get(url)
    time.sleep(DELAY)
    if not r:
        return url, None, set()
    variantes = buscar_json(r.text, lambda o: isinstance(o, list) and o and isinstance(o[0], dict)
                            and "sku" in o[0] and "price" in o[0])
    producto = buscar_json(r.text, lambda o: isinstance(o, dict) and set(o) == {"id", "name"})
    sopa = BeautifulSoup(r.text, "html.parser")
    h1 = sopa.find("h1")
    datos = {
        "url": url,
        "nombre": (producto or {}).get("name") or (h1.get_text(strip=True) if h1 else ""),
        "variantes": variantes or [],
        "tamanos": tamanos_de_pagina(sopa),
    }
    return url, datos, links_producto(r.text)


def recorrer_productos(semillas):
    """Lee los productos y sigue los 'También te puede interesar' para encontrar productos fuera del menú."""
    datos, pendientes, vistos = {}, set(semillas), set()
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        while pendientes:
            lote = list(pendientes)
            pendientes.clear()
            vistos.update(lote)
            for url, d, relacionados in pool.map(leer_producto, lote):
                if d:
                    datos[url] = d
                pendientes |= relacionados - vistos
            print(f"  {len(datos)} productos leídos ({time.time() - INICIO:.0f}s)", flush=True)
    return datos


# ---------- 3. Feed ----------

MINUSCULAS = {"de", "del", "y", "con", "en", "la", "el", "a",
              "cm", "mm", "m", "mts", "kg", "kgs", "g", "gr"}   # palabras y unidades en minúscula


def titulo_lindo(t):
    if TITULOS_EN_MAYUSCULAS:
        return t
    salida = []
    for i, p in enumerate(t.split()):
        if any(c.isdigit() for c in p) or p in ("|", "-", "·"):
            salida.append(p)
        elif p in ("x", "X"):
            salida.append("x")
        elif i and p.lower() in MINUSCULAS:
            salida.append(p.lower())
        else:
            salida.append(p[:1].upper() + p[1:].lower())
    return " ".join(salida)


def num(v):
    try:
        return float(str(v).replace(",", "."))
    except (TypeError, ValueError):
        return 0.0


def moneda(simbolo):
    s = (simbolo or "").upper()
    return "USD" if "U" in s else "UYU"


CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def generar_xml(productos, categoria_de):
    items, vistos = [], set()
    for url, p in productos.items():
        nombre = (p["nombre"] or "").strip()
        tamanos = p["tamanos"]
        for i, v in enumerate(p["variantes"]):
            pid = (v.get("sku") or "").strip() or str(v.get("id"))
            if not pid or pid in vistos or num(v.get("price")) <= 0:
                continue
            fotos = [f.get("url") for f in v.get("files") or [] if f.get("url")]
            if not fotos:
                continue
            vistos.add(pid)

            desc = html_a_texto(v.get("description"))
            titulo = (v.get("title") or nombre).strip()
            if titulo.upper() != nombre.upper() and nombre:
                titulo = f"{nombre} {titulo}"
            if titulo.upper().startswith(("PIEZA ÚNICA", "PIEZA UNICA")):
                m = re.match(r"^([A-ZÁÉÍÓÚÑ][\wÁÉÍÓÚÑáéíóúñ ]{2,30}?)\s*[–-]\s*Escultura", desc)
                if m:
                    titulo = f"{m.group(1)} {titulo}"
            tam = tamanos[i] if len(tamanos) == len(p["variantes"]) else (tamanos[0] if len(tamanos) == 1 else "")
            if tam and tam.lower() not in titulo.lower():
                titulo = f"{titulo} - {tam}"
            titulo = titulo_lindo(titulo)

            stock = num(v.get("stock"))
            if stock > 0 or not v.get("limited_by_stock"):
                disp = "in_stock"
            elif v.get("preorder_stock"):
                disp = PREORDEN_COMO
            else:
                disp = "out_of_stock"

            mon = moneda(v.get("currency_symbol"))
            precio, anterior = num(v.get("price")), num(v.get("old_price"))
            if anterior > precio:
                precios = [("g:price", f"{anterior:g} {mon}"), ("g:sale_price", f"{precio:g} {mon}")]
            else:
                precios = [("g:price", f"{precio:g} {mon}")]

            if disp == "preorder":
                desc = "Producto en pre-orden.\n\n" + desc
            campos = [
                ("g:id", pid),
                ("g:title", titulo[:150]),
                ("g:description", (desc or titulo)[:5000]),
                ("g:link", url),
                ("g:image_link", fotos[0]),
                *[("g:additional_image_link", f) for f in fotos[1:11]],
                ("g:availability", disp),
                *precios,
                ("g:product_type", categoria_de.get(url, "")),
                ("g:brand", MARCA),
                ("g:condition", "new"),
                ("g:identifier_exists", "no"),
            ]
            lineas = [f"      <{k}>{escape(CTRL.sub('', str(val)))}</{k}>" for k, val in campos if val]
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
    return len(items)


def main():
    try:
        with open("feed.json", encoding="utf-8") as f:
            previos = json.load(f).get("total", 0)
    except (OSError, ValueError):
        previos = 0

    categoria_de = recorrer_categorias()
    print(f"Categorías: {len(categoria_de)} productos", flush=True)
    productos = recorrer_productos(categoria_de.keys())
    con_datos = {u: p for u, p in productos.items() if p["variantes"]}
    print(f"Productos con datos internos: {len(con_datos)} de {len(productos)}", flush=True)

    if not con_datos or (previos and len(con_datos) < MIN_RATIO * previos):
        raise SystemExit(f"ERROR: solo {len(con_datos)} productos (antes {previos}). No se actualiza el feed.")

    with open("feed.json", "w", encoding="utf-8") as f:
        json.dump({"generado": datetime.now(timezone.utc).isoformat(), "total": len(con_datos),
                   "categorias": categoria_de, "productos": con_datos}, f, ensure_ascii=False, indent=2)
    generar_xml(con_datos, categoria_de)
    print(f"Estados HTTP: {ESTADOS} | listo en {time.time() - INICIO:.0f}s", flush=True)


if __name__ == "__main__":
    main()
