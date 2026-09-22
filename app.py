# -*- coding: utf-8 -*-
"""
ServiciosHogar - Liquidación Automática de Recibos
Bogotá D.C. - sabana de Tibabuyes / Bilbao
"""

import io
import os
import re
import math
import zipfile
import pypdf
import streamlit as st
from PIL import Image, ImageDraw, ImageFont

# -------------------------------------------------------------
# 1. FUNCIONES MATEMÁTICAS Y DE REDONDEO
# -------------------------------------------------------------

def round_cop(val: float, umbral: int = 25) -> int:
    """Redondea a la base $50, igual que las fórmulas del Excel original:
    si las dos últimas cifras >= umbral, redondea hacia arriba; si no, hacia abajo.
    Gas y Agua usan umbral=20; Energía y Aseo usan umbral=25."""
    if val <= 0:
        return 0
    ultimas_dos = int(math.floor(val)) % 100
    if ultimas_dos >= umbral:
        return int(math.ceil(val / 50.0) * 50)
    return int(math.floor(val / 50.0) * 50)

def format_cop(val: float) -> str:
    """Formatea valores a pesos colombianos: $ 37.100"""
    return f"$ {int(val):,}".replace(",", ".")

def clean_amount(s: str) -> float:
    """Limpia cadenas numéricas eliminando decimales tipo ,00."""
    if not s:
        return 0.0
    s_clean = re.sub(r',\d{1,2}$', '', str(s).strip())
    digits_only = re.sub(r"[^\d]", "", s_clean)
    return float(digits_only) if digits_only else 0.0

def find_cop_amounts(text: str):
    """Encuentra montos con formato de moneda en pesos colombianos (respaldo)."""
    matches = re.findall(r'(?:\$\s*)?([1-9]\d{1,2}[\.,]\d{3}(?:[\.,]\d{3})?(?:,\d{2})?)', text)
    nums = []
    for m in matches:
        val = clean_amount(m)
        if 10000 <= val <= 2500000:
            nums.append(val)
    return nums

def extract_date(text: str) -> str:
    """Extrae la fecha de pago oportuno o vencimiento."""
    m = re.search(r'(?:pago\s+oportuno|pague\s+hasta|vencimiento).{0,35}?([0-9]{1,2}[\s\/\-\.][a-zA-Z]{3,4}[\s\/\-\.][0-9]{4}|[a-zA-Z]{3,4}[\/\-\.][0-9]{1,2}[\/\-\.][0-9]{4}|\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{2,4})', text, re.IGNORECASE | re.DOTALL)
    if m:
        return m.group(1).strip()
    m2 = re.search(r'([0-9]{1,2}[\s\/\-\.][a-zA-Z]{3,4}\.?[\s\/\-\.][0-9]{4}|[a-zA-Z]{3,4}[\/\-\.][0-9]{1,2}[\/\-\.][0-9]{4}).{0,35}?(?:pago\s+oportuno|pague\s+hasta)', text, re.IGNORECASE | re.DOTALL)
    if m2:
        return m2.group(1).strip()
    return ""

# -------------------------------------------------------------
# 1b. TOTAL REAL: código de barras GS1-128, AI (3900) = valor a pagar
#     Toda factura electrónica de servicios públicos en Colombia lo trae en
#     texto plano, sin importar cómo esté diagramada la factura visualmente.
#     Es mucho más confiable que buscar "Total a pagar" cerca de un número,
#     porque esas etiquetas suelen ser gráficas y no texto extraíble.
# -------------------------------------------------------------

def extract_barcode_total(text: str):
    m = re.search(r'\(3900\)0*(\d+)', text)
    return int(m.group(1)) if m else None

def extract_aseo_enel(text: str):
    """En las facturas Enel de Bogotá, el total de Aseo se repite en una línea fija:
    'INFORMATIVO DEUDA TOTAL: 84,740.00' (formato con coma de miles), justo
    después del bloque de Aseo. Energía = Total factura - Aseo."""
    m = re.search(r'INFORMATIVO DEUDA TOTAL:?\s*([\d.,]+)\.\d{2}', text, re.IGNORECASE)
    if not m:
        return None
    return int(re.sub(r'[^\d]', '', m.group(1)))

