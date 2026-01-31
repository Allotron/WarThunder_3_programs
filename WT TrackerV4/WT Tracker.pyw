import sys
import requests
import json
import re
import os
import time
import threading
from PyQt6.QtWidgets import (QApplication, QWidget, QVBoxLayout, QHBoxLayout,
                             QCheckBox, QLabel, QPushButton, QGroupBox, QSlider, 
                             QFrame, QGridLayout, QMessageBox, QLineEdit, QComboBox) # Добавлен QComboBox
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QUrl, QThread, pyqtSlot
from PyQt6.QtMultimedia import QMediaPlayer, QAudioOutput
from PyQt6.QtGui import QScreen # Добавлен импорт QScreen

# Скрываем технические сообщения Qt
os.environ["QT_LOGGING_RULES"] = "qt.qpa.window=false;qt.multimedia.ffmpeg=false"

# Импорты для сканера
try:
    import pyautogui
    pyautogui.FAILSAFE = False
except ImportError:
    pyautogui = None
    print("ОШИБКА: Библиотека pyautogui не установлена!")

try:
    import keyboard
except ImportError:
    keyboard = None
    print("ОШИБКА: Библиотека keyboard не установлена!")

# --- КОНСТАНТЫ ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
VEHICLES_FILE = os.path.join(BASE_DIR, "vehicles.json")
UNKNOWN_FILE = os.path.join(BASE_DIR, "unknown.txt")
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
FLAGS_DIR = os.path.join(BASE_DIR, "flags") 
SOUND_ENEMY = os.path.join(BASE_DIR, "alert.mp3")
SOUND_ALLY = os.path.join(BASE_DIR, "alert_ally.mp3")
API_URL = "http://localhost:8111"

# --- ЛОГИКА ИГРЫ ---
class PlayerStats:
    def __init__(self, name):
        self.name = name
        self.kills = 0
        self.deaths = 0
        self.is_disqualified = False
        self.alert_played = False

