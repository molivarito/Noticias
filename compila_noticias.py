# --- Envío de resúmenes por email ---
import subprocess
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

# --- Para generación de resúmenes automáticos con IA Generativa ---
import argparse # Importar argparse
import google.generativeai as genai
import os
import time

# --- Para parseo de feeds RSS y manejo de fechas ---
import feedparser
from datetime import datetime, timedelta, timezone

# --- Para extracción de contenido de artículos ---
from newspaper import Article

# --- SSL Configuration for macOS and certifi ---
import ssl
import certifi
# --- Para cargar variables de entorno desde .env (útil para desarrollo local) ---
from dotenv import load_dotenv

load_dotenv() # Carga variables desde un archivo .env en el mismo directorio

try:
    # Configure Python's SSL context to use certifi's CA bundle.
    ssl_context = ssl.create_default_context(cafile=certifi.where())
    ssl._create_default_https_context = lambda: ssl_context
    print("✅ SSL context configured to use certifi's CA bundle.")
except Exception as e:
    print(f"⚠️ Warning: Could not configure SSL context with certifi: {e}")
    print("   Attempting to proceed without custom SSL context for certifi. SSL errors might persist.")

# --- Constantes ---
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__)) # Directorio donde se encuentra el script
FUENTES_RSS_JSON_PATH = os.path.join(SCRIPT_DIR, "fuentes_rss.json")
DEFAULT_HOURS_AGO = 24
DEFAULT_WEEKLY_HOURS = 7 * 24 # 7 días en horas
USER_AGENT = "NewsAggregatorBot/1.0 (+http://example.com/botinfo)"
MAX_ARTICLES_TO_SUMMARIZE_PER_CATEGORY = 5 # Límite de artículos a resumir por categoría

# Leer la URL base del servidor desde una variable de entorno.
# El workflow de GitHub Actions la proveerá desde un secret o un valor por defecto.
# Para GitHub Pages, el workflow pasa la URL completa.
github_repo_owner = os.getenv("GITHUB_REPOSITORY_OWNER")
github_repo_name = os.getenv("GITHUB_REPOSITORY_NAME")

if github_repo_owner and github_repo_name:
    default_gh_pages_url = f"https://{github_repo_owner}.github.io/{github_repo_name}/"
else:
    default_gh_pages_url = "https://tu-usuario.github.io/tu-repositorio/" # Placeholder si no se ejecuta en Actions
BASE_WEB_URL = os.getenv("BASE_WEB_URL", default_gh_pages_url)

import json

