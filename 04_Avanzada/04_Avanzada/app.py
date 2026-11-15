"""
Zonas censales por distrito (marco INEI) — 05_Intervention/04_Avanzada

Distritos del proyecto: Ate, El Porvenir y San Juan de Lurigancho.
Capas: todas salen del marco censal del INEI (LIMITES_CENSALES.gdb, dentro de
"marco censal inei.zip"): distritos, zonas censales, manzanas y ejes viales.
La app lee el .zip directamente; no hay que extraerlo.

Ubicar un punto: dirección (servicio de búsqueda de GEOPERÚ), coordenadas
escritas a mano (-12.0056, -77.0868) o el GPS del navegador. En los tres casos
se marca el punto y se indica en qué zona censal cae.
"""
from __future__ import annotations

import html
import inspect
import os
import re
import string
import unicodedata
import zipfile
from pathlib import Path

import folium
import geopandas as gpd
import pandas as pd
import pyogrio
import requests
import streamlit as st
from shapely.geometry import Point
from shapely.ops import polylabel
from streamlit_folium import st_folium
from streamlit_js_eval import streamlit_js_eval

st.set_page_config(page_title="Empadronamiento de aprendizajes · Marcha blanca", page_icon="🗺️", layout="wide",
                   initial_sidebar_state="collapsed")

# ----------------------------------------------------------------------------
# Configuración
# ----------------------------------------------------------------------------
BASE = Path(__file__).parent
MARCO_DEFECTO = BASE.parent / "02_Despliegue_manzanas" / "01_Data" / "marco censal inei.zip"
PROYECTO = {"150103": "Ate", "130102": "El Porvenir", "150132": "San Juan de Lurigancho"}


def _cfg(nombre: str, defecto: str = "") -> str:
    """Variable de entorno o .streamlit/secrets.toml."""
    v = os.getenv(nombre)
    if v:
        return v
    try:
        return str(st.secrets.get(nombre, defecto))
    except Exception:
        return defecto


_GPKG = Path(__file__).parent / "data" / "marco_proyecto.gpkg"   # recorte del marco a los 3 distritos
MARCO_PATH = _cfg("MARCO_PATH", str(_GPKG if _GPKG.exists() else MARCO_DEFECTO))
# Endpoint que usa el visor de GEOPERÚ para buscar direcciones (no documentado).
SEARCH_URL = _cfg("GEOPERU_SEARCH_URL", "https://www.geoidep.gob.pe/geoapify")
# Opcional: API key propia de Geoapify; si existe, se usa en vez del endpoint anterior.
GEOAPIFY_KEY = _cfg("GEOAPIFY_API_KEY")
UA = {"User-Agent": "zonas-censales-streamlit/0.3 (uso interno)"}

LAT_PERU, LON_PERU = (-18.5, 0.5), (-81.5, -68.5)
COLOR_ZONA, COLOR_SEL = "#84d0d4", "#f26529"   # paleta IPA: azul claro / rojo-naranja
COLUMNAS_GRID = 8

# Pide la posición al navegador y siempre responde (con coordenadas o con un error).
JS_GPS = """new Promise((resolve) => {
  if (!navigator.geolocation) { resolve({error: 'Este navegador no ofrece geolocalización.'}); return; }
  navigator.geolocation.getCurrentPosition(
    (p) => resolve({lat: p.coords.latitude, lon: p.coords.longitude, acc: p.coords.accuracy}),
    (e) => resolve({error: e.message || 'Permiso denegado', code: e.code}),
    {enableHighAccuracy: true, timeout: 20000, maximumAge: 0});
})"""


def _ancho(fn) -> dict:
    """width="stretch" en Streamlit recientes; use_container_width en versiones previas."""
    return ({"width": "stretch"} if "width" in inspect.signature(fn).parameters
            else {"use_container_width": True})


ANCHO_BTN, ANCHO_DF = _ancho(st.button), _ancho(st.dataframe)


# ----------------------------------------------------------------------------
# Utilidades
# ----------------------------------------------------------------------------
def norm(s: object) -> str:
    s = unicodedata.normalize("NFD", str(s)).encode("ascii", "ignore").decode().lower()
    return re.sub(r"\s+", " ", s).strip()


