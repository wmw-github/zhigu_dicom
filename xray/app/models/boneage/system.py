import sys
import os
from PyQt5.QtWidgets import (QApplication, QMainWindow, QVBoxLayout, QHBoxLayout, 
                             QPushButton, QLabel, QTextEdit, QFileDialog, QWidget, 
                             QMessageBox, QGroupBox, QComboBox, QProgressBar,
                             QRadioButton, QMenuBar, QStatusBar)
from PyQt5.QtCore import Qt, QThread, pyqtSignal, QTimer
from PyQt5.QtGui import QPixmap, QFont, QPainter, QPen
from datetime import datetime
import cv2
import numpy as np
from tools import BoneAgeDetector  # 导入你的骨龄检测类

class DetectionThread(QThread):
    """检测线程，避免界面卡顿"""
    finished = pyqtSignal(dict)
    error = pyqtSignal(str)
    progress = pyqtSignal(int)
    
    def __init__(self, detector, img_path, sex):
        super().__init__()
        self.detector = detector
        self.img_path = img_path
        self.sex = sex
    
    def run(self):
        try:
            # 模拟进度更新
            for i in range(0, 101, 10):
                self.progress.emit(i)
                QThread.msleep(200)
            
            result = self.detector.detect_bone_age(self.img_path, self.sex)
            self.progress.emit(100)
            self.finished.emit(result)
        except Exception as e:
            self.error.emit(str(e))