class WTLogic:
    def __init__(self, sound_callback):
        self.sound_callback = sound_callback
        self.last_dmg_id = 0
        self.active_enemies = {}
        self.dead_enemies = {}  # НОВОЕ: Буфер недавно убитых врагов
        self.all_players = {}
        self.vehicle_db = {}
        self.name_cache = {}
        self.unknown_buffer = set()
        
        self.shared_vehicles = [
            "mq-1",       
            "▄m55",
            "CLOVIS",
            "DF105",
        ]
        
        self.selected_nations = set()
        self.selected_types = set()
        self.show_notes = True
        self.show_names = True
        
        self.session = requests.Session()
        
        self.is_api_available = False
        self.is_in_battle = False
        self.skip_history_mode = True 

    def load_db(self):
        if not os.path.exists(VEHICLES_FILE): return False
        try:
            with open(VEHICLES_FILE, "r", encoding="utf-8-sig") as f:
                self.vehicle_db = json.load(f)
            return True
        except Exception as e:
            print("load_db error:", e)
            return False

    def save_unknowns(self):
        if not self.unknown_buffer: return
        try:
            existing = set()
            if os.path.exists(UNKNOWN_FILE):
                with open(UNKNOWN_FILE, "r", encoding="utf-8") as f:
                    existing = {line.strip() for line in f if line.strip() and not line.startswith("//")}
            to_write = existing.union(self.unknown_buffer)
            with open(UNKNOWN_FILE, "w", encoding="utf-8") as f:
                f.write("// Скопируйте строки ниже в vehicles.json (если в названии техники встречаются кавычки (вместо одинарных ставить двойные):'Type 90 (B) \'Fuji\'': { ... }). Для особого фильтра - special. Для добавление заметки (вместо одинарных ставить двойные): 'note': 'Пример заметки'\n")
                for name in sorted(to_write):
                    f.write(f"{name}\n")
            self.unknown_buffer.clear()
        except Exception as e:
            print("save_unknowns error:", e)

    def normalize_name(self, name):
        return (name or "").strip().lower()

    def find_vehicle_info(self, log_name):
        if not log_name: return None
        if log_name in self.name_cache: return self.name_cache[log_name]
        
        clean_name_lower = log_name.lower()

        if re.match(r'^\d+mm_', clean_name_lower):
            self.name_cache[log_name] = None
            return None

        garbage_list = [
            "weapons/", "us_hellfire", "agm_", "tow", "short", 
            "aim_", "mim_", "kh_", "rocket", "bomb", "torpedo", 
            "su_9m", "m8_hvap", "m82_shot"
        ]
        
        if "/" in clean_name_lower or any(x in clean_name_lower for x in garbage_list):
            self.name_cache[log_name] = None
            return None
        
        if re.match(r'^9m\d+', clean_name_lower):
             self.name_cache[log_name] = None
             return None

        if "recon micro" in clean_name_lower:
            self.name_cache[log_name] = None
            return None
            
        v_type = "tank" 
        if "mq-1" in clean_name_lower: v_type = "aircraft"
        
        for shared_name in self.shared_vehicles:
            if shared_name in clean_name_lower:
                note_text = "Ударный БПЛА" if "mq-1" in clean_name_lower else "Клон-техника"
                res = {"type": v_type, "nation": "unknown", "note": note_text}
                self.name_cache[log_name] = res
                return res

        if log_name in self.vehicle_db:
            res = self.vehicle_db[log_name]
            self.name_cache[log_name] = res
            return res

        clean_log = self.normalize_name(log_name)
        for db_name, info in self.vehicle_db.items():
            clean_db = self.normalize_name(db_name)
            if clean_log == clean_db:
                self.name_cache[log_name] = info
                return info

        self.unknown_buffer.add(log_name)
        self.name_cache[log_name] = None
        return None

    def get_player(self, name):
        if name not in self.all_players: self.all_players[name] = PlayerStats(name)
        return self.all_players[name]

    def is_air_vehicle(self, v_type):
        return v_type in ['plane', 'heli', 'jet_fighter']

    def parse_entity(self, text):
        if not text: return None, None
        match = re.match(r'^(.+?)\s\((.+)\)$', text.strip())
        if match: return match.group(1), match.group(2)
        return None, None

    def reset_session_data(self):
        self.active_enemies.clear()
        self.dead_enemies.clear() # Очищаем кладбище
        self.all_players.clear()
        self.name_cache.clear()
        self.skip_history_mode = True 
        print(">>> НОВАЯ СЕССИЯ: Данные сброшены.")

    def check_battle_status(self):
        try:
            r = self.session.get(f"{API_URL}/mission.json", timeout=0.5)
            if r.status_code != 200:
                # Если API ответил ошибкой (например, при загрузке), сбрасываем флаг
                if self.is_in_battle: 
                    self.reset_session_data() # <--- ДОБАВИТЬ ЭТО (сброс при потере связи с API боя)
                
                self.is_api_available = False
                self.is_in_battle = False
                return

            self.is_api_available = True
            data = r.json()
            status = data.get("status", "")
            currently_in_battle = (status == "running")

            # Вход в бой (старая логика, оставляем)
            if currently_in_battle and not self.is_in_battle:
                self.reset_session_data()
            
            # Выход из боя (НОВАЯ ЛОГИКА)
            if not currently_in_battle and self.is_in_battle:
                self.reset_session_data() # <--- ДОБАВИТЬ ЭТО

            self.is_in_battle = currently_in_battle
        except Exception:
            # Если игра закрылась или вылетела
            if self.is_in_battle:
                 self.reset_session_data() # <--- И ЭТО (для гарантии очистки при закрытии)
                 
            self.is_api_available = False
            self.is_in_battle = False

    def update_data(self):
        self.check_battle_status()
        if not self.is_api_available or not self.is_in_battle: return

        try:
            req_last_evt = 0 if self.skip_history_mode else self.last_dmg_id
            r = self.session.get(f"{API_URL}/hudmsg?lastEvt=-1&lastDmg={req_last_evt}", timeout=0.5)
            if r.status_code != 200: return
            
            data = r.json()
            msgs = data.get("damage", []) or []
            
            if not msgs:
                if self.skip_history_mode: self.skip_history_mode = False
                return

            if self.skip_history_mode:
                max_existing_id = msgs[-1].get("id", 0)
                self.last_dmg_id = max_existing_id
                self.skip_history_mode = False
                return 

            max_id = self.last_dmg_id
            kill_actions = [" уничтожил ", " сбил ", " destroyed ", " shot down "]
            solo_actions = [" разбился", " выведен из строя", " самоуничтожился", " crashed"]

            for msg in msgs:
                mid = msg.get("id", 0)
                if mid <= self.last_dmg_id: continue

                text = msg.get("msg", "") or ""
                action_found = None
                for act in kill_actions:
                    if act in text: action_found = act; break

                if action_found:
                    parts = text.split(action_found)
                    if len(parts) == 2:
                        p1_raw = parts[0].strip()
                        p2_raw = parts[1].strip()

                        p1_name, p1_veh = self.parse_entity(p1_raw)
                        p2_name, p2_veh = self.parse_entity(p2_raw)
                        
                        if not p2_veh:
                            p2_veh = p2_raw
                            p2_name = "AI_Drone"

                        if p1_name: 
                            p1_name = re.sub(r'^\d+:\d+', '', p1_name).strip()
                            if p2_name != "AI_Drone":
                                p2_name = re.sub(r'^\d+:\d+', '', p2_name).strip()

                            p1_veh_clean = re.sub(r'\[(ии|ai)\]\s*', '', p1_veh, flags=re.IGNORECASE).strip() if p1_veh else ""
                            p2_veh_clean = re.sub(r'\[(ии|ai)\]\s*', '', p2_veh, flags=re.IGNORECASE).strip() if p2_veh else ""

                            killer_info = self.find_vehicle_info(p1_veh_clean)
                            victim_info = self.find_vehicle_info(p2_veh_clean)

                            is_enemy = False
                            
                            if p1_veh_clean == "MQ-1":
                                if victim_info:
                                    victim_nation = victim_info.get('nation')
                                    if victim_nation and victim_nation not in self.selected_nations:
                                        is_enemy = True
                            elif killer_info:
                                if killer_info.get('nation') in self.selected_nations:
                                    is_enemy = True

                            killer = self.get_player(p1_name)
                            killer.kills += 1
                            
                            if killer_info and self.is_air_vehicle(killer_info.get('type', 'tank')): 
                                killer.is_disqualified = True

                            if is_enemy:
                                n_val = killer_info.get('nation', 'unknown') if killer_info else 'unknown'
                                t_val = killer_info.get('type', 'special') if killer_info else 'aircraft'
                                note_val = killer_info.get('note', '') if killer_info else ''
                                
                                if p1_veh_clean == "MQ-1": 
                                    note_val = "Ударный БПЛА"

                                current_veh_display = p1_veh if p1_veh else p1_veh_clean

                                # --- ЛОГИКА ФИКСА "ЗОМБИ" ---
                                should_add_enemy = True
                                
                                # Проверяем, не умирал ли этот враг недавно в этой же технике
                                if p1_name in self.dead_enemies:
                                    last_death = self.dead_enemies[p1_name]
                                    # Если техника та же и прошло менее 25 секунд с момента смерти
                                    if (last_death["veh"] == current_veh_display) and (time.time() - last_death["time"] < 25):
                                        should_add_enemy = False # Это посмертный фраг, не добавляем
                                    else:
                                        # Либо прошло много времени, либо другая техника (респ)
                                        del self.dead_enemies[p1_name]

                                if should_add_enemy:
                                    self.active_enemies[p1_name] = {
                                        "veh": current_veh_display, 
                                        "nation": n_val,
                                        "type": t_val,
                                        "note": note_val
                                    }
                            
                            self.check_nuke(killer, is_enemy)

                            is_recon_drone = ("recon micro" in p2_veh_clean.lower())
                            
                            if not is_recon_drone:
                                victim = self.get_player(p2_name)
                                victim.deaths += 1
                                if victim_info and self.is_air_vehicle(victim_info.get('type', 'tank')): 
                                    victim.is_disqualified = True
                                
                                # --- ИСПРАВЛЕННАЯ ЛОГИКА ЗАПИСИ СМЕРТИ ---
                                # Определяем название техники для записи в "кладбище"
                                death_veh_name = p2_veh if p2_veh else p2_veh_clean
                                
                                # Если враг был в активных, удаляем его, но запоминаем его технику (она точнее)
                                if p2_name in self.active_enemies: 
                                    death_veh_name = self.active_enemies[p2_name]["veh"]
                                    del self.active_enemies[p2_name]
                                    
                                # ЗАПИСЫВАЕМ В МЕРТВЫЕ ВСЕГДА (даже если трекер видит его впервые)
                                self.dead_enemies[p2_name] = {
                                    "veh": death_veh_name,
                                    "time": time.time()
                                }
                                # -----------------------------------------
                                    
                    if mid > max_id: max_id = mid
                    continue

                solo_found = None
                for act in solo_actions:
                    if act in text: solo_found = act; break
                if solo_found:
                    parts = text.split(solo_found)
                    if len(parts) > 0:
                        p_name, p_veh = self.parse_entity(parts[0].strip())
                        if p_name:
                            p_name = re.sub(r'^\d+:\d+', '', p_name).strip()
                            info = self.find_vehicle_info(p_veh)
                            
                            victim = self.get_player(p_name)
                            victim.deaths += 1
                            if info and self.is_air_vehicle(info.get('type', 'tank')): victim.is_disqualified = True
                            
                            # --- ИСПРАВЛЕННАЯ ЛОГИКА ДЛЯ СОЛО СМЕРТИ ---
                            death_veh_name = p_veh
                            
                            if p_name in self.active_enemies: 
                                death_veh_name = self.active_enemies[p_name]["veh"]
                                del self.active_enemies[p_name]

                            # Тоже записываем в мертвые всегда
                            self.dead_enemies[p_name] = {
                                "veh": death_veh_name,
                                "time": time.time()
                            }
                            # -------------------------------------------

                if mid > max_id: max_id = mid
            self.last_dmg_id = max_id
        except Exception: pass

    def check_nuke(self, player, is_enemy):
        if player.kills >= 9 and player.deaths <= 2 and not player.is_disqualified:
            if not player.alert_played:
                if is_enemy: self.sound_callback('enemy')
                else: self.sound_callback('ally')
                player.alert_played = True