def fmt(x: object) -> str:
    try:
        return f"{int(round(float(x))):,}".replace(",", " ")
    except (TypeError, ValueError):
        return "—"


def codigo_zona(codzona: str, sufzona: str) -> str:
    """009 + sufijo 01 -> 009A (el marco guarda el sufijo como número: 01=A, 02=B...)."""
    try:
        n = int(sufzona)
    except (TypeError, ValueError):
        return str(codzona)
    if n == 0:
        return str(codzona)
    return f"{codzona}{string.ascii_uppercase[n - 1]}" if n <= 26 else f"{codzona}-{n:02d}"


def parsear_coordenadas(q: str) -> tuple[str, tuple[float, float] | None]:
    """
    Interpreta «lat, lon» o «lon, lat» (se detecta el orden por los rangos del Perú).
    Devuelve ("no", None) si no parece un par de coordenadas, ("fuera", None) si lo parece
    pero cae fuera del Perú, y ("ok", (lat, lon)) si es válido.
    Acepta coma o punto decimal: -12.0056, -77.0868 / -12,0056; -77,0868 / -12.0056 -77.0868.
    """
    s = q.replace("−", "-").replace("°", " ").strip()
    nums = re.findall(r"-?\d+(?:[.,]\d+)?", s)
    resto = re.sub(r"-?\d+(?:[.,]\d+)?|[\s,;]", "", s)
    if len(nums) != 2 or resto:
        return "no", None
    # Evita confundir dos enteros sueltos (p. ej. «28 30») con coordenadas.
    if not (any(re.search(r"[.,]\d", n) for n in nums) or re.search(r"[,;]", s)):
        return "no", None
    a, b = (float(n.replace(",", ".")) for n in nums)
    if LAT_PERU[0] <= a <= LAT_PERU[1] and LON_PERU[0] <= b <= LON_PERU[1]:
        return "ok", (a, b)
    if LON_PERU[0] <= a <= LON_PERU[1] and LAT_PERU[0] <= b <= LAT_PERU[1]:
        return "ok", (b, a)
    return "fuera", None


# ----------------------------------------------------------------------------
# Capas del marco censal INEI
# ----------------------------------------------------------------------------
@st.cache_data(show_spinner=False)
def uri_gdb(ruta: str) -> str:
    """Ruta GDAL de la geodatabase: admite el .zip del marco o la carpeta .gdb."""
    p = Path(ruta.strip().strip('"'))
    if not p.exists():
        raise FileNotFoundError(f"No existe: {p}")
    if p.suffix.lower() == ".zip":
        with zipfile.ZipFile(p) as z:
            gdb = next((n.split(".gdb/")[0] + ".gdb" for n in z.namelist() if ".gdb/" in n), None)
        if gdb is None:
            raise FileNotFoundError("El .zip no contiene una geodatabase (.gdb).")
        return f"/vsizip/{p.as_posix()}/{gdb}"
    return p.as_posix()


@st.cache_resource(show_spinner="Leyendo el marco censal…")
def cargar_marco(uri: str) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame]:
    """Distritos y zonas de los distritos del proyecto. Solo lectura: no modificar los resultados."""
    lista = ",".join(f"'{u}'" for u in PROYECTO)
    dist = pyogrio.read_dataframe(uri, layer="TB_DISTRITOS", columns=["UBIGEO", "NOMBDEP", "NOMBPROV", "NOMBDIST"],
                                  where=f"UBIGEO IN ({lista})")
    zon = pyogrio.read_dataframe(
        uri, layer="TB_ZONAS",
        columns=["ID_ZONA", "UBIGEO", "NOM_DEP", "NOM_PROV", "NOM_DIS", "CODZONA", "SUFZONA",
                 "FECHA_ACT", "FUENTE", "VERSION"])
    zon = zon[zon["UBIGEO"].isin(PROYECTO)].copy()
    zon["COD"] = [codigo_zona(c, s) for c, s in zip(zon["CODZONA"], zon["SUFZONA"])]
    zon["etiqueta"] = "Zona " + zon["CODZONA"] + "-" + zon["SUFZONA"].astype(str)
    zon["_num"] = pd.to_numeric(zon["CODZONA"], errors="coerce").fillna(10**6).astype(int)
    zon["_suf"] = pd.to_numeric(zon["SUFZONA"], errors="coerce").fillna(0).astype(int)
    zon["FECHA_ACT"] = zon["FECHA_ACT"].astype(str).replace("NaT", "")
    zon = zon.sort_values(["UBIGEO", "_num", "_suf"]).reset_index(drop=True)
    return dist, zon


