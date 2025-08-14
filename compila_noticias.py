# --- LIBRERÍAS ESTÁNDAR Y EXTERNAS ---
import smtplib
import argparse
import os
import time
import json
import ssl
import certifi
from pathlib import Path
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText

# --- LIBRERÍAS DE TERCEROS ---
try:
    import google.generativeai as genai
    from dotenv import load_dotenv
    import feedparser
    from newspaper import Article, ArticleException
except ImportError as e:
    print(f"Error: Falta una librería necesaria: {e}. Por favor, instala las dependencias con 'pip install -r requirements.txt'")
    exit(1)

# --- TIPADO ESTÁTICO ---
from typing import List, Dict, Any, Optional

# --- CONFIGURACIÓN INICIAL ---
load_dotenv(override=False)

try:
    ssl_context = ssl.create_default_context(cafile=certifi.where())
    ssl._create_default_https_context = lambda: ssl_context
    print("✅ Contexto SSL configurado para usar el bundle de CA de certifi.")
except Exception as e:
    print(f"⚠️ Advertencia: No se pudo configurar el contexto SSL con certifi: {e}")

# ==============================================================================
# CLASE DE CONFIGURACIÓN
# ==============================================================================
class Config:
    SCRIPT_DIR = Path(__file__).resolve().parent
    FUENTES_RSS_JSON_PATH = SCRIPT_DIR / "fuentes_rss.json"
    HISTORIAL_JSON_PATH = SCRIPT_DIR / "historial_noticias.json"
    DEFAULT_HOURS_AGO = 24
    WEEKLY_REPORT_DAYS = 7
    USER_AGENT = "NewsAggregatorBot/1.0 (+https://github.com/features/actions)"
    MAX_ARTICLES_TO_SUMMARIZE_PER_CATEGORY = 5
    MAX_ARTICLES_WEEKLY_PER_CATEGORY = 7
    DAILY_REPORT_FILENAME = "index.html"
    WEEKLY_REPORT_FILENAME = "semanal.html"
    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
    GMAIL_USER = os.getenv("GMAIL_USER")
    GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD")
    GMAIL_DESTINATARIO = os.getenv("GMAIL_DESTINATARIO", GMAIL_USER)
    GITHUB_REPO_OWNER = os.getenv("GITHUB_REPOSITORY_OWNER")
    GITHUB_REPO_NAME = os.getenv("GITHUB_REPOSITORY_NAME")
    
    if GITHUB_REPO_OWNER and GITHUB_REPO_NAME:
        BASE_WEB_URL = f"https://{GITHUB_REPO_OWNER}.github.io/{GITHUB_REPO_NAME}/"
    else:
        BASE_WEB_URL = f"file://{SCRIPT_DIR.absolute()}/"

