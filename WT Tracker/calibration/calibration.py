import os
import cv2
import numpy as np
from PyQt6.QtWidgets import (QApplication, QWidget, QVBoxLayout, QPushButton, 
                             QLabel, QFileDialog, QSlider, QHBoxLayout)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QImage, QPixmap

class FlagTester(QWidget):
    def __init__(self):
        super().__init__()
        self.flags_dir = os.path.join(os.path.dirname(__file__), "flags")
        self.test_image_path = None
        self.confidence = 0.8
        self.init_ui()

    def init_ui(self):
        self.setWindowTitle("Тестер точности поиска флагов")
        layout = QVBoxLayout()

        # Кнопка выбора картинки
        self.btn_load = QPushButton("Выбрать скриншот для теста")
        self.btn_load.clicked.connect(self.load_test_image)
        layout.addWidget(self.btn_load)

        # Слайдер точности
        self.lbl_conf = QLabel(f"Точность (confidence): {self.confidence}")
        layout.addWidget(self.lbl_conf)
        
        slider_layout = QHBoxLayout()
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(50, 99)
        self.slider.setValue(80)
        self.slider.valueChanged.connect(self.update_confidence)
        slider_layout.addWidget(self.slider)
        layout.addWidget(self.slider)

        # Кнопка запуска
        self.btn_run = QPushButton("ЗАПУСТИТЬ ТЕСТ")
        self.btn_run.setStyleSheet("background-color: #2ecc71; font-weight: bold;")
        self.btn_run.clicked.connect(self.run_test)
        layout.addWidget(self.btn_run)

        # Область вывода результата
        self.result_label = QLabel("Результат появится здесь")
        self.result_label.setWordWrap(True)
        self.result_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.result_label)

        self.setLayout(layout)
        self.resize(400, 300)

    def update_confidence(self):
        self.confidence = self.slider.value() / 100.0
        self.lbl_conf.setText(f"Точность (confidence): {self.confidence}")

    def load_test_image(self):
        file, _ = QFileDialog.getOpenFileName(self, "Открыть скриншот", "", "Images (*.png *.jpg)")
        if file:
            self.test_image_path = file
            self.btn_load.setText(f"Файл: {os.path.basename(file)}")

    def run_test(self):
        if not self.test_image_path:
            self.result_label.setText("❌ Сначала выбери картинку!")
            return
        
        if not os.path.exists(self.flags_dir):
            self.result_label.setText("❌ Папка 'flags' не найдена рядом со скриптом!")
            return

        results = []
        # Загружаем основной скриншот через OpenCV
        img_rgb = cv2.imread(self.test_image_path)
        img_gray = cv2.cvtColor(img_rgb, cv2.COLOR_BGR2GRAY)

        for filename in os.listdir(self.flags_dir):
            if filename.endswith((".png", ".jpg")):
                template_path = os.path.join(self.flags_dir, filename)
                template = cv2.imread(template_path, 0) # Читаем в сером для скорости
                
                if template is None: continue

                # Метод поиска совпадений
                res = cv2.matchTemplate(img_gray, template, cv2.TM_CCOEFF_NORMED)
                min_val, max_val, min_loc, max_loc = cv2.minMaxLoc(res)

                status = "✅ НАЙДЕН" if max_val >= self.confidence else "❌ НЕТ"
                results.append(f"{filename}: {status} (совпадение: {max_val:.2f})")

        self.result_label.setText("\n".join(results))

if __name__ == "__main__":
    app = QApplication([])
    window = FlagTester()
    window.show()
    app.exec()