@st.cache_data(show_spinner=False)
def manzanas_por_zona(uri: str, ubigeo: str) -> pd.Series:
    """Número de manzanas del marco por zona, para un distrito."""
    df = pyogrio.read_dataframe(uri, layer="TB_MANZANAS", columns=["ID_ZONA"],
                                where=f"ID_ZONA LIKE '{ubigeo}%'", read_geometry=False)
    return df["ID_ZONA"].value_counts()


@st.cache_data(show_spinner=False)
def manzanas_de_zona(uri: str, id_zona: str, bounds: tuple) -> gpd.GeoDataFrame:
    mz = pyogrio.read_dataframe(uri, layer="TB_MANZANAS", bbox=bounds,
                                columns=["ID_MANZANA", "ID_ZONA", "MANZANA", "ESTR_MZN"])
    return mz[mz["ID_ZONA"] == id_zona][["ID_MANZANA", "MANZANA", "ESTR_MZN", "geometry"]].reset_index(drop=True)


@st.cache_data(show_spinner=False)
def vias_en_bbox(uri: str, bounds: tuple) -> gpd.GeoDataFrame:
    return pyogrio.read_dataframe(uri, layer="TB_EJE_VIAL", bbox=bounds,
                                  columns=["TIP_VIA", "NOM_VIA", "CUADRA"])


def _punto_central(g) -> tuple[float, float]:
    """Centro visual (punto interior más alejado del borde) de una geometría: (lat, lon)."""
    try:
        parte = max(g.geoms, key=lambda x: x.area) if hasattr(g, "geoms") else g
        c = polylabel(parte, tolerance=1e-5)
    except Exception:
        c = g.representative_point()
    return c.y, c.x


@st.cache_data(show_spinner=False)
def centros_zonas(ubigeo: str) -> dict[str, tuple[float, float]]:
    z = ZN[ZN["UBIGEO"] == ubigeo]
    return {i: _punto_central(g) for i, g in zip(z["ID_ZONA"], z.geometry)}


@st.cache_data(show_spinner=False)
def centros_manzanas(uri: str, id_zona: str, bounds: tuple) -> list[tuple[float, float, str]]:
    mz = manzanas_de_zona(uri, id_zona, bounds)
    return [(*_punto_central(g), str(t)) for g, t in zip(mz.geometry, mz["MANZANA"])]


def etiqueta_mapa(texto: str, clase: str, lat: float, lon: float) -> folium.Marker:
    """Texto fijo sobre el mapa, centrado en (lat, lon)."""
    icono = folium.DivIcon(html=f'<div class="lbl {clase}">{html.escape(texto)}</div>',
                           icon_size=(0, 0), icon_anchor=(0, 0), class_name="lbl-wrap")
    return folium.Marker([lat, lon], icon=icono, interactive=False, keyboard=False)


# ----------------------------------------------------------------------------
# Búsqueda de zonas y direcciones
# ----------------------------------------------------------------------------
def buscar_zonas(zonas: gpd.GeoDataFrame, q: str) -> gpd.GeoDataFrame:
    """Busca por código (9, 009, 009A) o por texto (etiqueta / ID de zona)."""
    q = q.strip()
    if not q:
        return zonas.iloc[0:0]
    mm = re.fullmatch(r"0*(\d{1,3})([A-Za-z]?)", q.replace(" ", ""))
    if mm:
        n, suf = int(mm.group(1)), mm.group(2).upper()
        m = zonas["_num"] == n
        if suf:
            m &= zonas["COD"].str.upper().str.endswith(suf)
        return zonas[m]
    qn = norm(q)
    m = zonas["etiqueta"].map(norm).str.contains(re.escape(qn), regex=True)
    m |= zonas["COD"].map(norm).str.contains(re.escape(qn), regex=True)
    m |= zonas["ID_ZONA"].str.contains(re.escape(q), regex=True)
    return zonas[m]


