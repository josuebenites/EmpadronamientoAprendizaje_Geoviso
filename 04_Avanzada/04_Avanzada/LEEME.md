# Zonas censales por distrito (marco INEI)

App Streamlit para los distritos del proyecto (Ate, El Porvenir y San Juan de Lurigancho):
selector de distrito, tablero con todas sus zonas censales (009A, 009B…), buscador y mapa
con las manzanas y las vías de la zona elegida.

## Cómo correrla
```
cd 05_Intervention\04_Avanzada
pip install -r requirements.txt
streamlit run app.py
```

## Ubicar un punto (el mapa lo marca y dice en qué zona censal cae)
- **Dirección**: servicio de búsqueda de GEOPERÚ.
- **Coordenadas manuales**: escribirlas en el buscador, como «lat, lon» o «lon, lat»
  (ej. `-12.0056, -77.0868`). Acepta coma o punto decimal.
- **Mi ubicación**: botón «Usar mi ubicación» (GPS del navegador). Funciona en `localhost` o
  `https`; el navegador pedirá permiso. Si el punto cae en otro distrito del proyecto, la app cambia a ese distrito.

## De dónde salen los datos
- Todas las capas (distritos, zonas, manzanas, ejes viales) salen del marco censal INEI:
  `02_Despliegue_manzanas\01_Data\marco censal inei.zip`. Se lee el .zip directo; no hay que extraerlo.
- Otra ubicación del marco: variable de entorno `MARCO_PATH`.

## Ojo
- El marco tiene 343 zonas en los tres distritos (Ate 108, El Porvenir 22, San Juan de
  Lurigancho 213). La base de despliegue usaba 409 (zonas del precenso, más subdivididas, p. ej. 002F).
- Buscador de direcciones: usa el endpoint que emplea el visor de GEOPERÚ (no es una API
  pública y puede cambiar). Alternativa: definir `GEOAPIFY_API_KEY` con una key propia.
- La primera lectura puede tardar si Box Drive aún no descargó el .zip (237 MB).

## Etiquetas del mapa
- Código de cada zona (p. ej. 009A) en su centro visual; se muestra desde zoom 12. La zona elegida se resalta en rojo.
- Etiqueta `MANZANA` del marco (p. ej. 001, 015P) sobre cada manzana de la zona elegida; se muestra desde zoom 16 (ahí el rótulo de la zona se oculta para no tapar las manzanas).
- Ambas capas se pueden apagar desde el control de capas del mapa.

## Diseño
Paleta de marca IPA (verde #49ac57 / verde oscuro #155240, azul claro, azul oscuro, rojo-naranja, grises), títulos en Georgia y texto en Arial, franja con el logo de IPA. Pensado para celular: botones grandes, tablero de zonas en 4 columnas y mapa al 62 % de la altura de la pantalla. El tema está en `.streamlit/config.toml`; el logo, en `assets/`.

## Aviso de NumPy 2 / bottleneck
El mensaje `_ARRAY_API not found` es una advertencia de pandas con un `bottleneck` antiguo; no detiene la app. Para quitarlo: `pip install -U bottleneck` (o `pip install "numpy<2"`).

## Zonas en el mapa
El mapa es el centro de la pantalla. El selector «Zonas en el mapa» admite una o varias zonas (también se agregan desde el buscador, el GPS o las coordenadas). Cada zona elegida se resalta en rojo-naranja con sus manzanas y etiquetas; el mapa se encuadra sobre todas. Sin selección se ve el distrito completo.

## Despliegue en Streamlit Community Cloud
1. Sube esta carpeta tal cual (incluye `data/marco_proyecto.gpkg`, 36 MB: marco INEI recortado a Ate, El Porvenir y SJL) a un repositorio de GitHub. Los datos del marco son públicos.
2. En share.streamlit.io: *Create app* → repositorio, rama, archivo principal `app.py` → *Deploy*.
3. (Opcional) En *Settings → Secrets*: `GEOAPIFY_API_KEY = "..."` si tienes llave propia; sin ella se usa el buscador de GEOPERÚ.
4. La URL es https, así que el botón de GPS funciona en el celular.
Si el repositorio es público, cualquiera con el enlace verá la app; para restringirla, usa repositorio privado y *Sharing → Only specific people*.
