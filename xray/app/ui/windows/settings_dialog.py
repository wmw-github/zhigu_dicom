"""
检测阈值设置对话框
"""
import json
import os
from pathlib import Path

from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel,
    QSlider, QDoubleSpinBox, QPushButton, QGroupBox,
    QFormLayout, QMessageBox, QApplication
)
from PyQt5.QtCore import Qt


class SettingsDialog(QDialog):
    """检测阈值设置对话框"""

    SETTINGS_VERSION = 2

    DEFAULT_SETTINGS = {
        "settings_version": 2,
        "chest_conf": 0.15,
        "boneage_detect_conf": 0.20,
        "boneage_cls_conf": 0.20,
        "fracture_conf": 0.03
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("X线辅助诊断系统 - 检测设置")
        self.setMinimumSize(480, 380)
        self.settings = self._load_settings()
        self._init_ui()

    def _get_settings_path(self):
        base_dir = Path(__file__).parent.parent.parent
        return base_dir / "data" / "detection_settings.json"

    def _load_settings(self):
        settings_path = self._get_settings_path()
        if settings_path.exists():
            try:
                with open(settings_path, 'r', encoding='utf-8') as f:
                    saved = json.load(f)
                if saved.get('settings_version', 0) < self.SETTINGS_VERSION:
                    saved = {**self.DEFAULT_SETTINGS, **saved}
                    saved['settings_version'] = self.SETTINGS_VERSION
                    with open(settings_path, 'w', encoding='utf-8') as f:
                        json.dump(saved, f, indent=2, ensure_ascii=False)
                return saved
            except:
                pass
        return self.DEFAULT_SETTINGS.copy()

    def _save_settings(self):
        settings_path = self._get_settings_path()
        os.makedirs(settings_path.parent, exist_ok=True)
        with open(settings_path, 'w', encoding='utf-8') as f:
            json.dump(self.settings, f, indent=2, ensure_ascii=False)

    def _create_slider_group(self, title, key, min_val, max_val, step, desc):
        group = QGroupBox(title)
        layout = QVBoxLayout()

        desc_label = QLabel(desc)
        desc_label.setStyleSheet("color: #666; font-size: 12px;")
        layout.addWidget(desc_label)

        slider_layout = QHBoxLayout()

        self.sliders[key] = QSlider(Qt.Horizontal)
        self.sliders[key].setMinimum(int(min_val * 100))
        self.sliders[key].setMaximum(int(max_val * 100))
        self.sliders[key].setSingleStep(int(step * 100))
        self.sliders[key].setValue(int(self.settings.get(key, min_val) * 100))
        self.sliders[key].valueChanged.connect(lambda v, k=key: self._on_slider_changed(k))

        self.spinboxes[key] = QDoubleSpinBox()
        self.spinboxes[key].setRange(min_val, max_val)
        self.spinboxes[key].setSingleStep(step)
        self.spinboxes[key].setDecimals(2)
        self.spinboxes[key].setValue(self.settings.get(key, min_val))
        self.spinboxes[key].setFixedWidth(80)
        self.spinboxes[key].valueChanged.connect(lambda v, k=key: self._on_spinbox_changed(k))

        slider_layout.addWidget(self.sliders[key])
        slider_layout.addWidget(self.spinboxes[key])
        slider_layout.addWidget(QLabel("%"))

        layout.addLayout(slider_layout)
        group.setLayout(layout)
        return group

    def _on_slider_changed(self, key):
        value = self.sliders[key].value() / 100.0
        self.spinboxes[key].blockSignals(True)
        self.spinboxes[key].setValue(value)
        self.spinboxes[key].blockSignals(False)

    def _on_spinbox_changed(self, key):
        value = self.spinboxes[key].value()
        self.sliders[key].blockSignals(True)
        self.sliders[key].setValue(int(value * 100))
        self.sliders[key].blockSignals(False)

    def _init_ui(self):
        self.setStyleSheet("""
            QDialog {
                background-color: #f5f5f5;
            }
            QGroupBox {
                font-weight: bold;
                border: 1px solid #ddd;
                border-radius: 8px;
                margin-top: 12px;
                padding-top: 24px;
                background-color: white;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 12px;
                padding: 0 8px;
                color: #0078d4;
            }
            QSlider::groove:horizontal {
                height: 8px;
                background: #e0e0e0;
                border-radius: 4px;
            }
            QSlider::handle:horizontal {
                width: 18px;
                margin: -5px 0;
                background: #0078d4;
                border-radius: 9px;
            }
            QSlider::sub-page:horizontal {
                background: #0078d4;
                border-radius: 4px;
            }
            QDoubleSpinBox {
                padding: 4px;
                border: 1px solid #ddd;
                border-radius: 4px;
            }
            QPushButton {
                padding: 10px 20px;
                border-radius: 4px;
                font-weight: bold;
            }
        """)

        layout = QVBoxLayout(self)
        layout.setSpacing(16)
        layout.setContentsMargins(24, 24, 24, 24)

        title = QLabel("🔧 检测阈值调节")
        title.setStyleSheet("font-size: 18px; font-weight: bold; color: #333; padding: 8px;")
        layout.addWidget(title)

        info_label = QLabel("调整各检测模块的置信度阈值，值越低检测越敏感（可能增加误检），值越高越严格（可能漏检）")
        info_label.setStyleSheet("color: #666; font-size: 12px; padding: 4px;")
        info_label.setWordWrap(True)
        layout.addWidget(info_label)

        self.sliders = {}
        self.spinboxes = {}

        chest_group = self._create_slider_group(
            "🫁 胸片疾病检测",
            "chest_conf", 0.05, 0.95, 0.01,
            "控制14种胸肺部疾病的检出灵敏度"
        )
        layout.addWidget(chest_group)

        boneage_det_group = self._create_slider_group(
            "🦴 骨龄-关节检测",
            "boneage_detect_conf", 0.20, 0.95, 0.01,
            "骨龄评估中13个关节的定位检测灵敏度"
        )
        layout.addWidget(boneage_det_group)

        boneage_cls_group = self._create_slider_group(
            "📊 骨龄-等级分类",
            "boneage_cls_conf", 0.20, 0.95, 0.01,
            "骨龄等级分类的置信度要求"
        )
        layout.addWidget(boneage_cls_group)

        fracture_group = self._create_slider_group(
            "🔍 骨折检测",
            "fracture_conf", 0.05, 0.95, 0.01,
            "骨折线及异常区域的检出灵敏度"
        )
        layout.addWidget(fracture_group)

        layout.addStretch()

        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        reset_btn = QPushButton("恢复默认")
        reset_btn.setStyleSheet("""
            QPushButton {
                background-color: #f0f0f0;
                color: #333;
                border: 1px solid #ccc;
            }
            QPushButton:hover {
                background-color: #e0e0e0;
            }
        """)
        reset_btn.clicked.connect(self._on_reset)
        btn_layout.addWidget(reset_btn)

        save_btn = QPushButton("保存设置")
        save_btn.setStyleSheet("""
            QPushButton {
                background-color: #107c10;
                color: white;
                border: none;
            }
            QPushButton:hover {
                background-color: #0e6e0e;
            }
        """)
        save_btn.clicked.connect(self._on_save)
        btn_layout.addWidget(save_btn)

        cancel_btn = QPushButton("取消")
        cancel_btn.setStyleSheet("""
            QPushButton {
                background-color: #fff;
                color: #333;
                border: 1px solid #ccc;
            }
            QPushButton:hover {
                background-color: #f5f5f5;
            }
        """)
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(cancel_btn)

        layout.addLayout(btn_layout)

    def _on_reset(self):
        reply = QMessageBox.question(
            self, "确认重置",
            "确定要恢复所有设置为默认值吗？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )
        if reply == QMessageBox.Yes:
            for key, default_val in self.DEFAULT_SETTINGS.items():
                if key == 'settings_version':
                    continue
                self.sliders[key].setValue(int(default_val * 100))

    def _on_save(self):
        for key in self.DEFAULT_SETTINGS:
            if key == 'settings_version':
                continue
            self.settings[key] = round(self.spinboxes[key].value(), 2)
        self._save_settings()
        QMessageBox.information(self, "保存成功", "检测阈值设置已保存！\n\n下次检测时将使用新设置的阈值。")
        self.accept()

    def get_settings(self):
        return self.settings.copy()


def get_detection_settings():
    """获取当前检测设置"""
    dialog = SettingsDialog()
    return dialog._load_settings()


if __name__ == "__main__":
    import sys
    app = QApplication(sys.argv)
    dialog = SettingsDialog()
    if dialog.exec_() == QDialog.Accepted:
        print("Settings:", dialog.get_settings())
    sys.exit(0)
