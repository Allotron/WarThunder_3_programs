# -*- coding: utf-8 -*-

import os
import time
import subprocess
import sys
from datetime import datetime, timedelta
import logging
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk
import ctypes
import re

# Сторонние библиотеки
import pyperclip
from pynput import keyboard
from pynput.keyboard import Key, Controller as KeyController

# ======================
# ГЛОБАЛЬНЫЕ НАСТРОЙКИ
# ======================
DEFAULT_CONFIG = {
    "log_path": r"C:\Users\Allotron\AppData\Roaming\.minecraft\versions\vortex\logs\latest.log",
    "allowed_players": "SiPeRNiK, nikita, SpiderDog, Allotron",
    "shutdown_time": "02:15",
    "activation_hotkey": "F8",
    "chat_key": "t",
    "warning_minutes": 15,
    "save_timeout": 20
}

PROTOCOL_MESSAGES = {
    "activation": "⚔️ Программа запущена. Для завершения сессии введите 'exit'.",
    "warning": "🌙 ВНИМАНИЕ! Через {} минут сервер будет остановлен. Напишите 'delay' чтобы продлить.",
    "shutdown_start": "⚠️ Начало процедуры выключения. Сохранение мира...",
    "delayed": "✅ Выключение отложено на 1 час. Новое время: {}.",
    "emergency_scheduled": "⚠️ Сервер выключится через 30 секунд. Напишите 'cancel' для отмены.",
    "emergency_cancelled": "✅ Экстренное выключение ОТМЕНЕНО!",
    "success": "🌌 Мир сохранен. Команда выключения отправлена системе (60 сек)...",
}

# ======================
# СИСТЕМНЫЕ ФУНКЦИИ
# ======================

def is_admin():
    try:
        return ctypes.windll.shell32.IsUserAnAdmin()
    except:
        return False

def run_as_admin():
    script = os.path.abspath(sys.argv[0])
    params = f'"{script}"'
    try:
        ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, params, None, 1)
        return True
    except Exception as e:
        return False

class TextHandler(logging.Handler):
    def __init__(self, text_widget):
        logging.Handler.__init__(self)
        self.text_widget = text_widget

    def emit(self, record):
        msg = self.format(record)
        def append():
            self.text_widget.configure(state='normal')
            self.text_widget.insert(tk.END, msg + '\n')
            self.text_widget.see(tk.END)
            self.text_widget.configure(state='disabled')
        self.text_widget.after(0, append)