def es_codigo_zona(q: str) -> bool:
    return bool(re.fullmatch(r"0*\d{1,3}[A-Za-z]?", q.replace(" ", "")))


def parsear_respuesta(data: object) -> list[dict]:
    """Normaliza la respuesta (GeoJSON de Geoapify) a una lista de direcciones."""
    if not isinstance(data, dict):
        return []
    items = data.get("features") or data.get("predictions") or data.get("properties") or []
    if isinstance(items, dict):
        items = [items]
    salida, vistos = [], set()
    for f in items:
        if not isinstance(f, dict):
            continue
        p = f.get("properties") or f.get("address") or {}
        coords = (f.get("geometry") or {}).get("coordinates") or [p.get("lon"), p.get("lat")]
        try:
            lon, lat = float(coords[0]), float(coords[1])
        except (TypeError, ValueError, IndexError):
            continue
        if not (LON_PERU[0] <= lon <= LON_PERU[1] and LAT_PERU[0] <= lat <= LAT_PERU[1]):
            continue
        nombre = (p.get("formatted") or "").split(",")[0].strip() or str(p.get("name") or "")
        linea = str(p.get("address_line2") or p.get("address_line1") or "").strip()
        label = nombre if (not linea or linea == nombre) else f"{nombre} — {linea}"
        if not label:
            continue
        clave = (round(lat, 5), round(lon, 5), label)
        if clave in vistos:
            continue
        vistos.add(clave)
        salida.append({"label": label, "lat": lat, "lon": lon})
    return salida


@st.cache_data(ttl=600, show_spinner=False)
def _consultar(texto: str, proveedor: str) -> object:
    """Consulta remota. Las excepciones no se cachean."""
    if proveedor == "geoapify":
        r = requests.get(
            "https://api.geoapify.com/v1/geocode/autocomplete",
            params={"text": texto, "lang": "es", "filter": "countrycode:pe",
                    "limit": 10, "apiKey": GEOAPIFY_KEY},
            timeout=10,
        )
    else:
        r = requests.get(SEARCH_URL, params={"q": texto}, timeout=10, headers=UA)
    r.raise_for_status()
    return r.json()


def buscar_direcciones(texto: str) -> tuple[list[dict], str | None]:
    prov = "geoapify" if GEOAPIFY_KEY else "geoperu"
    try:
        return parsear_respuesta(_consultar(texto, prov)), None
    except Exception as e:  # red, HTTP, JSON
        msg = re.sub(r"apiKey=[^&\s'\"]+", "apiKey=***", f"{type(e).__name__}: {e}")
        return [], msg


# ----------------------------------------------------------------------------
# Datos y estado
# ----------------------------------------------------------------------------

ASSETS = Path(__file__).parent / "assets"
CSS = """
<style>
:root{--verde:#49ac57;--verde-osc:#155240;--gris:#f1f2f2;--gris-osc:#c9c9c8;--carbon:#414042}
body,.stApp,.stMarkdown,.stButton,.stTextInput,.stMultiSelect,.stSelectbox,.stCaption{font-family:Arial,Helvetica,sans-serif}
[data-testid="stIconMaterial"]{font-family:"Material Symbols Rounded"!important}
h1,h2,h3{font-family:Georgia,serif!important;color:var(--verde-osc)!important;font-weight:400!important}
[data-testid="stToolbar"],#MainMenu,footer{display:none}
.block-container{padding:0 1rem 2rem!important;max-width:1400px}
.ipa-banda{background:var(--verde-osc);margin:0 -1rem 1rem;padding:.8rem 1rem;display:flex;align-items:center;gap:1rem;border-bottom:4px solid var(--verde)}
.ipa-banda img{height:34px}
.ipa-banda .t{color:#fff;font-family:Georgia,serif;font-size:1.25rem;line-height:1.15}
.ipa-banda .s{color:#84d0d4;font-size:.75rem;letter-spacing:.06em;text-transform:uppercase;font-weight:700}
.stButton>button{min-height:2.75rem;border-radius:.6rem;border:1px solid var(--gris-osc);color:var(--carbon);font-weight:700}
.stButton>button:hover{border-color:var(--verde);color:var(--verde-osc)}
.stButton>button[kind="primary"]{background:var(--verde-osc);border-color:var(--verde-osc);color:#fff}
.stTextInput input{min-height:2.75rem;font-size:1rem}
[data-testid="stMetricValue"]{color:var(--verde-osc);font-family:Georgia,serif}
@media (max-width:900px){
 iframe[title*="folium"]{height:70vh!important}
}
@media (max-width:640px){
 .ipa-banda .t{font-size:1.05rem}
 h2,h3{font-size:1.25rem!important}
 .stButton>button{min-height:3rem}
}
</style>
"""
st.markdown(CSS, unsafe_allow_html=True)

