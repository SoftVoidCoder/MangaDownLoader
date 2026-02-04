import sys
import os
import re
import json
import zipfile
import shutil
import requests
import time
import img2pdf
import threading
import queue
import subprocess
from pathlib import Path
from PIL import Image
from io import BytesIO

from PyQt5.QtWidgets import (QApplication, QWidget, QVBoxLayout, QHBoxLayout, 
                             QPushButton, QTextEdit, QLabel, QComboBox, 
                             QLineEdit, QGroupBox, QGridLayout, QTabWidget,
                             QFileDialog, QRadioButton, QProgressBar, QMessageBox,
                             QCheckBox, QSpinBox, QDialog)
from PyQt5.QtCore import QThread, pyqtSignal, Qt
from PyQt5.QtGui import QFont

from selenium import webdriver
from selenium.webdriver.firefox.options import Options
from selenium.webdriver.firefox.service import Service as FirefoxService
from selenium.webdriver.common.by import By

import fitz  # PyMuPDF


# ============================================================================
# КОНФИГУРАЦИЯ
# ============================================================================

class Config:
    """Класс для работы с конфигурацией"""
    CONFIG_FILE = "manga_downloader_config.json"
    
    DEFAULT_CONFIG = {
        "firefox_path": "",
        "default_url": "https://com-x.life",
        "default_format": "PDF",
        "default_mode": 0,
        "auto_save_settings": True
    }
    
    @classmethod
    def load(cls):
        """Загружает конфигурацию из файла"""
        try:
            if os.path.exists(cls.CONFIG_FILE):
                with open(cls.CONFIG_FILE, 'r', encoding='utf-8') as f:
                    config = json.load(f)
                    # Объединяем с дефолтными значениями
                    for key, value in cls.DEFAULT_CONFIG.items():
                        if key not in config:
                            config[key] = value
                    return config
            else:
                # Создаем файл с дефолтными настройками
                cls.save(cls.DEFAULT_CONFIG)
                return cls.DEFAULT_CONFIG.copy()
        except Exception as e:
            print(f"❌ Ошибка загрузки конфигурации: {e}")
            return cls.DEFAULT_CONFIG.copy()
    
    @classmethod
    def save(cls, config):
        """Сохраняет конфигурацию в файл"""
        try:
            with open(cls.CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(config, f, indent=2, ensure_ascii=False)
            return True
        except Exception as e:
            print(f"❌ Ошибка сохранения конфигурации: {e}")
            return False
    
    @classmethod
    def update(cls, key, value):
        """Обновляет одно значение в конфигурации"""
        config = cls.load()
        config[key] = value
        return cls.save(config)


# ============================================================================
# МОДУЛЬ СКАЧИВАНИЯ МАНГИ
# ============================================================================

class MangaDownloader(QThread):
    """
    Класс для автоматического скачивания манги с сайта com-x.life
    С автоматическим разделением PDF на части по 100 страниц
    """
    log = pyqtSignal(str)
    finished = pyqtSignal(bool, list)  # добавили список созданных файлов
    download_started = pyqtSignal()
    progress = pyqtSignal(int, str)  # прогресс в процентах и сообщение

    # Константы
    COOKIE_FILE = "comx_life_cookies_v2.json"
    DOWNLOADS_DIR = "downloads"
    TEMP_DIR = "combined_temp"
    REQUEST_DELAY = 0.5
    PAGES_PER_PDF = 100  # Автоматическое разделение по 100 страниц
    
    def __init__(self, output_format="cbz", base_url="https://com-x.life", download_all=False, firefox_path=None):
        super().__init__()
        self.url = None
        self.cookies = None
        self.cookie_file = Path(self.COOKIE_FILE)
        self.headers = {
            "Referer": f"{base_url}/home",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
        }
        self.base_url = base_url.rstrip('/')
        self.output_format = output_format.lower()
        self.download_all = download_all
        self.firefox_path = firefox_path
        self._is_cancelled = False
        self.created_files = []  # список созданных файлов

    def run(self):
        self.cleanup()
        try:
            self.log.emit("🌐 Открытие браузера...")
            self.progress.emit(10, "Открытие браузера...")
            driver = self._open_browser_with_cookies()
            if driver:
                if self.download_all:
                    self.log.emit("🔍 Режим: Скачать всю мангу")
                    self.progress.emit(20, "Поиск всех глав манги...")
                    self._download_all_manga(driver)
                else:
                    self.log.emit("🔎 Запуск отслеживания страницы манги...")
                    self.progress.emit(30, "Поиск манги...")
                    self._auto_download_if_manga_page(driver)
        except Exception as e:
            self.log.emit(f"❌ Ошибка: {e}")
            self.finished.emit(False, [])

    def cancel(self):
        self._is_cancelled = True

    def cleanup(self):
        for dir_name in [self.DOWNLOADS_DIR, self.TEMP_DIR]:
            dir_path = Path(dir_name)
            if dir_path.exists():
                try:
                    shutil.rmtree(dir_path)
                    self.log.emit(f"🧹 Очищено: {dir_name}")
                except:
                    pass

    def _open_browser_with_cookies(self):
        options = Options()
        options.add_argument('--detach')
        
        try:
            # Пытаемся найти или скачать драйвер автоматически
            driver = self._get_webdriver_with_autodownload(options)
            if not driver:
                return None
                
        except Exception as e:
            self.log.emit(f"❌ Ошибка при запуске браузера: {e}")
            self.finished.emit(False, [])
            return None

        driver.get(f"{self.base_url}/home")

        if self.cookie_file.exists():
            self.log.emit("🍪 Пробую восстановить сессию...")
            with open(self.cookie_file, "r", encoding="utf-8") as f:
                cookies = json.load(f)

            driver.delete_all_cookies()
            for c in cookies:
                c.pop("sameSite", None)
                try:
                    driver.add_cookie(c)
                except Exception as e:
                    self.log.emit(f"⚠️ Cookie {c.get('name')} не добавлен: {e}")

            driver.refresh()
            time.sleep(2)
            if driver.get_cookie("dle_user_id"):
                self.cookies = driver.get_cookies()
                self.log.emit("✅ Авторизация восстановлена!")
                return driver
            self.log.emit("⚠️ Сессия устарела, нужна новая авторизация")

        self.log.emit("🔐 Войдите вручную, я запомню cookies")
        self.log.emit("📦 Ожидание страницы манги...")

        while not driver.get_cookie("dle_user_id"):
            if self._is_cancelled:
                driver.quit()
                self.finished.emit(False, [])
                return None
            time.sleep(1)

        self.cookies = driver.get_cookies()
        with open(self.cookie_file, "w", encoding="utf-8") as f:
            json.dump(self.cookies, f, indent=2, ensure_ascii=False)

        return driver

    def _get_webdriver_with_autodownload(self, options):
        """Автоматически находит или скачивает драйвер"""
        import platform
        
        # Указываем путь к Firefox если задан
        if self.firefox_path and os.path.exists(self.firefox_path):
            options.binary_location = self.firefox_path
            self.log.emit(f"✅ Используется Firefox: {self.firefox_path}")
        
        # Определяем где мы находимся
        if getattr(sys, 'frozen', False):
            # Если запущено из EXE
            if hasattr(sys, '_MEIPASS'):
                base_path = sys._MEIPASS
            else:
                base_path = os.path.dirname(sys.executable)
        else:
            # Если запущено из скрипта
            base_path = os.path.dirname(os.path.abspath(__file__))
        
        # Пробуем найти драйвер в разных местах
        possible_paths = [
            os.path.join(base_path, 'geckodriver.exe'),
            os.path.join(base_path, 'geckodriver'),
            os.path.join('.', 'geckodriver.exe'),
            os.path.join('.', 'geckodriver'),
        ]
        
        # Добавляем системные пути
        if platform.system() == "Windows":
            possible_paths.extend([
                os.path.join(os.getcwd(), 'geckodriver.exe'),
                r'C:\geckodriver\geckodriver.exe',
            ])
        else:
            possible_paths.extend([
                os.path.join(os.getcwd(), 'geckodriver'),
                '/usr/local/bin/geckodriver',
                '/usr/bin/geckodriver',
            ])
        
        geckodriver_path = None
        for path in possible_paths:
            if os.path.exists(path):
                geckodriver_path = path
                self.log.emit(f"✅ Найден драйвер: {path}")
                break
        
        if not geckodriver_path:
            # Пытаемся скачать драйвер автоматически
            self.log.emit("⚠️ Драйвер не найден, пробую скачать...")
            geckodriver_path = self._download_geckodriver(base_path)
        
        if not geckodriver_path:
            self.log.emit("❌ Не удалось найти или скачать драйвер!")
            self.log.emit("📥 Скачайте вручную с: https://github.com/mozilla/geckodriver/releases")
            self.log.emit("📁 Положите geckodriver.exe в папку с программой")
            return None
        
        # Запускаем браузер
        try:
            service = FirefoxService(executable_path=geckodriver_path)
            driver = webdriver.Firefox(service=service, options=options)
            self.log.emit("✅ Браузер успешно запущен!")
            return driver
        except Exception as e:
            self.log.emit(f"❌ Ошибка запуска браузера: {e}")
            return None

    def _download_geckodriver(self, base_path):
        """Скачивает geckodriver автоматически"""
        import platform
        import zipfile
        import tarfile
        
        try:
            self.log.emit("🌐 Определение системы...")
            
            # Определяем ОС и архитектуру
            system = platform.system().lower()
            arch = platform.machine().lower()
            
            # Маппинг для скачивания
            if system == "windows":
                if "64" in arch or "amd64" in arch:
                    url = "https://github.com/mozilla/geckodriver/releases/download/v0.34.0/geckodriver-v0.34.0-win64.zip"
                    filename = "geckodriver.exe"
                    archive_type = "zip"
                else:
                    url = "https://github.com/mozilla/geckodriver/releases/download/v0.34.0/geckodriver-v0.34.0-win32.zip"
                    filename = "geckodriver.exe"
                    archive_type = "zip"
                    
            elif system == "linux":
                if "64" in arch:
                    url = "https://github.com/mozilla/geckodriver/releases/download/v0.34.0/geckodriver-v0.34.0-linux64.tar.gz"
                    filename = "geckodriver"
                    archive_type = "tar.gz"
                else:
                    url = "https://github.com/mozilla/geckodriver/releases/download/v0.34.0/geckodriver-v0.34.0-linux32.tar.gz"
                    filename = "geckodriver"
                    archive_type = "tar.gz"
                    
            elif system == "darwin":  # macOS
                if "arm" in arch:  # Apple Silicon
                    url = "https://github.com/mozilla/geckodriver/releases/download/v0.34.0/geckodriver-v0.34.0-macos-aarch64.tar.gz"
                    filename = "geckodriver"
                    archive_type = "tar.gz"
                else:  # Intel
                    url = "https://github.com/mozilla/geckodriver/releases/download/v0.34.0/geckodriver-v0.34.0-macos.tar.gz"
                    filename = "geckoddriver"
                    archive_type = "tar.gz"
            else:
                self.log.emit(f"❌ Неподдерживаемая система: {system}")
                return None
            
            self.log.emit(f"📥 Скачиваю драйвер для {system} {arch}...")
            self.log.emit(f"🔗 URL: {url}")
            
            # Скачиваем архив
            response = requests.get(url, stream=True)
            if response.status_code != 200:
                self.log.emit(f"❌ Ошибка скачивания: {response.status_code}")
                return None
            
            # Сохраняем архив
            temp_dir = os.path.join(base_path, "temp_geckodriver")
            os.makedirs(temp_dir, exist_ok=True)
            
            archive_path = os.path.join(temp_dir, f"geckodriver.{archive_type}")
            with open(archive_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)
            
            self.log.emit("📦 Распаковываю архив...")
            
            # Распаковываем
            extract_path = os.path.join(base_path, filename)
            
            if archive_type == "zip":
                with zipfile.ZipFile(archive_path, 'r') as zip_ref:
                    # Ищем geckodriver в архиве
                    for file_info in zip_ref.infolist():
                        if "geckodriver" in file_info.filename.lower():
                            with open(extract_path, 'wb') as f:
                                f.write(zip_ref.read(file_info.filename))
                            break
            else:  # tar.gz
                import tarfile
                with tarfile.open(archive_path, 'r:gz') as tar_ref:
                    for member in tar_ref.getmembers():
                        if "geckodriver" in member.name.lower():
                            with open(extract_path, 'wb') as f:
                                f.write(tar_ref.extractfile(member).read())
                            break
            
            # Делаем исполняемым на Unix-системах
            if system != "windows":
                os.chmod(extract_path, 0o755)
            
            # Очищаем временные файлы
            shutil.rmtree(temp_dir, ignore_errors=True)
            
            self.log.emit(f"✅ Драйвер скачан: {extract_path}")
            return extract_path
            
        except Exception as e:
            self.log.emit(f"❌ Ошибка при скачивании драйвера: {e}")
            return None

    def _auto_download_if_manga_page(self, driver):
        processed_url = None

        while not self._is_cancelled:
            try:
                current_url = driver.current_url
                if current_url and current_url.endswith('/download'):
                    self.url = current_url.replace('/download', '')
                    self.log.emit(f"📍 Начинаем скачивание манги: {self.url}")
                    driver.quit()
                    self.download_manga()
                    self.finished.emit(True, self.created_files)
                    return

                elif current_url and "/" in current_url and ".html" in current_url and current_url != processed_url:
                    self.log.emit(f"🔍 Проверка страницы: {current_url}")
                    try:
                        btn = driver.find_element(By.CSS_SELECTOR, 'a.page__btn-track.js-follow-status')
                        driver.execute_script('''
                            arguments[0].textContent = '⬇️ Скачать';
                            arguments[0].style.backgroundColor = '#28a745';
                            arguments[0].style.color = '#fff';
                            arguments[0].style.fontWeight = 'bold';
                            arguments[0].onclick = () => { window.location.href += '/download'; };
                        ''', btn)
                        self.log.emit("✅ Кнопка заменена на 'Скачать'")
                        processed_url = current_url
                    except Exception as e:
                        self.log.emit(f"⚠️ Кнопка не найдена: {e}")

                time.sleep(0.1)

            except Exception as e:
                self.log.emit(f"❌ Ошибка: {e}")
                driver.quit()
                self.finished.emit(False, [])
                return

    def _download_all_manga(self, driver):
        """Скачивает всю мангу с текущей страницы"""
        try:
            # Ждем пока пользователь перейдет на страницу манги
            self.log.emit("📚 Перейдите на страницу любой манги")
            self.log.emit("⏳ Ожидаю страницу манги...")
            
            manga_url = None
            while not self._is_cancelled:
                current_url = driver.current_url
                if current_url and "/" in current_url and ".html" in current_url and "read" not in current_url:
                    manga_url = current_url
                    break
                time.sleep(1)
            
            if self._is_cancelled:
                driver.quit()
                return
            
            self.url = manga_url
            self.log.emit(f"📍 Найдена манга: {self.url}")
            
            # Получаем данные о манге
            driver.quit()
            self.download_manga()
            self.finished.emit(True, self.created_files)
            
        except Exception as e:
            self.log.emit(f"❌ Ошибка в режиме 'Скачать всю мангу': {e}")
            driver.quit()
            self.finished.emit(False, [])

    def download_manga(self):
        """Основной метод скачивания манги"""
        if not self._load_cookies():
            return
            
        manga_data = self._get_manga_data()
        if not manga_data:
            return
            
        chapters, manga_title, news_id = manga_data
        
        manga_title_safe = self._prepare_directories(manga_title)
        
        self._download_chapters(chapters, news_id)
        
        if not self._is_cancelled:
            if self.output_format == "pdf":
                created_files = self._create_auto_split_pdf(manga_title_safe)
                if created_files:
                    for pdf_file in created_files:
                        self.log.emit(f"✅ Создан: {pdf_file}")
            else:
                final_file = Path(f"{manga_title_safe}.cbz")
                self._create_cbz_archive(final_file)
                if not self._is_cancelled and final_file.exists():
                    self.log.emit(f"✅ Готово: {final_file}")
        
        self.cleanup()

    def _load_cookies(self):
        """Загружает cookies из файла если они не заданы"""
        if not self.cookies:
            self.log.emit("⚠️ Предупреждение: cookies не заданы — загружаю из файла")
            try:
                with open(self.cookie_file, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                    self.cookies = raw if isinstance(raw, list) else [
                        {"name": k, "value": v} for k, v in raw.items()
                    ]
            except Exception as e:
                self.log.emit(f"❌ Не удалось загрузить cookies из файла: {e}")
                return False
        return True

    def _get_manga_data(self):
        """Получает данные манги из HTML страницы"""
        self.download_started.emit()
        self.log.emit(f"📥 Скачивание HTML: {self.url}")
        self.progress.emit(50, "Получение данных манги...")
        
        resp = requests.get(self.url, headers=self.headers, cookies={c['name']: c['value'] for c in self.cookies})
        html = resp.text

        match = re.search(r'window\.__DATA__\s*=\s*({.*?})\s*;', html, re.DOTALL)
        if not match:
            self.log.emit("❌ Не найден window.__DATA__")
            return None

        data = json.loads(match.group(1))
        chapters = data["chapters"][::-1]
        manga_title = data.get("title", "Manga").strip()
        
        # Извлекаем news_id из данных или URL
        news_id = data.get("news_id")
        if not news_id:
            url_match = re.search(r'/(\d+)-', self.url)
            if url_match:
                news_id = url_match.group(1)
            else:
                self.log.emit("❌ news_id не найден ни в данных, ни в URL!")
                return None
                
        return chapters, manga_title, news_id

    def _prepare_directories(self, manga_title):
        """Подготавливает директории для скачивания"""
        manga_title_safe = re.sub(r"[^\w\- ]", "_", manga_title)
        
        downloads_dir = Path(self.DOWNLOADS_DIR)
        combined_dir = Path(self.TEMP_DIR)
        
        downloads_dir.mkdir(exist_ok=True)
        combined_dir.mkdir(exist_ok=True)
        
        return manga_title_safe

    def _download_chapters(self, chapters, news_id):
        """Скачивает все главы манги"""
        self.log.emit(f"🔢 Глав: {len(chapters)}")
        
        for i, chapter in enumerate(chapters, 1):
            if self._is_cancelled:
                self.log.emit("❌ Скачивание отменено")
                self.cleanup()
                return

            title = chapter["title"]
            chapter_id = chapter["id"]
            filename = re.sub(r"[^\w\- ]", "_", f"{i:06}_{title}") + ".zip"
            zip_path = Path(self.DOWNLOADS_DIR) / filename

            progress = 50 + (i / len(chapters)) * 40
            self.progress.emit(int(progress), f"Скачивание главы {i}/{len(chapters)}: {title}")
            self.log.emit(f"⬇️ {i}/{len(chapters)}: {title}")
            
            if self._download_chapter(chapter_id, news_id, zip_path, title):
                self.log.emit(f"✅ Скачано: {title}")
            
            time.sleep(self.REQUEST_DELAY)

    def _download_chapter(self, chapter_id, news_id, zip_path, title):
        """Скачивает одну главу манги"""
        try:
            payload = f"chapter_id={chapter_id}&news_id={news_id}"
            domain = self.base_url
            
            headers = {
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "Referer": self.url,
                "X-Requested-With": "XMLHttpRequest",
                "Origin": domain,
                "User-Agent": self.headers["User-Agent"]
            }

            cookies = {c["name"]: c["value"] for c in self.cookies}
            
            api_url = f"{domain}/engine/ajax/controller.php?mod=api&action=chapters/download"
            link_resp = requests.post(api_url, headers=headers, data=payload, cookies=cookies)
            
            if link_resp.status_code != 200:
                raise ValueError(f"Ошибка API: {link_resp.status_code}")

            json_data = link_resp.json()
            raw_url = json_data.get("data")
            if not raw_url:
                raise ValueError("Поле 'data' не найдено в JSON")

            download_url = "https:" + raw_url.replace("\\/", "/")
            r = requests.get(download_url, headers=self.headers, cookies=cookies)
            
            if r.ok:
                with open(zip_path, "wb") as f:
                    f.write(r.content)
                return True
            else:
                self.log.emit(f"❌ Ошибка {r.status_code} при скачивании {title}")
                return False

        except Exception as e:
            self.log.emit(f"❌ Ошибка при обработке главы {title}: {e}")
            return False

    def _create_cbz_archive(self, final_cbz):
        """Создает CBZ архив из скачанных файлов"""
        self.log.emit("📦 Архивация в CBZ...")
        self.progress.emit(95, "Создание CBZ архива...")
        
        index = 1
        with zipfile.ZipFile(final_cbz, "w") as cbz:
            for zip_file in sorted(Path(self.DOWNLOADS_DIR).glob("*.zip")):
                if self._is_cancelled:
                    self.log.emit("❌ Архивация отменена")
                    break

                with zipfile.ZipFile(zip_file) as z:
                    for name in sorted(z.namelist()):
                        if self._is_cancelled:
                            break

                        ext = os.path.splitext(name)[1].lower()
                        out_name = f"{index:06}{ext}"
                        combined_dir = Path(self.TEMP_DIR)
                        z.extract(name, path=combined_dir)
                        os.rename(combined_dir / name, combined_dir / out_name)
                        cbz.write(combined_dir / out_name, arcname=out_name)
                        index += 1

        if self._is_cancelled and final_cbz.exists():
            try:
                final_cbz.unlink()
                self.log.emit(f"🧹 Удалён неполный архив: {final_cbz}")
            except Exception as e:
                self.log.emit(f"⚠️ Не удалось удалить архив: {e}")

    def _create_auto_split_pdf(self, manga_title_safe):
        """Автоматически создает разделенные PDF файлы по 100 страниц"""
        self.log.emit("📄 Создание PDF файлов (автоматическое разделение по 100 страниц)...")
        self.progress.emit(95, f"Создание PDF по {self.PAGES_PER_PDF} страниц в каждом...")
        
        image_files = []
        
        for zip_file in sorted(Path(self.DOWNLOADS_DIR).glob("*.zip")):
            if self._is_cancelled:
                self.log.emit("❌ Создание PDF отменено")
                break
                
            with zipfile.ZipFile(zip_file) as z:
                z.extractall(path=Path(self.TEMP_DIR))
                
                for name in sorted(z.namelist()):
                    if self._is_cancelled:
                        break
                        
                    if name.lower().endswith(('.jpg', '.jpeg', '.png', '.bmp', '.gif')):
                        image_path = Path(self.TEMP_DIR) / name
                        if image_path.exists():
                            image_files.append(str(image_path))
        
        if not image_files:
            self.log.emit("❌ Не найдено изображений для создания PDF")
            return []
            
        if self._is_cancelled:
            return []
        
        total_pages = len(image_files)
        self.log.emit(f"📊 Всего страниц: {total_pages}")
        
        # Если меньше или равно 100 страниц - создаем один файл
        if total_pages <= self.PAGES_PER_PDF:
            pdf_name = f"{manga_title_safe}.pdf"
            self.log.emit(f"📄 Создание единого PDF файла ({total_pages} страниц)...")
            
            try:
                with open(pdf_name, "wb") as f:
                    f.write(img2pdf.convert(image_files))
                
                self.created_files.append(pdf_name)
                self.log.emit(f"✅ Создан: {pdf_name}")
                return self.created_files
                
            except Exception as e:
                self.log.emit(f"❌ Ошибка при создании {pdf_name}: {e}")
                return []
        
        # Если больше 100 страниц - разделяем
        num_files = (total_pages + self.PAGES_PER_PDF - 1) // self.PAGES_PER_PDF
        
        created_files = []
        
        for i in range(num_files):
            if self._is_cancelled:
                break
                
            start_idx = i * self.PAGES_PER_PDF
            end_idx = min((i + 1) * self.PAGES_PER_PDF, total_pages)
            
            current_images = image_files[start_idx:end_idx]
            
            # Все файлы получают номер части
            pdf_name = f"{manga_title_safe}_part_{i+1:03d}.pdf"
                
            self.log.emit(f"📄 Создание PDF {i+1}/{num_files} (страницы {start_idx+1}-{end_idx})...")
            
            try:
                with open(pdf_name, "wb") as f:
                    f.write(img2pdf.convert(current_images))
                
                self.created_files.append(pdf_name)
                created_files.append(pdf_name)
                self.log.emit(f"✅ Создан: {pdf_name}")
                
            except Exception as e:
                self.log.emit(f"❌ Ошибка при создании {pdf_name}: {e}")
        
        return created_files


# ============================================================================
# МОДУЛЬ АПСКЕЙЛА PDF
# ============================================================================

class PDFUpscaler(QThread):
    """
    Класс для апскейла PDF файлов с помощью Real-ESRGAN
    """
    log = pyqtSignal(str)
    progress = pyqtSignal(int, str)  # прогресс в процентах и сообщение
    finished = pyqtSignal(bool, list)  # возвращаем список созданных файлов
    
    def __init__(self, input_files):
        super().__init__()
        self.input_files = input_files  # список файлов для апскейла
        self._stop_flag = False
        self.output_folder = "upscaled"
        
    def run(self):
        try:
            Image.MAX_IMAGE_PIXELS = None
            
            # Определяем путь к realesrgan
            realesrgan_path = self._find_realesrgan()
            if not realesrgan_path:
                self.finished.emit(False, [])
                return
            
            # Создаем папку для апскейленных файлов
            if not os.path.exists(self.output_folder):
                os.makedirs(self.output_folder)
                self.log.emit(f"📁 Создана папка: {self.output_folder}")
            
            total_files = len(self.input_files)
            upscaled_files = []
            
            for file_index, input_pdf in enumerate(self.input_files, 1):
                if self._stop_flag:
                    self.log.emit("🛑 Процесс остановлен пользователем")
                    break
                
                self.log.emit("=" * 50)
                self.log.emit(f"📄 Обработка файла {file_index}/{total_files}: {os.path.basename(input_pdf)}")
                self.log.emit("🤖 Модель: realesr-animevideov3")
                self.log.emit("📏 Масштаб: 2x")
                
                # Создаем имя для апскейленного файла
                base_name = os.path.basename(input_pdf)
                output_pdf = os.path.join(self.output_folder, base_name)
                
                self.progress.emit(int((file_index-1)/total_files*100), 
                                  f"Апскейл файла {file_index}/{total_files}...")
                
                try:
                    # Проверяем существует ли уже апскейленный файл
                    if os.path.exists(output_pdf):
                        self.log.emit(f"⚠️ Файл уже существует, пропускаю: {output_pdf}")
                        upscaled_files.append(output_pdf)
                        continue
                    
                    doc = fitz.open(input_pdf)
                    total_pages = len(doc)
                    doc.close()
                    
                    self.log.emit(f"📊 Всего страниц в PDF: {total_pages}")
                    
                    temp_img_folder = f'temp_pdf_images_{file_index}'
                    upscaled_img_folder = f'upscaled_pdf_images_{file_index}'
                    os.makedirs(temp_img_folder, exist_ok=True)
                    os.makedirs(upscaled_img_folder, exist_ok=True)
                    
                    # Извлечение страниц
                    doc = fitz.open(input_pdf)
                    image_paths = []
                    
                    for i in range(total_pages):
                        if self._stop_flag:
                            break
                            
                        page = doc[i]
                        pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), dpi=150)
                        img_path = os.path.join(temp_img_folder, f"page_{i+1:04d}.png")
                        pix.save(img_path)
                        image_paths.append(img_path)
                    
                    doc.close()
                    
                    if self._stop_flag:
                        self._cleanup_folders([temp_img_folder, upscaled_img_folder])
                        break
                    
                    # Апскейл изображений
                    upscaled_image_paths = []
                    
                    for i, img_path in enumerate(image_paths):
                        if self._stop_flag:
                            break
                            
                        page_num = i + 1
                        
                        imgname = os.path.splitext(os.path.basename(img_path))[0]
                        output_name = f'{imgname}_upscaled.png'
                        output_path = os.path.join(upscaled_img_folder, output_name)
                        
                        cmd = [
                            realesrgan_path,
                            '-i', img_path,
                            '-o', output_path,
                            '-n', 'realesr-animevideov3',
                            '-s', '2',
                            '-f', 'png'
                        ]
                        
                        try:
                            self.log.emit(f"🔍 Апскейл страницы {page_num}/{total_pages}...")
                            
                            # Скрываем консоль при запуске subprocess
                            if sys.platform == "win32":
                                # На Windows используем CREATE_NO_WINDOW
                                startupinfo = subprocess.STARTUPINFO()
                                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                                startupinfo.wShowWindow = subprocess.SW_HIDE
                                
                                process = subprocess.Popen(cmd, 
                                                         stdout=subprocess.PIPE, 
                                                         stderr=subprocess.STDOUT,
                                                         text=True, 
                                                         bufsize=1,
                                                         universal_newlines=True,
                                                         startupinfo=startupinfo)
                            else:
                                # На Linux/Mac
                                process = subprocess.Popen(cmd,
                                                         stdout=subprocess.PIPE,
                                                         stderr=subprocess.STDOUT,
                                                         text=True,
                                                         bufsize=1,
                                                         universal_newlines=True)
                            
                            for line in process.stdout:
                                if self._stop_flag:
                                    process.terminate()
                                    break
                                # Фильтруем вывод, показываем только прогресс
                                line = line.strip()
                                if line and '%' in line:
                                    # Извлекаем процент
                                    percent_match = re.search(r'(\d+\.?\d*)%', line)
                                    if percent_match:
                                        percent = percent_match.group(1)
                                        self.log.emit(f"  📊 Прогресс: {percent}%")
                            
                            process.wait()
                            
                            if process.returncode == 0:
                                upscaled_image_paths.append(output_path)
                            else:
                                self.log.emit(f"❌ Ошибка апскейла страницы {page_num}")
                                
                        except Exception as e:
                            self.log.emit(f"❌ Ошибка при апскейле страницы {page_num}: {e}")
                    
                    if self._stop_flag:
                        self._cleanup_folders([temp_img_folder, upscaled_img_folder])
                        break
                    
                    if not upscaled_image_paths:
                        self.log.emit("❌ Нет апскейленных изображений!")
                        self._cleanup_folders([temp_img_folder, upscaled_img_folder])
                        continue
                    
                    # Создание PDF
                    try:
                        with open(output_pdf, "wb") as f:
                            f.write(img2pdf.convert(upscaled_image_paths))
                        upscaled_files.append(output_pdf)
                        self.log.emit(f"✅ PDF успешно создан: {output_pdf}")
                    except Exception as e:
                        self.log.emit(f"❌ Ошибка создания PDF: {e}")
                    
                    # Очистка временных файлов
                    self._cleanup_folders([temp_img_folder, upscaled_img_folder])
                    
                except Exception as e:
                    self.log.emit(f"❌ Ошибка при обработке файла {input_pdf}: {e}")
            
            if self._stop_flag:
                self.finished.emit(False, [])
                return
            
            self.progress.emit(100, "Готово!")
            self.log.emit("=" * 50)
            self.log.emit("✅ Обработка завершена успешно!")
            self.log.emit(f"📁 Создано файлов: {len(upscaled_files)}")
            for f in upscaled_files:
                self.log.emit(f"  • {f}")
            self.log.emit("=" * 50)
            self.finished.emit(True, upscaled_files)
            
        except Exception as e:
            self.log.emit(f"❌ Критическая ошибка: {e}")
            import traceback
            self.log.emit(traceback.format_exc())
            self.finished.emit(False, [])
    
    def _find_realesrgan(self):
        """Находит путь к realesrgan"""
        if getattr(sys, 'frozen', False):
            # Если запущено из EXE
            if hasattr(sys, '_MEIPASS'):
                base_path = sys._MEIPASS
            else:
                base_path = os.path.dirname(sys.executable)
            
            # Ищем в разных местах
            paths_to_try = [
                os.path.join(base_path, 'realesrgan-ncnn-vulkan-20220424-windows', 'realesrgan-ncnn-vulkan.exe'),
                os.path.join(base_path, 'realesrgan-ncnn-vulkan.exe'),
                os.path.join('.', 'realesrgan-ncnn-vulkan-20220424-windows', 'realesrgan-ncnn-vulkan.exe'),
                os.path.join('.', 'realesrgan-ncnn-vulkan.exe'),
            ]
        else:
            # Если запущено из скрипта
            script_dir = os.path.dirname(os.path.abspath(__file__))
            paths_to_try = [
                os.path.join(script_dir, 'realesrgan-ncnn-vulkan-20220424-windows', 'realesrgan-ncnn-vulkan.exe'),
                os.path.join(script_dir, 'realesrgan-ncnn-vulkan.exe'),
            ]
        
        for path in paths_to_try:
            if os.path.exists(path):
                self.log.emit(f"✅ Найден Real-ESRGAN: {path}")
                return path
        
        self.log.emit("❌ Real-ESRGAN не найден!")
        self.log.emit("📥 Скачайте с: https://github.com/xinntao/Real-ESRGAN/releases")
        self.log.emit("📁 Положите в папку с программой")
        return None
    
    def _cleanup_folders(self, folders):
        """Очищает временные папки"""
        for folder in folders:
            if os.path.exists(folder):
                try:
                    for f in os.listdir(folder):
                        try:
                            os.remove(os.path.join(folder, f))
                        except:
                            pass
                    os.rmdir(folder)
                except Exception as e:
                    pass
    
    def stop(self):
        """Останавливает процесс апскейла"""
        self._stop_flag = True
        self.log.emit("🛑 Остановка процесса...")