# ======================
# ГЛАВНЫЙ КЛАСС
# ======================
class NightGuardianApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Night Guardian")
        self.root.geometry("620x800")
        
        self.running = False
        self.target_window_handle = None
        self.key_controller = KeyController()
        self.monitoring_thread = None
        self.hotkey_listener = None
        
        self.last_log_position = 0
        self.next_shutdown_time = None
        self.next_warning_time = None
        self.warning_sent = False
        self.shutdown_triggered = False
        self.emergency_shutdown_time = None
        self.last_command_time = {}  # Для предотвращения спама командами
        self.sent_messages = []  # Кэш отправленных сообщений
        
        self.setup_ui()
        self.setup_logger()

        if not is_admin():
            self.log("⚠️ ЗАПУЩЕНО БЕЗ ПРАВ АДМИНИСТРАТОРА!")

    def setup_ui(self):
        settings_frame = ttk.LabelFrame(self.root, text="Настройки")
        settings_frame.pack(fill="x", padx=10, pady=5)

        ttk.Label(settings_frame, text="Путь к latest.log:").grid(row=0, column=0, sticky="w", padx=5, pady=5)
        self.log_path_var = tk.StringVar(value=DEFAULT_CONFIG["log_path"])
        ttk.Entry(settings_frame, textvariable=self.log_path_var, width=50).grid(row=0, column=1, padx=5, pady=5)
        ttk.Button(settings_frame, text="...", command=self.browse_file).grid(row=0, column=2, padx=5, pady=5)

        ttk.Label(settings_frame, text="Время выключения (HH:MM):").grid(row=1, column=0, sticky="w", padx=5, pady=5)
        self.shutdown_time_var = tk.StringVar(value=DEFAULT_CONFIG["shutdown_time"])
        ttk.Entry(settings_frame, textvariable=self.shutdown_time_var, width=10).grid(row=1, column=1, sticky="w", padx=5, pady=5)

        ttk.Label(settings_frame, text="Время на сохранение (сек):").grid(row=2, column=0, sticky="w", padx=5, pady=5)
        self.save_timeout_var = tk.StringVar(value=str(DEFAULT_CONFIG["save_timeout"]))
        ttk.Entry(settings_frame, textvariable=self.save_timeout_var, width=10).grid(row=2, column=1, sticky="w", padx=5, pady=5)

        ttk.Label(settings_frame, text="Клавиша старта (Hotkey):").grid(row=3, column=0, sticky="w", padx=5, pady=5)
        self.hotkey_var = tk.StringVar(value=DEFAULT_CONFIG["activation_hotkey"])
        ttk.Entry(settings_frame, textvariable=self.hotkey_var, width=10).grid(row=3, column=1, sticky="w", padx=5, pady=5)

        ttk.Label(settings_frame, text="Игроки (через запятую):").grid(row=4, column=0, sticky="w", padx=5, pady=5)
        self.players_var = tk.StringVar(value=DEFAULT_CONFIG["allowed_players"])
        ttk.Entry(settings_frame, textvariable=self.players_var, width=50).grid(row=4, column=1, columnspan=2, padx=5, pady=5)

        control_frame = ttk.Frame(self.root)
        control_frame.pack(fill="x", padx=10, pady=10)

        self.btn_start = ttk.Button(control_frame, text="1. СТАРТ (Жду клавишу)", command=self.start_hotkey_listener)
        self.btn_start.pack(side="left", expand=True, fill="x", padx=5)

        self.btn_stop = ttk.Button(control_frame, text="СТОП", command=self.stop_monitoring, state="disabled")
        self.btn_stop.pack(side="right", expand=True, fill="x", padx=5)

        log_frame = ttk.LabelFrame(self.root, text="Журнал событий")
        log_frame.pack(fill="both", expand=True, padx=10, pady=5)
        
        self.log_area = scrolledtext.ScrolledText(log_frame, state='disabled', font=("Consolas", 9))
        self.log_area.pack(fill="both", expand=True)

    def setup_logger(self):
        self.logger = logging.getLogger("NightGuardianGUI")
        self.logger.setLevel(logging.INFO)
        handler = TextHandler(self.log_area)
        handler.setFormatter(logging.Formatter('%(asctime)s | %(message)s', datefmt='%H:%M:%S'))
        self.logger.addHandler(handler)

    def log(self, message):
        self.logger.info(message)

    def browse_file(self):
        filename = filedialog.askopenfilename(filetypes=[("Log files", "*.log"), ("All files", "*.*")])
        if filename:
            self.log_path_var.set(filename)

    # ======================
    # ЛОГИКА
    # ======================
    def start_hotkey_listener(self):
        path = self.log_path_var.get()
        if not os.path.exists(path):
            messagebox.showerror("Ошибка", f"Файл логов не найден:\n{path}")
            return

        hotkey = self.hotkey_var.get()
        self.log(f"🕒 ОЖИДАНИЕ: Откройте Minecraft и нажмите '{hotkey}'...")
        self.btn_start.config(state="disabled")
        
        self.hotkey_listener = keyboard.Listener(on_release=self.on_key_release)
        self.hotkey_listener.start()

    def on_key_release(self, key):
        target_key = self.hotkey_var.get().lower()
        try:
            if hasattr(key, 'char'): key_str = key.char
            else: key_str = key.name
            
            if key_str and key_str.lower() == target_key:
                hwnd = ctypes.windll.user32.GetForegroundWindow()
                length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
                buff = ctypes.create_unicode_buffer(length + 1)
                ctypes.windll.user32.GetWindowTextW(hwnd, buff, length + 1)
                window_title = buff.value

                self.log(f"🎯 ЗАХВАЧЕНО ОКНО: {window_title}")
                self.target_window_handle = hwnd
                self.root.after(0, self.start_monitoring_loop)
                return False 
        except Exception as e:
            self.log(f"Ошибка клавиши: {e}")

    def start_monitoring_loop(self):
        self.running = True
        self.btn_stop.config(state="normal")
        
        try:
            now = datetime.now()
            shutdown_dt = datetime.strptime(self.shutdown_time_var.get(), "%H:%M")
            shutdown_dt = shutdown_dt.replace(year=now.year, month=now.month, day=now.day)
            
            if shutdown_dt <= now:
                shutdown_dt += timedelta(days=1)
            
            self.next_shutdown_time = shutdown_dt
            self.next_warning_time = shutdown_dt - timedelta(minutes=DEFAULT_CONFIG["warning_minutes"])
            self.log(f"✅ Мониторинг запущен. Выключение в {self.next_shutdown_time.strftime('%H:%M')}")
            
            self.last_log_position = os.path.getsize(self.log_path_var.get())
            self.send_chat_message(PROTOCOL_MESSAGES["activation"])
            
            self.monitoring_thread = threading.Thread(target=self.loop_logic, daemon=True)
            self.monitoring_thread.start()
            
        except Exception as e:
            self.log(f"❌ Ошибка старта: {e}")
            self.stop_monitoring()

    def stop_monitoring(self):
        self.running = False
        self.log("🛑 Мониторинг остановлен.")
        self.btn_start.config(state="normal")
        self.btn_stop.config(state="disabled")
        if self.hotkey_listener:
            self.hotkey_listener.stop()

    def focus_target_window(self):
        if not self.target_window_handle: return False
        try:
            if not ctypes.windll.user32.IsWindow(self.target_window_handle):
                self.log("⚠️ Окно игры закрыто!")
                return False
            if ctypes.windll.user32.IsIconic(self.target_window_handle):
                ctypes.windll.user32.ShowWindow(self.target_window_handle, 9)
            ctypes.windll.user32.SetForegroundWindow(self.target_window_handle)
            time.sleep(0.2)
            return True
        except: return False

    def send_chat_message(self, message):
        # Проверка на дублирование сообщений
        current_time = time.time()
        for t, msg in self.sent_messages[:]:
            if current_time - t > 10:  # Удаляем старые записи (>10 сек)
                self.sent_messages.remove((t, msg))
            elif msg == message:
                return  # Пропускаем дубликат
        
        if not self.focus_target_window(): 
            return
        try:
            chat_key = DEFAULT_CONFIG["chat_key"]
            self.key_controller.press(chat_key)
            self.key_controller.release(chat_key)
            time.sleep(0.5)
            pyperclip.copy(message)
            with self.key_controller.pressed(Key.ctrl):
                self.key_controller.press('v')
                self.key_controller.release('v')
            time.sleep(0.2)
            self.key_controller.press(Key.enter)
            self.key_controller.release(Key.enter)
            self.log(f"📤 Чат: {message}")
            
            # Запоминаем отправленное сообщение
            self.sent_messages.append((current_time, message))
        except Exception as e:
            self.log(f"Ошибка отправки: {e}")

    def exit_game_commands(self):
        self.log("🚪 Выход в меню...")
        if not self.focus_target_window(): return
        try:
            self.key_controller.press(Key.esc)
            self.key_controller.release(Key.esc)
            time.sleep(1.5)
            self.key_controller.press(Key.up)
            self.key_controller.release(Key.up)
            time.sleep(0.8)
            self.key_controller.press(Key.enter)
            self.key_controller.release(Key.enter)
        except: pass

    def perform_shutdown(self, reason):
        self.log(f"🔌 ВЫКЛЮЧЕНИЕ: {reason}")
        # Мягкое выключение через 60 секунд
        subprocess.run(f'shutdown /s /t 60 /c "{reason}. У вас есть 60 секунд."', shell=True)
        self.stop_monitoring()

    # ======================
    # ЦИКЛ (ИСПРАВЛЕНО)
    # ======================
    def loop_logic(self):
        while self.running:
            try:
                # 1. СНАЧАЛА читаем логи (чтобы успеть поймать команды)
                self.check_logs()
                
                now = datetime.now()

                # 2. Проверяем экстренное выключение
                if self.emergency_shutdown_time:
                    if now >= self.emergency_shutdown_time:
                        self.log("⚡ Таймер истек! Выключение...")
                        self.exit_game_commands()
                        
                        save_time = int(self.save_timeout_var.get())
                        self.log(f"⏳ Сохранение мира ({save_time} сек)...")
                        time.sleep(save_time)
                        
                        self.perform_shutdown("Экстренное завершение")
                        break

                # 3. Плановые проверки (только если нет экстренного)
                if not self.emergency_shutdown_time:
                    # Предупреждение
                    if not self.warning_sent and now >= self.next_warning_time:
                        self.send_chat_message(PROTOCOL_MESSAGES["warning"].format(DEFAULT_CONFIG["warning_minutes"]))
                        self.warning_sent = True

                    # Плановое выключение
                    if not self.shutdown_triggered and now >= self.next_shutdown_time:
                        self.shutdown_triggered = True
                        self.send_chat_message(PROTOCOL_MESSAGES["shutdown_start"])
                        time.sleep(2)
                        self.exit_game_commands()
                        
                        save_time = int(self.save_timeout_var.get())
                        self.log(f"⏳ Сохранение мира ({save_time} сек)...")
                        time.sleep(save_time)
                        
                        self.send_chat_message(PROTOCOL_MESSAGES["success"])
                        time.sleep(2)
                        self.perform_shutdown("Плановое выключение")
                        break

                time.sleep(1) # Пауза цикла

            except Exception as e:
                self.log(f"🔥 Ошибка цикла: {e}")
                time.sleep(5)

    def parse_chat_line(self, line):
        # Улучшенный парсер для Vanilla и TLauncher
        patterns = [
            r'<([^>]+)> (.+)',  # Vanilla: <Player> message
            r'\[CHAT\] <([^>]+)> (.+)',  # TLauncher: [CHAT] <Player> message
            r'\[([^\]]+)\] (.+)'  # Другие форматы: [Player] message
        ]
        
        for pattern in patterns:
            match = re.search(pattern, line)
            if match:
                return match.group(1).strip(), match.group(2).strip()
        return None, None

    def check_logs(self):
        try:
            current_size = os.path.getsize(self.log_path_var.get())
            if current_size == self.last_log_position:
                return

            with open(self.log_path_var.get(), "r", encoding="utf-8", errors="ignore") as f:
                f.seek(self.last_log_position)
                lines = f.readlines()
                self.last_log_position = f.tell()

            allowed_str = self.players_var.get().lower()
            allowed = [p.strip() for p in allowed_str.split(",") if p.strip()]

            for line in lines:
                player, message = self.parse_chat_line(line)
                
                if player and message:
                    player_clean = re.sub(r'[^\w\s]', '', player).lower()
                    message_clean = message.strip().lower()
                    
                    # Пропускаем сообщения от бота
                    bot_phrases = [
                        "страж активирован", "внимание", "начало процедуры", 
                        "выключение отложено", "сервер выключится", "отменено", 
                        "мир сохранен"
                    ]
                    if any(phrase in message_clean for phrase in bot_phrases):
                        continue
                    
                    # Проверка на разрешенного игрока
                    if allowed and not any(p in player_clean for p in allowed):
                        continue

                    # Проверка на спам (не чаще 1 раза в 2 сек для каждой команды)
                    current_time = time.time()
                    if player_clean in self.last_command_time:
                        if current_time - self.last_command_time[player_clean] < 2.0:
                            continue
                    
                    self.last_command_time[player_clean] = current_time

                    # Обработка команд (без префикса /)
                    if "cancel" in message_clean and self.emergency_shutdown_time:
                        self.emergency_shutdown_time = None
                        self.send_chat_message(PROTOCOL_MESSAGES["emergency_cancelled"])
                        self.log("✅ Команда 'cancel' принята!")
                    
                    elif "exit" in message_clean and not self.emergency_shutdown_time:
                        # Предотвращаем двойную отправку сообщения
                        if not self.emergency_shutdown_time:
                            self.emergency_shutdown_time = datetime.now() + timedelta(seconds=30)
                            self.send_chat_message(PROTOCOL_MESSAGES["emergency_scheduled"])
                            self.log("⚠️ Команда 'exit' принята! Таймер 30 сек.")
                    
                    elif "delay" in message_clean:
                        # Предотвращаем двойное продление
                        new_shutdown_time = self.next_shutdown_time + timedelta(hours=1)
                        if (new_shutdown_time - self.next_shutdown_time).total_seconds() >= 3500:  # Проверяем, что продление на 1 час
                            self.next_shutdown_time = new_shutdown_time
                            self.next_warning_time = self.next_shutdown_time - timedelta(minutes=DEFAULT_CONFIG["warning_minutes"])
                            self.warning_sent = False
                            self.shutdown_triggered = False
                            new_time = self.next_shutdown_time.strftime("%H:%M")
                            self.send_chat_message(PROTOCOL_MESSAGES["delayed"].format(new_time))
                            self.log(f"⏳ Продлено до {new_time}")

        except Exception as e:
            self.log(f"Ошибка чтения лога: {e}")

if __name__ == "__main__":
    if not is_admin():
        if run_as_admin():
            sys.exit()
    
    root = tk.Tk()
    app = NightGuardianApp(root)
    root.mainloop()