# ==============================================================================
# CLASE PRINCIPAL DEL PROCESADOR DE NOTICIAS
# ==============================================================================
class NewsProcessor:
    def __init__(self, config: Config):
        self.config = config
        self.gemini_model = self._init_gemini_model()

    def _init_gemini_model(self) -> Optional[genai.GenerativeModel]:
        if not self.config.GEMINI_API_KEY:
            print("❌ Error CRÍTICO: La variable de entorno GEMINI_API_KEY no está configurada.")
            return None
        try:
            genai.configure(api_key=self.config.GEMINI_API_KEY)
            model = genai.GenerativeModel('gemini-1.5-flash-latest', generation_config={"temperature": 0.5, "response_mime_type": "application/json"})
            print("✅ Modelo Gemini inicializado correctamente.")
            return model
        except Exception as e:
            print(f"❌ Error CRÍTICO al inicializar el modelo de Gemini: {e}")
            return None

    def _cargar_fuentes(self) -> Dict[str, Any]:
        try:
            with open(self.config.FUENTES_RSS_JSON_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError) as e:
            print(f"❌ Error CRÍTICO al cargar 'fuentes_rss.json': {e}")
            return {}

    def obtener_articulos_recientes(self, rss_url: str, horas: int) -> List[Dict[str, Any]]:
        print(f"\n📡 Analizando feed: {rss_url}")
        articulos_recientes = []
        try:
            feed = feedparser.parse(rss_url, agent=self.config.USER_AGENT, request_headers={'Accept': 'application/rss+xml, application/xml'})
            if feed.bozo and not isinstance(feed.bozo_exception, (feedparser.NonXMLContentType, feedparser.CharacterEncodingOverride)):
                raise feed.bozo_exception
            if feed.get("status", 200) not in [200, 301, 302]:
                 print(f"  ⚠️ Error HTTP {feed.status} al acceder al feed. Se omite.")
                 return articulos_recientes
            if not feed.entries:
                print(f"  ℹ️ No se encontraron entradas en el feed.")
                return articulos_recientes

            print(f"  📰 Encontradas {len(feed.entries)} entradas totales.")
            limite_tiempo_utc = datetime.now(timezone.utc) - timedelta(hours=horas)
            for entry in feed.entries:
                fecha_publicacion = entry.get('published_parsed') or entry.get('updated_parsed')
                if not fecha_publicacion: continue
                fecha_articulo_utc = datetime.fromtimestamp(time.mktime(fecha_publicacion), tz=timezone.utc)
                if fecha_articulo_utc > limite_tiempo_utc:
                    articulos_recientes.append({
                        "titulo": entry.get("title", "[sin título]"),
                        "link": entry.get("link", "[sin link]"),
                        "fecha_obj": fecha_articulo_utc,
                        "fecha_str": fecha_articulo_utc.strftime("%d-%m-%Y %H:%M UTC")
                    })
            print(f"  ✅ Se encontraron {len(articulos_recientes)} artículos recientes (últimas {horas} horas).")
        except Exception as e:
            print(f"  ❌ Error inesperado procesando el feed {rss_url}: {e}")
        return articulos_recientes

    def extraer_contenido(self, url: str) -> Optional[str]:
        try:
            article = Article(url, browser_user_agent=self.config.USER_AGENT)
            # SOLUCIÓN: Se elimina el parámetro 'timeout' para compatibilidad con versiones antiguas de newspaper3k
            article.download()
            article.parse()
            return article.text
        except (ArticleException, Exception) as e:
            print(f"    ⚠️ Error extrayendo de {url} (se omite): {e}")
        return None

    def resumir_con_gemini(self, titulo: str, contenido: str, categoria: str) -> Optional[Dict[str, Any]]:
        if not self.gemini_model: return None
        prompt = f"""
        Analiza el siguiente artículo en español. Título: {titulo}\nContenido:\n{contenido}
        Tu tarea es:
        1. Crea una frase concisa (máximo 20 palabras) como "teaser".
        2. Resume el artículo (100-150 palabras).
        3. Evalúa la relevancia para la categoría '{categoria.replace('_', ' ').title()}' de 1 a 10.
        4. Justifica brevemente la puntuación.
        Responde ESTRICTAMENTE en este formato JSON:
        {{ "teaser_sentence": "...", "resumen": "...", "relevancia_score": <int>, "relevancia_justificacion": "..." }}
        """
        try:
            print(f"    📝 Solicitando resumen para: {titulo}")
            response = self.gemini_model.generate_content(prompt)
            clean_response_text = response.text.strip().lstrip("```json").rstrip("```")
            return json.loads(clean_response_text)
        except (json.JSONDecodeError, Exception) as e:
            print(f"    ❌ Error procesando con Gemini para '{titulo}': {e}")
            return None

    def save_to_history(self, processed_articles: Dict[str, List[Dict]]):
        history = []
        if self.config.HISTORIAL_JSON_PATH.exists():
            try:
                history = json.loads(self.config.HISTORIAL_JSON_PATH.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                print("⚠️ El archivo de historial estaba corrupto. Se creará uno nuevo.")
        
        count = 0
        for categoria, articles in processed_articles.items():
            for art in articles:
                if 'fecha_obj' in art['info'] and isinstance(art['info']['fecha_obj'], datetime):
                    art['info']['fecha_obj'] = art['info']['fecha_obj'].isoformat()
                art_copy = art.copy()
                art_copy['categoria'] = categoria
                history.append(art_copy)
                count += 1
        
        self.config.HISTORIAL_JSON_PATH.write_text(json.dumps(history, indent=2, ensure_ascii=False), encoding="utf-8")
        if count > 0:
            print(f"💾 Historial actualizado con {count} nuevos artículos.")

    def generate_html_report(self, processed_articles_by_category: Dict[str, list], report_type: str) -> str:
        generation_timestamp = datetime.now(timezone.utc)
        generation_timestamp_str = generation_timestamp.strftime("%d de %B de %Y, %H:%M:%S UTC")

        category_icons = {
            "internacional": "🌍", "nacional": "🇨🇱", "opinion_ensayo": "✍️",
            "ciencia_tecnologia": "🔬", "cultura_arte": "🎨", "default": "🔹"
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
        body {{ font-family: 'Roboto', sans-serif; line-height: 1.6; margin: 0; padding: 20px; background-color: #f4f7f6; color: #333; }}
        .container {{ max-width: 900px; margin: 30px auto; background-color: #ffffff; padding: 30px; border-radius: 10px; box-shadow: 0 6px 12px rgba(0,0,0,0.08); }}
        h1 {{ font-family: 'Montserrat', sans-serif; color: #2c3e50; text-align: center; margin-bottom: 15px; font-weight: 700; font-size: 2.2em; }}
        .report-timestamp {{ text-align: center; font-size: 0.9em; color: #555; margin-top: -10px; margin-bottom: 35px; }}
        h2 {{ font-family: 'Montserrat', sans-serif; color: #34495e; border-bottom: 3px solid #1abc9c; padding-bottom: 12px; margin-top: 40px; margin-bottom: 25px; font-weight: 600; font-size: 1.8em; }}
        details {{ background-color: #fdfdfd; border: 1px solid #e0e0e0; border-radius: 8px; margin-bottom: 20px; padding: 20px; box-shadow: 0 3px 6px rgba(0,0,0,0.04); transition: box-shadow 0.3s ease; }}
        details:hover {{ box-shadow: 0 5px 10px rgba(0,0,0,0.06); }}
        details[open] {{ background-color: #ffffff; border-left: 5px solid #1abc9c; }}
        summary {{ font-family: 'Montserrat', sans-serif; font-weight: 600; cursor: pointer; color: #2980b9; font-size: 1.15em; margin-bottom: 8px; list-style-position: inside; outline: none; }}
        .article-content-wrapper {{ padding-top: 10px; }}
        .article-title {{ font-size: 1.15em; font-weight: bold; color: #343a40; margin-bottom: 5px; }}
        .article-meta {{ font-size: 0.85em; color: #6c757d; margin-bottom: 8px; }}
        .article-summary {{ margin-top: 10px; color: #495057; }}
        a {{ color: #007bff; text-decoration: none; }}
        a:hover {{ text-decoration: underline; }}
        .no-articles {{ text-align: center; font-style: italic; color: #6c757d; margin: 40px 10px; font-size: 1.1em; }}
    </style>
</head>
<body>
    <div class="container">
        <h1>Resumen {report_type.title()} de Noticias</h1>
        <p class="report-timestamp">Generado el: {generation_timestamp_str}</p>
        """
        
        if not processed_articles_by_category or not any(processed_articles_by_category.values()):
            html_content += "<p class='no-articles'>No se encontraron noticias nuevas en las últimas 24 horas.</p>"
        else:
            for categoria, articulos in processed_articles_by_category.items():
                if not articulos: continue
                icon = category_icons.get(categoria, category_icons["default"])
                html_content += f"<h2>{icon} {categoria.replace('_', ' ').title()}</h2>"
                for item in articulos:
                    info = item["info"]
                    resumen = item["resumen_datos"]
                    html_content += f"""
                    <details>
                        <summary>{resumen.get('teaser_sentence', 'Click para ver más')} (Puntuación: {resumen.get('relevancia_score', 'N/A')}/10)</summary>
                        <div class="article-content-wrapper">
                            <p class="article-title">{info.get('titulo', 'Sin título')}</p>
                            <p class="article-meta">
                                Fuente: {info.get('source_name', 'N/A')} | Fecha: {info.get('fecha_str', 'N/A')} | Justificación: {resumen.get('relevancia_justificacion', 'N/A')}
                            </p>
                            <p class="article-summary">{resumen.get('resumen', 'No hay resumen disponible.')}</p>
                            <a href='{info.get('link', '#')}' target='_blank'>Leer más en la fuente original</a>
                        </div>
                    </details>
                    """
        html_content += "</div></body></html>"
        return html_content

    def run_daily_report(self):
        fuentes = self._cargar_fuentes()
        if not fuentes: return

        all_articles = {}
        for categoria, lista_fuentes in fuentes.items():
            print(f"\n📚 Recopilando para: {categoria.replace('_', ' ').title()}")
            articles_this_category = []
            for fuente in lista_fuentes:
                articles = self.obtener_articulos_recientes(fuente["url"], self.config.DEFAULT_HOURS_AGO)
                for art in articles:
                    art['source_name'] = fuente['name']
                articles_this_category.extend(articles)
            all_articles[categoria] = articles_this_category

        processed_articles = {}
        total_processed_count = 0
        for categoria, articles in all_articles.items():
            print(f"\n✨ Procesando y resumiendo para: {categoria.replace('_', ' ').title()}")
            articles.sort(key=lambda x: x['fecha_obj'], reverse=True)
            articles_to_process = articles[:self.config.MAX_ARTICLES_TO_SUMMARIZE_PER_CATEGORY]
            processed_list = []
            for i, art in enumerate(articles_to_process):
                print(f"  ▶️ ({i+1}/{len(articles_to_process)}) Artículo: '{art['titulo']}'")
                contenido = self.extraer_contenido(art['link'])
                if not contenido: continue
                time.sleep(1)
                resumen_datos = self.resumir_con_gemini(art['titulo'], contenido, categoria)
                if resumen_datos:
                    processed_list.append({"info": art, "resumen_datos": resumen_datos})
            processed_list.sort(key=lambda x: x['resumen_datos'].get('relevancia_score', 0), reverse=True)
            processed_articles[categoria] = processed_list
            if processed_list:
                print(f"  ✅ Procesados con éxito {len(processed_list)} artículos para '{categoria}'.")
                total_processed_count += len(processed_list)

        self.save_to_history(processed_articles)
        html_content = self.generate_html_report(processed_articles, "Diario")
        output_path = self.config.SCRIPT_DIR / self.config.DAILY_REPORT_FILENAME
        output_path.write_text(html_content, encoding="utf-8")
        print(f"\n📄 Reporte Diario guardado en: {output_path}")
        daily_report_url = f"{self.config.BASE_WEB_URL}{self.config.DAILY_REPORT_FILENAME}"
        print(f"🔗 URL de despliegue: {daily_report_url}")

        if total_processed_count > 0:
            email_subject = f"Resumen Diario de Noticias ({total_processed_count} artículos)"
            email_body = f"Tu resumen diario de noticias está listo.\nSe procesaron {total_processed_count} artículos.\n\nPuedes verlo en: {daily_report_url}"
            send_email_notification(self.config, email_body, email_subject)

    def run_weekly_report(self):
        print("✨ Iniciando procesamiento del reporte semanal desde el historial.")
        if not self.config.HISTORIAL_JSON_PATH.exists():
            print("❌ No se encontró el archivo de historial. No se puede generar el reporte semanal.")
            return

        try:
            history = json.loads(self.config.HISTORIAL_JSON_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print("❌ Error al leer el archivo de historial. Está corrupto.")
            return

        limite_tiempo_utc = datetime.now(timezone.utc) - timedelta(days=self.config.WEEKLY_REPORT_DAYS)
        weekly_articles = []
        for art in history:
            try:
                fecha_articulo_utc = datetime.fromisoformat(art['info']['fecha_obj'])
                if fecha_articulo_utc > limite_tiempo_utc:
                    weekly_articles.append(art)
            except (KeyError, TypeError, ValueError) as e:
                print(f"⚠️ Omitiendo artículo del historial por datos inválidos: {e}")
                continue

        if not weekly_articles:
            print("ℹ️ No se encontraron artículos en la última semana para generar el reporte.")
            return

        print(f"🔍 Encontrados {len(weekly_articles)} artículos en el historial de la última semana.")

        articles_by_category = {}
        for art in weekly_articles:
            categoria = art.get('categoria')
            if categoria not in articles_by_category: articles_by_category[categoria] = []
            articles_by_category[categoria].append(art)

        top_articles_by_category = {}
        for categoria, articles in articles_by_category.items():
            articles.sort(key=lambda x: x['resumen_datos'].get('relevancia_score', 0), reverse=True)
            top_articles_by_category[categoria] = articles[:self.config.MAX_ARTICLES_WEEKLY_PER_CATEGORY]

        html_content = self.generate_html_report(top_articles_by_category, "Semanal")
        output_path = self.config.SCRIPT_DIR / self.config.WEEKLY_REPORT_FILENAME
        output_path.write_text(html_content, encoding="utf-8")
        print(f"\n📄 Reporte Semanal guardado en: {output_path}")

        weekly_report_url = f"{self.config.BASE_WEB_URL}{self.config.WEEKLY_REPORT_FILENAME}"
        print(f"🔗 URL de despliegue: {weekly_report_url}")

        total_articles = sum(len(arts) for arts in top_articles_by_category.values())
        if total_articles > 0:
            email_subject = f"🏆 Tu Resumen Semanal de Noticias está listo"
            email_body = f"El resumen con las noticias más relevantes de la semana está listo.\nSe seleccionaron {total_articles} artículos.\n\nPuedes verlo en: {weekly_report_url}"
            send_email_notification(self.config, email_body, email_subject)

def send_email_notification(config: Config, body_text: str, subject: str):
    if not all([config.GMAIL_USER, config.GMAIL_APP_PASSWORD, config.GMAIL_DESTINATARIO]):
        print("⚠️ Faltan credenciales de Gmail. No se enviará correo.")
        return
    msg = MIMEText(body_text, 'plain', 'utf-8')
    msg["Subject"] = subject
    msg["From"] = config.GMAIL_USER
    msg["To"] = config.GMAIL_DESTINATARIO
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            print(f"📧 Intentando enviar correo a {config.GMAIL_DESTINATARIO}...")
            server.login(config.GMAIL_USER, config.GMAIL_APP_PASSWORD)
            server.send_message(msg)
            print("✅ Correo de notificación enviado exitosamente.")
    except Exception as e:
        print(f"❌ Error al enviar correo: {e}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Compilador de noticias con IA.")
    parser.add_argument("-w", "--weekly", action="store_true", help="Genera un reporte semanal.")
    args = parser.parse_args()
    config = Config()
    processor = NewsProcessor(config)
    if not processor.gemini_model: exit(1)

    if args.weekly:
        processor.run_weekly_report()
    else:
        processor.run_daily_report()