# --- Función para cargar fuentes desde JSON ---
def cargar_fuentes_desde_json():
    try:
        with open(FUENTES_RSS_JSON_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        print(f"❌ Error CRÍTICO: No se encontró el archivo de fuentes RSS en {FUENTES_RSS_JSON_PATH}")
        print("   Asegúrate de que el archivo 'fuentes_rss.json' exista en el mismo directorio que el script.")
        exit(1) # Termina el script si el archivo de fuentes no existe.
    except json.JSONDecodeError as e:
        print(f"❌ Error CRÍTICO: El archivo de fuentes RSS en {FUENTES_RSS_JSON_PATH} no es un JSON válido: {e}")
        exit(1) # Termina el script si el JSON es inválido.

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

        # Intenta extraer el JSON incluso si hay texto adicional antes/después
        json_start = raw_text.find('{')
        json_end = raw_text.rfind('}')
        if json_start != -1 and json_end != -1 and json_end > json_start:
            json_text = raw_text[json_start:json_end+1]
        else:
            json_text = raw_text # Asume que es JSON si no se encuentran los delimitadores

        return json.loads(json_text)
    except json.JSONDecodeError as e:
        print(f"    ❌ Error al decodificar JSON de Gemini para '{titulo}': {e}. Respuesta recibida: {raw_text[:200]}...")
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

def procesar_y_resumir_articulos(fuentes, gemini_model, horas_a_revisar):
    def generate_html_content(processed_articles_by_category, report_type, generation_timestamp_str):
        # Iconos Unicode para categorías (puedes personalizarlos)
        category_icons = {
            "internacional": "🌍",
            "nacional": "🇨🇱", # O un icono más genérico como 📰
            "opinion_ensayo": "✍️",
            "ciencia_tecnologia": "🔬",
            "cultura_arte": "🎨",
            "default": "🔹" # Icono por defecto
        }

        html_content = f"""
    <!DOCTYPE html>
    <html lang="es">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <link href="https://fonts.googleapis.com/css2?family=Montserrat:wght@400;600;700&family=Roboto:wght@400;700&display=swap" rel="stylesheet">
        <title>Resumen {report_type.title()} de Noticias</title>
        <style>
            body {{ 
                font-family: 'Roboto', sans-serif; 
                line-height: 1.6; margin: 0; padding: 20px; 
                background-color: #f4f7f6; /* Fondo ligeramente más cálido */
                color: #333; 
            }}
            .container {{ max-width: 900px; margin: 30px auto; background-color: #ffffff; padding: 30px; border-radius: 10px; box-shadow: 0 6px 12px rgba(0,0,0,0.08); }}
            h1 {{ font-family: 'Montserrat', sans-serif; color: #2c3e50; text-align: center; margin-bottom: 15px; font-weight: 700; font-size: 2.2em; }}
            .report-timestamp {{ text-align: center; font-size: 0.9em; color: #555; margin-top: -10px; margin-bottom: 35px; }}
            h2 {{ font-family: 'Montserrat', sans-serif; color: #34495e; border-bottom: 3px solid #1abc9c; /* Color de acento */ padding-bottom: 12px; margin-top: 40px; margin-bottom: 25px; font-weight: 600; font-size: 1.8em; }}
            details {{ background-color: #fdfdfd; border: 1px solid #e0e0e0; border-radius: 8px; margin-bottom: 20px; padding: 20px; box-shadow: 0 3px 6px rgba(0,0,0,0.04); transition: box-shadow 0.3s ease; }}
            details:hover {{ box-shadow: 0 5px 10px rgba(0,0,0,0.06); }}
            details[open] {{ background-color: #ffffff; border-left: 5px solid #1abc9c; /* Color de acento al abrir */ }}
            summary {{ font-family: 'Montserrat', sans-serif; font-weight: 600; cursor: pointer; color: #2980b9; /* Azul más vibrante */ font-size: 1.15em; margin-bottom: 8px; list-style-position: inside; outline: none; }}
            summary:hover {{ color: #1f618d; }}
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
            <h1>Resumen {report_type.title()} de Noticias</h1>
            <p class="report-timestamp">Generado el: {generation_timestamp_str}</p>
            """
        
        if not processed_articles_by_category:
            html_content += "<p class='no-articles'><i>No se procesaron artículos para ninguna categoría.</i></p>"
        else:
            for categoria, articulos_procesados_list in processed_articles_by_category.items():
                icon = category_icons.get(categoria, category_icons["default"])
                html_content += f"<h2>{icon} {categoria.replace('_', ' ').title()}</h2>"
                
                if not articulos_procesados_list:
                    html_content += f"<p class='no-articles'><i>No se encontraron artículos procesados para la categoría {icon} {categoria.replace('_', ' ').title()}.</i></p>"
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
                                            Fuente: {articulo_info['source_name']} | Fecha: {articulo_info.get('fecha_str', 'No disp.')} | Justificación: {resumen_datos.get('relevancia_justificacion', 'N/A')}
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

    all_articles_by_category = {}
    for categoria, lista_fuentes in fuentes.items():
        all_articles_this_category = []
        print(f"\n📚 Recopilando artículos para la categoría: {categoria.replace('_', ' ').title()}")
        for fuente in lista_fuentes:
            articulos_de_fuente = obtener_articulos_recientes(fuente["url"], horas=horas_a_revisar) 
            for art in articulos_de_fuente:
                art['source_name'] = fuente['name']
                all_articles_this_category.append(art)
        all_articles_by_category[categoria] = all_articles_this_category
        print(f"  📰 Total de artículos recientes encontrados en '{categoria.replace('_', ' ').title()}': {len(all_articles_this_category)}")

    processed_articles_by_category = {}
    any_article_processed_overall = False

    for categoria, all_articles_this_category in all_articles_by_category.items():
        print(f"\n✨ Procesando artículos para la categoría: {categoria.replace('_', ' ').title()}")
        all_articles_this_category.sort(key=lambda x: x['fecha_obj'], reverse=True)
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
            time.sleep(1.5)
            datos_gemini = resumir_y_puntuar_con_gemini(gemini_model, articulo_para_resumir["titulo"], contenido, categoria)
            if datos_gemini and \
               datos_gemini.get("teaser_sentence") and \
               datos_gemini.get("resumen") and isinstance(datos_gemini.get("relevancia_score"), int):
                articulos_procesados_final_categoria.append({
                    "info": articulo_para_resumir,
                    "resumen_datos": datos_gemini
                })
                any_article_processed_overall = True
            else:
                print(f"    ⚠️ Gemini no devolvió datos válidos para: {articulo_para_resumir['titulo']}")
        articulos_procesados_final_categoria.sort(key=lambda x: x["resumen_datos"].get("relevancia_score", 0), reverse=True)
        processed_articles_by_category[categoria] = articulos_procesados_final_categoria

    report_type = "Semanal" if horas_a_revisar == DEFAULT_WEEKLY_HOURS else "Diario"

    # Determinar el nombre del archivo de salida según el tipo de reporte
    if report_type == "Semanal":
        output_html_file_name = "resumen_semanal.html"
    else:
        # Usar index.html para el reporte diario para que sea la página principal del sitio
        output_html_file_name = "index.html"
    output_html_file_path = os.path.join(SCRIPT_DIR, output_html_file_name)

    # Obtener la fecha y hora actual para el timestamp
    generation_timestamp = datetime.now(timezone.utc)
    generation_timestamp_str = generation_timestamp.strftime("%d de %B de %Y, %H:%M:%S UTC")
    html_content = generate_html_content(processed_articles_by_category, report_type, generation_timestamp_str)
    if not any_article_processed_overall:
        print("ℹ️ No se procesó ningún artículo para el resumen final.")

    try:
        with open(output_html_file_path, "w", encoding="utf-8") as f:
            f.write(html_content)

        full_web_url = BASE_WEB_URL + output_html_file_name
        print(f"📄 Resumen {report_type} interactivo guardado en: {output_html_file_path}")

        # Ya no se usa SCP, la subida la hace GitHub Actions a GitHub Pages
        print(f"ℹ️  El archivo se desplegará en GitHub Pages: {full_web_url}")

        email_subject = f"Resumen {report_type} de Noticias Interactivo"        
        if not any_article_processed_overall:
            email_subject += " (Sin artículos nuevos)"

        email_body = f"""
        Hola,

        Tu resumen de noticias {report_type.lower()} está listo.
        Puedes verlo directamente en GitHub Pages: {full_web_url}
        """
        email_body += """

        Saludos,
        Tu Compilador de Noticias
        """
        enviar_correo_con_enlace(email_subject, email_body)

    except IOError as e:
        print(f"❌ Error al guardar el archivo HTML: {e}")

# def subir_archivo_con_scp(archivo_local, usuario_remoto, host_remoto, ruta_remota_base, nombre_archivo_remoto):
#     ruta_completa_remota_destino = f"{ruta_remota_base}/{nombre_archivo_remoto}"
#     comando_scp = [
#         "scp",
#         "-o", "BatchMode=yes",
#         "-o", "ConnectTimeout=10",
#         archivo_local,
#         f"{usuario_remoto}@{host_remoto}:{ruta_completa_remota_destino}"
#     ]
#     try:
#         print(f"🚀 Intentando subir {archivo_local} a {host_remoto}...")
#         print(f"   Comando: {' '.join(comando_scp)}")
#         proceso = subprocess.run(comando_scp, check=True, capture_output=True, text=True, timeout=60)
#         print(f"✅ Archivo subido exitosamente a {host_remoto}:{ruta_completa_remota_destino}")
#         if proceso.stdout: print(f"   Salida de SCP (stdout): {proceso.stdout.strip()}")
#         if proceso.stderr: print(f"   Salida de SCP (stderr): {proceso.stderr.strip()}")
#         return True
#     except subprocess.CalledProcessError as e:
#         print(f"❌ Error al subir archivo con SCP (Código de retorno: {e.returncode}):")
#         if e.stdout: print(f"   Salida Estándar: {e.stdout.strip()}")
#         if e.stderr: print(f"   Salida de Error: {e.stderr.strip()}")
#         return False
#     except FileNotFoundError:
#         print("❌ Error: El comando 'scp' no se encontró. Asegúrate de que esté instalado y en tu PATH.")
#         return False
#     except subprocess.TimeoutExpired:
#         print("❌ Error: El comando SCP tardó demasiado tiempo (timeout).")
#         return False

def enviar_correo_con_enlace(subject, body_text):
    remitente = os.getenv("GMAIL_USER", "tu_email@gmail.com") # Lee de variable de entorno o usa un default
    destinatario = os.getenv("GMAIL_DESTINATARIO", remitente) # Lee de variable o envía a remitente
    
    gmail_app_password = os.getenv("GMAIL_APP_PASSWORD")
    if not gmail_app_password:
        print("❌ Error: La variable de entorno GMAIL_APP_PASSWORD no está configurada. No se puede enviar correo.")
        return
    if not remitente or remitente == "tu_email@gmail.com":
        print("❌ Error: La variable de entorno GMAIL_USER no está configurada o usa el valor por defecto. No se puede enviar correo.")
        return


    msg = MIMEText(body_text)
    msg["Subject"] = subject
    msg["From"] = remitente
    msg["To"] = destinatario

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(remitente, gmail_app_password)
            server.sendmail(remitente, destinatario, msg.as_string())
            print("📧 Correo con enlace enviado exitosamente.")
    except Exception as e:
        print(f"❌ Error al enviar correo con enlace: {e}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Compila, resume y puntúa noticias de feeds RSS.")
    parser.add_argument(
        "-w", "--weekly", action="store_true",
        help=f"Genera un reporte semanal (últimas {DEFAULT_WEEKLY_HOURS} horas) en lugar del reporte diario (últimas {DEFAULT_HOURS_AGO} horas)."
    )
    args = parser.parse_args()

    horas_a_revisar = DEFAULT_WEEKLY_HOURS if args.weekly else DEFAULT_HOURS_AGO
    report_type_str = "Semanal" if args.weekly else "Diario"
    print(f"🚀 Iniciando compilador de noticias para el reporte {report_type_str} (últimas {horas_a_revisar} horas).")

    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
    if not GEMINI_API_KEY:
        print("❌ Error CRÍTICO: La variable de entorno GEMINI_API_KEY no está configurada.")
        exit(1)

    try:
        genai.configure(api_key=GEMINI_API_KEY)
        gemini_generative_model = genai.GenerativeModel(
            'gemini-1.5-flash-latest',
            generation_config=genai.types.GenerationConfig(
                max_output_tokens=450,
                temperature=0.5,
                response_mime_type="application/json"
            )
        )
        print("✅ Modelo Gemini inicializado correctamente.")
    except Exception as e:
        print(f"❌ Error CRÍTICO al inicializar el modelo de Gemini: {e}")
        exit(1)

    fuentes = cargar_fuentes_desde_json()
    procesar_y_resumir_articulos(fuentes, gemini_generative_model, horas_a_revisar)

# === INSTRUCCIÓN IMPORTANTE ===
# Para que este script funcione correctamente, especialmente en entornos automatizados como GitHub Actions:
# 1. Asegúrate de que el archivo 'fuentes_rss.json' esté presente en el mismo directorio que este script.
# 2. Configura las siguientes variables de entorno (o "secrets" en GitHub Actions):
#    - GEMINI_API_KEY: Tu clave API de Google Gemini.
#    - GMAIL_APP_PASSWORD: Tu contraseña de aplicación de Gmail.
#    - GMAIL_USER: Tu dirección de correo de Gmail (remitente).
#    - GMAIL_DESTINATARIO: (Opcional) Dirección de correo del destinatario, si es diferente al remitente.
#    - BASE_WEB_URL: (Automáticamente provista por el workflow para GitHub Pages)
#    - GITHUB_REPOSITORY_OWNER: (Automáticamente provista por el workflow)
#    - GITHUB_REPOSITORY_NAME: (Automáticamente provista por el workflow)
# 
#  Variables de SCP ya no son necesarias si solo usas GitHub Pages:
#    - STANFORD_USER, STANFORD_HOST_SCP, STANFORD_REMOTE_PATH, STANFORD_SSH_PRIVATE_KEY
# Para desarrollo local, puedes crear un archivo '.env' en el mismo directorio y definir estas variables allí.
# La línea 'load_dotenv()' al principio del script cargará estas variables.