# --- ПОТОК ОПРОСА ИГРЫ ---
class GamePoller(QThread):
    data_updated = pyqtSignal()

    def __init__(self, logic):
        super().__init__()
        self.logic = logic
        self.running = False

    def run(self):
        self.running = True
        while self.running:
            self.logic.update_data()
            self.data_updated.emit()
            time.sleep(1) 

    def stop(self):
        self.running = False
        self.wait()

# --- НОВЫЙ ПОТОК ОПРОСА КЛАВИШ ---
class KeyPollingWorker(QThread):
    scan_signal = pyqtSignal()
    toggle_overlay_signal = pyqtSignal()

    def __init__(self, scan_key, overlay_key):
        super().__init__()
        self.scan_key = scan_key
        self.overlay_key = overlay_key
        self.running = True
    
    def update_keys(self, scan, overlay):
        self.scan_key = scan
        self.overlay_key = overlay

    def run(self):
        if not keyboard: return
        
        is_scan_pressed = False
        is_ov_pressed = False

        while self.running:
            try:
                if self.scan_key and keyboard.is_pressed(self.scan_key):
                    if not is_scan_pressed:
                        self.scan_signal.emit()
                        is_scan_pressed = True
                else:
                    is_scan_pressed = False

                if self.overlay_key and keyboard.is_pressed(self.overlay_key):
                    if not is_ov_pressed:
                        self.toggle_overlay_signal.emit()
                        is_ov_pressed = True
                else:
                    is_ov_pressed = False
                
                time.sleep(0.05)

            except Exception:
                pass
    
    def stop(self):
        self.running = False
        self.wait()

