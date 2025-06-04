# --- Envío de resúmenes por email ---
import subprocess
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

# --- Para generación de resúmenes automáticos con IA Generativa ---
import argparse # Importar argparse
import google.generativeai as genai # Cambiado de openai a google.generativeai
import os
import time # Necesario para time.mktime si se usa, o para struct_time

# --- Para parseo de feeds RSS y manejo de fechas ---
import feedparser
from datetime import datetime, timedelta, timezone

# --- Para extracción de contenido de artículos ---
from newspaper import Article

# --- SSL Configuration for macOS and certifi ---
import ssl
import certifi
# --- Para cargar variables de entorno desde .env ---
from dotenv import load_dotenv

load_dotenv() # Carga variables desde un archivo .env en el mismo directorio

try:
    # Configure Python's SSL context to use certifi's CA bundle.
    # This helps resolve "CERTIFICATE_VERIFY_FAILED" errors on some systems (like macOS).
    ssl_context = ssl.create_default_context(cafile=certifi.where())
    ssl._create_default_https_context = lambda: ssl_context
    print("✅ SSL context configured to use certifi's CA bundle.")
except Exception as e:
    print(f"⚠️ Warning: Could not configure SSL context with certifi: {e}")
    print("   Attempting to proceed without custom SSL context for certifi. SSL errors might persist.")

# --- Constantes ---
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__)) # Directorio donde se encuentra el script
FUENTES_RSS_JSON_PATH = os.path.join(SCRIPT_DIR, "fuentes_rss.json")
DEFAULT_HOURS_AGO = 24 # Cambiado de 72 a 24 horas
DEFAULT_WEEKLY_HOURS = 7 * 24 # 7 días en horas
USER_AGENT = "NewsAggregatorBot/1.0 (+http://example.com/botinfo)" # User agent genérico
MAX_ARTICLES_TO_SUMMARIZE_PER_CATEGORY = 5 # Límite de artículos a resumir por categoría (ajusta si quieres más para el reporte semanal)
OUTPUT_HTML_FILE_NAME = "resumen_noticias.html" # Solo el nombre del archivo
OUTPUT_HTML_FILE_PATH = os.path.join(SCRIPT_DIR, OUTPUT_HTML_FILE_NAME) # Ruta completa al archivo HTML local

# Reemplaza esto con la URL base donde alojarás el archivo HTML en tu servidor de Stanford
BASE_WEB_URL = "https://ccrma.stanford.edu/~pdelac/" # ¡¡¡IMPORTANTE: MODIFICA ESTO!!!
# Guarded block to define and save initial RSS sources as a JSON file
import json
fuentes_rss = {
    "internacional": [
        {"name": "France24 Español", "url": "https://www.france24.com/es/rss"},
        {"name": "Le Monde International", "url": "https://www.lemonde.fr/rss/en_continu.xml"},
        {"name": "BBC Mundo", "url": "https://feeds.bbci.co.uk/mundo/rss.xml"},
        {"name": "Reuters Top News", "url": "http://feeds.reuters.com/reuters/topNews"},
        {"name": "Le Figaro", "url": "https://www.lefigaro.fr/rss/figaro_actualites.xml"}
    ],
    "nacional": [
        {"name": "La Tercera", "url": "https://www.latercera.com/rss/"},
        {"name": "CIPER Chile", "url": "https://www.ciperchile.cl/feed/"}
    ],
    "opinion_ensayo": [
        {"name": "Jacobin América Latina", "url": "https://jacobinlat.com/feed/"},
        {"name": "The Guardian Opinion", "url": "https://www.theguardian.com/uk/commentisfree/rss"},
        {"name": "El País Opinión", "url": "https://feeds.elpais.com/mrss-s/pages/ep/site/elpais.com/section/opinion/portada"}
    ],
    "ciencia_tecnologia": [
        {"name": "Sciences et Avenir", "url": "https://www.sciencesetavenir.fr/rss.xml"},
        {"name": "Nature Current", "url": "http://feeds.nature.com/nature/rss/current?x=1"}
    ],
    "cultura_arte": [
        {"name": "The Guardian Culture", "url": "https://www.theguardian.com/uk/culture/rss"},
        {"name": "El País Cultura", "url": "https://feeds.elpais.com/mrss-s/pages/ep/site/elpais.com/section/cultura/portada"},
        {"name": "Revista Ñ – Clarín Cultura", "url": "https://www.clarin.com/rss/cultura/"}
    ]
}

