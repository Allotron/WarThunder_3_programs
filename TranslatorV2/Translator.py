import sys
import time
import requests
import re
import json
import os
import pyperclip
import keyboard
from googletrans import Translator
import speech_recognition as sr

from PyQt6.QtWidgets import (QApplication, QWidget, QVBoxLayout, QLabel, 
                             QSystemTrayIcon, QMenu, QSlider, QPushButton, 
                             QGroupBox, QFormLayout, QMainWindow, 
                             QMessageBox, QLineEdit, QSpinBox, QTextBrowser, QComboBox)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QTimer
from PyQt6.QtGui import QIcon, QPixmap, QPainter, QPen

# --- КОНФИГУРАЦИЯ ---
CONFIG_FILE = "config.json"
API_URL = "http://localhost:8111"

DEFAULT_CONFIG = {
    "overlay_x": 50,
    "overlay_y": 400,
    "overlay_w": 600,
    "overlay_h": 300,
    "monitor_idx": 0,           # Индекс монитора
    "target_lang": "en",
    "trigger_word": "программа",
    "term_word": "точка",       # Это и есть ваше слово "СТОП"
    "result_duration": 5,
    "toggle_hotkey": "F8",      # Скрыть/Показать
    "scroll_hotkey": "F7",      # Режим скролла (мыши)
    "input_mode": 0,            # 0 = Голос (постоянно), 1 = По кнопке
    "mic_hotkey": "F10"         # Кнопка для активации (если режим 1)
}

LANG_MAP = {
    "английский": "en",
    "немецкий": "de",
    "китайский": "zh-cn",
    "японский": "ja",
    "французский": "fr",
    "испанский": "es",
    "русский": "ru"
}

# --- МЕНЕДЖЕР НАСТРОЕК ---
class ConfigManager:
    @staticmethod
    def load():
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, "r") as f:
                    data = json.load(f)
                    for k, v in DEFAULT_CONFIG.items():
                        if k not in data: data[k] = v
                    return data
            except: pass
        return DEFAULT_CONFIG.copy()

    @staticmethod
    def save(config):
        with open(CONFIG_FILE, "w") as f:
            json.dump(config, f)

# --- ПОТОК: ЗАПИСЬ КНОПКИ (ДЛЯ НАСТРОЕК) ---
class HotKeyRecorder(QThread):
    finished_signal = pyqtSignal(str)

    def run(self):
        try:
            time.sleep(0.3) # Пауза, чтобы не словить нажатие мыши или старой кнопки
            key = keyboard.read_hotkey(suppress=False)
            self.finished_signal.emit(key)
        except Exception:
            pass

# --- ПОТОК: ОПРОС КЛАВИШ ---
class KeyPollingWorker(QThread):
    toggle_signal = pyqtSignal()
    scroll_signal = pyqtSignal()
    mic_trigger_signal = pyqtSignal()

    def __init__(self, config):
        super().__init__()
        self.config = config
        self.running = True

    def run(self):
        is_toggle_pressed = False
        is_scroll_pressed = False
        is_mic_pressed = False

        while self.running:
            time.sleep(0.05) 
            try:
                # 1. Скрыть/Показать
                hk_toggle = self.config.get('toggle_hotkey', 'F8')
                if keyboard.is_pressed(hk_toggle):
                    if not is_toggle_pressed:
                        self.toggle_signal.emit()
                        is_toggle_pressed = True
                else:
                    is_toggle_pressed = False 

                # 2. Скролл
                hk_scroll = self.config.get('scroll_hotkey', 'F7')
                if keyboard.is_pressed(hk_scroll):
                    if not is_scroll_pressed:
                        self.scroll_signal.emit()
                        is_scroll_pressed = True
                else:
                    is_scroll_pressed = False

                # 3. Микрофон (Только если включен режим "По кнопке")
                if self.config.get('input_mode', 0) == 1:
                    hk_mic = self.config.get('mic_hotkey', 'F10')
                    if keyboard.is_pressed(hk_mic):
                        if not is_mic_pressed:
                            self.mic_trigger_signal.emit()
                            is_mic_pressed = True
                    else:
                        is_mic_pressed = False

            except Exception:
                pass

    def stop(self):
        self.running = False