# --- ОВЕРЛЕЙ ---
class Overlay(QWidget):
    def __init__(self, logic):
        super().__init__()
        self.logic = logic
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setWindowFlag(Qt.WindowType.WindowTransparentForInput, True)
        self.layout = QVBoxLayout()
        self.label = QLabel("ТЕСТ")
        self.layout.addWidget(self.label)
        self.setLayout(self.layout)
        # Начальная геометрия, будет переопределена при старте
        self.setGeometry(30, 150, 400, 400)
        
        self.font_size = 20
        self.show_border = False
        self.temp_test_mode = False
        
        self.scan_message = "" 
        self.scan_timer = QTimer()
        self.scan_timer.setSingleShot(True)
        self.scan_timer.timeout.connect(self.clear_scan_message)
        
        self.update_style()

    def update_style(self):
        border = "border: 2px dashed rgba(255, 255, 255, 150);" if self.show_border else "border: none;"
        self.label.setStyleSheet(f"font-size: {self.font_size}px; font-weight: bold; font-family: 'Segoe UI'; {border}")

    def is_visible_by_filter(self, nation, v_type):
        if nation == "unknown": return True
        
        if nation not in self.logic.selected_nations: return False
        
        type_key = "special"
        if "tank" in v_type or "mbt" in v_type: type_key = "tank"
        elif "plane" in v_type or "fighter" in v_type: type_key = "plane"
        elif "heli" in v_type: type_key = "heli"
        elif "spaa" in v_type or "zsu" in v_type: type_key = "zsu"
        elif "aircraft" in v_type: type_key = "plane"
        
        if type_key not in self.logic.selected_types: return False
        return True
    
    def show_scan_result(self, nations):
        rus_names_map = {
            "USA": "США", "Germany": "Германия", "USSR": "СССР", "Great Britain": "Британия",
            "Japan": "Япония", "China": "Китай", "Italy": "Италия", "France": "Франция",
            "Sweden": "Швеция", "Israel": "Израиль"
        }

        if not nations:
            self.scan_message = "<span style='color:#FFFF00;'>🔎 СКАН: НИЧЕГО НЕ НАЙДЕНО</span>"
        else:
            rus_names = [rus_names_map.get(n, n) for n in nations]
            names_str = ", ".join(rus_names)
            self.scan_message = f"<span style='color:#FFFF00;'>🔎 СКАН: [{names_str}]</span>"
        
        self.update_view()
        self.scan_timer.start(10000)

    def clear_scan_message(self):
        self.scan_message = ""
        self.update_view()

    def update_view(self):
        if self.show_border:
            self.label.setText("<span style='color:red'>НАСТРОЙКА ОВЕРЛЕЯ</span>")
            return

        text_content = ""
        
        if self.scan_message:
            text_content += f"{self.scan_message}<br>"

        if self.temp_test_mode:
            text_content += "<span style='color:#00FF00'>✓ ЗАПУЩЕНО</span>"
        elif self.logic.is_in_battle:
            enemy_lines = []
            current_enemies = self.logic.active_enemies.copy()
            
            type_icons = {
                "tank": "🛡️", "plane": "✈️", "heli": "🚁", "zsu": "📡", "special": "⭐"
            }

            for name, data in current_enemies.items():
                veh_name = data["veh"]
                nation = data["nation"]
                v_type = str(data["type"]).lower()
                
                if not self.is_visible_by_filter(nation, v_type): continue
                
                player_stats = self.logic.all_players.get(name)
                is_nuke = player_stats and player_stats.kills >= 9 and player_stats.deaths <= 2 and not player_stats.is_disqualified
                
                if self.logic.show_names or is_nuke:
                    display_text = f"{veh_name} ({name})"
                else:
                    display_text = f"{veh_name}"
                
                if self.logic.show_notes:
                    note_text = data.get("note", "")
                    if note_text:
                        display_text += f" — {note_text}"

                icon = type_icons.get(v_type, "•")

                if is_nuke:
                    line = f"<span style='color:#00FFFF'>☢️ {display_text} [{player_stats.kills} K]</span>"
                else:
                    line = f"<span style='color:#FF0000'>{icon} {display_text}</span>"
                
                enemy_lines.append(line)
            
            if enemy_lines:
                text_content += "⚠️ ВРАГИ:<br>" + "<br>".join(enemy_lines)

        self.label.setText(text_content)

# --- ПОТОК ДЛЯ СКАНЕРА ФЛАГОВ ---
class FlagScannerWorker(QThread):
    flags_found = pyqtSignal(list)

    def __init__(self, region, flags_dir):
        super().__init__()
        self.region = region 
        self.flags_dir = flags_dir

    def run(self):
        if not pyautogui: 
            print("Сканер: Нет библиотеки pyautogui")
            return
        
        print(f"Сканер запущен. Зона: {self.region}")
        time.sleep(0.3) 
        
        try:
            screenshot = pyautogui.screenshot(region=self.region)
            found_nations = []

            if os.path.exists(self.flags_dir):
                files = os.listdir(self.flags_dir)
                for filename in files:
                    if filename.endswith(".png"):
                        nation_name = filename.replace(".png", "").replace(".jpg", "")
                        path = os.path.join(self.flags_dir, filename)
                        try:
                            if pyautogui.locate(path, screenshot, confidence=0.8):
                                print(f"-> НАЙДЕНО: {nation_name}")
                                found_nations.append(nation_name)
                        except Exception: pass
            else:
                print("Папка flags не найдена!")
            
            self.flags_found.emit(found_nations)
            
        except Exception as e:
            print("Scan error:", e)