# -------------------------------------------------------------
# 2. MOTOR DE EXTRACCIÓN AUTOMÁTICA DE PDFS
# -------------------------------------------------------------

def parse_pdf_file(filename: str, file_bytes: bytes):
    reader = pypdf.PdfReader(io.BytesIO(file_bytes))
    full_text = ""
    for page in reader.pages:
        txt = page.extract_text()
        if txt:
            full_text += txt + "\n"

    fn = filename.lower()
    t = full_text.lower()

    service = "DESCONOCIDO"
    empresa = "Desconocida"
    if "enel" in fn or "codensa" in fn or "energia" in fn or "enel" in t or "codensa" in t:
        service = "ENERGIA"
        empresa = "Enel Colombia (Energía y Aseo)"
    elif "vanti" in fn or "gas" in fn or "vanti" in t or "gas natural" in t:
        service = "GAS"
        empresa = "Vanti (Gas Natural)"
    elif "acueducto" in fn or "agua" in fn or "eaab" in fn or "acueducto" in t or "alcantarillado" in t or "eaab" in t:
        service = "AGUA"
        empresa = "Acueducto de Bogotá (EAAB)"

    fecha_val = extract_date(full_text)
    total_barra = extract_barcode_total(full_text)
    all_amounts = find_cop_amounts(full_text)

    res = {
        "service": service, "empresa": empresa, "filename": filename, "fecha": fecha_val,
        "total": 0.0, "energia": 0.0, "aseo": 0.0, "gas": 0.0, "agua": 0.0,
        "aviso": "", "char_count": len(full_text.strip())
    }

    if service == "ENERGIA":
        aseo_val = extract_aseo_enel(full_text)
        if total_barra is not None and aseo_val is not None:
            res["total"], res["aseo"], res["energia"] = total_barra, aseo_val, total_barra - aseo_val
        elif total_barra is not None:
            res["total"], res["energia"], res["aseo"] = total_barra, total_barra, 0
            res["aviso"] = "No pude separar Aseo del total; revisa y divide los valores a mano."
        elif all_amounts:
            res["total"] = max(all_amounts)
            res["energia"] = res["total"]
            res["aviso"] = "No encontré el código de barras del total; verifica el valor."

    elif service == "GAS":
        if total_barra is not None:
            res["gas"] = total_barra
        elif all_amounts:
            res["gas"] = max(all_amounts)
            res["aviso"] = "No encontré el código de barras del total; verifica el valor."
        res["total"] = res["gas"]

    elif service == "AGUA":
        if total_barra is not None:
            res["agua"] = total_barra
        elif all_amounts:
            res["agua"] = max(all_amounts)
            res["aviso"] = "No encontré el código de barras del total; verifica el valor."
        res["total"] = res["agua"]

    return res

# -------------------------------------------------------------
# 3. GENERADOR DINÁMICO DE TARJETAS PARA WHATSAPP
# -------------------------------------------------------------