# --- ПОТОК: ЧАТ ---
class ChatWorker(QThread):
    new_message_signal = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.translator = Translator()
        self.last_msg_id = 0
        self.running = True

    def run(self):
        while self.running:
            try:
                r = requests.get(f"{API_URL}/gamechat?lastId={self.last_msg_id}", timeout=1)
                if r.status_code == 200:
                    msgs = r.json()
                    if isinstance(msgs, list):
                        for msg in msgs:
                            mid = msg.get("id", 0)
                            if mid <= self.last_msg_id: continue
                            
                            sender = msg.get("sender", "Unknown")
                            raw_text = msg.get("msg", "")
                            
                            if not raw_text or re.search('[а-яА-Я]', raw_text):
                                self.last_msg_id = mid
                                continue

                            try:
                                tr = self.translator.translate(raw_text, dest='ru')
                                html = (f"<div style='margin-bottom: 5px;'>"
                                        f"<span style='color:#AAA; font-size:10pt;'>[{tr.src.upper()}]</span> "
                                        f"<span style='color:#FFFF00; font-weight:bold;'>{sender}:</span> "
                                        f"<span style='color:#FFF;'>{raw_text}</span><br>"
                                        f"<span style='color:#00FF00; font-style:italic;'>&nbsp;&nbsp;» {tr.text}</span>"
                                        f"</div>")
                                self.new_message_signal.emit(html)
                            except: pass
                            self.last_msg_id = mid
            except: pass
            time.sleep(1)

    def stop(self): self.running = False