# --- ГЛАВНОЕ ОКНО ---
class Settings(QWidget):
    def __init__(self):
        super().__init__()
        self.player = QMediaPlayer(); self.audio_output = QAudioOutput()
        self.player.setAudioOutput(self.audio_output)
        
        self.logic = WTLogic(self.play_sound)
        self.ov = Overlay(self.logic) 
        
        self.poller = GamePoller(self.logic)
        self.poller.data_updated.connect(self.ov.update_view)
        
        self.scan_region = None 
        self.temp_top_left = None
        
        self.current_scan_hotkey = "/" 
        self.current_overlay_hotkey = "f8"
        self.is_overlay_visible = True
        self.tracking_started = False
        self.hooks = {} 

        self.init_ui()
        self.load_config() 
        self.logic.load_db()
        self.update_db_counter()

        self.key_worker = KeyPollingWorker(self.current_scan_hotkey, self.current_overlay_hotkey)
        self.key_worker.scan_signal.connect(self.start_flag_scan)
        self.key_worker.toggle_overlay_signal.connect(self.toggle_ov_visibility)
        self.key_worker.start()

        self.ui_timer = QTimer()
        self.ui_timer.timeout.connect(self.update_status_display)
        
        self.update_filters()
        self.update_ov_status_label()

    def update_keys_in_worker(self):
        if self.key_worker:
            self.key_worker.update_keys(self.current_scan_hotkey, self.current_overlay_hotkey)

    def enable_setup_hotkeys(self):
        if not keyboard: return
        try:
            if "setup_f6" not in self.hooks:
                self.hooks["setup_f6"] = keyboard.add_hotkey('F6', self.set_top_left)
            if "setup_f7" not in self.hooks:
                self.hooks["setup_f7"] = keyboard.add_hotkey('F7', self.set_bottom_right)
            print("Режим настройки: F6 и F7 активированы.")
        except Exception as e: print("Ошибка setup keys:", e)

    def disable_setup_hotkeys(self):
        if not keyboard: return
        try:
            if "setup_f6" in self.hooks:
                keyboard.remove_hotkey(self.hooks["setup_f6"])
                del self.hooks["setup_f6"]
            if "setup_f7" in self.hooks:
                keyboard.remove_hotkey(self.hooks["setup_f7"])
                del self.hooks["setup_f7"]
            print("Режим настройки: F6 и F7 отключены.")
        except Exception: pass

    def play_sound(self, target_type):
        if not hasattr(self, 'cb_sound_enabled') or not self.cb_sound_enabled.isChecked():
            return
        sound_file = SOUND_ENEMY if target_type == 'enemy' else SOUND_ALLY
        if os.path.exists(sound_file):
            self.player.setSource(QUrl.fromLocalFile(sound_file))
            self.audio_output.setVolume(1.0); self.player.play()

    def init_ui(self):
        self.setWindowTitle("WT Tracker")
        self.setFixedSize(380, 600) # Увеличили высоту для списка мониторов
        main = QVBoxLayout(); main.setContentsMargins(10, 10, 10, 10); main.setSpacing(5)

        self.btn_settings = QPushButton("⚙ НАСТРОЙКИ ОВЕРЛЕЯ И СКАНЕРА")
        self.btn_settings.setCheckable(True)
        self.btn_settings.clicked.connect(self.toggle_settings_panel)
        main.addWidget(self.btn_settings)

        self.settings_frame = QFrame(); self.settings_frame.hide()
        sl_layout = QVBoxLayout()
        
        # --- ВЫБОР МОНИТОРА ---
        screen_layout = QHBoxLayout()
        screen_lbl = QLabel("Монитор:")
        self.combo_screens = QComboBox()
        # Заполняем список экранов
        screens = QApplication.screens()
        for i, screen in enumerate(screens):
            name = screen.name()
            geo = screen.geometry()
            self.combo_screens.addItem(f"Экран {i} ({geo.width()}x{geo.height()})", i)
        
        # Обработка смены монитора
        self.combo_screens.currentIndexChanged.connect(self.on_screen_changed)
        
        screen_layout.addWidget(screen_lbl)
        screen_layout.addWidget(self.combo_screens)
        sl_layout.addLayout(screen_layout)
        # ----------------------

        screen = QApplication.primaryScreen().geometry()
        # Диапазон слайдеров зададим дефолтный, потом он обновится в on_screen_changed
        self.slider_x = self.create_slider(sl_layout, "Позиция X (отн. экрана)", 0, screen.width(), self.update_ov_preview)
        self.slider_y = self.create_slider(sl_layout, "Позиция Y (отн. экрана)", 0, screen.height(), self.update_ov_preview)
        self.slider_size = self.create_slider(sl_layout, "Размер текста", 10, 60, self.update_ov_preview)
        self.slider_opa = self.create_slider(sl_layout, "Прозрачность %", 10, 100, self.update_ov_preview)
        
        key_layout = QHBoxLayout()
        key_lbl = QLabel("Кнопка скан:")
        self.entry_hotkey = QLineEdit("tab")
        self.entry_hotkey.setPlaceholderText("напр. /")
        btn_apply_key = QPushButton("Сет")
        btn_apply_key.setFixedWidth(50)
        btn_apply_key.clicked.connect(self.update_scan_hotkey_from_ui)
        
        key_layout.addWidget(key_lbl)
        key_layout.addWidget(self.entry_hotkey)
        key_layout.addWidget(btn_apply_key)
        sl_layout.addLayout(key_layout)

        key_ov_layout = QHBoxLayout()
        key_ov_lbl = QLabel("Кнопка скрыт:")
        self.entry_ov_hotkey = QLineEdit("f8")
        self.entry_ov_hotkey.setPlaceholderText("напр. f8")
        btn_apply_ov = QPushButton("Сет")
        btn_apply_ov.setFixedWidth(50)
        btn_apply_ov.clicked.connect(self.update_overlay_hotkey_from_ui)

        key_ov_layout.addWidget(key_ov_lbl)
        key_ov_layout.addWidget(self.entry_ov_hotkey)
        key_ov_layout.addWidget(btn_apply_ov)
        sl_layout.addLayout(key_ov_layout)
        
        scan_info = QLabel("<b>Настройка зоны (F6/F7 активны только при открытых настройках):</b><br>1. Курсор в левый-верх угол -> <b>F6</b><br>2. Курсор в правый-нижний -> <b>F7</b>")
        scan_info.setStyleSheet("font-size: 10px; color: #aaa; margin-top: 5px;")
        scan_info.setWordWrap(True)
        sl_layout.addWidget(scan_info)
        
        self.lbl_region = QLabel("Зона сканирования: Не задана")
        self.lbl_region.setStyleSheet("color: #FF4444; font-size: 10px;")
        sl_layout.addWidget(self.lbl_region)

        self.settings_frame.setLayout(sl_layout)
        main.addWidget(self.settings_frame)

        n_group = QGroupBox("1. Нации")
        n_grid = QGridLayout()
        self.n_checks = {}
        nations_map = [
            ("USA", "США"), ("Germany", "Германия"), ("USSR", "СССР"), ("Great Britain", "Британия"),
            ("Japan", "Япония"), ("China", "Китай"), ("Italy", "Италия"), ("France", "Франция"),
            ("Sweden", "Швеция"), ("Israel", "Израиль")
        ]
        row, col = 0, 0
        for db_name, display_name in nations_map:
            cb = QCheckBox(display_name)
            cb.stateChanged.connect(self.update_filters)
            self.n_checks[db_name] = cb
            n_grid.addWidget(cb, row, col)
            col += 1; 
            if col > 1: col = 0; row += 1
        n_group.setLayout(n_grid)
        main.addWidget(n_group)

        t_group = QGroupBox("2. Тип техники")
        t_layout = QHBoxLayout()
        self.t_checks = {
            "tank": QCheckBox("Танк"), "plane": QCheckBox("Лёт"), "heli": QCheckBox("Вер"),
            "zsu": QCheckBox("ЗСУ"), "special": QCheckBox("Особые")
        }
        for k, v in self.t_checks.items(): 
            v.setChecked(True)
            v.stateChanged.connect(self.update_filters)
            t_layout.addWidget(v)
        t_group.setLayout(t_layout)
        main.addWidget(t_group)

        self.cb_show_notes = QCheckBox("Показывать заметки")
        self.cb_show_notes.setChecked(True) 
        self.cb_show_notes.stateChanged.connect(self.update_filters)
        main.addWidget(self.cb_show_notes)

        self.cb_show_names = QCheckBox("Показывать ники игроков")
        self.cb_show_names.setChecked(True)
        self.cb_show_names.stateChanged.connect(self.update_filters)
        main.addWidget(self.cb_show_names)

        self.cb_sound_enabled = QCheckBox("Включить оповещение")
        self.cb_sound_enabled.setChecked(True) 
        main.addWidget(self.cb_sound_enabled)

        self.lbl_db_count = QLabel("В базе: 0 ед. техники")
        self.lbl_db_count.setStyleSheet("font-size: 11px; color: #777; margin-bottom: 2px;")
        self.lbl_db_count.setAlignment(Qt.AlignmentFlag.AlignRight)
        main.addWidget(self.lbl_db_count)

        self.btn_start = QPushButton("СТАРТ")
        self.btn_start.setFixedHeight(45)
        self.btn_start.setStyleSheet("font-weight: bold; font-size: 14px; background-color: #2b2b2b; color: white;")
        self.btn_start.clicked.connect(self.start_tracking)
        main.addWidget(self.btn_start)

        self.lbl_ov_status = QLabel("Оверлей: ВИДЕН")
        self.lbl_ov_status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_ov_status.setStyleSheet("font-weight: bold; color: #00FF00; margin-bottom: 5px;")
        main.addWidget(self.lbl_ov_status)

        self.status = QLabel("Нажми Старт")
        self.status.setAlignment(Qt.AlignmentFlag.AlignCenter); self.status.setStyleSheet("color: #888;")
        main.addWidget(self.status)
        self.setLayout(main)

    def update_db_counter(self):
        count = len(self.logic.vehicle_db)
        self.lbl_db_count.setText(f"В базе: {count} ед. техники")

    def update_filters(self):
        self.logic.selected_nations = {n for n, cb in self.n_checks.items() if cb.isChecked()}
        self.logic.selected_types = {t for t, cb in self.t_checks.items() if cb.isChecked()}
        self.logic.show_notes = self.cb_show_notes.isChecked()
        self.logic.show_names = self.cb_show_names.isChecked()
        self.ov.update_view()

    def create_slider(self, layout, name, mn, mx, func):
        l = QLabel(name); l.setStyleSheet("font-size: 11px; margin-top: 2px;")
        layout.addWidget(l)
        s = QSlider(Qt.Orientation.Horizontal); s.setRange(mn, mx); s.valueChanged.connect(func)
        layout.addWidget(s); return s

    def update_scan_hotkey_from_ui(self):
        new_key = self.entry_hotkey.text().lower().strip()
        if not new_key: return
        self.current_scan_hotkey = new_key
        self.update_keys_in_worker()
        self.save_config() 
        QMessageBox.information(self, "Успех", f"Кнопка скан: {new_key}")

    def update_overlay_hotkey_from_ui(self):
        new_key = self.entry_ov_hotkey.text().lower().strip()
        if not new_key: return
        self.current_overlay_hotkey = new_key
        self.update_keys_in_worker() 
        self.save_config()
        self.update_ov_status_label() 
        QMessageBox.information(self, "Успех", f"Кнопка скрытия: {new_key}")

    # --- НОВАЯ ФУНКЦИЯ ОБНОВЛЕНИЯ ПРИ СМЕНЕ ЭКРАНА ---
    def on_screen_changed(self, index):
        screens = QApplication.screens()
        if 0 <= index < len(screens):
            screen_geo = screens[index].geometry()
            # Обновляем пределы слайдеров под разрешение выбранного монитора
            self.slider_x.setRange(0, screen_geo.width())
            self.slider_y.setRange(0, screen_geo.height())
            self.update_ov_preview()

    def set_top_left(self):
        if not self.settings_frame.isVisible(): return
        if pyautogui:
            x, y = pyautogui.position()
            self.temp_top_left = (x, y)
            self.lbl_region.setText(f"Точка 1 задана: {x}, {y}. Жми F7 во второй точке.")
            self.lbl_region.setStyleSheet("color: orange; font-size: 10px;")

    def set_bottom_right(self):
        if not self.settings_frame.isVisible() or not self.temp_top_left: return
        if pyautogui:
            x2, y2 = pyautogui.position()
            x1, y1 = self.temp_top_left
            w = abs(x2 - x1)
            h = abs(y2 - y1)
            final_x = min(x1, x2)
            final_y = min(y1, y2)
            if w < 5 or h < 5: 
                print("Слишком маленькая зона!")
                return
            self.scan_region = (final_x, final_y, w, h)
            self.lbl_region.setText(f"Зона: X={final_x}, Y={final_y}, W={w}, H={h}")
            self.lbl_region.setStyleSheet("color: #00FF00; font-size: 10px;")
            self.save_config() 

    def start_flag_scan(self):
        print(f"Нажат {self.current_scan_hotkey}. Скан...")
        if self.scan_region:
            self.worker = FlagScannerWorker(self.scan_region, FLAGS_DIR)
            self.worker.flags_found.connect(self.apply_detected_flags)
            self.worker.start()
        else:
            self.ov.show_scan_result([])

    def apply_detected_flags(self, nations):
        self.ov.show_scan_result(nations)
        if not nations: return
        for cb in self.n_checks.values(): cb.blockSignals(True)
        for cb in self.n_checks.values(): cb.setChecked(False)
        count = 0
        for nat_name in nations:
            for key, cb in self.n_checks.items():
                if key.lower() == nat_name.lower():
                    cb.setChecked(True)
                    count += 1
        for cb in self.n_checks.values(): cb.blockSignals(False)
        self.update_filters()

    def toggle_settings_panel(self):
        if self.btn_settings.isChecked():
            self.settings_frame.show(); self.setFixedSize(380, 880)
            self.ov.show_border = True; self.ov.show(); self.ov.update_style()
            self.enable_setup_hotkeys()
        else:
            self.settings_frame.hide(); self.setFixedSize(380, 550)
            self.ov.show_border = False; self.ov.update_style()
            if not self.logic.active_enemies: self.ov.label.setText("")
            self.disable_setup_hotkeys()

    def update_ov_preview(self):
        # --- ЛОГИКА КООРДИНАТ ДЛЯ ВТОРОГО МОНИТОРА ---
        screens = QApplication.screens()
        idx = self.combo_screens.currentIndex()
        
        if 0 <= idx < len(screens):
            target_screen = screens[idx]
            geo = target_screen.geometry()
            
            # Координата оверлея = (Х экрана + Х слайдера), (Y экрана + Y слайдера)
            global_x = geo.x() + self.slider_x.value()
            global_y = geo.y() + self.slider_y.value()
            
            self.ov.move(global_x, global_y)
        else:
            # Fallback если что-то пошло не так
            self.ov.move(self.slider_x.value(), self.slider_y.value())

        self.ov.font_size = self.slider_size.value()
        self.ov.setWindowOpacity(self.slider_opa.value() / 100.0)
        self.ov.update_style()

    def start_tracking(self):
        self.btn_start.setEnabled(False); self.btn_start.setText("ЗАПУЩЕНО")
        self.tracking_started = True 
        
        if self.btn_settings.isChecked(): 
            self.btn_settings.setChecked(False)
            self.toggle_settings_panel()
        
        self.update_filters()
        
        self.ov.temp_test_mode = True; self.ov.update_view()
        QTimer.singleShot(3000, self.disable_temp_test)
        
        if self.is_overlay_visible:
            self.ov.show()
        
        if not self.poller.isRunning():
            self.poller.start()
        
        self.ui_timer.start(1000)
        self.status.setText("Поиск игры...")

    def update_status_display(self):
        if not self.tracking_started:
            self.status.setText("Ожидание запуска...")
            return

        if not self.logic.is_api_available:
            self.status.setText("⛔ ИГРА НЕ ЗАПУЩЕНА")
            self.status.setStyleSheet("color: #FF4444; font-weight: bold;")
        elif not self.logic.is_in_battle:
            self.status.setText("⚓ В АНГАРЕ")
            self.status.setStyleSheet("color: #FFA500; font-weight: bold;")
        else:
            self.status.setText("⚔️ В БОЮ (ОТСЛЕЖИВАНИЕ)")
            self.status.setStyleSheet("color: #00FF00; font-weight: bold;")

    def disable_temp_test(self): self.ov.temp_test_mode = False; self.ov.update_view()
    
    def toggle_ov_visibility(self):
        if self.ov.isVisible(): 
            self.ov.hide()
            self.is_overlay_visible = False
        else: 
            self.ov.show()
            self.is_overlay_visible = True
        
        self.update_ov_status_label()

    def update_ov_status_label(self):
        if self.is_overlay_visible:
            self.lbl_ov_status.setText(f"Оверлей: ВИДЕН [{self.current_overlay_hotkey}]")
            self.lbl_ov_status.setStyleSheet("font-weight: bold; color: #00FF00; margin-bottom: 5px;")
        else:
            self.lbl_ov_status.setText(f"Оверлей: СКРЫТ [{self.current_overlay_hotkey}]")
            self.lbl_ov_status.setStyleSheet("font-weight: bold; color: #FF4444; margin-bottom: 5px;")
    
    def save_config(self):
        cfg = {
            "x": self.slider_x.value(), "y": self.slider_y.value(),
            "monitor_idx": self.combo_screens.currentIndex(), # Сохраняем индекс монитора
            "size": self.slider_size.value(), "opa": self.slider_opa.value(),
            "nations": [n for n, cb in self.n_checks.items() if cb.isChecked()],
            "types": [t for t, cb in self.t_checks.items() if cb.isChecked()],
            "scan_region": self.scan_region,
            "scan_hotkey": self.current_scan_hotkey,
            "overlay_hotkey": self.current_overlay_hotkey,
            "show_notes": self.cb_show_notes.isChecked(),
            "show_names": self.cb_show_names.isChecked(),
            "sound_enabled": self.cb_sound_enabled.isChecked()
        }
        try:
            with open(CONFIG_FILE, "w") as f: json.dump(cfg, f)
            print("Конфиг сохранен.")
        except Exception as e: print("save_config error:", e)
        self.logic.save_unknowns()

    def load_config(self):
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, "r") as f: c = json.load(f)
                
                # Загружаем индекс монитора
                mon_idx = c.get("monitor_idx", 0)
                if mon_idx < self.combo_screens.count():
                    self.combo_screens.setCurrentIndex(mon_idx)
                
                self.slider_x.setValue(c.get("x", 30)); self.slider_y.setValue(c.get("y", 150))
                self.slider_size.setValue(c.get("size", 20)); self.slider_opa.setValue(c.get("opa", 100))
                for n in c.get("nations", []):
                    if n in self.n_checks: self.n_checks[n].setChecked(True)
                for t in c.get("types", []):
                    if t in self.t_checks: self.t_checks[t].setChecked(True)
                self.scan_region = c.get("scan_region", None)
                self.cb_show_notes.setChecked(c.get("show_notes", True))
                self.cb_show_names.setChecked(c.get("show_names", True))
                self.cb_sound_enabled.setChecked(c.get("sound_enabled", True))
                if self.scan_region:
                    self.lbl_region.setText(f"Зона из памяти: {self.scan_region}")
                    self.lbl_region.setStyleSheet("color: #00FF00; font-size: 10px;")
                
                self.current_scan_hotkey = c.get("scan_hotkey", "/")
                self.entry_hotkey.setText(self.current_scan_hotkey)
                
                self.current_overlay_hotkey = c.get("overlay_hotkey", "f8")
                self.entry_ov_hotkey.setText(self.current_overlay_hotkey)

            except Exception as e: print("load_config error:", e)
    
    def closeEvent(self, event):
        if self.poller.isRunning(): self.poller.stop()
        if self.key_worker.isRunning(): self.key_worker.stop()
        self.save_config()
        event.accept()

if __name__ == "__main__":
    app = QApplication(sys.argv)
    ex = Settings()
    ex.show()
    sys.exit(app.exec())