import base64
_logo = base64.b64encode((ASSETS / "logoipa.png").read_bytes()).decode() if (ASSETS / "logoipa.png").exists() else ""
st.markdown(
    f'<div class="ipa-banda">{"<img src=data:image/png;base64," + _logo + " alt=IPA>" if _logo else ""}'
    '<div><div class="t">Empadronamiento de aprendizajes</div><div class="s">Marcha blanca</div></div></div>',
    unsafe_allow_html=True)
ss = st.session_state

try:
    URI = uri_gdb(MARCO_PATH)
    DIST, ZN = cargar_marco(URI)
except Exception as e:
    st.error(f"No pude leer el marco censal. {type(e).__name__}: {e}")
    st.info("Deja «marco censal inei.zip» en 02_Despliegue_manzanas\\01_Data, "
            "o define la variable de entorno MARCO_PATH con su ruta.")
    st.stop()

for k, v in {"zonas_ms": [], "punto": None, "punto_zona": None, "pendiente": None,
             "gps_pedir": False, "gps_n": 0, "gps_error": None}.items():
    ss.setdefault(k, v)


def limpiar() -> None:
    ss.zonas_ms, ss.punto, ss.punto_zona = [], None, None
    ss["q"] = ""


def elegir_zona(idzona: str) -> None:
    """Agrega la zona a la selección (se pueden elegir varias)."""
    ss.zonas_ms = list(dict.fromkeys([*ss.zonas_ms, idzona]))
    ss.punto = ss.punto_zona = None


def quitar_punto() -> None:
    ss.punto = ss.punto_zona = None


def aplicar_punto(label: str, lat: float, lon: float) -> None:
    """Marca un punto, busca su zona censal y, si cae en otro distrito del proyecto, cambia a él."""
    idx = ZN.sindex.query(Point(lon, lat), predicate="intersects")
    ss.punto = {"label": label, "lat": lat, "lon": lon}
    if len(idx):
        r = ZN.iloc[int(idx[0])]
        ss.punto_zona = {k: str(r[k]) for k in ("ID_ZONA", "COD", "etiqueta", "NOM_DIS", "UBIGEO")}
        ss["ubigeo_sel"] = str(r["UBIGEO"])
        ss.zonas_ms = list(dict.fromkeys([*ss.zonas_ms, str(r["ID_ZONA"])]))
    else:
        ss.punto_zona = None


def pedir_gps() -> None:
    ss.gps_n += 1
    ss.gps_pedir, ss.gps_error = True, None


def cancelar_gps() -> None:
    ss.gps_pedir = False


# Un punto pendiente (p. ej. del GPS) se aplica antes de crear el selector de distrito.
if ss.pendiente:
    aplicar_punto(*ss.pendiente)
    ss.pendiente = None

# ----------------------------------------------------------------------------
# Selector de distrito
# ----------------------------------------------------------------------------
c1, _ = st.columns([1, 2])
with c1:
    ubigeo = st.selectbox("Distrito", list(PROYECTO), index=None, placeholder="Selecciona un distrito",
                          format_func=PROYECTO.get, key="ubigeo_sel", on_change=limpiar)