# --- ПОТОК: ГОЛОС ---
class VoiceWorker(QThread):
    status_signal = pyqtSignal(str, str, int)
    toggle_window_signal = pyqtSignal(bool)
    toggle_mouse_signal = pyqtSignal(bool) 
    lang_update_signal = pyqtSignal(str)

    def __init__(self, config):
        super().__init__()
        self.recognizer = sr.Recognizer()
        self.mic = sr.Microphone()
        self.translator = Translator()
        self.running = True
        
        self.manual_trigger = False # Флаг нажатия кнопки
        self.update_params(config)

        self.recognizer.dynamic_energy_threshold = True
        self.recognizer.energy_threshold = 1000 
        self.recognizer.pause_threshold = 0.8  

    def update_params(self, config):
        self.config = config
        self.target_lang = config.get('target_lang', 'en')
        self.trigger = config.get('trigger_word', 'программа').lower().strip()
        self.term_word = config.get('term_word', 'точка').lower().strip()
        self.result_dur = config.get('result_duration', 5) * 1000
        self.input_mode = config.get('input_mode', 0)

    def trigger_manual_listen(self):
        """Вызывается из GUI при нажатии хоткея микрофона"""
        if self.input_mode == 1:
            self.manual_trigger = True

    def listen_until_stop(self, source):
        """Режим бесконечного прослушивания до стоп-слова"""
        full_text = []
        self.recognizer.pause_threshold = 2.0 
        
        while True:
            try:
                audio = self.recognizer.listen(source, timeout=None)
                self.status_signal.emit("⏳ Обработка фразы...", "rgba(255, 165, 0, 180)", 0)
                
                chunk = self.recognizer.recognize_google(audio, language="ru-RU").lower()
                
                if self.term_word in chunk:
                    parts = chunk.split(self.term_word)
                    if parts[0].strip(): full_text.append(parts[0])
                    break 
                else:
                    full_text.append(chunk)
                    self.status_signal.emit(f"🔴 ЗАПИСЬ (Скажите '{self.term_word}')...", "rgba(255, 0, 0, 200)", 0)
                    
            except sr.UnknownValueError: continue
            except Exception: break
            
        self.recognizer.pause_threshold = 0.8
        return " ".join(full_text).strip()

    def run(self):
        with self.mic as source:
            self.recognizer.adjust_for_ambient_noise(source, duration=1)
        
        self.status_signal.emit(f"Цель: {self.target_lang.upper()}", "rgba(0,0,255,100)", 0)

        with self.mic as source:
            while self.running:
                try:
                    # --- 1. ОЖИДАНИЕ АКТИВАЦИИ ---
                    if self.input_mode == 1: # Режим кнопки
                        if not self.manual_trigger:
                            time.sleep(0.05)
                            continue
                        else:
                            # Кнопка нажата!
                            self.manual_trigger = False
                            self.status_signal.emit("🎤 Слушаю (4 сек)...", "rgba(0, 150, 255, 200)", 0)
                            phrase_limit = 4
                            timeout_val = 3
                    else: # Голосовой режим (постоянно)
                        phrase_limit = 3
                        timeout_val = 1

                    # --- 2. СЛУШАЕМ КОМАНДУ ---
                    try:
                        audio = self.recognizer.listen(source, phrase_time_limit=phrase_limit, timeout=timeout_val)
                    except sr.WaitTimeoutError:
                        if self.input_mode == 1:
                            self.status_signal.emit(f"Цель: {self.target_lang.upper()}", "rgba(0,0,255,100)", 0)
                        continue 

                    command = self.recognizer.recognize_google(audio, language="ru-RU").lower()
                    
                    # --- 3. ПРОВЕРКА ТРИГГЕРА ---
                    clean_cmd = command
                    if self.input_mode == 0:
                        if self.trigger not in command: 
                            continue
                        clean_cmd = command.replace(self.trigger, "").strip()
                    
                    # --- 4. ОБРАБОТКА КОМАНД ---
                    if "сообщен" in clean_cmd:
                        self.status_signal.emit(f"🔴 ДИКТУЙТЕ (Стоп: '{self.term_word}')", "rgba(255, 0, 0, 200)", 0)
                        text = self.listen_until_stop(source)
                        
                        if text:
                            self.status_signal.emit("⏳ Перевод...", "rgba(255, 165, 0, 200)", 0)
                            text = text.capitalize()
                            if self.target_lang == 'ru':
                                final_text = text
                                out = f"🗣 {final_text}"
                            else:
                                try:
                                    final_text = self.translator.translate(text, dest=self.target_lang).text
                                    out = f"🇷🇺 {text}<br>⬇️<br>🚩 {final_text}"
                                except:
                                    out = "❌ Ошибка сети"
                                    final_text = text
                            
                            pyperclip.copy(final_text)
                            self.status_signal.emit(out, "rgba(0, 150, 0, 200)", self.result_dur)
                        else:
                             self.status_signal.emit(f"Цель: {self.target_lang.upper()}", "rgba(0,0,255,100)", 0)

                    elif "скрыть" in clean_cmd: 
                        self.toggle_window_signal.emit(False)
                    elif "показать" in clean_cmd: 
                        self.toggle_window_signal.emit(True)
                    elif "чат" in clean_cmd:
                        self.status_signal.emit("🖱️ РЕЖИМ ЧАТА (Мышь)", "rgba(128, 0, 128, 200)", 0)
                        self.toggle_mouse_signal.emit(True)
                    elif "игра" in clean_cmd:
                        self.status_signal.emit("🎮 РЕЖИМ ИГРЫ", "rgba(0,0,255,100)", 2000)
                        self.toggle_mouse_signal.emit(False)
                    else:
                        found_lang = False
                        for name, code in LANG_MAP.items():
                            if name in clean_cmd:
                                self.target_lang = code
                                self.lang_update_signal.emit(code)
                                self.status_signal.emit(f"РЕЖИМ: {name.upper()}", "rgba(255, 200, 0, 200)", 2000)
                                found_lang = True
                                break
                        
                        if not found_lang and self.input_mode == 1:
                            pass 

                    if self.input_mode == 1:
                        QTimer.singleShot(1500, lambda: self.status_signal.emit(f"Цель: {self.target_lang.upper()}", "rgba(0,0,255,100)", 0))

                except sr.UnknownValueError:
                    if self.input_mode == 1:
                        self.status_signal.emit(f"Цель: {self.target_lang.upper()}", "rgba(0,0,255,100)", 0)
                except Exception: pass

    def stop(self): self.running = False