# ============================================================================
# ГЛАВНЫЙ ИНТЕРФЕЙС
# ============================================================================

class MangaDownloaderApp(QWidget):
    """
    Главное окно приложения с одной вкладкой:
    Скачивание манги (с автоматическим разделением PDF по 100 страниц)
    """
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Manga Downloader")
        self.setGeometry(100, 100, 800, 600)
        
        main_layout = QVBoxLayout(self)
        
        settings_group = QGroupBox("Настройки скачивания")
        settings_layout = QGridLayout()
        
        # Режим скачивания
        settings_layout.addWidget(QLabel("Режим:"), 0, 0)
        self.mode_combo = QComboBox()
        self.mode_combo.addItems(["Скачать всю мангу (по умолчанию)", "Ждать кнопку скачивания"])
        self.mode_combo.setCurrentIndex(0)
        settings_layout.addWidget(self.mode_combo, 0, 1)
        
        # Формат вывода
        settings_layout.addWidget(QLabel("Формат:"), 1, 0)
        self.format_combo = QComboBox()
        self.format_combo.addItems(["CBZ", "PDF"])
        self.format_combo.setCurrentText("PDF")
        settings_layout.addWidget(self.format_combo, 1, 1)
        
        # URL сайта
        settings_layout.addWidget(QLabel("URL сайта:"), 2, 0)
        self.url_input = QLineEdit("https://com-x.life")
        settings_layout.addWidget(self.url_input, 2, 1)
        
        # Путь к Firefox
        settings_layout.addWidget(QLabel("Путь к Firefox:"), 3, 0)
        self.firefox_path_input = QLineEdit()
        self.firefox_path_input.setPlaceholderText("Автопоиск или укажите путь")
        settings_layout.addWidget(self.firefox_path_input, 3, 1)
        
        # Кнопка выбора Firefox
        self.select_firefox_button = QPushButton("Выбрать")
        self.select_firefox_button.clicked.connect(self.select_firefox_path)
        settings_layout.addWidget(self.select_firefox_button, 3, 2)
        
        # Информация о разделении PDF
        info_label = QLabel("При выборе формата PDF файлы автоматически делятся по 100 страниц")
        info_label.setStyleSheet("color: #2196F3; font-style: italic;")
        settings_layout.addWidget(info_label, 4, 0, 1, 3)
        
        # Кнопка сохранения настроек
        self.save_settings_button = QPushButton("Сохранить настройки")
        self.save_settings_button.clicked.connect(self.save_settings)
        settings_layout.addWidget(self.save_settings_button, 5, 0, 1, 3)
        
        settings_group.setLayout(settings_layout)
        main_layout.addWidget(settings_group)
        
        # Прогресс бар
        self.download_progress = QProgressBar()
        main_layout.addWidget(QLabel("Прогресс:"))
        main_layout.addWidget(self.download_progress)
        
        # Кнопки управления
        button_layout = QHBoxLayout()
        self.download_button = QPushButton("Открыть сайт")
        self.download_button.setFont(QFont("Arial", 10, QFont.Bold))
        self.download_button.setStyleSheet("background-color: #4CAF50; color: white; padding: 8px;")
        
        self.download_cancel_button = QPushButton("Отмена")
        self.download_cancel_button.setFont(QFont("Arial", 10))
        self.download_cancel_button.setStyleSheet("background-color: #f44336; color: white; padding: 8px;")
        self.download_cancel_button.hide()
        
        button_layout.addWidget(self.download_button)
        button_layout.addWidget(self.download_cancel_button)
        button_layout.addStretch()
        
        main_layout.addLayout(button_layout)
        
        # Логи
        self.download_logs = QTextEdit(readOnly=True)
        self.download_logs.setFont(QFont("Courier", 9))
        self.download_logs.setStyleSheet("background-color: #f5f5f5;")
        main_layout.addWidget(QLabel("Логи:"))
        main_layout.addWidget(self.download_logs)
        
        self.status_label = QLabel("Готов к работе")
        self.status_label.setStyleSheet("color: #666; padding: 5px; border-top: 1px solid #ddd;")
        main_layout.addWidget(self.status_label)
        
        self.manga_worker = None
        self.upscale_worker = None
        self.created_files = []
        
        self.download_button.clicked.connect(self.start_download)
        self.download_cancel_button.clicked.connect(self.cancel_download)
        
        # Загрузка настроек
        self.load_settings()

    def select_firefox_path(self):
        """Пользователь выбирает путь к Firefox"""
        filename, _ = QFileDialog.getOpenFileName(
            self, "Выберите Firefox.exe",
            "", "Firefox (firefox.exe);;Все файлы (*.*)"
        )
        if filename:
            self.firefox_path_input.setText(filename)
            # Автоматически сохраняем настройки
            self.save_settings()

    def save_settings(self):
        """Сохраняет все настройки в конфиг файл"""
        try:
            config = {
                "firefox_path": self.firefox_path_input.text(),
                "default_url": self.url_input.text(),
                "default_format": self.format_combo.currentText(),
                "default_mode": self.mode_combo.currentIndex(),
                "auto_save_settings": True
            }
            
            Config.save(config)
            self.download_logs.append("✅ Настройки сохранены")
        except Exception as e:
            self.download_logs.append(f"❌ Ошибка сохранения настроек: {e}")

    def load_settings(self):
        """Загружает настройки из конфиг файла"""
        try:
            config = Config.load()
            
            # Загружаем настройки в интерфейс
            if config.get("firefox_path"):
                self.firefox_path_input.setText(config["firefox_path"])
            
            if config.get("default_url"):
                self.url_input.setText(config["default_url"])
            
            if config.get("default_format"):
                index = self.format_combo.findText(config["default_format"])
                if index >= 0:
                    self.format_combo.setCurrentIndex(index)
            
            if config.get("default_mode") is not None:
                mode = config["default_mode"]
                if mode < self.mode_combo.count():
                    self.mode_combo.setCurrentIndex(mode)
                    
        except Exception as e:
            self.download_logs.append(f"⚠️ Ошибка загрузки настроек: {e}")

    def start_download(self):
        """Запуск скачивания манги"""
        url = self.url_input.text().strip()
        if not url:
            self.download_logs.append("❌ Введите URL сайта")
            return
            
        if not url.startswith(('http://', 'https://')):
            url = 'https://' + url
            
        self.url_input.setText(url)
        
        download_all = self.mode_combo.currentIndex() == 0
        firefox_path = self.firefox_path_input.text().strip()
        
        self.download_logs.append("▶️ Запуск Manga Downloader...")
        self.download_logs.append(f"📁 Формат вывода: {self.format_combo.currentText()}")
        self.download_logs.append(f"🌐 URL сайта: {url}")
        self.download_logs.append(f"📚 Режим: {'Скачать всю мангу' if download_all else 'Ждать кнопку скачивания'}")
        self.download_logs.append("📄 PDF файлы будут автоматически разделены по 100 страниц")
        if firefox_path:
            self.download_logs.append(f"🦊 Путь к Firefox: {firefox_path}")
        
        self.download_button.setEnabled(False)
        self.download_cancel_button.show()
        
        self.manga_worker = MangaDownloader(
            output_format=self.format_combo.currentText().lower(),
            base_url=url,
            download_all=download_all,
            firefox_path=firefox_path if firefox_path else None
        )
        self.manga_worker.download_started.connect(self.download_started)
        self.manga_worker.log.connect(self.download_logs.append)
        self.manga_worker.progress.connect(self.update_download_progress)
        self.manga_worker.finished.connect(self.download_finished)
        self.manga_worker.start()
        
        # Автоматически сохраняем настройки
        self.save_settings()

    def download_started(self):
        """Слот для начала скачивания"""
        self.status_label.setText("Скачивание начато...")
        self.status_label.setStyleSheet("color: #4CAF50; font-weight: bold; padding: 5px;")

    def update_download_progress(self, value, message):
        """Обновление прогресса скачивания"""
        self.download_progress.setValue(value)
        self.status_label.setText(message)

    def cancel_download(self):
        """Отмена скачивания"""
        if self.manga_worker:
            self.manga_worker.cancel()
            self.download_logs.append("🛑 Запрошена отмена...")
            self.status_label.setText("Отмена...")
            self.status_label.setStyleSheet("color: #f44336; font-weight: bold; padding: 5px;")

    def download_finished(self, ok, created_files):
        """Завершение скачивания"""
        self.download_button.setEnabled(True)
        self.download_cancel_button.hide()
        self.download_progress.setValue(0)
        self.created_files = created_files
        
        if self.manga_worker._is_cancelled:
            self.download_logs.append("🛑 Скачивание отменено пользователем")
            self.status_label.setText("Скачивание отменено")
            self.status_label.setStyleSheet("color: #ff9800; padding: 5px;")
        elif ok:
            self.download_logs.append("✅ Скачивание завершено успешно!")
            self.status_label.setText("Готово!")
            self.status_label.setStyleSheet("color: #4CAF50; font-weight: bold; padding: 5px;")
            
            if self.format_combo.currentText().upper() == "PDF" and created_files:
                self.download_logs.append(f"📁 Создано файлов: {len(created_files)}")
                for f in created_files:
                    self.download_logs.append(f"  • {f}")
                
                # Предлагаем апскейл
                self.offer_upscale(created_files)
            else:
                QMessageBox.information(self, "Готово!", 
                    "Манга успешно скачана в CBZ формате!")
        else:
            self.download_logs.append("❌ Скачивание завершено с ошибкой.")
            self.status_label.setText("Ошибка!")
            self.status_label.setStyleSheet("color: #f44336; font-weight: bold; padding: 5px;")

    def offer_upscale(self, pdf_files):
        """Предлагает пользователю сделать апскейл созданных файлов"""
        reply = QMessageBox.question(
            self, 'Апскейл PDF',
            f'Скачивание завершено! Создано {len(pdf_files)} PDF файл(ов).\n\n'
            'Хотите сделать апскейл всех файлов для улучшения качества?\n'
            'Апскейленные файлы будут сохранены в папку "upscaled/"',
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes
        )
        
        if reply == QMessageBox.Yes:
            self.start_upscale(pdf_files)
        else:
            QMessageBox.information(self, "Готово!", 
                f"Скачивание завершено!\nСоздано {len(pdf_files)} файл(ов).")

    def start_upscale(self, pdf_files):
        """Запуск апскейла всех PDF файлов"""
        self.download_logs.append("\n▶️ Запуск апскейла PDF файлов...")
        self.download_logs.append(f"📁 Файлы будут сохранены в папку: upscaled/")
        self.status_label.setText("Апскейл файлов...")
        
        self.upscale_worker = PDFUpscaler(
            input_files=pdf_files
        )
        self.upscale_worker.log.connect(self.download_logs.append)
        self.upscale_worker.progress.connect(self.update_upscale_progress)
        self.upscale_worker.finished.connect(self.upscale_finished)
        self.upscale_worker.start()

    def update_upscale_progress(self, value, message):
        """Обновление прогресса апскейла"""
        self.download_progress.setValue(value)
        self.status_label.setText(message)

    def upscale_finished(self, ok, upscaled_files):
        """Завершение апскейла"""
        if ok:
            self.download_logs.append("✅ Апскейл всех файлов завершен успешно!")
            self.download_logs.append(f"📁 Апскейленные файлы сохранены в папку: upscaled/")
            self.status_label.setText("Апскейл завершен!")
            self.status_label.setStyleSheet("color: #4CAF50; font-weight: bold; padding: 5px;")
            
            QMessageBox.information(self, "Готовo!", 
                f"Все PDF файлы успешно апскейлены!\n"
                f"Создано {len(upscaled_files)} файл(ов) в папке 'upscaled/'")
        else:
            self.download_logs.append("❌ Апскейл завершен с ошибкой.")
            self.status_label.setText("Ошибка апскейла!")
            self.status_label.setStyleSheet("color: #f44336; font-weight: bold; padding: 5px;")
        
        self.download_progress.setValue(0)


def main():
    app = QApplication(sys.argv)
    
    try:
        import img2pdf
        from PIL import Image
        import fitz
    except ImportError as e:
        print(f"❌ Необходимо установить дополнительные библиотеки:")
        print(f"pip install img2pdf pillow PyMuPDF")
        print(f"Ошибка: {e}")
        
        msg_box = QMessageBox()
        msg_box.setIcon(QMessageBox.Critical)
        msg_box.setText("Не установлены необходимые библиотеки!")
        msg_box.setInformativeText(f"Ошибка: {e}\n\nУстановите: pip install img2pdf pillow PyMuPDF")
        msg_box.exec_()
        sys.exit(1)
    
    win = MangaDownloaderApp()
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()