if not ubigeo:
    st.info("Elige un distrito para ver el tablero de sus zonas censales.")
    st.stop()

dname = PROYECTO[ubigeo]
Z = ZN[ZN["UBIGEO"] == ubigeo].copy()
geom_dist = DIST.loc[DIST["UBIGEO"] == ubigeo, "geometry"].iloc[0]
prov, dep = Z["NOM_PROV"].iloc[0].title(), Z["NOM_DEP"].iloc[0].title()
Z["n_mz"] = Z["ID_ZONA"].map(manzanas_por_zona(URI, ubigeo)).fillna(0).astype(int)

# ----------------------------------------------------------------------------
# Buscador, coordenadas y GPS
# ----------------------------------------------------------------------------
st.subheader(f"{dname} · {prov}, {dep}")
st.multiselect("Zonas en el mapa (una o varias)", options=list(Z["ID_ZONA"]), key="zonas_ms",
               format_func=lambda i: Z.set_index("ID_ZONA").loc[i, "COD"],
               placeholder="Elige zonas (ej. 009A) o déjalo vacío para ver todo el distrito")
q = st.text_input("Buscar en GEOPERÚ", key="q",
                  placeholder="Zona (ej. 009A), dirección o coordenadas (ej. -12.0056, -77.0868)").strip()

b1, b2, b3 = st.columns([1, 1.4, 2])
b1.button("📍 Usar mi ubicación", on_click=pedir_gps, **ANCHO_BTN)
acotar = b2.toggle("Acotar direcciones al distrito", value=True)
b3.caption("Las coordenadas se pueden escribir como «lat, lon» o «lon, lat».")

if ss.gps_pedir:
    res = streamlit_js_eval(js_expressions=JS_GPS, key=f"gps_{ss.gps_n}")
    if res is None:
        st.info("Esperando el permiso de ubicación del navegador…")
        st.button("Cancelar", on_click=cancelar_gps)
    else:
        ss.gps_pedir = False
        if isinstance(res, dict) and "lat" in res:
            acc = res.get("acc")
            etq = "Mi ubicación (GPS" + (f", ±{fmt(acc)} m)" if isinstance(acc, (int, float)) else ")")
            ss.pendiente = (etq, float(res["lat"]), float(res["lon"]))
        else:
            err = (res or {}).get("error", "respuesta vacía") if isinstance(res, dict) else "respuesta vacía"
            ss.gps_error = f"No pude obtener tu ubicación: {err}"
        st.rerun()
if ss.gps_error:
    st.warning(ss.gps_error + " Revisa el permiso de ubicación del navegador; "
               "la ubicación solo funciona en localhost o en https.")

# ----------------------------------------------------------------------------
# Resultados de búsqueda
# ----------------------------------------------------------------------------
if q:
    estado_coord, coord = parsear_coordenadas(q)
    with st.container(border=True):
        if estado_coord == "ok":
            lat, lon = coord
            st.markdown("**Coordenadas**")
            st.button(f"📍 Ir a {lat:.5f}, {lon:.5f}", key="ir_coord",
                      on_click=aplicar_punto, args=(f"Coordenadas {lat:.5f}, {lon:.5f}", lat, lon))
        elif estado_coord == "fuera":
            st.warning("Esas coordenadas quedan fuera del Perú (latitud entre -18.5 y 0, "
                       "longitud entre -81.5 y -68.5).")
        else:
            locales = buscar_zonas(Z, q)
            if len(locales):
                st.markdown("**Zonas censales**")
                for _, r in locales.head(8).iterrows():
                    st.button(f"{r['COD']}", key=f"loc_{r['ID_ZONA']}",
                              on_click=elegir_zona, args=(r["ID_ZONA"],))
                if len(locales) > 8:
                    st.caption(f"+{len(locales) - 8} más. Afina la búsqueda para verlas.")
            if len(q) >= 3 and not (es_codigo_zona(q) and len(locales)):
                consulta = ", ".join([q, dname, prov, dep, "Perú"]) if acotar else q
                with st.spinner("Buscando direcciones…"):
                    dirs, err = buscar_direcciones(consulta)
                if err:
                    st.warning(f"No pude consultar el buscador de direcciones. {err}")
                else:
                    ocultas = 0
                    if acotar:
                        area = geom_dist.buffer(0.01)  # distrito + ~1 km de margen
                        dentro = [d for d in dirs if area.covers(Point(d["lon"], d["lat"]))]
                        ocultas, dirs = len(dirs) - len(dentro), dentro
                    if dirs:
                        st.markdown("**Direcciones**")
                        for i, d in enumerate(dirs[:8]):
                            st.button(f"📍 {d['label']}", key=f"dir_{i}_{d['lat']:.5f}_{d['lon']:.5f}",
                                      on_click=aplicar_punto, args=(d["label"], d["lat"], d["lon"]))
                    if ocultas:
                        st.caption(f"{ocultas} resultado(s) fuera del distrito. "
                                   "Desactiva «Acotar» para verlos.")
            if not len(locales) and len(q) < 3:
                st.caption("Escribe al menos 3 caracteres para buscar direcciones.")

