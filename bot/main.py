import os
import logging
from typing import Any, Dict, List, cast, Optional
import re
import time
import random
import json
from urllib.parse import urlparse
import requests
from bs4 import BeautifulSoup
from io import BytesIO
from PyPDF2 import PdfReader

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from openai import OpenAI
from openai.types.chat import ChatCompletionMessageParam

# Enable logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Initialize OpenAI client
openai_client = None


class SmartParser:
    """Умный парсер: сначала бесплатно, при 403 - ScrapingBee."""
    
    def __init__(self):
        # Ключ ScrapingBee из переменных окружения
        self.scrapingbee_key = os.getenv('SCRAPINGBEE_API_KEY', '')
        
        # Сайты, для которых ВСЕГДА пробуем ScrapingBee при 403
        self.priority_sites = [
            'tochka.com',
            'yandex.ru/jobs',
            'яндекс-работа',
            'tinkoff.ru/career',
            'sber.ru/career',
            'vk.com/jobs',
            'vc.ru/jobs',
        ]
        
        # Статистика
        self.stats = {
            'direct_success': 0,
            'direct_403': 0,
            'direct_other_error': 0,
            'scrapingbee_success': 0,
            'scrapingbee_failed': 0,
            'total_requests': 0,
        }
    
    def should_try_scrapingbee(self, url: str, status_code: Optional[int] = None) -> bool:
        """
        Решаем, стоит ли пробовать ScrapingBee.
        
        Правила:
        1. Должен быть ключ API
        2. Должна быть 403 ошибка ИЛИ сайт в приоритетном списке
        3. Для приоритетных сайтов пробуем даже при других ошибках
        """
        if not self.scrapingbee_key:
            logger.debug("No ScrapingBee API key available")
            return False
        
        parsed = urlparse(url)
        domain = parsed.netloc.lower()
        
        # Проверяем, приоритетный ли сайт
        is_priority = any(site in domain for site in self.priority_sites)
        
        # Если сайт приоритетный - пробуем ScrapingBee при ЛЮБОЙ ошибке
        if is_priority and status_code is not None:
            logger.info(f"Priority site {domain}, will try ScrapingBee for error {status_code}")
            return True
        
        # Для остальных сайтов пробуем только при 403
        if status_code == 403:
            logger.info(f"403 error for {domain}, will try ScrapingBee")
            return True
        
        return False
    
    def parse(self, url: str) -> str:
        """Основной метод парсинга с приоритетом бесплатного метода."""
        logger.info(f"Smart parser starting for: {url}")
        self.stats['total_requests'] += 1
        
        # ШАГ 1: Пробуем БЕСПЛАТНЫЙ прямой парсинг
        try:
            result = self._try_direct_parsing(url)
            self.stats['direct_success'] += 1
            logger.info(f"Direct parsing SUCCESS for {url}")
            return result
            
        except requests.HTTPError as e:
            # Обрабатываем HTTP ошибки
            status_code = e.response.status_code if hasattr(e, 'response') else None
            
            if status_code == 403:
                self.stats['direct_403'] += 1
                logger.warning(f"Direct parsing got 403 for {url}")
                
                # Проверяем, стоит ли пробовать ScrapingBee
                if self.should_try_scrapingbee(url, status_code):
                    return self._try_with_scrapingbee(url)
                else:
                    raise RuntimeError("DIRECT_403_NO_FALLBACK")
                    
            else:
                self.stats['direct_other_error'] += 1
                logger.error(f"Direct parsing HTTP error {status_code} for {url}")
                
                # Для приоритетных сайтов пробуем ScrapingBee даже при других ошибках
                if self.should_try_scrapingbee(url, status_code):
                    return self._try_with_scrapingbee(url)
                else:
                    raise RuntimeError(f"DIRECT_HTTP_ERROR_{status_code}")
                    
        except Exception as e:
            self.stats['direct_other_error'] += 1
            logger.error(f"Direct parsing general error for {url}: {e}")
            
            # Для приоритетных сайтов пробуем ScrapingBee даже при других ошибках
            if self.should_try_scrapingbee(url):
                return self._try_with_scrapingbee(url)
            else:
                raise RuntimeError("DIRECT_GENERAL_ERROR")
    
    def _try_direct_parsing(self, url: str) -> str:
        """Бесплатный прямой парсинг с улучшенными заголовками."""
        headers = self._get_realistic_headers()
        
        # Случайная задержка 1-3 секунды (имитация человека)
        time.sleep(random.uniform(1, 3))
        
        logger.info(f"Trying DIRECT parsing for: {url}")
        
        response = requests.get(
            url, 
            headers=headers, 
            timeout=25,
            allow_redirects=True,
            verify=True
        )
        
        # Проверяем статус
        response.raise_for_status()  # Вызовет HTTPError если статус не 2xx
        
        # Успешно получили ответ
        logger.info(f"Direct parsing SUCCESS: {response.status_code}")
        
        # Парсим контент
        return self._parse_html_content(response.text, url, "direct")
    
    def _try_with_scrapingbee(self, url: str) -> str:
        """Используем ScrapingBee API как fallback."""
        if not self.scrapingbee_key:
            raise RuntimeError("SCRAPINGBEE_NO_KEY")
        
        logger.info(f"Trying ScrapingBee for: {url}")
        
        api_url = "https://app.scrapingbee.com/api/v1/"
        
        # Параметры для сложных сайтов
        params = {
            'api_key': self.scrapingbee_key,
            'url': url,
            'render_js': 'true',        # ВКЛЮЧАЕМ JavaScript рендеринг!
            'premium_proxy': 'true',    # Премиум прокси (лучше обход)
            'country_code': 'ru',       # Российские прокси
            'wait': '3000',             # Ждем 3 секунды для JS
            'wait_for': '3000',         # Альтернативный параметр ожидания
            'timeout': '30000',         # Таймаут 30 секунд
        }
        
        # Для tochka.com добавляем дополнительные параметры
        if 'tochka.com' in url:
            params.update({
                'stealth_proxy': 'true',  # Стелс-прокси для сложных сайтов
                'session_id': str(int(time.time())),  # Уникальная сессия
            })
        
        try:
            # Делаем запрос к ScrapingBee
            response = requests.get(
                api_url, 
                params=params, 
                timeout=35,  # Большой таймаут для JS рендеринга
                headers={
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
                }
            )
            
            logger.info(f"ScrapingBee response: {response.status_code}")
            
            if response.status_code != 200:
                error_msg = f"ScrapingBee error {response.status_code}"
                if response.text:
                    error_msg += f": {response.text[:200]}"
                logger.error(error_msg)
                
                self.stats['scrapingbee_failed'] += 1
                raise RuntimeError("SCRAPINGBEE_API_ERROR")
            
            # Успех!
            self.stats['scrapingbee_success'] += 1
            
            # Парсим результат
            return self._parse_html_content(response.text, url, "scrapingbee")
            
        except requests.Timeout:
            logger.error("ScrapingBee timeout")
            self.stats['scrapingbee_failed'] += 1
            raise RuntimeError("SCRAPINGBEE_TIMEOUT")
            
        except requests.RequestException as e:
            logger.error(f"ScrapingBee request error: {e}")
            self.stats['scrapingbee_failed'] += 1
            raise RuntimeError("SCRAPINGBEE_NETWORK_ERROR")
            
        except Exception as e:
            logger.error(f"ScrapingBee unexpected error: {e}")
            self.stats['scrapingbee_failed'] += 1
            raise RuntimeError("SCRAPINGBEE_UNKNOWN_ERROR")
    
    def _get_realistic_headers(self) -> Dict[str, str]:
        """Генерируем реалистичные заголовки браузера."""
        user_agents = [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:123.0) Gecko/20100101 Firefox/123.0",
        ]
        
        headers = {
            'User-Agent': random.choice(user_agents),
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
            'Accept-Language': 'ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7',
            'Accept-Encoding': 'gzip, deflate',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
            'Sec-Fetch-Dest': 'document',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Site': 'none',
            'Cache-Control': 'max-age=0',
            'Pragma': 'no-cache',
        }
        
        return headers
    
    def _parse_html_content(self, html: str, url: str, source: str) -> str:
        """Парсим HTML контент из любого источника."""
        logger.info(f"Parsing HTML from {source} for {url}")
        
        try:
            soup = BeautifulSoup(html, 'html.parser')
            
            # Удаляем ненужные элементы
            for tag in soup(['script', 'style', 'noscript', 'iframe', 'svg', 'link', 'meta']):
                tag.decompose()
            
            # Извлекаем JSON-LD структурированные данные
            json_ld_text = self._extract_json_ld(soup)
            
            # Извлекаем мета-информацию
            meta_text = self._extract_meta_info(soup)
            
            # Ищем основной контент
            content_text = self._find_main_content(soup)
            
            # Объединяем все источники
            result_parts = []
            
            if json_ld_text:
                result_parts.append(f"📋 Структурированные данные:\n{json_ld_text}")
            
            if meta_text:
                result_parts.append(f"🏷️ Мета-информация:\n{meta_text}")
            
            if content_text:
                result_parts.append(f"📄 Основной контент:\n{content_text}")
            
            if result_parts:
                result = "\n\n".join(result_parts)
                
                # Проверяем длину результата
                if len(result.strip()) > 150:
                    logger.info(f"Successfully extracted {len(result)} chars from {source}")
                    return self._clean_text(result)
            
            # Если ничего не нашли, пробуем получить весь текст
            full_text = soup.get_text(separator='\n', strip=True)
            cleaned = self._clean_text(full_text)
            
            if len(cleaned.strip()) > 100:
                logger.info(f"Using full text: {len(cleaned)} chars")
                return cleaned
            
            # Если текст слишком короткий
            logger.warning(f"Extracted text too short from {source}: {len(cleaned)} chars")
            raise RuntimeError("CONTENT_TOO_SHORT")
            
        except Exception as e:
            logger.error(f"Error parsing HTML from {source}: {e}")
            raise RuntimeError("HTML_PARSING_ERROR")
    
    def _extract_json_ld(self, soup: BeautifulSoup) -> str:
        """Извлекаем JSON-LD структурированные данные."""
        try:
            scripts = soup.find_all('script', type='application/ld+json')
            results = []
            
            for script in scripts:
                try:
                    data = json.loads(script.string)
                    if isinstance(data, dict):
                        # Ищем JobPosting
                        if data.get('@type') == 'JobPosting':
                            job_info = []
                            for key in ['title', 'description', 'responsibilities', 
                                       'requirements', 'qualifications', 'skills']:
                                if key in data and data[key]:
                                    value = data[key]
                                    if isinstance(value, list):
                                        job_info.append(f"{key}: " + ", ".join(str(v) for v in value))
                                    else:
                                        job_info.append(f"{key}: {value}")
                            
                            if job_info:
                                results.append("\n".join(job_info))
                except:
                    continue
            
            return "\n\n".join(results) if results else ""
        except:
            return ""
    
    def _extract_meta_info(self, soup: BeautifulSoup) -> str:
        """Извлекаем мета-информацию."""
        meta_parts = []
        
        # Title
        title = soup.find('title')
        if title and title.string:
            meta_parts.append(f"Заголовок: {title.string.strip()}")
        
        # H1
        h1_tags = soup.find_all('h1')
        for h1 in h1_tags[:2]:  # Берем первые 2 h1
            if h1 and h1.get_text(strip=True):
                meta_parts.append(f"H1: {h1.get_text(strip=True)}")
        
        # Meta description
        meta_desc = soup.find('meta', attrs={'name': 'description'})
        if meta_desc and meta_desc.get('content'):
            meta_parts.append(f"Описание: {meta_desc['content'].strip()}")
        
        # Open Graph
        og_desc = soup.find('meta', attrs={'property': 'og:description'})
        if og_desc and og_desc.get('content'):
            meta_parts.append(f"OG Описание: {og_desc['content'].strip()}")
        
        return "\n".join(meta_parts) if meta_parts else ""
    
    def _find_main_content(self, soup: BeautifulSoup) -> str:
        """Ищем основной контент страницы."""
        content_selectors = [
            # Вакансии
            '[data-qa*="vacancy"]', '[data-test*="vacancy"]', '[data-qa*="description"]',
            '[class*="vacancy" i]', '[class*="job" i]', '[class*="description" i]',
            '[itemtype*="JobPosting"]', '[itemprop="description"]',
            
            # Основной контент
            'article', 'main', '[role="main"]',
            '[class*="content" i]', '[class*="text" i]', '[class*="body" i]',
            '.container', '.wrapper', '.page-content',
            
            # Общие
            'section', '.post-content', '.article-content',
        ]
        
        for selector in content_selectors:
            try:
                elements = soup.select(selector)
                for elem in elements:
                    text = elem.get_text(separator='\n', strip=True)
                    # Проверяем, похоже ли на вакансию
                    if (len(text) > 300 and 
                        any(word in text.lower() for word in 
                            ['требован', 'обязанност', 'задач', 'квалификац', 'опыт', 'навык'])):
                        return text
                    elif len(text) > 500:  # Достаточно длинный текст
                        return text
            except Exception as e:
                logger.debug(f"Selector {selector} failed: {e}")
                continue
        
        return ""
    
    def _clean_text(self, text: str) -> str:
        """Очищаем текст."""
        if not text:
            return ""
        
        # Заменяем множественные переносы строк
        text = re.sub(r'\n\s*\n\s*\n+', '\n\n', text)
        
        # Удаляем лишние пробелы
        text = re.sub(r'[ \t]{2,}', ' ', text)
        
        # Разбиваем на строки и фильтруем
        lines = [line.strip() for line in text.split('\n') if line.strip()]
        
        # Удаляем слишком короткие строки если их много
        if len(lines) > 50:
            lines = [line for line in lines if len(line) > 20]
        
        return '\n'.join(lines)
    
    def get_stats(self) -> Dict[str, int]:
        """Получить статистику."""
        return self.stats.copy()
    
    def print_stats(self):
        """Вывести статистику в лог."""
        stats = self.get_stats()
        logger.info("=== Parser Statistics ===")
        for key, value in stats.items():
            logger.info(f"{key}: {value}")