# Guardar las fuentes RSS en un archivo JSON. Esto se ejecuta cada vez.
try:
    with open(FUENTES_RSS_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(fuentes_rss, f, ensure_ascii=False, indent=4)
    print(f"Archivo {FUENTES_RSS_JSON_PATH} guardado exitosamente.")
except IOError as e:
    print(f"Error al guardar el archivo {FUENTES_RSS_JSON_PATH}: {e}")

# --- Función para cargar fuentes desde JSON ---
def cargar_fuentes_desde_json():
    with open(FUENTES_RSS_JSON_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

# --- Función para resumir artículos usando Gemini ---
def resumir_y_puntuar_con_gemini(model, titulo, contenido, categoria):
    """
    Resume un artículo y le asigna una puntuación de relevancia utilizando la API de Gemini.
    El 'model' de Gemini debe ser preinicializado y pasado como argumento.
    Devuelve un diccionario con 'teaser_sentence', 'resumen', 'relevancia_score', 'relevancia_justificacion' o None.
    """
    prompt = f"""
Analiza el siguiente artículo.
Título: {titulo}
Contenido:
{contenido}

Tu tarea es:
1.  Crea una frase única y concisa (máximo 15-25 palabras) que sirva como un "gancho" o "teaser" del artículo.
2.  Resume brevemente el artículo en español (aproximadamente 100-150 palabras). Asegúrate de que el resumen sea un texto plano válido, escapando correctamente cualquier carácter especial si es necesario para JSON.
3.  Evalúa la relevancia e interés general del artículo para un lector interesado en la categoría '{categoria.replace('_', ' ').title()}', en una escala numérica del 1 (poco relevante/interesante) al 10 (muy relevante/interesante).
4.  Proporciona una frase breve justificando tu puntuación de relevancia. Asegúrate de que la justificación sea un texto plano válido, escapando correctamente cualquier carácter especial si es necesario para JSON.

Proporciona tu respuesta ESTRICTAMENTE en el siguiente formato JSON. No incluyas ningún texto antes o después del bloque JSON.
{{
  "teaser_sentence": "Tu frase gancho aquí...",
  "resumen": "Tu resumen aquí...",
  "relevancia_score": <tu puntuación numérica entera entre 1 y 10>,
  "relevancia_justificacion": "Una frase breve justificando tu puntuación de relevancia."
}}
"""
    try:
        print(f"    📝 Solicitando resumen y puntuación para: {titulo} (Categoría: {categoria})")
        response = model.generate_content(prompt)
        raw_text = response.text

        json_start = raw_text.find('{')
        json_end = raw_text.rfind('}')
        if json_start != -1 and json_end != -1 and json_end > json_start:
            json_text = raw_text[json_start:json_end+1]
        else:
            json_text = raw_text

        return json.loads(json_text) # Confía en que Gemini devuelve JSON válido si se especifica response_mime_type
    except json.JSONDecodeError as e:
        print(f"    ❌ Error al decodificar JSON de Gemini para '{titulo}': {e}. Respuesta recibida: {response.text[:200]}...")
        return None
    except Exception as e:
        print(f"    ❌ Error al resumir y puntuar con Gemini para '{titulo}': {e}")
        return None


def extraer_contenido(url):
    try:
        headers = {
            'User-Agent': USER_AGENT
        }
        articulo = Article(url, browser_user_agent=USER_AGENT, headers=headers)
        articulo.download()
        articulo.parse()
        return articulo.text
    except Exception as e:
        print(f"Error al extraer contenido del artículo ({url}): {e}")
        return ""


def obtener_articulos_recientes(rss_url, horas):
    """
    Obtiene artículos de un feed RSS publicados en las últimas 'horas'.
    """
    print(f"\nFetching RSS feed: {rss_url}")
    feed = feedparser.parse(rss_url, agent=USER_AGENT)
    articulos_recientes = []

    if feed.bozo:
        bozo_type = feed.bozo_exception.__class__.__name__
        bozo_message = str(feed.bozo_exception)
        print(f"⚠️ Warning: Feed {rss_url} may be malformed. Type: {bozo_type}, Message: {bozo_message}")
    
    if not feed.entries:
        print(f"ℹ️ No entries found in feed: {rss_url}")
        return articulos_recientes

    print(f"  Found {len(feed.entries)} total entries in {rss_url}.")

    ahora_utc = datetime.now(timezone.utc)
    limite_tiempo_utc = ahora_utc - timedelta(hours=horas)

    for entry in feed.entries:
        fecha_articulo_utc = None
        date_struct = None

        if hasattr(entry, 'published_parsed') and entry.published_parsed:
            date_struct = entry.published_parsed
        elif hasattr(entry, 'updated_parsed') and entry.updated_parsed:
            date_struct = entry.updated_parsed
        
        if date_struct:
            try:
                fecha_articulo_naive = datetime(
                    date_struct.tm_year, date_struct.tm_mon, date_struct.tm_mday,
                    date_struct.tm_hour, date_struct.tm_min, date_struct.tm_sec,
                )
                fecha_articulo_utc = fecha_articulo_naive.replace(tzinfo=timezone.utc)

                if fecha_articulo_utc > limite_tiempo_utc:
                    articulos_recientes.append({
                        "titulo": entry.get("title", "[sin título]"),
                        "link": entry.get("link", "[sin link]"),
                        "fecha_obj": fecha_articulo_utc, # datetime object para ordenar
                        "fecha_str": fecha_articulo_utc.strftime("%Y-%m-%d %H:%M:%S UTC") # string para mostrar
                    })
            except Exception as e:
                print(f"    - Error processing date for entry '{entry.get('title', '[sin título]')}': {e}")
            
    print(f"  Found {len(articulos_recientes)} recent articles from {rss_url} (last {horas} hours).")
    return articulos_recientes

def procesar_y_resumir_articulos(fuentes, gemini_model):
    def generate_html_content(processed_articles_by_category, report_type):
        html_content = f"""
    <!DOCTYPE html>
    <html lang="es">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Resumen {report_type.title()} de Noticias</title>
        <style>
            body {{ font-family: 'Segoe UI', Roboto, Oxygen, Ubuntu, Cantarell, 'Open Sans', 'Helvetica Neue', sans-serif; line-height: 1.6; margin: 0; padding: 20px; background-color: #f8f9fa; color: #212529; }}
            .container {{ max-width: 900px; margin: 20px auto; background-color: #ffffff; padding: 25px; border-radius: 8px; box-shadow: 0 4px 6px rgba(0,0,0,0.1); }}
            h1 {{ color: #343a40; text-align: center; margin-bottom: 30px; font-weight: 600; }}
            h2 {{ color: #495057; border-bottom: 2px solid #dee2e6; padding-bottom: 10px; margin-top: 30px; margin-bottom: 20px; font-weight: 500; }}
            h3 {{ color: #6c757d; margin-top: 25px; margin-bottom: 15px; font-weight: 500; }}
            details {{ background-color: #ffffff; border: 1px solid #e9ecef; border-radius: 6px; margin-bottom: 15px; padding: 15px; box-shadow: 0 2px 4px rgba(0,0,0,0.05); }}
            details[open] {{ background-color: #fdfdff; }}
            summary {{ font-weight: 600; cursor: pointer; color: #0056b3; font-size: 1.1em; margin-bottom: 5px; list-style-position: inside; }}
            summary:hover {{ color: #003d82; }}
            .article-content-wrapper {{ padding-top: 10px; }}
            .article-title {{ font-size: 1.15em; font-weight: bold; color: #343a40; margin-bottom: 5px; }}
            .article-meta {{ font-size: 0.85em; color: #6c757d; margin-bottom: 8px; }}
            .article-summary {{ margin-top: 10px; color: #495057; }}
            a {{ color: #007bff; text-decoration: none; }}
            a:hover {{ text-decoration: underline; }}
            .no-articles {{ font-style: italic; color: #6c757d; margin-left: 10px; }}
        </style>
    </head>
    <body>
        <div class="container">
            <h1>Resumen {report_type.title()} de Noticias</h1>"""
        
        if not processed_articles_by_category:
            html_content += "<p class='no-articles'><i>No se procesaron artículos para ninguna categoría.</i></p>"
        else:
            for categoria, articulos_procesados_list in processed_articles_by_category.items():
                html_content += f"<h2>{categoria.replace('_', ' ').title()}</h2>"
                
                if not articulos_procesados_list:
                    html_content += f"<p class='no-articles'><i>No se encontraron artículos procesados para la categoría {categoria.replace('_', ' ').title()}.</i></p>"
                else:
                    for item_procesado in articulos_procesados_list:
                        articulo_info = item_procesado["info"]
                        resumen_datos = item_procesado["resumen_datos"]
                        html_content += f"""
                                <details>
                                    <summary>
                                        {resumen_datos['teaser_sentence']} (Puntuación: {resumen_datos['relevancia_score']}/10)
                                    </summary>
                                    <div class="article-content-wrapper">
                                        <p class="article-title">{articulo_info['titulo']}</p>
                                        <p class="article-meta">
                                            Fuente: {articulo_info['source_name']} | Fecha: {articulo_info.get('fecha_str', 'No disp.')}
                                        </p>
                                        <p class="article-summary">{resumen_datos['resumen']}</p>
                                        <a href='{articulo_info['link']}' target='_blank'>Leer más en la fuente original</a>
                                    </div>
                                </details>
                        """

        html_content += """
        </div>
    </body>
    </html>
    """
        return html_content



    # --- Lógica principal de procesamiento ---
    # Esta parte necesita ser modificada para recopilar todos los artículos primero
    # y luego procesarlos por categoría.

    # Recopilar todos los artículos recientes de todas las fuentes, etiquetados por fuente
    all_articles_by_category = {}
    for categoria, lista_fuentes in fuentes.items():
        all_articles_this_category = []
        print(f"\n📚 Recopilando artículos para la categoría: {categoria.replace('_', ' ').title()}")
        for fuente in lista_fuentes:
            # obtener_articulos_recientes ya imprime su progreso detallado
            # Usamos 'horas_a_revisar' que se definirá en el bloque __main__
            articulos_de_fuente = obtener_articulos_recientes(fuente["url"], horas=horas_a_revisar) 
            for art in articulos_de_fuente:
                art['source_name'] = fuente['name'] # Etiquetar con el nombre de la fuente
                all_articles_this_category.append(art)
        all_articles_by_category[categoria] = all_articles_this_category
        print(f"  📰 Total de artículos recientes encontrados en '{categoria.replace('_', ' ').title()}': {len(all_articles_this_category)}")

    # Procesar y resumir los N artículos principales por categoría
    processed_articles_by_category = {}
    any_article_processed_overall = False # Reset flag for processing stage

    for categoria, all_articles_this_category in all_articles_by_category.items():
        print(f"\n✨ Procesando artículos para la categoría: {categoria.replace('_', ' ').title()}")
        
        # Ordenar todos los artículos de la categoría por fecha (más recientes primero)
        all_articles_this_category.sort(key=lambda x: x['fecha_obj'], reverse=True)

        # Seleccionar los N artículos principales para resumir para esta categoría
        articles_to_summarize_for_category = all_articles_this_category[:MAX_ARTICLES_TO_SUMMARIZE_PER_CATEGORY]
        print(f"  🎯 Seleccionados para resumir en '{categoria.replace('_', ' ').title()}': {len(articles_to_summarize_for_category)} artículos (límite: {MAX_ARTICLES_TO_SUMMARIZE_PER_CATEGORY})")

        articulos_procesados_final_categoria = []
        
        for i, articulo_para_resumir in enumerate(articles_to_summarize_for_category):
            print(f"  🔄 Procesando ({i+1}/{len(articles_to_summarize_for_category)}) '{articulo_para_resumir['titulo']}' de {articulo_para_resumir['source_name']}...")
            
            if not articulo_para_resumir["link"] or articulo_para_resumir["link"] == "[sin link]":
                print(f"    ⚠️ Saltando artículo con enlace faltante: {articulo_para_resumir['titulo']}")
                continue

            contenido = extraer_contenido(articulo_para_resumir["link"])
            if not contenido:
                print(f"    ⚠️ No se pudo extraer contenido de: {articulo_para_resumir['link']}")
                continue
            
            time.sleep(1.5) # Límite de tasa de API
            datos_gemini = resumir_y_puntuar_con_gemini(gemini_model, articulo_para_resumir["titulo"], contenido, categoria)

            if datos_gemini and \
               datos_gemini.get("teaser_sentence") and \
               datos_gemini.get("resumen") and isinstance(datos_gemini.get("relevancia_score"), int):
                articulos_procesados_final_categoria.append({
                    "info": articulo_para_resumir, # Contiene titulo, link, fecha_obj, fecha_str, source_name
                    "resumen_datos": datos_gemini
                })
                any_article_processed_overall = True
            else:
                print(f"    ⚠️ Gemini no devolvió datos válidos para: {articulo_para_resumir['titulo']}")

        # Ordenar artículos procesados por puntuación de relevancia
        articulos_procesados_final_categoria.sort(key=lambda x: x["resumen_datos"].get("relevancia_score", 0), reverse=True)
        processed_articles_by_category[categoria] = articulos_procesados_final_categoria

    # Generar HTML y enviar correo
    report_type = "Semanal" if horas_a_revisar == DEFAULT_WEEKLY_HOURS else "Diario"
    html_content = generate_html_content(processed_articles_by_category, report_type)

    if not any_article_processed_overall: # Check the flag after processing all categories
        print("ℹ️ No se procesó ningún artículo para el resumen final.")
        
    try:
        with open(OUTPUT_HTML_FILE_PATH, "w", encoding="utf-8") as f:
            f.write(html_content)
        
        full_web_url = BASE_WEB_URL + OUTPUT_HTML_FILE_NAME
        print(f"📄 Resumen {report_type} interactivo guardado en: {OUTPUT_HTML_FILE_PATH}")

        # Intentar subir el archivo automáticamente
        usuario_stanford = "pdelac"  # ¡¡¡IMPORTANTE: MODIFICA ESTO con tu nombre de usuario en Stanford!!!
        host_stanford = "ccrma-gate.stanford.edu" # Host para SCP
        directorio_web_remoto_base = "Library/Web" # RUTA A TU DIRECTORIO WEB PÚBLICO EN STANFORD

        subida_exitosa = subir_archivo_con_scp(
            OUTPUT_HTML_FILE_PATH,
            usuario_stanford,
            host_stanford,
            directorio_web_remoto_base,
            OUTPUT_HTML_FILE_NAME
        )

        email_subject = f"Resumen {report_type} de Noticias Interactivo"        
        if not any_article_processed_overall:
            email_subject += " (Sin artículos nuevos)"

        email_body = f"""
        Hola,

        Tu resumen de noticias interactivo está listo.
        El archivo ha sido guardado localmente en: {OUTPUT_HTML_FILE_PATH}
        """

        if subida_exitosa:
            email_body += f"""

        ¡El archivo ha sido subido automáticamente a tu servidor!
        Puedes verlo directamente en: {full_web_url}
        """
        else:
            email_body += f"""

        ⚠️ La subida automática del archivo a tu servidor falló.
        Para subirlo a tu servidor de Stanford, puedes usar un comando similar a este desde tu terminal:
        scp {OUTPUT_HTML_FILE_PATH} {usuario_stanford}@ccrma-gate.stanford.edu:{directorio_web_remoto_base}/{OUTPUT_HTML_FILE_NAME}
        
        Una vez subido, podrás verlo en: {full_web_url}
        (Recuerda que si el archivo {OUTPUT_HTML_FILE_NAME} ya existe en {directorio_web_remoto_base}, scp lo sobrescribirá.)
        """
        email_body += """

        Saludos,
        Tu Compilador de Noticias
        """
        enviar_correo_con_enlace(email_subject, email_body)

    except IOError as e:
        print(f"❌ Error al guardar el archivo HTML: {e}")

def subir_archivo_con_scp(archivo_local, usuario_remoto, host_remoto, ruta_remota_base, nombre_archivo_remoto):
    ruta_completa_remota_destino = f"{ruta_remota_base}/{nombre_archivo_remoto}"
    
    comando_scp = [
        "scp",
        "-o", "BatchMode=yes", # Evita prompts interactivos de contraseña si la clave SSH falla
        "-o", "ConnectTimeout=10", # Timeout para la conexión
        archivo_local,
        f"{usuario_remoto}@{host_remoto}:{ruta_completa_remota_destino}"
    ]
    try:
        print(f"🚀 Intentando subir {archivo_local} a {host_remoto}...")
        print(f"   Comando: {' '.join(comando_scp)}")
        proceso = subprocess.run(comando_scp, check=True, capture_output=True, text=True, timeout=60)
        print(f"✅ Archivo subido exitosamente a {host_remoto}:{ruta_completa_remota_destino}")
        if proceso.stdout: print(f"   Salida de SCP (stdout): {proceso.stdout.strip()}")
        if proceso.stderr: print(f"   Salida de SCP (stderr): {proceso.stderr.strip()}") # scp a veces usa stderr para mensajes de éxito
        return True
    except subprocess.CalledProcessError as e:
        print(f"❌ Error al subir archivo con SCP (Código de retorno: {e.returncode}):")
        if e.stdout: print(f"   Salida Estándar: {e.stdout.strip()}")
        if e.stderr: print(f"   Salida de Error: {e.stderr.strip()}")
        return False
    except FileNotFoundError:
        print("❌ Error: El comando 'scp' no se encontró. Asegúrate de que esté instalado y en tu PATH.")
        return False
    except subprocess.TimeoutExpired:
        print("❌ Error: El comando SCP tardó demasiado tiempo (timeout).")
        return False

def enviar_correo_con_enlace(subject, body_text):
    remitente = "patodelac@gmail.com"
    destinatario = "patodelac@gmail.com"
    msg = MIMEText(body_text)
    msg["Subject"] = subject
    msg["From"] = remitente
    msg["To"] = destinatario

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            gmail_app_password = os.getenv("GMAIL_APP_PASSWORD")
            if not gmail_app_password:
                print("❌ Error: La variable de entorno GMAIL_APP_PASSWORD no está configurada. No se puede enviar correo.")
                return
            server.login(remitente, gmail_app_password)
            server.sendmail(remitente, destinatario, msg.as_string())
            print("📧 Correo con enlace enviado exitosamente.")
    except Exception as e:
        print(f"❌ Error al enviar correo con enlace: {e}")

if __name__ == "__main__":
    # --- Configuración de argumentos de línea de comandos ---
    parser = argparse.ArgumentParser(description="Compila, resume y puntúa noticias de feeds RSS.")
    parser.add_argument(
        "-w", "--weekly", action="store_true",
        help=f"Genera un reporte semanal (últimas {DEFAULT_WEEKLY_HOURS} horas) en lugar del reporte diario (últimas {DEFAULT_HOURS_AGO} horas)."
    )
    args = parser.parse_args()

    horas_a_revisar = DEFAULT_WEEKLY_HOURS if args.weekly else DEFAULT_HOURS_AGO
    print(f"Configurado para revisar noticias de las últimas {horas_a_revisar} horas.")

    # --- Configuración de Gemini ---
    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
    if not GEMINI_API_KEY:
        print("❌ Error CRÍTICO: La variable de entorno GEMINI_API_KEY no está configurada.")
        print("   Por favor, configura la variable de entorno GEMINI_API_KEY para continuar.")
        print("   Ejemplo: export GEMINI_API_KEY=\"tu_clave_api_de_gemini\"")
        exit(1) # Termina el script si la clave no está.

    try:
        genai.configure(api_key=GEMINI_API_KEY)
        gemini_generative_model = genai.GenerativeModel(
            'gemini-1.5-flash-latest',
            generation_config=genai.types.GenerationConfig(
                max_output_tokens=450, # Ajusta según necesidad, pero el prompt pide ~150 palabras + teaser + justificación
                temperature=0.5,
                response_mime_type="application/json"
            )
        )
        print("✅ Modelo Gemini inicializado correctamente.")
    except Exception as e:
        print(f"❌ Error CRÍTICO al inicializar el modelo de Gemini: {e}")
        print("   Verifica tu API Key y la configuración de la librería google-generativeai.")
        exit(1) # Termina el script si el modelo no se puede inicializar.

    fuentes = cargar_fuentes_desde_json()
    procesar_y_resumir_articulos(fuentes, gemini_generative_model) # Pasa horas_a_revisar implícitamente a obtener_articulos_recientes

# === INSTRUCCIÓN IMPORTANTE ===
# Para que el envío de email funcione, debes crear una contraseña de aplicación en tu cuenta de Gmail
# y guardarla como variable de entorno:
#   export GMAIL_APP_PASSWORD="tu_clave_de_aplicacion"
#
# Para que la generación de resúmenes con Gemini funcione, necesitas una API Key de Google AI Studio
# y guardarla como variable de entorno:
#   export GEMINI_API_KEY="tu_clave_api_de_gemini"
#
# Para la subida automática por SCP, asegúrate de tener configurada la autenticación por clave SSH
# entre tu máquina local y ccrma.stanford.edu para el usuario que configures en la variable 'usuario_stanford'.
# Si tu clave SSH privada tiene contraseña, asegúrate de que ssh-agent la esté gestionando.