# --- ИНТЕРФЕЙС ОВЕРЛЕЯ ---
class Overlay(QWidget):
    def __init__(self, config):
        super().__init__()
        self.config = config
        self.show_border = False
        self.interaction_timer = QTimer()
        self.interaction_timer.setInterval(10000)
        self.interaction_timer.timeout.connect(self.disable_mouse)
        self.init_ui()

    def init_ui(self):
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool | Qt.WindowType.WindowTransparentForInput)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        self.layout = QVBoxLayout()
        self.layout.setContentsMargins(5, 5, 5, 5)
        self.layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        
        self.lbl_status = QLabel(f"Цель: {self.config['target_lang'].upper()}")
        self.lbl_status.setStyleSheet("color: white; background-color: rgba(0,0,255,100); border-radius: 5px; padding: 5px; font-size: 16px; font-weight: bold;")
        self.lbl_status.setWordWrap(True) 
        self.lbl_status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        
        self.chat_area = QTextBrowser()
        self.chat_area.setStyleSheet("""
            QTextBrowser {
                background-color: rgba(0, 0, 0, 120);
                color: white;
                border: none;
                border-radius: 5px;
                font-size: 16px;
                font-family: Arial;
            }
            QScrollBar:vertical {
                background: rgba(0,0,0,50);
                width: 10px;
            }
            QScrollBar::handle:vertical {
                background: rgba(255,255,255,100);
                min-height: 20px;
                border-radius: 5px;
            }
        """)
        self.chat_area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.chat_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        
        self.layout.addWidget(self.lbl_status)
        self.layout.addWidget(self.chat_area)
        self.setLayout(self.layout)
        self.update_geometry()

    def update_geometry(self):
        # --- НОВАЯ ЛОГИКА: ОПРЕДЕЛЯЕМ КООРДИНАТЫ ЭКРАНА ---
        screens = QApplication.screens()
        idx = self.config.get('monitor_idx', 0)
        
        start_x, start_y = 0, 0
        if idx < len(screens):
            geo = screens[idx].geometry()
            start_x = geo.x()
            start_y = geo.y()
            
        # Позиция = Координата монитора + Отступ из конфига
        abs_x = start_x + self.config['overlay_x']
        abs_y = start_y + self.config['overlay_y']
        
        self.setGeometry(abs_x, abs_y, 
                         self.config['overlay_w'], self.config.get('overlay_h', 300))
        
        self.show_border = True
        self.update()
        QTimer.singleShot(2000, self.hide_border)

    def hide_border(self):
        self.show_border = False
        self.update()

    def paintEvent(self, event):
        if self.show_border:
            p = QPainter(self)
            p.setPen(QPen(Qt.GlobalColor.red, 3, Qt.PenStyle.DashLine))
            p.drawRect(0, 0, self.width()-1, self.height()-1)

    def append_chat(self, html_text):
        self.chat_area.append(html_text)
        if self.windowFlags() & Qt.WindowType.WindowTransparentForInput:
            sb = self.chat_area.verticalScrollBar()
            sb.setValue(sb.maximum())

    def set_status(self, text, style, duration_ms):
        self.lbl_status.setText(text)
        self.lbl_status.setStyleSheet(f"color: white; background-color: {style}; border-radius: 5px; padding: 5px; font-size: 16px; font-weight: bold;")
        self.lbl_status.adjustSize()
        if duration_ms > 0:
            QTimer.singleShot(duration_ms, self.reset_status)

    def reset_status(self):
        self.lbl_status.setText(f"Цель: {self.config['target_lang'].upper()}")
        self.lbl_status.setStyleSheet("color: white; background-color: rgba(0,0,255,100); border-radius: 5px; padding: 5px; font-size: 16px; font-weight: bold;")
        
        if not self.interaction_timer.isActive():
             self.disable_mouse()
        
        # Обновляем геометрию (на случай смены монитора)
        self.update_geometry()

    def enable_mouse(self, active):
        if active:
            self.setWindowFlag(Qt.WindowType.WindowTransparentForInput, False)
            self.show()
            self.chat_area.setStyleSheet("""
                QTextBrowser {
                    background-color: rgba(0, 0, 0, 200);
                    color: white;
                    border: 1px solid yellow;
                    font-size: 16px;
                    font-family: Arial;
                }
            """)
            self.interaction_timer.start()
        else:
            self.disable_mouse()

    def disable_mouse(self):
        self.interaction_timer.stop()
        self.setWindowFlag(Qt.WindowType.WindowTransparentForInput, True)
        self.show()
        
        self.chat_area.setStyleSheet("""
            QTextBrowser {
                background-color: rgba(0, 0, 0, 120);
                color: white;
                border: none;
                font-size: 16px;
                font-family: Arial;
            }
        """)
        
        self.lbl_status.setText(f"Цель: {self.config['target_lang'].upper()}")
        self.lbl_status.setStyleSheet("color: white; background-color: rgba(0,0,255,100); border-radius: 5px; padding: 5px; font-size: 16px; font-weight: bold;")
        
        sb = self.chat_area.verticalScrollBar()
        sb.setValue(sb.maximum())

