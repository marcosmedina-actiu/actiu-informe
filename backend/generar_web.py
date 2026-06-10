#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
generar_web.py — backend del informe SAP<->Tookane
- Queries PostgreSQL actiu_co (live KPIs + charts)
- Extracts full content from analisis_sap_tookane.html (outputs only, no code)
- Renders frontend/template.html -> actiu-informe/index.html
"""

import io
import base64
import re
from collections import defaultdict
from datetime import datetime

import psycopg2
from bs4 import BeautifulSoup, Tag
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker

# ── CONFIG ────────────────────────────────────────────────────────────────────
DB_URL        = 'postgresql://postgres@localhost:5432/actiu_co'
TEMPLATE      = r'C:\Users\medina\n8n-build\frontend\template.html'
OUT           = r'C:\Users\medina\actiu-informe\index.html'
NOTEBOOK_HTML = r'C:\Users\medina\n8n-build\analisis_sap_tookane.html'
SAP_CSV       = r'C:\Users\medina\Downloads\CAMPOS PARA SUBIR A TOOKANE - Hoja 1.csv'

# Notebook sections cuyo contenido está cubierto por secciones dedicadas del template
SKIP_HEADINGS = {
    'estado de los 26 campos',   # → reemplazado por tabla CSV dedicada
    'próximos pasos',            # → reemplazado por sección estática en el template
    'sap s4',                    # intro del notebook — duplica el hero + contiene caja oscura no deseada
}

# (SUPPRESS_HEADINGS eliminado — su único uso era 'sap s4', ahora en SKIP_HEADINGS)

# Fragmentos de texto a eliminar del contenido extraído del notebook
FILTER_PARAGRAPHS = [
    'Eso es toda la integración con Tookane. El resto de workflows (alertas, casos CO, Google Sheets)',
]

NAVY  = '#1B1B1B'    # Actiu near-black
BLUE  = '#C96C1E'    # Actiu warm orange (primary accent)
TEAL  = '#2E7D6E'    # muted teal
GREEN = '#2E7D32'
GOLD  = '#8B6914'
CORAL = '#C94040'
PURP  = '#6B3FA0'
ORNG  = '#C96C1E'    # same as BLUE = Actiu orange
GRAY  = '#6B7280'

# Normalización nombres transportistas en texto libre de casos.transportista
CARRIER_ALIASES = {
    'DACHSER': 'Dachser',
    'DSV': 'DSV',
    'KUEHNE & NAGEL': 'Kuehne+Nagel', 'KUEHNE+NAGEL': 'Kuehne+Nagel',
    'KUEHNE-NAGEL': 'Kuehne+Nagel', 'KUEHNE NAGEL': 'Kuehne+Nagel',
    'DB SCHENKER': 'DB Schenker', 'DB SCHENKER (COLISSIMO)': 'DB Schenker',
    'GB GRUPAJES': 'GB Grupajes',
    'ERCHIGA LOGISTICA': 'Erchiga', 'ERCHIGA': 'Erchiga',
    'UPS FREIGHT': 'UPS',
    'BERGÉ': 'Bergé', 'BERGÉ LOGISTICS': 'Bergé', 'BERGE LOGISTICS': 'Bergé',
    'ARIN EXPRESS': 'Arin Express',
    'LUIS TORTOSA': 'Luis Tortosa',
    'GB TRANSP': 'GB Grupajes',
}

# Section anchor IDs for nav links (matched by keywords in heading text)
SECTION_ANCHORS = {
    'la integración entre': 'problema',  # "Por qué la integración entre SAP y Tookane no es óptima"
    'no es óptima':         'problema',
    'estado de los 26':     'brechas',
    'propuesta de auto':    'arquitectura',
    'arquitectura propuesta': 'arquitectura',
    'hub central':          'arquitectura',  # "¿Por qué N8N como hub central?"
    'qué resuelve':         'arquitectura',  # "2.2 ¿Qué resuelve esta arquitectura?"
    'detalle de cada':      'workflows',
    'esquema postgresql':   'bd',
    'resumen final':        'resumen',
}

# ── NEXT STEPS (static) ───────────────────────────────────────────────────────
NEXT_STEPS = [
    dict(title='Migrar PostgreSQL al servidor n8n', color=CORAL,
         desc='Prerequisito bloqueante de todo. Coordinar con Tino: instalar '
              'PostgreSQL en VM corporativa y restaurar backup actiu_co.'),
    dict(title='Registrar URL webhook Tookane (W2, W5)', color=GOLD,
         desc='Configurar endpoint n8n en panel Tookane para recibir eventos '
              'de estado de envíos en tiempo real.'),
    dict(title='Credencial Gmail OAuth2 (W6)', color=BLUE,
         desc='Autorizar cuenta Gmail CO en n8n para el Email Agent de '
              'clasificación y respuesta automática.'),
    dict(title='Google Gemini API Key (W6)', color=BLUE,
         desc='Crear API Key en Google AI Studio para gemini-2.0-flash '
              '(clasificación y redacción de correos CO).'),
    dict(title='SAP OData VTTK con IT (W1)', color=PURP,
         desc='Solicitar a Quiles/Tino acceso al servicio OData SAP S/4 '
              'para leer expediciones VTTK.'),
    dict(title='SAP OData VBAK — pedido + cliente (W1)', color=PURP,
         desc='Además de VTTK, acceder a VBAK para enriquecer envíos con '
              'datos de pedido, cliente e importe declarado.'),
    dict(title='Google Calendar OAuth2 (W7)', color=GRAY,
         desc='Autorizar acceso al calendario SAT para actualización diaria '
              'de disponibilidad de técnicos.'),
]

# ── DATABASE ──────────────────────────────────────────────────────────────────

def _query(conn, sql, params=None):
    with conn.cursor() as cur:
        cur.execute(sql, params or ())
        return cur.fetchall()


def get_kpis(conn):
    kpis = {}

    def safe(sql, fallback=0, params=None):
        try:
            return _query(conn, sql, params)[0][0]
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            return fallback

    def safe_rows(sql, fallback=None):
        try:
            return _query(conn, sql)
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            return fallback or []

    kpis['casos_total']   = safe('SELECT count(*) FROM casos')
    kpis['envios_total']  = safe('SELECT count(*) FROM envios')
    kpis['transportistas']= safe('SELECT count(*) FROM transportistas', 26)
    kpis['trans_con_bp']  = safe(
        'SELECT count(*) FROM transportistas WHERE bp_sap IS NOT NULL', 20)
    kpis['contactos']     = safe('SELECT count(*) FROM transportistas_contactos')
    kpis['procedimientos']= safe('SELECT count(*) FROM procedimientos')

    kpis['casos_tipo'] = [
        (r[0] or 'Sin tipo', int(r[1]))
        for r in safe_rows(
            'SELECT tipo_caso, count(*) n FROM casos '
            'WHERE tipo_caso IS NOT NULL GROUP BY tipo_caso ORDER BY n DESC'
        )
    ]

    # Top transportistas: usar texto libre (transportista_id es NULL en la mayoría)
    raw_trans = safe_rows(
        "SELECT UPPER(TRIM(transportista)), count(*) n FROM casos "
        "WHERE transportista IS NOT NULL AND TRIM(transportista) NOT IN ('','-') "
        "GROUP BY UPPER(TRIM(transportista)) ORDER BY n DESC"
    )
    counts_trans = defaultdict(int)
    for raw, cnt in raw_trans:
        norm = CARRIER_ALIASES.get(raw, raw.title() if raw else '—')
        counts_trans[norm] += int(cnt)
    kpis['top_trans'] = sorted(counts_trans.items(), key=lambda x: x[1], reverse=True)[:7]

    # Distribución por país destino (normalizado vía subquery)
    kpis['pais_data'] = [
        (r[0], int(r[1]))
        for r in safe_rows("""
            SELECT pais_norm, n FROM (
              SELECT
                CASE
                  WHEN UPPER(TRIM(pais_destino)) IN ('ES','SPAIN','ESPAÑA','ESPANA') THEN 'España'
                  WHEN UPPER(TRIM(pais_destino)) IN ('FR','FRANCE','FRANCIA') THEN 'Francia'
                  WHEN UPPER(TRIM(pais_destino)) IN ('DE','GERMANY','ALEMANIA') THEN 'Alemania'
                  WHEN UPPER(TRIM(pais_destino)) IN ('PT','PORTUGAL') THEN 'Portugal'
                  WHEN UPPER(TRIM(pais_destino)) IN ('GB','UK','UNITED KINGDOM','REINO UNIDO') THEN 'Reino Unido'
                  WHEN UPPER(TRIM(pais_destino)) IN ('IT','ITALY','ITALIA') THEN 'Italia'
                  WHEN UPPER(TRIM(pais_destino)) IN ('CH','SWITZERLAND','SUIZA') THEN 'Suiza'
                  WHEN UPPER(TRIM(pais_destino)) IN ('AD','ANDORRA') THEN 'Andorra'
                  WHEN UPPER(TRIM(pais_destino)) IN ('BE','BELGIUM','BÉLGICA','BELGICA') THEN 'Bélgica'
                  WHEN UPPER(TRIM(pais_destino)) IN ('NL','NETHERLANDS','PAÍSES BAJOS','PAISES BAJOS') THEN 'P.Bajos'
                  WHEN UPPER(TRIM(pais_destino)) IN ('AT','AUSTRIA') THEN 'Austria'
                  WHEN UPPER(TRIM(pais_destino)) IN ('IE','IRELAND','IRLANDA') THEN 'Irlanda'
                  ELSE NULL
                END as pais_norm, count(*) as n
              FROM casos
              WHERE pais_destino IS NOT NULL
                AND UPPER(TRIM(pais_destino)) NOT IN ('','-')
              GROUP BY pais_norm
            ) sub
            WHERE pais_norm IS NOT NULL
            ORDER BY n DESC
        """)
    ]
    kpis['paises_count'] = len(kpis['pais_data'])
    kpis['incidencias_total'] = safe(
        "SELECT count(*) FROM casos WHERE inc_tipo IS NOT NULL")
    kpis['clientes_total'] = safe(
        "SELECT count(*) FROM clientes", 0)

    return kpis

# ── CHARTS ───────────────────────────────────────────────────────────────────

def _to_b64(fig):
    buf = io.BytesIO()
    fig.savefig(buf, format='png', bbox_inches='tight', dpi=110,
                facecolor='white', edgecolor='none')
    plt.close(fig)
    buf.seek(0)
    return base64.b64encode(buf.read()).decode('utf-8')


def chart_casos_tipo(data):
    if not data:
        return None
    labels  = [d[0] for d in data]
    values  = [d[1] for d in data]
    palette = [BLUE, TEAL, PURP, CORAL, GOLD, ORNG, GREEN]
    colors  = [palette[i % len(palette)] for i in range(len(labels))]
    fig, ax = plt.subplots(figsize=(5.5, 3.2))
    bars = ax.bar(labels, values, color=colors, edgecolor='none', width=0.6)
    ax.set_ylabel('Casos', fontsize=9, color='#64748B')
    ax.tick_params(axis='x', labelsize=8, rotation=15)
    ax.tick_params(axis='y', labelsize=8)
    ax.yaxis.set_major_locator(mticker.MaxNLocator(integer=True))
    ax.spines[['top', 'right', 'left']].set_visible(False)
    ax.yaxis.grid(True, color='#F1F5F9', linewidth=0.8)
    ax.set_axisbelow(True)
    for bar, val in zip(bars, values):
        ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 0.1,
                str(val), ha='center', va='bottom', fontsize=8,
                fontweight='bold', color=NAVY)
    fig.tight_layout()
    return _to_b64(fig)


def chart_top_trans(data):
    if not data:
        return None
    labels  = [d[0][:22] for d in data]
    values  = [d[1] for d in data]
    palette = [TEAL, BLUE, GREEN, PURP, CORAL, ORNG, GOLD]
    colors  = [palette[i % len(palette)] for i in range(len(labels))]
    fig, ax = plt.subplots(figsize=(5.5, 3.2))
    bars = ax.barh(labels[::-1], values[::-1], color=colors[::-1],
                   edgecolor='none', height=0.55)
    ax.set_xlabel('Casos', fontsize=9, color='#64748B')
    ax.tick_params(axis='y', labelsize=8)
    ax.tick_params(axis='x', labelsize=8)
    ax.xaxis.set_major_locator(mticker.MaxNLocator(integer=True))
    ax.spines[['top', 'right', 'bottom']].set_visible(False)
    ax.xaxis.grid(True, color='#F1F5F9', linewidth=0.8)
    ax.set_axisbelow(True)
    for bar, val in zip(bars[::-1], values[::-1]):
        ax.text(val + 0.05, bar.get_y() + bar.get_height() / 2,
                str(val), va='center', fontsize=8, fontweight='bold', color=NAVY)
    fig.tight_layout()
    return _to_b64(fig)


def chart_pais_destino(data):
    if not data:
        return None
    labels  = [d[0] for d in data]
    values  = [d[1] for d in data]
    palette = [BLUE, TEAL, PURP, CORAL, GOLD, GREEN, ORNG, '#14B8A6', '#F97316', '#8B5CF6']
    colors  = [palette[i % len(palette)] for i in range(len(labels))]
    fig, ax = plt.subplots(figsize=(5.5, 3.2))
    bars = ax.barh(labels[::-1], values[::-1], color=colors[::-1],
                   edgecolor='none', height=0.6)
    ax.set_xlabel('Casos', fontsize=9, color='#64748B')
    ax.tick_params(axis='y', labelsize=8)
    ax.tick_params(axis='x', labelsize=8)
    ax.xaxis.set_major_locator(mticker.MaxNLocator(integer=True))
    ax.spines[['top', 'right', 'bottom']].set_visible(False)
    ax.xaxis.grid(True, color='#F1F5F9', linewidth=0.8)
    ax.set_axisbelow(True)
    for bar, val in zip(bars[::-1], values[::-1]):
        ax.text(val + 0.1, bar.get_y() + bar.get_height() / 2,
                str(val), va='center', fontsize=8, fontweight='bold', color=NAVY)
    fig.tight_layout()
    return _to_b64(fig)

# ── NOTEBOOK EXTRACTION ───────────────────────────────────────────────────────

def _anchor_for_heading(text):
    """Map heading text to nav anchor id."""
    t = text.lower()
    for keyword, anchor in SECTION_ANCHORS.items():
        if keyword in t:
            return anchor
    return None



def extract_notebook_sections(html_path):
    """
    Parse the notebook HTML and return a list of section dicts:
      { heading, anchor, html_parts: [markdown_html, output_html, ...] }
    Code input is excluded; only markdown renders and output cells are kept.

    Actual JupyterLab HTML structure (verified):
      MarkdownCell -> jp-Cell-inputWrapper -> jp-InputArea -> jp-MarkdownOutput
      CodeCell     -> jp-Cell-outputWrapper -> jp-Cell-outputArea
                       -> jp-OutputArea-child -> jp-OutputArea-output
    """
    print(f'  Parsing {html_path}...')
    with open(html_path, encoding='utf-8') as f:
        soup = BeautifulSoup(f.read(), 'html.parser')

    sections = []
    current = {'heading': None, 'anchor': None, 'html_parts': []}

    # Exact class membership (not substring) to avoid false matches
    cells = soup.find_all('div', class_=lambda c: c and (
        'jp-MarkdownCell' in c or 'jp-CodeCell' in c))

    skip_current = False  # True when current section is in SKIP_HEADINGS

    for cell in cells:
        cell_classes = cell.get('class', [])

        if 'jp-MarkdownCell' in cell_classes:
            # Rendered markdown is in jp-MarkdownOutput (not jp-MarkdownCell-renderedMarkdown)
            rendered = cell.find('div', class_=lambda c: c and 'jp-MarkdownOutput' in c)
            if not rendered:
                continue

            heading_tag = rendered.find(['h1', 'h2'])
            if heading_tag:
                if (current['html_parts'] or current['heading']) and not skip_current:
                    sections.append(current)
                # get_text() incluye '¶' del anchor-link de JupyterLab — eliminarlo
                heading_text = heading_tag.get_text(strip=True).replace('¶', '').strip()
                skip_current = any(s in heading_text.lower() for s in SKIP_HEADINGS)
                current = {
                    'heading': heading_text,
                    'anchor': _anchor_for_heading(heading_text),
                    'html_parts': [],
                }
                heading_tag.decompose()

            if skip_current:
                continue

            inner = rendered.decode_contents().strip()
            # Eliminar párrafos específicos señalados por el usuario
            for phrase in FILTER_PARAGRAPHS:
                if phrase in inner:
                    # borrar el <p> completo que contiene la frase
                    inner = re.sub(
                        r'<p>[^<]*' + re.escape(phrase) + r'[^<]*</p>',
                        '', inner, flags=re.DOTALL
                    )
            if inner:
                current['html_parts'].append(
                    f'<div class="nb-markdown">{inner}</div>'
                )

        elif 'jp-CodeCell' in cell_classes:
            if skip_current:
                continue
            # Only outputs: find the output area wrapper
            out_wrapper = cell.find('div', class_='jp-Cell-outputWrapper')
            if not out_wrapper:
                continue
            out_area = out_wrapper.find('div', class_='jp-Cell-outputArea')
            if not out_area:
                continue

            for out in out_area.find_all('div', class_=lambda c: c and 'jp-OutputArea-output' in c):
                # Remove output prompt siblings
                for p in out.parent.find_all('div', class_=lambda c: c and 'jp-OutputPrompt' in c):
                    p.decompose()
                # Fix table styles
                for tbl in out.find_all('table'):
                    style = re.sub(r'width\s*:\s*100%\s*;?', '', tbl.get('style', ''))
                    tbl['style'] = style + '; margin: 0 auto; width: auto;'
                inner = out.decode_contents().strip()
                if inner:
                    current['html_parts'].append(
                        f'<div class="nb-output">{inner}</div>'
                    )

    if (current['html_parts'] or current['heading']) and not skip_current:
        sections.append(current)

    print(f'  Extraidas {len(sections)} secciones del notebook')
    return sections


# ── SAP CSV TABLE ─────────────────────────────────────────────────────────────

def read_sap_csv():
    import pandas as pd
    for enc in ('utf-8-sig', 'latin-1', 'cp1252'):
        try:
            df = pd.read_csv(SAP_CSV, encoding=enc, header=0)
            df.columns = [c.strip() for c in df.columns]
            # Skip sub-header row (row 0: NaN, Tabla, Campo, ...)
            df = df.iloc[1:].reset_index(drop=True)
            df = df.dropna(how='all')
            return df
        except FileNotFoundError:
            return None
        except Exception:
            continue
    return None


def build_sap_table_rows(df):
    if df is None:
        return (
            '<tr><td colspan="7" style="text-align:center;padding:32px;color:#64748B;">'
            'CSV no encontrado. Guardar el archivo en Downloads.'
            '</td></tr>'
        )

    def clean(val):
        s = str(val).strip()
        return '' if s in ('nan', 'NaN', 'None') else s

    rows = []
    for _, row in df.iterrows():
        campo     = clean(row.iloc[0])
        tabla     = clean(row.iloc[1])
        campo_tec = clean(row.iloc[2])
        estado    = clean(row.iloc[3])
        notificar = clean(row.iloc[5])
        donde     = clean(row.iloc[6])
        uso       = clean(row.iloc[7])
        subimos   = clean(row.iloc[10])

        if not campo:
            continue

        tabla_campo = ''
        if tabla and campo_tec:
            tabla_campo = f'<code>{tabla}.{campo_tec}</code>'
        elif tabla:
            tabla_campo = f'<code>{tabla}</code>'
        elif campo_tec:
            tabla_campo = f'<code>{campo_tec}</code>'

        rows.append(
            f'<tr>'
            f'<td><strong>{campo}</strong></td>'
            f'<td>{tabla_campo}</td>'
            f'<td>{_map_badge(estado) if estado else ""}</td>'
            f'<td style="text-align:center;font-size:.78rem">{notificar if notificar else "—"}</td>'
            f'<td style="font-size:.78rem">{donde if donde else "—"}</td>'
            f'<td style="font-size:.78rem;max-width:260px">{uso if uso else "—"}</td>'
            f'<td>{_map_badge(subimos) if subimos else ""}</td>'
            f'</tr>'
        )
    return '\n'.join(rows)


def build_notebook_html(sections):
    """Render extracted sections as HTML with section-title headings."""
    parts = []
    used_anchors = set()   # evitar IDs duplicados en el DOM
    for sec in sections:
        if not sec['html_parts'] and not sec['heading']:
            continue
        anchor = sec.get('anchor') or ''
        # Solo asignar id= la primera vez que aparece un anchor concreto
        if anchor and anchor not in used_anchors:
            anchor_attr = f' id="{anchor}"'
            used_anchors.add(anchor)
        else:
            anchor_attr = ''
        body = '\n'.join(sec['html_parts'])
        heading = sec.get('heading') or ''
        if heading:
            parts.append(
                f'<section{anchor_attr}>\n'
                f'<h2 class="section-title">{heading}</h2>\n'
                f'{body}\n'
                f'</section>\n'
            )
        else:
            parts.append(f'<section{anchor_attr}>\n{body}\n</section>\n')

    return '\n'.join(parts)

# ── HTML BUILDERS ─────────────────────────────────────────────────────────────

def _badge(text, cls):
    return f'<span class="badge badge-{cls}">{text}</span>'


def _map_badge(val):
    v = str(val).strip().upper()
    if 'NATIVO' in v or v == 'OK':
        return _badge(val, 'ok')
    if 'EXTRA' in v:
        return _badge(val, 'extra')
    if 'PARCIAL' in v:
        return _badge(val, 'parcial')
    if v in ('NO', 'NO EXISTE', '-'):
        return _badge(val, 'no')
    if v in ('SÍ', 'SI', 'S', 'YES'):
        return _badge(val, 'si')
    if 'YA EST' in v:
        return _badge(val, 'yaesta')
    if 'POSIBLE' in v or 'EVAL' in v:
        return _badge(val, 'posible')
    return f'<span style="font-size:.78rem">{val}</span>'


def _kpi_card(value, label, sub='', color=BLUE):
    sub_html = f'<div class="sub">{sub}</div>' if sub else ''
    return (
        f'<div class="kpi-card" style="border-left-color:{color}">'
        f'<div class="value">{value}</div>'
        f'<div class="label">{label}</div>'
        f'{sub_html}'
        f'</div>'
    )


def build_kpi_cards(kpis):
    trans       = kpis.get('transportistas', 26)
    trans_bp    = kpis.get('trans_con_bp', 20)
    paises      = kpis.get('paises_count', 0)
    incidencias = kpis.get('incidencias_total', 0)
    clientes    = kpis.get('clientes_total', 0)
    return ''.join([
        _kpi_card(kpis.get('casos_total', 0),   'Total casos CO',           color=BLUE),
        _kpi_card(kpis.get('envios_total', 0),   'Envíos en BD',             color=TEAL),
        _kpi_card(f'{trans_bp}/{trans}',         'Transportistas con BP SAP',
                  f'{trans - trans_bp} sin BP SAP aún',                      CORAL),
        _kpi_card(paises,                        'Países atendidos',
                  'destinos con casos registrados',                          ORNG),
        _kpi_card(incidencias,                   'Incidencias registradas',
                  'casos con tipo de incidencia',                            GOLD),
        _kpi_card(clientes,                      'Clientes en BD',
                  'desde Base de Datos Notion CO',                           PURP),
        _kpi_card(kpis.get('contactos', 0),      'Contactos transportistas', color=GREEN),
        _kpi_card(kpis.get('procedimientos', 0), 'Procedimientos CO',
                  'sincronizados desde Notion',                              TEAL),
        _kpi_card('8', 'Workflows N8N', 'importados · esperando credenciales', GRAY),
    ])


def build_charts(_kpis):
    # Charts eliminados por petición del usuario — sección vacía
    return ''


def build_next_steps():
    parts = []
    for i, step in enumerate(NEXT_STEPS, 1):
        parts.append(
            f'<li class="step-item">'
            f'<div class="step-num" style="background:{step["color"]}">{i}</div>'
            f'<div class="step-content">'
            f'<div class="title">{step["title"]}</div>'
            f'<div class="desc">{step["desc"]}</div>'
            f'</div>'
            f'</li>'
        )
    return '\n'.join(parts)

# ── MAIN ──────────────────────────────────────────────────────────────────────

def main():
    print('Conectando a PostgreSQL actiu_co...')
    try:
        conn = psycopg2.connect(DB_URL)
        print('  Conexion OK')
    except Exception as e:
        print(f'  AVISO: {e} — usando valores por defecto')
        conn = None

    kpis = get_kpis(conn) if conn else {}
    if conn:
        conn.close()

    print('Extrayendo contenido del notebook...')
    try:
        sections = extract_notebook_sections(NOTEBOOK_HTML)
        notebook_html = build_notebook_html(sections)
    except FileNotFoundError:
        print(f'  AVISO: {NOTEBOOK_HTML} no encontrado')
        notebook_html = (
            '<section><p style="color:#64748B;padding:32px;text-align:center;">'
            'Notebook HTML no encontrado. Regenerar con nbconvert primero.</p></section>'
        )

    print('Leyendo CSV campos SAP...')
    df_sap = read_sap_csv()
    print(f'  {"CSV OK — " + str(len(df_sap)) + " campos" if df_sap is not None else "CSV no encontrado"}')

    print('Generando KPIs y graficos...')
    kpi_html  = build_kpi_cards(kpis)
    charts    = build_charts(kpis)
    sap_rows  = build_sap_table_rows(df_sap)
    nxt_steps = build_next_steps()
    timestamp = datetime.now().strftime('%d/%m/%Y %H:%M')

    print('Cargando template...')
    with open(TEMPLATE, encoding='utf-8') as f:
        html = f.read()

    html = html.replace('{{LAST_UPDATE}}',        timestamp)
    html = html.replace('{{KPI_CARDS}}',          kpi_html)
    html = html.replace('{{CHARTS}}',             charts)
    html = html.replace('{{SAP_TABLE_ROWS}}',     sap_rows)
    html = html.replace('{{NOTEBOOK_SECTIONS}}',  notebook_html)
    html = html.replace('{{PROXIMOS_PASOS}}',     nxt_steps)

    print(f'Escribiendo {OUT}...')
    size_kb = len(html.encode('utf-8')) // 1024
    with open(OUT, 'w', encoding='utf-8') as f:
        f.write(html)

    casos = kpis.get('casos_total', 0)
    bp    = kpis.get('trans_con_bp', 0)
    total = kpis.get('transportistas', 0)
    print(f'Listo. {size_kb} KB generados — {casos} casos · {bp}/{total} trans con BP SAP')


if __name__ == '__main__':
    main()