# Punto elegido
if ss.punto:
    d, zp = ss.punto, ss.punto_zona
    fila = st.container(border=True)
    if zp:
        fila.success(f"📍 {d['label']} → cae en la zona **{zp['COD']}** de {PROYECTO.get(zp['UBIGEO'], zp['NOM_DIS'].title())}")
    else:
        fila.warning(f"📍 {d['label']} no cae en ninguna zona censal de los distritos del proyecto.")
    fila.button("Quitar punto", on_click=quitar_punto)

def construir_mapa(zonas: gpd.GeoDataFrame, idsels: tuple, punto: dict | None) -> folium.Map:
    x0, y0, x1, y1 = geom_dist.bounds
    m = folium.Map(location=[(y0 + y1) / 2, (x0 + x1) / 2], tiles="OpenStreetMap",
                   control_scale=True, zoom_start=13, zoom_snap=0.25, zoom_delta=0.5)
    limites = [[y0, x0], [y1, x1]]

    folium.GeoJson(
        gpd.GeoSeries([geom_dist], crs=4326).to_json(), name="Límite distrital",
        style_function=lambda f: {"fillOpacity": 0, "color": "#155240", "weight": 2, "dashArray": "6 4"},
    ).add_to(m)

    cols = ["ID_ZONA", "COD", "etiqueta", "n_mz", "geometry"]
    folium.GeoJson(
        zonas[cols], name="Zonas censales",
        style_function=lambda f: {"fillColor": COLOR_ZONA, "color": "#ffffff", "weight": 1, "fillOpacity": 0.5},
        tooltip=folium.GeoJsonTooltip(fields=["COD", "n_mz"], aliases=["Zona", "Manzanas"]),
    ).add_to(m)

    # Código de cada zona (009A…) en su centro; se oculta si el zoom es muy bajo.
    grupo_zonas = folium.FeatureGroup(name="Etiquetas de zonas")
    centros = centros_zonas(ubigeo)
    for idz, cod in zip(zonas["ID_ZONA"], zonas["COD"]):
        if idz in centros:
            etiqueta_mapa(cod, "lbl-zona sel" if idz in idsels else "lbl-zona", *centros[idz]).add_to(grupo_zonas)
    grupo_zonas.add_to(m)

    if idsels:
        sels = zonas[zonas["ID_ZONA"].isin(idsels)]
        a0, b0, c0, d0 = sels.total_bounds
        limites = [[b0, a0], [d0, c0]]
        n_mz_tot = 0
        for idz, zg in zip(sels["ID_ZONA"], sels.geometry):
            a, b, c, d = zg.bounds
            bounds = (a, b, c, d)
            mz = manzanas_de_zona(URI, idz, bounds)
            if len(mz):
                n_mz_tot += len(mz)
                folium.GeoJson(
                    mz, name=f"Manzanas {zonas.loc[zonas.ID_ZONA == idz, 'COD'].iloc[0]}",
                    style_function=lambda f: {"fillColor": "#2b4085", "color": "#2b4085",
                                              "weight": 1, "fillOpacity": 0.25},
                    tooltip=folium.GeoJsonTooltip(fields=["MANZANA"], aliases=["Manzana"]),
                ).add_to(m)
                grupo_mz = folium.FeatureGroup(name=f"Etiquetas manzanas {zonas.loc[zonas.ID_ZONA == idz, 'COD'].iloc[0]}")
                for lat, lon, texto in centros_manzanas(URI, idz, bounds):
                    etiqueta_mapa(texto, "lbl-mz", lat, lon).add_to(grupo_mz)
                grupo_mz.add_to(m)
        folium.GeoJson(
            sels[cols], name="Zonas elegidas",
            style_function=lambda f: {"fillOpacity": 0, "color": COLOR_SEL, "weight": 4},
            tooltip=folium.GeoJsonTooltip(fields=["COD", "n_mz"], aliases=["Zona", "Manzanas"]),
        ).add_to(m)
    if punto:
        folium.Marker([punto["lat"], punto["lon"]], tooltip=punto["label"],
                      icon=folium.Icon(color="orange", icon="map-marker", prefix="fa")).add_to(m)
        if not idsels:
            lat, lon = punto["lat"], punto["lon"]
            limites = [[lat - 0.004, lon - 0.004], [lat + 0.004, lon + 0.004]]
    folium.LayerControl(collapsed=True).add_to(m)
    # Estilo de las etiquetas y visibilidad según el zoom (zonas desde 12, manzanas desde 16).
    m.get_root().header.add_child(folium.Element(
        "<style>.lbl-wrap{background:transparent;border:0}"
        ".lbl{position:absolute;transform:translate(-50%,-50%);white-space:nowrap;pointer-events:none;"
        "font-family:system-ui,sans-serif;text-shadow:-1px -1px 0 #fff,1px -1px 0 #fff,"
        "-1px 1px 0 #fff,1px 1px 0 #fff,0 0 3px #fff}"
        ".lbl-zona{font-weight:700;font-size:11px;color:#155240}"
        ".lbl-zona.sel{font-size:13px;color:#f26529}"
        ".lbl-mz{font-weight:600;font-size:10px;color:#2b4085}"
        ".hide-zona .lbl-zona,.hide-mz .lbl-mz{display:none}"
        ".leaflet-container:not(.hide-mz) .lbl-zona.sel{display:none}</style>"))
    nm = m.get_name()
    m.get_root().script.add_child(folium.Element(
        f"(function(){{var mp={nm};function f(){{var z=mp.getZoom(),c=mp.getContainer();"
        f"c.classList.toggle('hide-zona',z<12);c.classList.toggle('hide-mz',z<16);}}"
        f"mp.on('zoomend',f);f();}})();"))
    m.fit_bounds(limites, padding=(20, 20), max_zoom=18)
    # El iframe puede cambiar de tamaño después de crear el mapa: reencuadrar al terminar de cargar.
    nm = m.get_name()
    m.get_root().script.add_child(folium.Element(
        f"setTimeout(function(){{{nm}.invalidateSize();"
        f"{nm}.fitBounds({limites},{{padding:[20,20],maxZoom:18}});}},500);"))
    return m


sel_ids = tuple(i for i in ss.zonas_ms if i in set(Z["ID_ZONA"]))
if sel_ids:
    sub = Z[Z["ID_ZONA"].isin(sel_ids)]
    st.caption(f"{len(sub)} zona(s): {', '.join(sub['COD'])} · {fmt(sub['n_mz'].sum())} manzanas del marco. "
               "Toca el mapa para ver el detalle de cada zona o manzana.")
else:
    st.caption(f"{len(Z)} zonas del distrito. Elige una o varias arriba para ver sus manzanas.")
mapa = construir_mapa(Z, sel_ids, ss.punto)
clave = f"mapa_{ubigeo}_{'-'.join(sel_ids)}_{(ss.punto or {}).get('label', '')}"
st_folium(mapa, height=640, use_container_width=True, returned_objects=[], key=clave)

st.caption("Capas: marco censal INEI (distritos, zonas, manzanas, ejes viales). "
           "Direcciones: servicio de búsqueda de GEOPERÚ.")