# --- ГЛАВНОЕ ОКНО ---
class Settings(QMainWindow):
    def __init__(self):
        super().__init__()
        self.config = ConfigManager.load()
        self.overlay = Overlay(self.config)
        self.overlay.show()
        self.is_overlay_visible = True
        
        self.recording_target = None 

        self.t_chat = ChatWorker()
        self.t_chat.new_message_signal.connect(self.overlay.append_chat)
        self.t_chat.start()

        self.t_voice = VoiceWorker(self.config)
        self.t_voice.status_signal.connect(self.overlay.set_status)
        self.t_voice.toggle_window_signal.connect(self.set_overlay_visibility)
        self.t_voice.toggle_mouse_signal.connect(self.overlay.enable_mouse)
        self.t_voice.lang_update_signal.connect(self.on_lang_voice_change)
        self.t_voice.start()
        
        self.t_keys = KeyPollingWorker(self.config)
        self.t_keys.toggle_signal.connect(self.toggle_overlay_by_hotkey)
        self.t_keys.scroll_signal.connect(self.toggle_scroll_mode_by_hotkey)
        self.t_keys.mic_trigger_signal.connect(self.trigger_mic_by_hotkey)
        self.t_keys.start()
        
        self.hotkey_recorder = None
        self.init_ui()

    def init_ui(self):
        self.setWindowTitle("Translator")
        self.setFixedSize(400, 600) # Чуть увеличили высоту
        cw = QWidget()
        self.setCentralWidget(cw)
        lay = QVBoxLayout(cw)

        grp_pos = QGroupBox("Размер и Позиция")
        form_pos = QFormLayout()
        
        # --- ВЫБОР МОНИТОРА ---
        self.cb_monitor = QComboBox()
        screens = QApplication.screens()
        for i, s in enumerate(screens):
            geo = s.geometry()
            self.cb_monitor.addItem(f"Экран {i} ({geo.width()}x{geo.height()})", i)
        
        # Установка текущего монитора из конфига
        saved_idx = self.config.get('monitor_idx', 0)
        if saved_idx < len(screens):
            self.cb_monitor.setCurrentIndex(saved_idx)
            
        self.cb_monitor.currentIndexChanged.connect(self.on_monitor_changed)
        form_pos.addRow("Монитор:", self.cb_monitor)
        # ----------------------

        self.sl_x = QSlider(Qt.Orientation.Horizontal)
        self.sl_y = QSlider(Qt.Orientation.Horizontal)
        
        # Настройка диапазонов под текущий монитор
        self.update_slider_ranges()
        
        self.sl_x.setValue(self.config['overlay_x'])
        self.sl_y.setValue(self.config['overlay_y'])
        
        self.sl_w = QSlider(Qt.Orientation.Horizontal); self.sl_w.setRange(200, 1200); self.sl_w.setValue(self.config['overlay_w'])
        self.sl_h = QSlider(Qt.Orientation.Horizontal); self.sl_h.setRange(100, 1000); self.sl_h.setValue(self.config.get('overlay_h', 300))
        
        for s in [self.sl_x, self.sl_y, self.sl_w, self.sl_h]: s.valueChanged.connect(self.upd)
        
        form_pos.addRow("X (Отступ):", self.sl_x)
        form_pos.addRow("Y (Отступ):", self.sl_y)
        form_pos.addRow("Ширина:", self.sl_w)
        form_pos.addRow("Высота:", self.sl_h)
        grp_pos.setLayout(form_pos)
        lay.addWidget(grp_pos)

        grp_voice = QGroupBox("Команды")
        form_voice = QFormLayout()
        self.txt_trigger = QLineEdit(self.config.get("trigger_word", "программа"))
        self.txt_trigger.textChanged.connect(self.upd_voice_cfg)
        self.txt_term = QLineEdit(self.config.get("term_word", "точка"))
        self.txt_term.textChanged.connect(self.upd_voice_cfg)
        self.spin_dur = QSpinBox(); self.spin_dur.setRange(1, 60); self.spin_dur.setValue(self.config.get("result_duration", 5))
        self.spin_dur.valueChanged.connect(self.upd_voice_cfg)

        self.cb_mode = QComboBox()
        self.cb_mode.addItems(["Голосовая активация (Постоянно)", "По кнопке (Push-to-Talk)"])
        self.cb_mode.setCurrentIndex(self.config.get('input_mode', 0))
        self.cb_mode.currentIndexChanged.connect(self.upd_voice_cfg)

        self.btn_toggle_hotkey = QPushButton(self.config.get('toggle_hotkey', 'F9'))
        self.btn_toggle_hotkey.clicked.connect(lambda: self.start_hotkey_recording('toggle'))
        
        self.btn_scroll_hotkey = QPushButton(self.config.get('scroll_hotkey', 'F7'))
        self.btn_scroll_hotkey.clicked.connect(lambda: self.start_hotkey_recording('scroll'))

        self.btn_mic_hotkey = QPushButton(self.config.get('mic_hotkey', 'F10'))
        self.btn_mic_hotkey.clicked.connect(lambda: self.start_hotkey_recording('mic'))

        form_voice.addRow("Режим ввода:", self.cb_mode)
        form_voice.addRow("Кнопка микрофона:", self.btn_mic_hotkey)
        form_voice.addRow("Старт слово:", self.txt_trigger)
        form_voice.addRow("Стоп слово:", self.txt_term)
        form_voice.addRow("Показ (сек):", self.spin_dur)
        form_voice.addRow("Скрыть/Показ:", self.btn_toggle_hotkey) 
        form_voice.addRow("Режим чата:", self.btn_scroll_hotkey) 
        
        grp_voice.setLayout(form_voice)
        lay.addWidget(grp_voice)

        btn_help = QPushButton("📜 Справка")
        btn_help.clicked.connect(self.show_help)
        lay.addWidget(btn_help)
        lay.addWidget(QLabel("Когда вы нажимаете на крестик, программа исчезает\n" "в системном трее."))

    # --- ЛОГИКА ХОТКЕЕВ ---
    def start_hotkey_recording(self, target):
        self.recording_target = target
        
        self.btn_toggle_hotkey.setEnabled(False)
        self.btn_scroll_hotkey.setEnabled(False)
        self.btn_mic_hotkey.setEnabled(False)
        
        if target == 'toggle': self.btn_toggle_hotkey.setText("Жду...")
        elif target == 'scroll': self.btn_scroll_hotkey.setText("Жду...")
        elif target == 'mic': self.btn_mic_hotkey.setText("Жду...")

        self.txt_trigger.clearFocus()
        self.txt_term.clearFocus()
        self.setFocus()
        
        self.t_keys.stop()

        self.hotkey_recorder = HotKeyRecorder()
        self.hotkey_recorder.finished_signal.connect(self.finish_hotkey_recording)
        self.hotkey_recorder.start()

    def finish_hotkey_recording(self, key_name):
        if self.recording_target == 'toggle':
            self.config['toggle_hotkey'] = key_name
            self.btn_toggle_hotkey.setText(key_name)
        elif self.recording_target == 'scroll':
            self.config['scroll_hotkey'] = key_name
            self.btn_scroll_hotkey.setText(key_name)
        elif self.recording_target == 'mic':
            self.config['mic_hotkey'] = key_name
            self.btn_mic_hotkey.setText(key_name)
            
        ConfigManager.save(self.config)
        self.btn_toggle_hotkey.setEnabled(True)
        self.btn_scroll_hotkey.setEnabled(True)
        self.btn_mic_hotkey.setEnabled(True)
        
        self.t_keys = KeyPollingWorker(self.config)
        self.t_keys.toggle_signal.connect(self.toggle_overlay_by_hotkey)
        self.t_keys.scroll_signal.connect(self.toggle_scroll_mode_by_hotkey)
        self.t_keys.mic_trigger_signal.connect(self.trigger_mic_by_hotkey)
        self.t_keys.start()

    def toggle_overlay_by_hotkey(self):
        self.set_overlay_visibility(not self.is_overlay_visible)

    def toggle_scroll_mode_by_hotkey(self):
        current_state_input = not (self.overlay.windowFlags() & Qt.WindowType.WindowTransparentForInput)
        
        if current_state_input:
             self.overlay.enable_mouse(False)
             self.overlay.set_status("🎮 РЕЖИМ ИГРЫ (Кнопка)", "rgba(0,0,255,100)", 2000)
        else:
             self.overlay.enable_mouse(True)
             self.overlay.set_status("🖱️ РЕЖИМ ЧАТА (Кнопка)", "rgba(128, 0, 128, 200)", 0)

    def trigger_mic_by_hotkey(self):
        self.t_voice.trigger_manual_listen()

    def set_overlay_visibility(self, visible):
        self.is_overlay_visible = visible
        if visible:
            self.overlay.show()
        else:
            self.overlay.hide()
    # ---------------------
    
    def update_slider_ranges(self):
        screens = QApplication.screens()
        idx = self.cb_monitor.currentIndex()
        if idx < len(screens):
            geo = screens[idx].geometry()
            self.sl_x.setRange(0, geo.width())
            self.sl_y.setRange(0, geo.height())
    
    def on_monitor_changed(self, index):
        self.config['monitor_idx'] = index
        self.update_slider_ranges()
        self.upd() # Обновляем позицию

    def upd(self):
        self.config['overlay_x'] = self.sl_x.value()
        self.config['overlay_y'] = self.sl_y.value()
        self.config['overlay_w'] = self.sl_w.value()
        self.config['overlay_h'] = self.sl_h.value()
        self.overlay.config = self.config
        self.overlay.update_geometry()
        ConfigManager.save(self.config)

    def upd_voice_cfg(self):
        self.config['trigger_word'] = self.txt_trigger.text()
        self.config['term_word'] = self.txt_term.text()
        self.config['result_duration'] = self.spin_dur.value()
        self.config['input_mode'] = self.cb_mode.currentIndex()
        self.t_voice.update_params(self.config)
        ConfigManager.save(self.config)

    def on_lang_voice_change(self, lang_code):
        self.config['target_lang'] = lang_code
        self.overlay.config = self.config
        ConfigManager.save(self.config)

    def show_help(self):
        t = self.config.get("trigger_word", "программа")
        term = self.config.get("term_word", "точка")
        hk_toggle = self.config.get("toggle_hotkey", "F9")
        hk_scroll = self.config.get("scroll_hotkey", "F7")
        hk_mic = self.config.get("mic_hotkey", "F10")

        msg = (
            f"1. <b>'{t} сообщение'</b> — начать ввод. Запись идет, пока не скажете <b>'{term}'</b>.<br>"
            f"   (В режиме 'По кнопке' нажмите <b>{hk_mic}</b> и говорите).<br>"
            f"2. <b>'{t} (язык)'</b> — переключить перевод на выбранный язык.<br>"
            f"3. <b>Поддерживаемые языки:</b> английский, немецкий, китайский, японский, французский, испанский, русский (голос без перевода).<br>"
            f"4. <b>'{t} скрыть/показать'</b> — управление оверлеем (или клавиша <b>{hk_toggle}</b>).<br>"
            f"5. <b>Режим Чтения (Скролл):</b><br>Скажите <b>'{t} ЧАТ'</b> или нажмите <b>{hk_scroll}</b>.<br>"
            f"6. <b>Возврат в игру:</b><br>Скажите <b>'{t} ИГРА'</b> или снова нажмите <b>{hk_scroll}</b>."
        )
        QMessageBox.information(self, "Справка", msg)

    def closeEvent(self, e): self.hide(); e.ignore()
    def init_tray(self):
        self.tray = QSystemTrayIcon(self)
        self.tray.setIcon(QIcon(QPixmap(16, 16)))
        m = QMenu(); m.addAction("Настройки", self.show); m.addAction("Выход", sys.exit)
        self.tray.setContextMenu(m); self.tray.show()
        self.tray.activated.connect(lambda r: self.show() if r == QSystemTrayIcon.ActivationReason.Trigger else None)

if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    ex = Settings()
    ex.init_tray()
    ex.show()
    sys.exit(app.exec())