class BoneAgeDetectionGUI(QMainWindow):
    def __init__(self):
        super().__init__()
        self.detector = None
        self.current_image_path = None
        self.current_result = None
        self.init_ui()
        self.load_detector()
    
    def init_ui(self):
        """初始化用户界面 - 使用您提供的UI布局"""
        self.setWindowTitle("骨龄检测系统 - RUS-CHN法")
        self.setGeometry(0, 0, 876, 797)
        
        # 中央窗口部件
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        
        # 使用您提供的UI布局
        self.setup_custom_ui(central_widget)
        
        # 状态栏
        self.statusbar = QStatusBar()
        self.setStatusBar(self.statusbar)
        self.statusbar.showMessage("就绪")
        
        # 初始化UI状态
        self.init_ui_state()
    
    def setup_custom_ui(self, central_widget):
        """设置您提供的自定义UI布局"""
        # 主布局
        layout = QVBoxLayout(central_widget)
        
        # 图像显示区域
        image_layout = QHBoxLayout()
        
        # 左侧图像标签
        self.labelImg = QLabel("请选择X光片图像")
        self.labelImg.setMinimumSize(311, 241)
        self.labelImg.setStyleSheet("color:rgb(255, 255, 255); background-color:rgb(0,0,0);")
        self.labelImg.setAlignment(Qt.AlignCenter)
        image_layout.addWidget(self.labelImg)
        
        # 右侧结果标签
        self.labelResult = QLabel("检测结果将显示在这里")
        self.labelResult.setMinimumSize(331, 251)
        self.labelResult.setStyleSheet("color:rgb(255, 255, 255); background-color:rgb(0,0,0);")
        self.labelResult.setAlignment(Qt.AlignCenter)
        image_layout.addWidget(self.labelResult)
        
        layout.addLayout(image_layout)
        
        # 按钮区域
        button_layout = QHBoxLayout()
        
        # 选择图片按钮
        self.btnOpen = QPushButton("选择图片")
        self.btnOpen.clicked.connect(self.upload_image)
        button_layout.addWidget(self.btnOpen)
        
        # 初始化模型按钮
        self.pushButton_2 = QPushButton("初始化模型")
        self.pushButton_2.clicked.connect(self.load_detector)
        button_layout.addWidget(self.pushButton_2)
        
        # 开始检测按钮
        self.btnPredict = QPushButton("开始检测")
        self.btnPredict.clicked.connect(self.start_detection)
        self.btnPredict.setEnabled(False)
        button_layout.addWidget(self.btnPredict)
        
        layout.addLayout(button_layout)
        
        # 性别选择区域
        gender_layout = QHBoxLayout()
        gender_layout.addStretch()
        
        self.radioBoy = QRadioButton("男")
        self.radioBoy.setChecked(True)
        gender_layout.addWidget(self.radioBoy)
        
        self.radioGirl = QRadioButton("女")
        gender_layout.addWidget(self.radioGirl)
        
        gender_layout.addStretch()
        layout.addLayout(gender_layout)
        
        # 进度条
        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        layout.addWidget(self.progress_bar)
        
        # 文本编辑区域 - 用于显示详细报告
        self.textEdit = QTextEdit()
        self.textEdit.setMinimumSize(661, 151)
        self.textEdit.setReadOnly(True)
        
        # 设置欢迎文本
        welcome_text = """=== 骨龄检测系统 (RUS-CHN法) ===

使用说明：
1. 点击"选择图片"按钮上传手部X光片
2. 点击"初始化模型"加载检测模型
3. 选择受检者性别（男/女）
4. 点击"开始检测"进行骨龄分析

检测结果将显示在右侧区域，详细报告将显示在此处。

支持的图像格式：PNG, JPG, JPEG, BMP, TIFF"""
        self.textEdit.setText(welcome_text)
        
        layout.addWidget(self.textEdit)
        
        # 添加保存报告按钮
        self.save_btn = QPushButton("保存检测报告")
        self.save_btn.clicked.connect(self.save_report)
        self.save_btn.setEnabled(False)
        layout.addWidget(self.save_btn)
    
    def init_ui_state(self):
        """初始化UI状态"""
        # 设置按钮样式
        self.set_button_styles()
        
        # 初始化图像标签
        self.update_image_label(self.labelImg, "请选择X光片图像")
        self.update_image_label(self.labelResult, "检测结果将显示在这里")
    
    def set_button_styles(self):
        """设置按钮样式"""
        button_style = """
            QPushButton {
                background-color: #4CAF50;
                color: white;
                font-size: 14px;
                padding: 8px 16px;
                border: none;
                border-radius: 4px;
                min-width: 100px;
            }
            QPushButton:hover {
                background-color: #45a049;
            }
            QPushButton:disabled {
                background-color: #cccccc;
                color: #666666;
            }
            QPushButton#detect {
                background-color: #2196F3;
            }
            QPushButton#detect:hover {
                background-color: #0b7dda;
            }
            QPushButton#save {
                background-color: #ff9800;
            }
            QPushButton#save:hover {
                background-color: #e68900;
            }
        """
        self.btnOpen.setStyleSheet(button_style)
        self.pushButton_2.setStyleSheet(button_style)
        self.btnPredict.setStyleSheet(button_style)
        self.btnPredict.setObjectName("detect")
        self.save_btn.setStyleSheet(button_style)
        self.save_btn.setObjectName("save")
    
    def update_image_label(self, label, text):
        """更新图像标签显示"""
        label.setText(text)
        label.setAlignment(Qt.AlignCenter)
        label.setStyleSheet("""
            QLabel {
                color: white;
                background-color: black;
                border: 2px solid #666;
                font-size: 14px;
                qproperty-alignment: AlignCenter;
            }
        """)
    
    def load_detector(self):
        """加载检测模型"""
        try:
            self.statusbar.showMessage("正在加载检测模型...")
            self.pushButton_2.setEnabled(False)
            self.pushButton_2.setText("加载中...")
            
            # 模拟加载过程
            QTimer.singleShot(1000, self.on_detector_loaded)
            
        except Exception as e:
            QMessageBox.critical(self, "错误", f"模型加载失败: {str(e)}")
            self.statusbar.showMessage("模型加载失败")
            self.pushButton_2.setEnabled(True)
            self.pushButton_2.setText("初始化模型")
    
    def on_detector_loaded(self):
        """模型加载完成"""
        try:
            self.detector = BoneAgeDetector()  # 实际加载模型
            self.statusbar.showMessage("模型加载完成")
            self.pushButton_2.setText("模型已加载")
            self.pushButton_2.setEnabled(False)
            QMessageBox.information(self, "成功", "骨龄检测模型加载完成！")
        except Exception as e:
            QMessageBox.critical(self, "错误", f"模型加载失败: {str(e)}")
            self.statusbar.showMessage("模型加载失败")
            self.pushButton_2.setEnabled(True)
            self.pushButton_2.setText("初始化模型")
    
    def upload_image(self):
        """上传图像文件"""
        file_path, _ = QFileDialog.getOpenFileName(
            self, 
            "选择手部X光片", 
            "", 
            "图像文件 (*.png *.jpg *.jpeg *.bmp *.tiff)"
        )
        
        if file_path:
            self.current_image_path = file_path
            
            # 显示原始图像
            pixmap = QPixmap(file_path)
            if not pixmap.isNull():
                # 调整图像大小以适应标签
                scaled_pixmap = pixmap.scaled(
                    self.labelImg.width() - 20,
                    self.labelImg.height() - 20,
                    Qt.KeepAspectRatio,
                    Qt.SmoothTransformation
                )
                self.labelImg.setPixmap(scaled_pixmap)
                self.btnPredict.setEnabled(True)
                self.statusbar.showMessage(f"已加载图像: {os.path.basename(file_path)}")
                
                # 更新文本显示
                self.textEdit.append(f"\n✓ 已加载图像: {os.path.basename(file_path)}")
            else:
                QMessageBox.warning(self, "警告", "无法加载选定的图像文件")
                self.update_image_label(self.labelImg, "图像加载失败")
    
    def start_detection(self):
        """开始检测过程"""
        if not self.current_image_path:
            QMessageBox.warning(self, "警告", "请先上传X光片图像")
            return
        
        if not self.detector:
            QMessageBox.warning(self, "警告", "请先初始化检测模型")
            return
        
        # 禁用按钮，显示进度条
        self.btnOpen.setEnabled(False)
        self.btnPredict.setEnabled(False)
        self.save_btn.setEnabled(False)
        self.progress_bar.setVisible(True)
        self.progress_bar.setValue(0)
        
        self.statusbar.showMessage("正在进行骨龄检测...")
        
        # 获取性别选择
        sex = "boy" if self.radioBoy.isChecked() else "girl"
        
        # 在单独线程中运行检测
        self.detection_thread = DetectionThread(
            self.detector, 
            self.current_image_path, 
            sex
        )
        self.detection_thread.finished.connect(self.on_detection_finished)
        self.detection_thread.error.connect(self.on_detection_error)
        self.detection_thread.progress.connect(self.on_detection_progress)
        self.detection_thread.start()
        
        # 更新界面状态
        self.textEdit.append(f"\n🔍 开始骨龄检测...")
        self.textEdit.append(f"   性别: {'男' if sex == 'boy' else '女'}")
        self.textEdit.append(f"   图像: {os.path.basename(self.current_image_path)}")
    
    def on_detection_progress(self, value):
        """更新检测进度"""
        self.progress_bar.setValue(value)
    
    def on_detection_finished(self, result):
        """检测完成处理"""
        # 启用按钮，隐藏进度条
        self.btnOpen.setEnabled(True)
        self.btnPredict.setEnabled(True)
        self.save_btn.setEnabled(True)
        self.progress_bar.setVisible(False)
        
        self.current_result = result
        
        if "error" in result:
            QMessageBox.critical(self, "检测错误", result["error"])
            self.statusbar.showMessage("检测失败")
            self.textEdit.append(f"\n❌ 检测失败: {result['error']}")
            return
        
        # 显示检测结果图像
        if os.path.exists(result.get("result_img_path", "")):
            result_pixmap = QPixmap(result["result_img_path"])
            if not result_pixmap.isNull():
                scaled_pixmap = result_pixmap.scaled(
                    self.labelResult.width() - 20,
                    self.labelResult.height() - 20,
                    Qt.KeepAspectRatio,
                    Qt.SmoothTransformation
                )
                self.labelResult.setPixmap(scaled_pixmap)
        
        # 更新文本报告
        self.update_result_display(result)
        
        self.statusbar.showMessage("检测完成")
        self.textEdit.append("\n✅ 骨龄检测完成！")
    
    def update_result_display(self, result):
        """更新结果显示"""
        # 清空之前的显示
        self.labelResult.clear()
        
        # 创建结果摘要文本
        result_text = f"""
=== 骨龄检测结果 ===

📊 总体结果:
    • 总得分: {result.get('total_score', 'N/A')} 分
    • 骨龄: {result.get('bone_age', 'N/A')} 岁
    • 性别: {'男' if self.radioBoy.isChecked() else '女'}

🔬 关节得分详情:
"""
        
        # 添加关节得分详情
        joint_scores = result.get('joint_scores', {})
        for joint, (level, score) in joint_scores.items():
            result_text += f"    • {joint}: 等级{level}级, 得分{score}分\n"
        
        # 添加详细报告
        result_text += f"\n📋 详细报告:\n{result.get('report', '无详细报告')}"
        
        # 更新文本编辑框
        self.textEdit.setText(result_text)
        
        # 在结果标签中显示主要结果
        if 'bone_age' in result:
            summary_text = f"骨龄: {result['bone_age']}岁\n总得分: {result['total_score']}分"
            self.labelResult.setText(summary_text)
    
    def on_detection_error(self, error_msg):
        """检测错误处理"""
        self.btnOpen.setEnabled(True)
        self.btnPredict.setEnabled(True)
        self.progress_bar.setVisible(False)
        
        QMessageBox.critical(self, "检测错误", error_msg)
        self.statusbar.showMessage("检测失败")
        self.textEdit.append(f"\n❌ 检测错误: {error_msg}")
    
    def save_report(self):
        """保存检测报告"""
        if not self.current_result:
            QMessageBox.warning(self, "警告", "没有可保存的检测结果")
            return
        
        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "保存检测报告",
            f"骨龄检测报告_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt",
            "文本文件 (*.txt)"
        )
        
        if file_path:
            try:
                with open(file_path, 'w', encoding='utf-8') as f:
                    f.write("=== 骨龄检测报告 (RUS-CHN法) ===\n\n")
                    f.write(f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                    f.write(f"图像文件: {os.path.basename(self.current_image_path)}\n")
                    f.write(f"性别: {'男' if self.radioBoy.isChecked() else '女'}\n\n")
                    
                    f.write("📊 总体结果:\n")
                    f.write(f"   总得分: {self.current_result.get('total_score', 'N/A')} 分\n")
                    f.write(f"   骨龄: {self.current_result.get('bone_age', 'N/A')} 岁\n\n")
                    
                    f.write("🔬 关节得分详情:\n")
                    joint_scores = self.current_result.get('joint_scores', {})
                    for joint, (level, score) in joint_scores.items():
                        f.write(f"   {joint}: 等级{level}级, 得分{score}分\n")
                    
                    f.write(f"\n📋 详细报告:\n{self.current_result.get('report', '无详细报告')}")
                
                QMessageBox.information(self, "成功", f"报告已保存到: {file_path}")
                self.textEdit.append(f"\n💾 报告已保存: {file_path}")
            except Exception as e:
                QMessageBox.critical(self, "错误", f"保存报告失败: {str(e)}")

def main():
    app = QApplication(sys.argv)
    
    # 设置应用程序样式
    app.setStyle('Fusion')
    
    # 设置应用程序字体
    font = QFont("Microsoft YaHei", 10)
    app.setFont(font)
    
    window = BoneAgeDetectionGUI()
    window.show()
    
    sys.exit(app.exec_())

if __name__ == '__main__':
    main()