# Инициализируем парсер
smart_parser = SmartParser()


def load_system_prompt() -> str:
    """Load the system prompt from system_prompt.txt if it exists."""
    try:
        base_dir = os.path.dirname(os.path.abspath(__file__))
        file_path = os.path.join(base_dir, "system_prompt.txt")
        with open(file_path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return "You are a helpful assistant."
    except Exception as e:
        logger.warning(f"Failed to load system_prompt.txt: {e}")
        return "You is a helpful assistant."


# =========================
# Основные функции
# =========================

def clean_text(raw: str) -> str:
    """Приводит текст в аккуратный вид."""
    if not raw:
        return ""
    text = raw.replace("\r\n", "\n")
    lines = [line.strip() for line in text.split("\n")]
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def is_url(text: str) -> bool:
    """Проверяем, похожа ли строка на URL."""
    if not text:
        return False
    text = text.strip()
    
    if re.match(r'^https?://\S+$', text, re.IGNORECASE):
        return True
    
    try:
        parsed = urlparse(text)
        return bool(parsed.netloc) and '.' in parsed.netloc
    except:
        return False


def extract_text_from_pdf_bytes(data: bytes) -> str:
    """Достаём текст из PDF по сырым байтам."""
    try:
        reader = PdfReader(BytesIO(data))
        pages_text: List[str] = []
        for page in reader.pages:
            page_text = page.extract_text() or ""
            pages_text.append(page_text)
        return clean_text("\n\n".join(pages_text))
    except Exception as e:
        logger.error(f"Error while extracting text from PDF bytes: {e}")
        return ""


def extract_text_from_url(url: str) -> str:
    """Извлекаем текст из URL используя умный парсер."""
    logger.info(f"Extracting from URL: {url}")
    
    # Нормализуем URL
    if not url.startswith(('http://', 'https://')):
        url = 'https://' + url
    
    try:
        return smart_parser.parse(url)
        
    except RuntimeError as e:
        error_type = str(e)
        logger.warning(f"Smart parser failed: {error_type}")
        raise e
        
    except Exception as e:
        logger.error(f"Unexpected error in extract_text_from_url: {e}")
        raise RuntimeError("UNKNOWN_ERROR")


def prepare_input_text(raw: str) -> str:
    """
    Обрабатываем ввод пользователя.
    Всегда возвращаем текст, никогда не падаем с ошибкой.
    """
    if not raw:
        return ""
    
    raw = raw.strip()
    
    # Если это не ссылка, просто чистим текст
    if not is_url(raw):
        return clean_text(raw)
    
    # Если это ссылка - пробуем парсить
    logger.info(f"Processing URL: {raw}")
    
    try:
        text = extract_text_from_url(raw)
        
        # Проверяем результат
        if text and len(text.strip()) > 150:
            return text
        else:
            # Текст слишком короткий
            return clean_text(f"Ссылка на вакансию: {raw}")
            
    except RuntimeError as e:
        error_type = str(e)
        logger.info(f"Parser failed with: {error_type}")
        
        # Специальная обработка для разных ошибок
        if "403" in error_type or "DIRECT_403" in error_type:
            if 'tochka.com' in raw.lower():
                return clean_text(f"🔒 tochka.com (защищенный сайт)\nСсылка: {raw}\n\nДля анализа скопируйте текст вакансии.")
            else:
                return clean_text(f"🔒 Сайт заблокировал доступ\nСсылка: {raw}\n\nПожалуйста, скопируйте текст вакансии.")
        
        elif "SCRAPINGBEE" in error_type:
            return clean_text(f"⚠️ Не удалось получить доступ через прокси\nСсылка: {raw}\n\nСкопируйте текст вакансии вручную.")
        
        else:
            return clean_text(f"Ссылка на вакансию: {raw}")
            
    except Exception as e:
        logger.error(f"Unexpected error in prepare_input_text: {e}")
        return clean_text(f"Ссылка на вакансию: {raw}")


# =========================
# Telegram Handlers
# =========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик команды /start."""
    message = update.message
    if message is None:
        return

    user = update.effective_user
    welcome_text = (
        "👋 *Привет! Я твой помощник в поиске работы.*\n\n"
        "Я помогу:\n"
        "• Проанализировать вакансии\n"
        "• Сопоставить с вашим резюме\n"
        "• Составить сопроводительные письма\n"
        "• Подготовиться к собеседованиям\n\n"
        "📤 *Отправь мне:*\n"
        "✅ Текст резюме или PDF\n"
        "✅ Ссылку на вакансию\n"
        "✅ Текст вакансии\n\n"
        "Я работаю с большинством сайтов! 🚀"
    )
    
    await message.reply_text(welcome_text, parse_mode='Markdown')


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик команды /help."""
    message = update.message
    if message is None:
        return

    help_text = (
        "📖 *Помощь по использованию бота*\n\n"
        "*Основные команды:*\n"
        "/start - Начало работы\n"
        "/help - Эта справка\n"
        "/update_resume - Обновить резюме\n"
        "/stats - Статистика парсинга\n\n"
        "*Что можно отправлять:*\n"
        "1. Текст вашего резюме\n"
        "2. PDF файл с резюме\n"
        "3. Ссылку на вакансию\n"
        "4. Текст вакансии\n\n"
        "*Поддерживаемые сайты:*\n"
        "• hh.ru, habr.com/career\n"
        "• linkedin.com, moikrug.ru\n"
        "• rabota.ru, superjob.ru\n"
        "• и большинство других\n\n"
        "*Если ссылка не открывается:*\n"
        "Пожалуйста, скопируйте текст вакансии\n"
        "и отправьте его текстом."
    )
    
    await message.reply_text(help_text, parse_mode='Markdown')


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Показать статистику парсинга."""
    stats = smart_parser.get_stats()
    
    stats_text = (
        "📊 *Статистика парсинга*\n\n"
        f"✅ Прямой успешный: {stats['direct_success']}\n"
        f"🔒 Прямой 403 ошибок: {stats['direct_403']}\n"
        f"⚠️ Прямой других ошибок: {stats['direct_other_error']}\n"
        f"💰 ScrapingBee успешный: {stats['scrapingbee_success']}\n"
        f"💸 ScrapingBee неудачный: {stats['scrapingbee_failed']}\n"
        f"📈 Всего запросов: {stats['total_requests']}\n\n"
        f"*Эффективность:*\n"
        f"Бесплатных успешно: {stats['direct_success']}/{stats['total_requests']}\n"
        f"Платных использовано: {stats['scrapingbee_success'] + stats['scrapingbee_failed']}"
    )
    
    await update.message.reply_text(stats_text, parse_mode='Markdown')


async def update_resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик команды /update_resume."""
    message = update.message
    if message is None:
        return

    user_data = cast(Dict[str, Any], context.user_data)
    user_data['awaiting_resume'] = True
    user_data.pop('resume', None)
    
    await message.reply_text(
        "📝 *Обновление резюме*\n\n"
        "Загрузите новое резюме (PDF или текст).\n"
        "Я обновлю информацию о вашем опыте.",
        parse_mode='Markdown'
    )


async def chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Основной обработчик сообщений."""
    message = update.message
    if message is None:
        return

    if not openai_client:
        await message.reply_text(
            "⚠️ *Ошибка конфигурации*\n\n"
            "OpenAI не настроен. Пожалуйста, установите OPENAI_API_KEY.",
            parse_mode='Markdown'
        )
        return

    user_data = cast(Dict[str, Any], context.user_data)

    # Обработка PDF или текста
    user_message: str

    if message.document is not None:
        doc = message.document
        is_pdf = (
            doc.mime_type == "application/pdf"
            or (doc.file_name and doc.file_name.lower().endswith(".pdf"))
        )

        if not is_pdf:
            await message.reply_text(
                "⚠️ *Неподдерживаемый формат*\n\n"
                "Поддерживаются только PDF файлы и текстовые сообщения.",
                parse_mode='Markdown'
            )
            return

        try:
            file = await doc.get_file()
            bio = BytesIO()
            await file.download_to_memory(out=bio)
            pdf_bytes = bio.getvalue()
            extracted = extract_text_from_pdf_bytes(pdf_bytes)
            
            if not extracted:
                await message.reply_text(
                    "❌ *Не удалось извлечь текст*\n\n"
                    "Попробуйте другой PDF файл или отправьте текст.",
                    parse_mode='Markdown'
                )
                return
                
            user_message = extracted
        except Exception as e:
            logger.error(f"PDF error: {e}")
            await message.reply_text(
                "❌ *Ошибка чтения PDF*\n\n"
                "Попробуйте ещё раз или отправьте текст.",
                parse_mode='Markdown'
            )
            return

    else:  # Текстовое сообщение
        raw_text = message.text
        if raw_text is None:
            await message.reply_text(
                "⚠️ *Неподдерживаемый формат*\n\n"
                "Поддерживаются только текст или PDF файлы.",
                parse_mode='Markdown'
            )
            return
        
        # Показываем индикатор набора
        await message.chat.send_action(action="typing")
        
        # Обрабатываем текст (включая ссылки)
        user_message = prepare_input_text(raw_text)

    # Если ожидаем новое резюме
    if user_data.get("awaiting_resume"):
        user_data["resume"] = user_message
        user_data["awaiting_resume"] = False
        
        await message.reply_text(
            "✅ *Резюме обновлено!*\n\n"
            "Теперь отправьте вакансию для анализа.\n"
            "Я буду использовать это резюме как контекст.",
            parse_mode='Markdown'
        )
        return

    try:
        # Показываем, что обрабатываем
        await message.chat.send_action(action="typing")

        system_prompt = load_system_prompt()

        # История диалога
        history = cast(List[Dict[str, str]], user_data.get("history", []))
        max_history_messages = 10

        messages: List[ChatCompletionMessageParam] = [
            {
                "role": "system",
                "content": system_prompt
            }
        ]

        if history:
            messages.extend(history[-max_history_messages:])

        # Добавляем резюме если есть
        resume = user_data.get("resume")
        if resume:
            messages.append({
                "role": "user",
                "content": (
                    "Это резюме пользователя. Используй его как основной контекст "
                    "при анализе вакансий, составлении таблиц совпадений и подготовке сопроводительных писем:\n\n"
                    f"{resume}"
                ),
            })

        # Текущее сообщение
        messages.append({
            "role": "user",
            "content": user_message
        })

        # Вызов OpenAI API
        response = openai_client.chat.completions.create(
            model="gpt-4.1-mini",
            messages=messages,
            max_completion_tokens=2048
        )

        ai_response = response.choices[0].message.content or (
            "Извините, не удалось сформировать ответ."
        )
        
        await message.reply_text(ai_response)

        # Обновляем историю
        if ai_response:
            history.append({"role": "user", "content": user_message})
            history.append({"role": "assistant", "content": ai_response})
            user_data["history"] = history[-max_history_messages:]

    except Exception as e:
        logger.error(f"OpenAI API error: {e}")
        await message.reply_text(
            "❌ *Ошибка обработки запроса*\n\n"
            "Пожалуйста, попробуйте ещё раз позже.",
            parse_mode='Markdown'
        )


async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик ошибок."""
    logger.error(f"Update error: {context.error}")
    
    # Логируем, но пользователю не показываем


def main() -> None:
    """Запуск бота."""
    global openai_client

    # Получаем токены
    token = os.getenv('TELEGRAM_BOT_TOKEN')
    openai_api_key = os.getenv('OPENAI_API_KEY')
    scrapingbee_key = os.getenv('SCRAPINGBEE_API_KEY')

    if not token:
        logger.error("TELEGRAM_BOT_TOKEN not found!")
        print("ERROR: Set TELEGRAM_BOT_TOKEN environment variable")
        return

    if not openai_api_key:
        logger.warning("OPENAI_API_KEY not found - AI features disabled")
        print("WARNING: Set OPENAI_API_KEY to enable AI")
    else:
        openai_client = OpenAI(api_key=openai_api_key)
        logger.info("OpenAI client initialized")

    if scrapingbee_key:
        logger.info("ScrapingBee API key found - premium parsing enabled")
    else:
        logger.info("ScrapingBee API key not found - only free parsing")

    # Создаем приложение
    application = Application.builder().token(token).build()

    # Регистрируем обработчики
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("update_resume", update_resume))
    application.add_handler(CommandHandler("stats", stats_command))

    # Обработчик сообщений
    application.add_handler(
        MessageHandler(
            (filters.TEXT | filters.Document.PDF) & ~filters.COMMAND,
            chat,
        )
    )

    # Обработчик ошибок
    application.add_error_handler(error_handler)

    # Запускаем
    logger.info("Bot starting...")
    print("✅ Bot is running! Press Ctrl+C to stop.")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == '__main__':
    main()