def load_font(size: int, bold: bool = False):
    """Busca una fuente utilizable tanto en Windows (uso local) como en Linux
    (Streamlit Community Cloud y otros hostings), con Pillow por defecto al final."""
    candidates = [
        os.path.join(os.environ.get("WINDIR", "C:/Windows"), "Fonts", "arialbd.ttf" if bold else "arial.ttf"),
        os.path.join(os.environ.get("WINDIR", "C:/Windows"), "Fonts", "calibrib.ttf" if bold else "calibri.ttf"),
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()

def render_dynamic_card(piso_name, fecha_pago, services_items):
    width = 850
    row_h_fecha = 45
    row_h_piso = 40
    row_h_header = 40
    row_h_item = 40
    row_h_total = 45

    total_height = row_h_fecha + row_h_piso + row_h_header + (len(services_items) * row_h_item) + row_h_total
    img = Image.new("RGB", (width, total_height), "white")
    draw = ImageDraw.Draw(img)

    font_bold = load_font(20, bold=True)
    font_reg = load_font(19, bold=False)

    c0, c1, c2, c3 = 0, 210, 550, width

    y = 0
    draw.rectangle([c0, y, c1, y + row_h_fecha], fill="#FF9900", outline="black")
    draw.rectangle([c1, y, c3, y + row_h_fecha], fill="#FFFF00", outline="black")
    draw.text((15, y + 12), "FECHA DE PAGO:", fill="black", font=font_bold)
    draw.text((c1 + 90, y + 12), str(fecha_pago), fill="black", font=font_reg)
    y += row_h_fecha

    draw.rectangle([c0, y, c3, y + row_h_piso], fill="#FF0000", outline="black")
    draw.text(((width - draw.textlength(piso_name, font=font_bold)) // 2, y + 8), piso_name, fill="black", font=font_bold)
    y += row_h_piso

    draw.rectangle([c0, y, c1, y + row_h_header], fill="#D9D9D9", outline="black")
    draw.rectangle([c1, y, c3, y + row_h_header], fill="#D9D9D9", outline="black")
    draw.text((c0 + (c1 - c0 - draw.textlength("Servicio", font=font_reg)) // 2, y + 9), "Servicio", fill="black", font=font_reg)
    h_text = "Valor a pagar en $ Pesos (COP)"
    draw.text((c1 + (c3 - c1 - draw.textlength(h_text, font=font_reg)) // 2, y + 9), h_text, fill="black", font=font_reg)
    y += row_h_header

    total_val = 0
    for s in services_items:
        draw.rectangle([c0, y, c1, y + row_h_item], fill=s["color"], outline="black")
        draw.rectangle([c1, y, c2, y + row_h_item], fill="white", outline="black")
        draw.rectangle([c2, y, c3, y + row_h_item], fill="white", outline="black")

        draw.text((c0 + (c1 - c0 - draw.textlength(s["name"], font=font_bold)) // 2, y + 9), s["name"], fill="black", font=font_bold)

        if s["name"] in ["Aseo", "Agua"]:
            draw.text((c1 + (c2 - c1 - draw.textlength(s["detail"], font=font_reg)) // 2, y + 9), s["detail"], fill="black", font=font_reg)
        else:
            draw.text((c1 + 20, y + 9), s["detail"], fill="black", font=font_reg)

        draw.text((c3 - draw.textlength(s["val_str"], font=font_reg) - 20, y + 9), s["val_str"], fill="black", font=font_reg)

        total_val += s.get("raw_val", 0)
        y += row_h_item

    draw.rectangle([c0, y, c2, y + row_h_total], fill="#B8CCE4", outline="black")
    draw.rectangle([c2, y, c3, y + row_h_total], fill="#B8CCE4", outline="black")
    draw.text((c0 + (c2 - c0 - draw.textlength("TOTAL", font=font_bold)) // 2, y + 11), "TOTAL", fill="black", font=font_bold)
    tot_s = format_cop(total_val)
    draw.text((c3 - draw.textlength(tot_s, font=font_bold) - 20, y + 11), tot_s, fill="black", font=font_bold)

    return img

# -------------------------------------------------------------
# 4. INTERFAZ STREAMLIT
# -------------------------------------------------------------

st.set_page_config(page_title="ServiciosHogar - Cuentas de Cobro", layout="wide")
st.title("Sistema Automático de Liquidación de Recibos")

uploaded_files = st.file_uploader(
    "Arrastra y suelta aquí tus recibos en PDF (Enel, Vanti o Acueducto):",
    type=["pdf"],
    accept_multiple_files=True
)

val_energia = 0.0
val_aseo = 0.0
val_gas = 0.0
val_agua = 0.0
fecha_pago = "10 Sep. 2026"
recibos_procesados = []

if uploaded_files:
    for uploaded_file in uploaded_files:
        uploaded_file.seek(0)
        bytes_data = uploaded_file.read()
        p = parse_pdf_file(uploaded_file.name, bytes_data)
        recibos_procesados.append(p)

        if p["service"] == "ENERGIA":
            val_energia = p["energia"]
            val_aseo = p["aseo"]
            if p["fecha"]:
                fecha_pago = p["fecha"]
        elif p["service"] == "GAS":
            val_gas = p["gas"]
            if p["fecha"]:
                fecha_pago = p["fecha"]
        elif p["service"] == "AGUA":
            val_agua = p["agua"]
            if p["fecha"]:
                fecha_pago = p["fecha"]

    st.write("### Estado de lectura de archivos:")
    for r in recibos_procesados:
        aviso = f" ⚠️ {r['aviso']}" if r.get("aviso") else ""
        if r["service"] == "ENERGIA":
            st.success(f"✅ **{r['filename']}**: {r['empresa']} | Energía: {format_cop(r['energia'])} | Aseo: {format_cop(r['aseo'])} | Pago: {r['fecha']}{aviso}")
        elif r["service"] == "GAS":
            st.success(f"✅ **{r['filename']}**: {r['empresa']} | Gas: {format_cop(r['gas'])} | Pago: {r['fecha']}{aviso}")
        elif r["service"] == "AGUA":
            st.success(f"✅ **{r['filename']}**: {r['empresa']} | Agua: {format_cop(r['agua'])} | Pago: {r['fecha']}{aviso}")
        else:
            st.warning(f"⚠️ **{r['filename']}**: No se reconoció automáticamente. Caracteres leídos: {r['char_count']}")

st.write("---")

col_izq, col_der = st.columns(2)

with col_izq:
    st.subheader("1. Valores Detectados")
    fecha_val = st.text_input("Fecha Límite de Pago a mostrar", value=fecha_pago)

    m1, m2 = st.columns(2)
    m1.metric("Energía Enel", format_cop(val_energia))
    m2.metric("Aseo Área Limpia", format_cop(val_aseo))

    m3, m4 = st.columns(2)
    m3.metric("Gas Vanti", format_cop(val_gas))
    m4.metric("Agua Acueducto", format_cop(val_agua))

    st.write("#### Servicios incluidos en la tarjeta:")
    inc_ey = st.checkbox("Incluir Energía y Aseo", value=(val_energia > 0 or val_aseo > 0))
    inc_gas = st.checkbox("Incluir Gas", value=(val_gas > 0))
    inc_ag = st.checkbox("Incluir Agua", value=(val_agua > 0))

    st.write("#### Censo de Agua por Piso (Personas):")
    p_c1, p_c2 = st.columns(2)
    with p_c1:
        p1_pers = st.number_input("Piso 1 - Personas", value=2, min_value=0)
        p3_pers = st.number_input("Piso 3 - Personas", value=2, min_value=0)
    with p_c2:
        p2_pers = st.number_input("Piso 2 - Personas", value=4, min_value=0)
        p4_pers = st.number_input("Piso 4 - Personas", value=1, min_value=0)

    tot_p = p1_pers + p2_pers + p3_pers + p4_pers
    c_unit_agua = (val_agua / tot_p) if tot_p > 0 else 0
    pers_dict = {"Piso 1": p1_pers, "Piso 2": p2_pers, "Piso 3": p3_pers, "Piso 4": p4_pers}

    with st.expander("⚙️ Modificar valores manualmente si se requiere"):
        val_energia = st.number_input("Editar Energía (COP)", value=float(val_energia), step=500.0)
        val_aseo = st.number_input("Editar Aseo (COP)", value=float(val_aseo), step=500.0)
        val_gas = st.number_input("Editar Gas (COP)", value=float(val_gas), step=500.0)
        val_agua = st.number_input("Editar Agua (COP)", value=float(val_agua), step=1000.0)
        c_unit_agua = (val_agua / tot_p) if tot_p > 0 else 0

with col_der:
    st.subheader("2. Cuentas de Cobro para WhatsApp")

    pisos_config = {
        "Piso 1": {"pct_e": 0.215, "pct_g": 0.20, "extra": ""},
        "Piso 2": {"pct_e": 0.420, "pct_g": 0.50, "extra": ""},
        "Piso 3": {"pct_e": 0.150, "pct_g": 0.30, "extra": ""},
        "Piso 4": {"pct_e": 0.215, "pct_g": 0.00, "extra": ""},
    }

    cards = {}
    for piso, cfg in pisos_config.items():
        items = []

        if inc_gas:
            g_raw = round_cop(val_gas * cfg["pct_g"], umbral=20)
            items.append({"name": "Gas", "color": "#0000FF", "detail": "CALCULADO POR PORCENTAJE",
                          "val_str": format_cop(g_raw), "raw_val": g_raw})

        if inc_ey:
            e_raw = round_cop(val_energia * cfg["pct_e"], umbral=25)
            e_lbl = f"{cfg['extra']}{format_cop(e_raw)}" if cfg["extra"] else format_cop(e_raw)
            items.append({"name": "Energía", "color": "#FFFF00", "detail": "CALCULADO POR PORCENTAJE",
                          "val_str": e_lbl, "raw_val": e_raw})

            a_raw = round_cop(val_aseo / 4.0, umbral=25)
            items.append({"name": "Aseo", "color": "#FFFF00", "detail": "POR PISO",
                          "val_str": format_cop(a_raw), "raw_val": a_raw})

        if inc_ag:
            ag_raw = round_cop(pers_dict[piso] * c_unit_agua, umbral=20)
            p_text = f"{pers_dict[piso]} PERSONAS = {format_cop(ag_raw)}" if pers_dict[piso] != 1 else f"{pers_dict[piso]} PERSONA = {format_cop(ag_raw)}"
            items.append({"name": "Agua", "color": "#00CCFF", "detail": f"POR PERSONA = $ {int(c_unit_agua)}",
                          "val_str": p_text, "raw_val": ag_raw})

        cards[piso] = render_dynamic_card(piso, fecha_val, items)

    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for p_name, img_obj in cards.items():
            b = io.BytesIO()
            img_obj.save(b, format="PNG")
            zf.writestr(f"Cuenta_Cobro_{p_name.replace(' ', '_')}.png", b.getvalue())
    zip_buffer.seek(0)

    st.download_button(
        label="📦 Descargar TODAS las Cuentas de Cobro (ZIP)",
        data=zip_buffer.getvalue(),
        file_name="Cuentas_de_Cobro_Todos_los_Pisos.zip",
        mime="application/zip",
        use_container_width=True
    )

    st.write("")

    cp1, cp2 = st.columns(2)
    with cp1:
        st.image(cards["Piso 1"], use_container_width=True)
        buf1 = io.BytesIO(); cards["Piso 1"].save(buf1, format="PNG")
        st.download_button("📥 Descargar Piso 1 (PNG)", data=buf1.getvalue(), file_name="Cuenta_Cobro_Piso_1.png", mime="image/png")

        st.image(cards["Piso 3"], use_container_width=True)
        buf3 = io.BytesIO(); cards["Piso 3"].save(buf3, format="PNG")
        st.download_button("📥 Descargar Piso 3 (PNG)", data=buf3.getvalue(), file_name="Cuenta_Cobro_Piso_3.png", mime="image/png")

    with cp2:
        st.image(cards["Piso 2"], use_container_width=True)
        buf2 = io.BytesIO(); cards["Piso 2"].save(buf2, format="PNG")
        st.download_button("📥 Descargar Piso 2 (PNG)", data=buf2.getvalue(), file_name="Cuenta_Cobro_Piso_2.png", mime="image/png")

        st.image(cards["Piso 4"], use_container_width=True)
        buf4 = io.BytesIO(); cards["Piso 4"].save(buf4, format="PNG")
        st.download_button("📥 Descargar Piso 4 (PNG)", data=buf4.getvalue(), file_name="Cuenta_Cobro_Piso_4.png", mime="image/png")
