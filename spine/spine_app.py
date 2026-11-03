import sys
import os
import glob
import pydicom
import numpy as np
import cv2
import ctypes
import json
import xml.etree.ElementTree as ET
from datetime import datetime
from PyQt5.QtWidgets import (QApplication, QMainWindow, QWidget, QHBoxLayout, QVBoxLayout, 
                             QListWidget, QListWidgetItem, QLabel, QPushButton, QFileDialog,
                             QSplitter, QGraphicsView, QGraphicsScene, QGraphicsPixmapItem,
                             QMessageBox, QToolBar, QAction, QGraphicsLineItem, QGraphicsItem,
                             QSlider, QCheckBox, QGraphicsEllipseItem, QGraphicsRectItem, QGroupBox, QFrame, QMenuBar, QMenu,
                             QDialog, QTableWidget, QTableWidgetItem, QHeaderView, QTreeWidget, QTreeWidgetItem,
                             QProgressBar, QStatusBar, QTreeWidgetItemIterator)
from PyQt5.QtCore import Qt, QSize, QRectF, QPointF, QLineF, QThread, pyqtSignal
from PyQt5.QtGui import QPixmap, QImage, QIcon, QPen, QColor, QBrush, QTransform, QPainter, QCursor

# Helper to get resource path for PyInstaller
def resource_path(relative_path):
    """ Get absolute path to resource, works for dev and for PyInstaller """
    try:
        # PyInstaller creates a temp folder and stores path in _MEIPASS
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")

    return os.path.join(base_path, relative_path)

class DicomHelper:
    @staticmethod
    def get_patient_name(filepath):
        try:
            ds = pydicom.dcmread(filepath, stop_before_pixels=True)
            if hasattr(ds, 'PatientName'):
                return str(ds.PatientName).replace('^', ' ').strip()
            return "Unknown_Patient"
        except:
            return "Unknown_Patient"

    @staticmethod
    def is_spine_image(ds):
        """
        Check if the DICOM image is likely related to spine and not limbs.
        Returns True if it should be kept, False if it should be filtered out.
        """
        # Keywords
        spine_keywords = ['SPINE', 'CERVICAL', 'THORACIC', 'LUMBAR', 'SACRUM', 'VERTEBRA', 'SCOLIOSIS', 'WHOLE']
        limb_keywords = ['KNEE', 'ANKLE', 'FOOT', 'HAND', 'WRIST', 'ELBOW', 'SHOULDER', 'HIP', 'LEG', 'ARM', 'FEMUR', 'TIBIA', 'FIBULA', 'RADIUS', 'ULNA', 'EXTREMITY']
        
        tags_to_check = []
        if hasattr(ds, 'BodyPartExamined'): tags_to_check.append(str(ds.BodyPartExamined).upper())
        if hasattr(ds, 'SeriesDescription'): tags_to_check.append(str(ds.SeriesDescription).upper())
        if hasattr(ds, 'StudyDescription'): tags_to_check.append(str(ds.StudyDescription).upper())
        if hasattr(ds, 'ProtocolName'): tags_to_check.append(str(ds.ProtocolName).upper())
        
        text = " ".join(tags_to_check)
        
        # 1. If explicitly spine, keep it
        for kw in spine_keywords:
            if kw in text:
                return True
                
        # 2. If explicitly limb, discard it
        for kw in limb_keywords:
            if kw in text:
                # Double check: "Whole Spine including Hip" -> keep
                if "SPINE" in text:
                    return True
                return False
                
        # 3. Ambiguous -> Keep it to be safe
        return True

    @staticmethod
    def load_dicom_as_image(filepath):
        try:
            ds = pydicom.dcmread(filepath)
            pixel_array = ds.pixel_array
            
            # Normalize to 0-255
            if pixel_array.max() > 0:
                pixel_array = pixel_array.astype(float)
                pixel_array = (pixel_array / pixel_array.max()) * 255.0
            pixel_array = pixel_array.astype(np.uint8)
            
            # Handle Photometric Interpretation if needed (e.g., MONOCHROME1 needs inversion)
            if hasattr(ds, 'PhotometricInterpretation') and ds.PhotometricInterpretation == 'MONOCHROME1':
                pixel_array = 255 - pixel_array
                
            return pixel_array
        except Exception as e:
            print(f"Error loading DICOM {filepath}: {e}")
            return None

    @staticmethod
    def numpy_to_qpixmap(numpy_img):
        if numpy_img is None:
            return QPixmap()
        
        height, width = numpy_img.shape[:2]
        bytes_per_line = width * (4 if len(numpy_img.shape) == 3 and numpy_img.shape[2] == 4 else 1)
        
        if len(numpy_img.shape) == 2:
            q_img = QImage(numpy_img.data, width, height, bytes_per_line, QImage.Format_Grayscale8)
        elif len(numpy_img.shape) == 3 and numpy_img.shape[2] == 4:
            q_img = QImage(numpy_img.data, width, height, bytes_per_line, QImage.Format_ARGB32)
        else:
             q_img = QImage(numpy_img.data, width, height, bytes_per_line, QImage.Format_RGB888)
             
        return QPixmap.fromImage(q_img)

class DicomLoaderThread(QThread):
    progress = pyqtSignal(int)
    finished = pyqtSignal(dict)
    
    def __init__(self, directory):
        super().__init__()
        self.directory = directory
        
    def run(self):
        # 1. Collect all DCM files
        patterns = ['*.dcm', '*.DCM']
        all_files = []
        for pattern in patterns:
            all_files.extend(glob.glob(os.path.join(self.directory, '**', pattern), recursive=True))
        
        # FIX 1: Deduplicate files (case-insensitive on Windows)
        all_files = list(set(os.path.normpath(f) for f in all_files))
        
        # 2. Group by directory to handle XML logic
        dir_map = {}
        for f in all_files:
            d = os.path.dirname(f)
            if d not in dir_map: dir_map[d] = []
            dir_map[d].append(f)
            
        total_dirs = len(dir_map)
        patient_map = {}
        
        for i, (dpath, files) in enumerate(dir_map.items()):
            # Check for XML in this directory
            xml_info = None
            xml_files = glob.glob(os.path.join(dpath, '*.xml'))
            
            if xml_files:
                try:
                    tree = ET.parse(xml_files[0])
                    root = tree.getroot()
                    
                    # Extract Patient Info
                    pat_info = root.find('PatInfo')
                    name = None
                    pid = None
                    if pat_info is not None:
                        name_node = pat_info.find('PatName')
                        if name_node is not None: name = name_node.text
                        
                        id_node = pat_info.find('PatAccessNo')
                        if id_node is not None: pid = id_node.text
                        
                    # Extract Modality to filter
                    # Structure: SeriesInfo -> SeriesItem -> Modality
                    is_banned = False
                    series_info = root.find('SeriesInfo')
                    if series_info is not None:
                        for item in series_info.findall('SeriesItem'):
                            mod_node = item.find('Modality')
                            if mod_node is not None and mod_node.text:
                                mod = mod_node.text.upper()
                                if 'CT' in mod or 'MR' in mod or 'MRI' in mod:
                                    is_banned = True
                                    break
                    
                    if is_banned:
                        # Skip this directory
                        if total_dirs > 0:
                            self.progress.emit(int((i + 1) / total_dirs * 100))
                        continue
                        
                    if name:
                        xml_info = {'name': name, 'id': pid}
                        
                except Exception as e:
                    print(f"Error parsing XML in {dpath}: {e}")
            
            # Process files in this directory
            for fpath in files:
                try:
                    ds = pydicom.dcmread(fpath, stop_before_pixels=True)
                    
                    # FIX 2: Check Modality in DICOM tag (Double Check)
                    if hasattr(ds, 'Modality'):
                        mod = str(ds.Modality).upper()
                        if 'CT' in mod or 'MR' in mod or 'MRI' in mod:
                            continue

                    if not DicomHelper.is_spine_image(ds):
                        continue
                        
                    pname = "Unknown Patient"
                    image_id = "Unknown"
                    
                    # Name Priority: XML > DICOM
                    if xml_info and xml_info.get('name'):
                        pname = xml_info['name']
                    elif hasattr(ds, 'PatientName'):
                        pname = str(ds.PatientName).replace('^', ' ').strip()
                        
                    if not pname: pname = "Unknown Patient"
                    
                    # ID Priority: XML > DICOM
                    if xml_info and xml_info.get('id'):
                        image_id = xml_info['id']
                    else:
                        if hasattr(ds, 'AccessionNumber') and ds.AccessionNumber:
                            image_id = str(ds.AccessionNumber)
                        elif hasattr(ds, 'PatientID'):
                            image_id = str(ds.PatientID)
                
                    if pname not in patient_map:
                        patient_map[pname] = []
                    
                    # Store tuple: (fpath, ds, image_id)
                    patient_map[pname].append((fpath, ds, image_id))
                    
                except Exception as e:
                    pass
            
            # Update progress
            if total_dirs > 0:
                self.progress.emit(int((i + 1) / total_dirs * 100))
                
        self.finished.emit(patient_map)

class StitcherAlgo:
    @staticmethod
    def match_brightness_contrast(images):
        """
        Adjusts brightness and contrast of all images to match the first image.
        Uses Mean-Variance matching.
        """
        if not images or len(images) < 2:
            return images
            
        # Ensure reference is valid
        ref_img = images[0]
        # Convert to gray if needed for stats calculation
        if len(ref_img.shape) == 3:
            ref_gray = cv2.cvtColor(ref_img, cv2.COLOR_BGR2GRAY)
        else:
            ref_gray = ref_img
            
        ref_mean, ref_std = cv2.meanStdDev(ref_gray)
        ref_mean = ref_mean[0][0]
        ref_std = ref_std[0][0]
        
        # Avoid division by zero or extreme scaling
        if ref_std < 1.0: ref_std = 1.0
        
        matched_images = [images[0]]
        
        for i in range(1, len(images)):
            src_img = images[i]
            if len(src_img.shape) == 3:
                src_gray = cv2.cvtColor(src_img, cv2.COLOR_BGR2GRAY)
            else:
                src_gray = src_img
                
            src_mean, src_std = cv2.meanStdDev(src_gray)
            src_mean = src_mean[0][0]
            src_std = src_std[0][0]
            
            if src_std < 1.0: src_std = 1.0
            
            # Calculate alpha (contrast) and beta (brightness)
            alpha = ref_std / src_std
            beta = ref_mean - src_mean * alpha
            
            # Apply
            # cv2.convertScaleAbs implements: dst = saturate_cast(src * alpha + beta)
            matched = cv2.convertScaleAbs(src_img, alpha=alpha, beta=beta)
            matched_images.append(matched)
            
        return matched_images

    @staticmethod
    def enhance_for_feature_detection(image):
        """
        Enhance image contrast using CLAHE to improve feature detection.
        This image is ONLY used for calculation, not for display.
        """
        # Create a CLAHE object (Contrast Limited Adaptive Histogram Equalization)
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        enhanced_img = clahe.apply(image)
        return enhanced_img

    @staticmethod
    def apply_feathering(image, top=False, bottom=False, feather_height=150):
        """
        Apply alpha blending gradient to top and/or bottom edges of the image.
        Returns a 4-channel BGRA image.
        """
        # Convert to BGRA if it's grayscale
        if len(image.shape) == 2:
            img_bgra = cv2.cvtColor(image, cv2.COLOR_GRAY2BGRA)
        else:
            img_bgra = image.copy()
            if img_bgra.shape[2] == 3:
                img_bgra = cv2.cvtColor(img_bgra, cv2.COLOR_BGR2BGRA)
        
        h, w = img_bgra.shape[:2]
        
        # Clamp feather_height to half image height to avoid overlap issues
        feather_height = min(feather_height, h // 2)
        
        # Create alpha mask (initially all 255)
        alpha_mask = np.ones((h, w), dtype=np.float32) * 255.0
        
        if top and feather_height > 0:
            # 余弦过渡（比线性更平滑，避免过渡处出现可见的亮度台阶）：0 → 255
            t = np.linspace(0.0, 1.0, feather_height).reshape(-1, 1)
            gradient = (0.5 - 0.5 * np.cos(np.pi * t)) * 255.0
            gradient = np.tile(gradient, (1, w))
            alpha_mask[:feather_height, :] = np.minimum(alpha_mask[:feather_height, :], gradient)
            
        if bottom and feather_height > 0:
            # 余弦过渡：255 → 0
            t = np.linspace(0.0, 1.0, feather_height).reshape(-1, 1)
            gradient = (0.5 - 0.5 * np.cos(np.pi * t)) * 255.0
            gradient = 255.0 - gradient
            gradient = np.tile(gradient, (1, w))
            alpha_mask[h-feather_height:, :] = np.minimum(alpha_mask[h-feather_height, :], gradient)
            
        img_bgra[:, :, 3] = alpha_mask.astype(np.uint8)
        return img_bgra

    @staticmethod
    def blend_single_layer(images, positions, band_height=150):
        """把多张按 ``positions`` 摆放的片融合成**一张无缝图**（重叠区单层交叉淡化）。

        与逐片半透明叠加（双重曝光 / 重叠带更亮 / 错位重影）不同：重叠区
        每个像素只由相邻两层按余弦权重合成，权重和恒为 1，因此既不是双层
        叠加、也不会出现亮度台阶。

        返回 ``(out_bgr, (cx0, cy0), slice_masks, slice_offsets)``：
        - ``out_bgr``        : 合成结果（BGR uint8），用于导出；
        - ``(cx0, cy0)``     : 输出图左上角对应的场景坐标；
        - ``slice_masks``    : 长度同 ``images``，每项为该片在**自身像素坐标系**
                              下的余弦权重遮罩（uint8 0~255）。显示端据此给每张片
                              单独加 alpha，既能呈现无缝外观、又让每张片保持**独立
                              可拖动**（拖动后按新位置重算即可，导出仍无缝）；
        - ``slice_offsets``  : 每片在合成画布中的整数偏移 ``(ix0, iy0)``，显示端
                              摆放图元用 ``(cx0 + ix0, cy0 + iy0)``。
        输入不足时返回 ``None``。

        **只按垂直重叠做交叉淡化**（全宽过渡），与本项目既有逐片羽化口径一致；
        脊柱片横向错位通常很小，全宽过渡足够。
        """
        if not images:
            return None
        if len(images) == 1:
            img = images[0]
            if img.ndim == 2:
                img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
            elif img.shape[2] == 4:
                img = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
            if img.dtype != np.uint8:
                img = np.clip(img, 0, 255).astype(np.uint8)
            x0 = int(np.floor(float(positions[0][0]))) if positions else 0
            y0 = int(np.floor(float(positions[0][1]))) if positions else 0
            mask = np.full((img.shape[0], img.shape[1]), 255, dtype=np.uint8)
            return img.copy(), (x0, y0), [mask], [(0, 0)]

        # 统一为 BGR uint8
        norm = []
        for im in images:
            if im.ndim == 2:
                im = cv2.cvtColor(im, cv2.COLOR_GRAY2BGR)
            elif im.shape[2] == 4:
                im = cv2.cvtColor(im, cv2.COLOR_BGRA2BGR)
            if im.dtype != np.uint8:
                im = np.clip(im, 0, 255).astype(np.uint8)
            norm.append(im)
        images = norm

        xs = [float(p[0]) for p in positions]
        ys = [float(p[1]) for p in positions]
        x0 = min(xs)
        y0 = min(ys)
        x1 = max(x + im.shape[1] for x, im in zip(xs, images))
        y1 = max(y + im.shape[0] for y, im in zip(ys, images))
        # 整数画布（留 1px 余量，吸收舍入缝隙）
        cx0 = int(np.floor(x0)) - 1
        cy0 = int(np.floor(y0)) - 1
        cx1 = int(np.ceil(x1)) + 1
        cy1 = int(np.ceil(y1)) + 1
        W = max(1, cx1 - cx0)
        H = max(1, cy1 - cy0)

        acc = None  # (H, W, 3) float32，已合成结果
        slice_alpha = [np.zeros((H, W), dtype=np.float32) for _ in images]
        band = max(1.0, float(band_height))
        for i, im in enumerate(images):
            px = xs[i] - cx0
            py = ys[i] - cy0
            ix0 = int(round(px))
            iy0 = int(round(py))
            ih, iw = im.shape[:2]
            sx = max(0, ix0)
            sy = max(0, iy0)
            ex = min(W, ix0 + iw)
            ey = min(H, iy0 + ih)
            if ex <= sx or ey <= sy:
                continue
            src_x0 = sx - ix0
            src_y0 = sy - iy0
            patch = im[src_y0:src_y0 + (ey - sy), src_x0:src_x0 + (ex - sx)].astype(np.float32)

            if acc is None:
                acc = np.zeros((H, W, 3), dtype=np.float32)
                acc[sy:ey, sx:ex] = patch
                slice_alpha[i][sy:ey, sx:ex] = 1.0
                continue

            # 与上一片(i-1)的重叠 → 余弦交叉淡化权重（当前片在接触边权重趋 0）
            a = np.ones((ey - sy, ex - sx), dtype=np.float32)
            prev = i - 1
            prev_top = int(round(ys[prev] - cy0))
            prev_bot = prev_top + images[prev].shape[0]
            o_top = max(prev_top, iy0)
            o_bot = min(prev_bot, ey)
            if o_bot > o_top:
                r0 = max(0, o_top - sy)
                r1 = o_bot - sy
                yy = np.arange(o_top, o_bot, dtype=np.float32).reshape(-1, 1)
                if py >= ys[prev]:
                    # 当前片在下方：接触边在顶部 → 顶部权重 0
                    t = (yy - o_top) / band
                else:
                    # 当前片在上方：接触边在底部 → 底部权重 0
                    t = (o_bot - yy) / band
                t = np.clip(t, 0.0, 1.0)
                w = 0.5 - 0.5 * np.cos(np.pi * t)  # 0 → 1 余弦
                a[r0:r1, :] = w
            aa = a[..., None]
            region = acc[sy:ey, sx:ex]
            acc[sy:ey, sx:ex] = aa * patch + (1.0 - aa) * region
            slice_alpha[i][sy:ey, sx:ex] = a

        if acc is None:
            return None

        # 从画布空间切出每片自身坐标系的遮罩
        masks = []
        offsets = []
        for i, im in enumerate(images):
            ih, iw = im.shape[:2]
            ix0 = int(round(xs[i] - cx0))
            iy0 = int(round(ys[i] - cy0))
            sub = slice_alpha[i][iy0:iy0 + ih, ix0:ix0 + iw]
            local = np.ones((ih, iw), dtype=np.float32)
            mh, mw = sub.shape[:2]
            if mh > 0 and mw > 0:
                local[:mh, :mw] = sub
            masks.append(np.clip(local * 255, 0, 255).astype(np.uint8))
            offsets.append((ix0, iy0))

        out = np.clip(acc, 0, 255).astype(np.uint8)
        return out, (cx0, cy0), masks, offsets

    @staticmethod
    def calculate_offsets(images):
        """
        Calculates the relative offsets (dx, dy) for a list of images using SIFT feature matching.
        Returns a list of (x, y) coordinates for each image.
        Assumes images are ordered top-to-bottom.
        """
        if not images:
            return []
        
        # Positions: (x, y)
        positions = [(0, 0)]
        
        # Initialize SIFT
        try:
            sift = cv2.SIFT_create()
        except AttributeError:
            # Fallback for older opencv versions
            sift = cv2.xfeatures2d.SIFT_create()
            
        # Matcher
        bf = cv2.BFMatcher()
        
        current_x, current_y = 0, 0
        
        for i in range(len(images) - 1):
            # Use ENHANCED images for feature detection
            img1 = StitcherAlgo.enhance_for_feature_detection(images[i])
            img2 = StitcherAlgo.enhance_for_feature_detection(images[i+1])
            
            # Find keypoints and descriptors
            kp1, des1 = sift.detectAndCompute(img1, None)
            kp2, des2 = sift.detectAndCompute(img2, None)
            
            if des1 is None or des2 is None or len(kp1) < 2 or len(kp2) < 2:
                # Fallback: simple vertical stacking
                print(f"Not enough features between image {i} and {i+1}. Fallback to stacking.")
                current_y += images[i].shape[0] # Use original image height
                positions.append((0, current_y))
                continue
                
            # KNN Match
            matches = bf.knnMatch(des1, des2, k=2)
            
            # Lowe's ratio test
            good = []
            for m, n in matches:
                if m.distance < 0.75 * n.distance:
                    good.append(m)
            
            if len(good) > 4:
                src_pts = np.float32([kp1[m.queryIdx].pt for m in good]).reshape(-1, 1, 2)
                dst_pts = np.float32([kp2[m.trainIdx].pt for m in good]).reshape(-1, 1, 2)
                
                # Estimate translation
                diffs = src_pts - dst_pts
                dx = np.median(diffs[:, 0, 0])
                dy = np.median(diffs[:, 0, 1])
                
                print(f"Match found: {len(good)} points. Shift: dx={dx:.2f}, dy={dy:.2f}")
                
                current_x += dx
                current_y += dy
                positions.append((current_x, current_y))
            else:
                print(f"Not enough good matches ({len(good)}) between image {i} and {i+1}. Fallback to stacking.")
                current_y += images[i].shape[0] # Use original image height
                positions.append((0, current_y))
                
        return positions

class ImageGraphicsItem(QGraphicsPixmapItem):
    def __init__(self, pixmap):
        super().__init__(pixmap)
        self.setFlag(QGraphicsItem.ItemIsMovable)
        self.setFlag(QGraphicsItem.ItemIsSelectable)
        self.setFlag(QGraphicsItem.ItemSendsGeometryChanges)  # 拖动时发位置变化，供上层同步
        self.setOpacity(0.8)  # Default slightly transparent to see overlaps
        self.on_moved = None   # (item, QPointF) -> None，由拼接对话框设置

    def itemChange(self, change, value):
        if change == QGraphicsItem.ItemPositionHasChanged and self.on_moved is not None:
            try:
                self.on_moved(self, value)
            except Exception:                       # noqa: BLE001
                pass
        return super().itemChange(change, value)

class HandleItem(QGraphicsEllipseItem):
    def __init__(self, position, callback):
        r = 50 # Handle radius (Max increased size)
        super().__init__(-r/2, -r/2, r, r)
        self.setPos(position)
        self.setBrush(QBrush(QColor(255, 255, 0, 180))) # More visible yellow
        self.setPen(QPen(Qt.black))
        self.setFlag(QGraphicsItem.ItemIsMovable)
        self.setFlag(QGraphicsItem.ItemSendsGeometryChanges)
        self.setFlag(QGraphicsItem.ItemIsSelectable)
        self.setZValue(200) # Ensure it's on top of everything
        self.callback = callback

    def itemChange(self, change, value):
        if change == QGraphicsItem.ItemPositionHasChanged:
            if self.callback:
                self.callback()
        return super().itemChange(change, value)

class SmartCobbLine:
    def __init__(self, p1, p2, scene, on_change_callback=None):
        self.scene = scene
        # Line item
        self.line_item = QGraphicsLineItem(QLineF(QPointF(*p1), QPointF(*p2)))
        self.line_item.setPen(QPen(Qt.blue, 3, Qt.DashLine))
        self.scene.addItem(self.line_item)
        
        # Handles
        self.h1 = HandleItem(QPointF(*p1), self.update_line_geometry)
        self.h2 = HandleItem(QPointF(*p2), self.update_line_geometry)
        self.scene.addItem(self.h1)
        self.scene.addItem(self.h2)
        
        self.on_change_callback = on_change_callback

    def update_line_geometry(self):
        new_line = QLineF(self.h1.pos(), self.h2.pos())
        self.line_item.setLine(new_line)
        if self.on_change_callback:
            self.on_change_callback()
            
    def line(self):
        return self.line_item.line()
        
    def removeFromScene(self):
        if self.line_item.scene(): self.scene.removeItem(self.line_item)
        if self.h1.scene(): self.scene.removeItem(self.h1)
        if self.h2.scene(): self.scene.removeItem(self.h2)

class CobbAngleTool:
    def __init__(self, scene):
        self.scene = scene
        self.lines = []
        self.text_item = None
        self.current_line = None
        self.active = False
        self.pen = QPen(Qt.red, 3)

    def start_drawing(self, pos):
        if not self.active:
            return
        self.current_line = QGraphicsLineItem(pos.x(), pos.y(), pos.x(), pos.y())
        self.current_line.setPen(self.pen)
        self.scene.addItem(self.current_line)
        self.lines.append(self.current_line)

    def update_drawing(self, pos):
        if self.current_line:
            line = self.current_line.line()
            line.setP2(pos)
            self.current_line.setLine(line)

    def finish_drawing(self):
        self.current_line = None
        if len(self.lines) >= 2:
            self.calculate_cobb_angle()

    def calculate_cobb_angle(self):
        if len(self.lines) < 2:
            return None
        
        # Take the last two lines
        # Support both SmartCobbLine and QGraphicsLineItem
        l1_obj = self.lines[-2]
        l2_obj = self.lines[-1]
        
        l1 = l1_obj.line() if hasattr(l1_obj, 'line') else l1_obj.line()
        l2 = l2_obj.line() if hasattr(l2_obj, 'line') else l2_obj.line()
        
        angle1 = np.arctan2(l1.y2() - l1.y1(), l1.x2() - l1.x1()) * 180 / np.pi
        angle2 = np.arctan2(l2.y2() - l2.y1(), l2.x2() - l2.x1()) * 180 / np.pi
        
        diff = abs(angle1 - angle2)
        if diff > 90:
            diff = 180 - diff
            
        print(f"Measured Angle: {diff:.2f} degrees")
        
        # Add or update text item
        if self.text_item is None or self.text_item.scene() != self.scene:
            self.text_item = self.scene.addText("")
            self.text_item.setDefaultTextColor(Qt.red)
            self.text_item.setScale(3)
            self.text_item.setZValue(100)
            
        self.text_item.setPlainText(f"Cobb Angle: {diff:.1f}°")
        
        # Position text at the center of the second line
        mid_x = (l2.x1() + l2.x2()) / 2
        mid_y = (l2.y1() + l2.y2()) / 2
        self.text_item.setPos(mid_x, mid_y)
        
        self.active = False
        
        return diff

    def clear(self):
        # Helper to clear smart lines properly
        for l in self.lines:
            if hasattr(l, 'removeFromScene'):
                l.removeFromScene()
        self.lines = []
        self.text_item = None

class AutoCobbAlgo:
    @staticmethod
    def calculate_spine_centerline(image):
        """
        Detects the spine centerline using image processing techniques.
        Returns a list of (x, y) points representing the centerline, sorted by y.
        """
        # 1. Enhancement
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        if len(image.shape) == 3:
            gray = cv2.cvtColor(image, cv2.COLOR_BGRA2GRAY)
        else:
            gray = image
        enhanced = clahe.apply(gray)
        
        # 2. Thresholding / Edge Detection to find spine region
        # Spine is usually central and brighter (bone)
        # Apply Gaussian Blur
        blurred = cv2.GaussianBlur(enhanced, (5, 5), 0)
        
        # Adaptive Thresholding to find bone structures
        thresh = cv2.adaptiveThreshold(blurred, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, 
                                      cv2.THRESH_BINARY, 11, 2)
        
        # Morphological operations to clean up noise
        kernel = np.ones((3,3), np.uint8)
        opening = cv2.morphologyEx(thresh, cv2.MORPH_OPEN, kernel, iterations=2)
        dilated = cv2.dilate(opening, kernel, iterations=3)
        
        # 3. Find Contours
        contours, _ = cv2.findContours(dilated, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        
        # Filter contours - keep largest vertical structures
        spine_contours = []
        img_h, img_w = image.shape[:2]
        center_x = img_w // 2
        
        for cnt in contours:
            x, y, w, h = cv2.boundingRect(cnt)
            # Conditions:
            # 1. Should be reasonably tall
            # 2. Should be somewhat central (rough assumption for AP view)
            if h > img_h * 0.05 and abs(x + w/2 - center_x) < img_w * 0.4:
                spine_contours.append(cnt)
        
        if not spine_contours:
            return []
            
        # Combine valid contours to form a rough spine mask
        mask = np.zeros_like(gray)
        cv2.drawContours(mask, spine_contours, -1, 255, -1)
        
        # 4. Extract centerline from mask
        # We can scan row by row and find the center of white pixels
        centerline_points = []
        # Step size for scanning
        step = 10 
        for r in range(0, img_h, step):
            row_pixels = np.where(mask[r, :] > 0)[0]
            if len(row_pixels) > 0:
                cx = int(np.mean(row_pixels))
                # Outlier removal: if cx jumps too much from previous, ignore
                if centerline_points:
                    prev_cx = centerline_points[-1][0]
                    if abs(cx - prev_cx) > img_w * 0.1: # Allow some curve but not teleportation
                        continue 
                centerline_points.append((cx, r))
                
        # 5. Smoothing the centerline
        if len(centerline_points) > 10:
            pts = np.array(centerline_points)
            # Sort by Y just in case
            pts = pts[pts[:, 1].argsort()]
            
            # Fit a 5th degree polynomial (S-shape curve common in scoliosis)
            # x = f(y)
            try:
                z = np.polyfit(pts[:, 1], pts[:, 0], 5)
                p = np.poly1d(z)
                
                # Generate smooth points
                smooth_points = []
                for y in range(0, img_h, 5): # finer step for smoothness
                    x = int(p(y))
                    if 0 <= x < img_w:
                        smooth_points.append((x, y))
                return smooth_points
            except np.linalg.LinAlgError:
                return centerline_points # Fallback
            
        return centerline_points

    @staticmethod
    def estimate_cobb_angle_points(centerline_points, img_shape):
        """
        Estimates the inflection points and peaks of the curve to suggest Cobb angle lines.
        Returns two pairs of points [(p1_start, p1_end), (p2_start, p2_end)] for the lines.
        """
        if len(centerline_points) < 10:
            return None
            
        # Analyze the derivative (slope) of the curve to find max tilts
        pts = np.array(centerline_points)
        ys = pts[:, 1]
        xs = pts[:, 0]
        
        # Calculate local tangent angles
        # We use a window to calculate slope to be more robust
        window = 5
        angles = []
        indices = []
        
        for i in range(window, len(pts) - window):
            p_prev = pts[i - window]
            p_next = pts[i + window]
            dy = p_next[1] - p_prev[1]
            dx = p_next[0] - p_prev[0]
            
            if dy == 0: continue # Should not happen for vertical spine scan
            
            # Angle of the spine tangent relative to vertical (Y-axis)
            # dx/dy is the slope relative to vertical
            # arctan2(dx, dy) gives angle from Y-axis (0 is down)
            angle = np.arctan2(dx, dy) 
            angles.append(angle)
            indices.append(i)
            
        if not angles:
            return None
            
        angles = np.array(angles)
        
        # Strategy: Find the two points with the maximum difference in tangent angle.
        # This corresponds to the vertebrae tilted most to Left and most to Right.
        
        # We need to ensure these points are somewhat separated to avoid local noise
        # But for global max/min, they naturally should be separated in an S-curve.
        
        min_angle_idx = np.argmin(angles)
        max_angle_idx = np.argmax(angles)
        
        idx1 = indices[min_angle_idx]
        idx2 = indices[max_angle_idx]
        
        # If the spine is perfectly straight, angles are all ~0. 
        # In that case, we just pick top and bottom.
        angle_diff = abs(angles[max_angle_idx] - angles[min_angle_idx]) * 180 / np.pi
        
        if angle_diff < 5.0: # Threshold for "Straight Spine"
            # Fallback to 20% and 80% if no significant curve detected
            idx1 = int(len(centerline_points) * 0.2)
            idx2 = int(len(centerline_points) * 0.8)
        
        # Ensure idx1 is the upper one (smaller y) for consistent ordering
        if pts[idx1][1] > pts[idx2][1]:
            idx1, idx2 = idx2, idx1
            
        # Helper to get normal line
        def get_normal_line_at_idx(idx, points, length=100):
            p = points[idx]
            # Recalculate local tangent for this specific point
            prev_idx = max(0, idx - 5)
            next_idx = min(len(points)-1, idx + 5)
            
            dy = points[next_idx][1] - points[prev_idx][1]
            dx = points[next_idx][0] - points[prev_idx][0]
            
            if dx == 0: 
                normal_angle = 0 # Horizontal line
            else:
                tangent_angle = np.arctan2(dy, dx)
                # Normal is perpendicular to tangent
                normal_angle = tangent_angle + np.pi/2
            
            x, y = p
            x1 = x - length * np.cos(normal_angle)
            y1 = y - length * np.sin(normal_angle)
            x2 = x + length * np.cos(normal_angle)
            y2 = y + length * np.sin(normal_angle)
            
            return (int(x1), int(y1)), (int(x2), int(y2))

        line1 = get_normal_line_at_idx(idx1, centerline_points, img_shape[1]//4)
        line2 = get_normal_line_at_idx(idx2, centerline_points, img_shape[1]//4)
        
        return [line1, line2]

class RecordViewerDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("拼接记录查看 (Stitching Records)")
        self.resize(800, 500)
        self.layout = QVBoxLayout(self)
        
        self.table = QTableWidget()
        self.table.setColumnCount(4)
        self.table.setHorizontalHeaderLabels(["日期 (Date)", "姓名 (Name)", "影像号 (Image ID)", "Cobb角 (Angle)"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.layout.addWidget(self.table)
        
        self.load_records()
        
    def load_records(self):
        # Default path in app dir or exe dir
        if getattr(sys, 'frozen', False):
            application_path = os.path.dirname(sys.executable)
        else:
            application_path = os.path.dirname(os.path.abspath(__file__))
            
        json_path = os.path.join(application_path, "stitch_records.json")
        
        if not os.path.exists(json_path):
            return

        try:
            with open(json_path, 'r', encoding='utf-8') as f:
                records = json.load(f)
                
            self.table.setRowCount(len(records))
            for i, record in enumerate(reversed(records)): # Show newest first
                self.table.setItem(i, 0, QTableWidgetItem(record.get('date', '')))
                self.table.setItem(i, 1, QTableWidgetItem(record.get('name', '')))
                self.table.setItem(i, 2, QTableWidgetItem(record.get('image_id', '')))
                
                angle = record.get('cobb_angle')
                angle_str = f"{angle:.1f}°" if angle is not None else "-"
                self.table.setItem(i, 3, QTableWidgetItem(angle_str))
        except Exception as e:
            QMessageBox.warning(self, "错误", f"无法读取记录文件: {e}")

class StitchingGraphicsView(QGraphicsView):
    def __init__(self, scene, parent=None):
        super().__init__(scene, parent)
        self.setDragMode(QGraphicsView.ScrollHandDrag)
        self._item_drag = False   # 左键命中可移动图元时为 True，松开恢复平移
        self.setRenderHint(QPainter.Antialiasing)
        self.setRenderHint(QPainter.SmoothPixmapTransform)
        self.setFrameShape(QFrame.NoFrame)
        
        # Brightness Control
        self.brightness_val = 0 # -255 to 255
        self.dragging_brightness = False
        self.last_mouse_y = 0
        
        # Overlay Item
        self.brightness_item = QGraphicsRectItem()
        self.brightness_item.setZValue(9999) # Always on top
        self.brightness_item.setPen(QPen(Qt.NoPen))
        self.brightness_item.setBrush(Qt.black)
        self.brightness_item.setOpacity(0)
        self.brightness_item.setVisible(False)
        # Note: We don't add it yet, we add it when needed or reset
        
    def _create_overlay_item(self):
        self.brightness_item = QGraphicsRectItem()
        self.brightness_item.setZValue(9999) # Always on top
        self.brightness_item.setPen(QPen(Qt.NoPen))
        self.brightness_item.setBrush(Qt.black)
        self.brightness_item.setOpacity(0)
        self.brightness_item.setVisible(False)
        
    def reset_brightness(self):
        self.brightness_val = 0
        self.update_brightness_overlay()
        
    def update_brightness_overlay(self):
        # Ensure item is valid (handle case where scene.clear() deleted it)
        try:
            # Try to access a property to check validity
            _ = self.brightness_item.scene()
        except RuntimeError:
            # Object deleted, recreate it
            self._create_overlay_item()
            
        # Ensure item is in scene
        if self.scene() and self.brightness_item.scene() != self.scene():
            self.scene().addItem(self.brightness_item)
            
        if self.brightness_val == 0:
            self.brightness_item.setVisible(False)
            return
            
        self.brightness_item.setVisible(True)
        
        if self.brightness_val > 0:
            color = Qt.white
            opacity = self.brightness_val / 255.0
        else:
            color = Qt.black
            opacity = abs(self.brightness_val) / 255.0
            
        self.brightness_item.setBrush(color)
        self.brightness_item.setOpacity(opacity)
        
        # Update Rect to cover all items (except itself)
        # Temporarily hide to get correct bounding rect of other items
        self.brightness_item.setVisible(False)
        rect = self.scene().itemsBoundingRect()
        self.brightness_item.setVisible(True)
        
        # If rect is empty (no images), use scene rect or view rect
        if rect.isEmpty():
            rect = self.scene().sceneRect()
            
        self.brightness_item.setRect(rect)

    def mousePressEvent(self, event):
        if event.button() == Qt.RightButton:
            self.dragging_brightness = True
            self.last_mouse_y = event.y()
            self.setCursor(Qt.SizeVerCursor)
            event.accept()
            return
        if event.button() == Qt.LeftButton:
            # 命中可移动图元（如某张拼接片）→ 切到 NoDrag 让其可被拖动微调；
            # 命中空白/亮度层 → 保持当前模式（通常 ScrollHandDrag 平移画布）。
            # 手动 Cobb 测量期间事件被对话框 eventFilter 拦截、不会走到这里。
            item = self.itemAt(event.pos())
            if (item is not None and item is not self.brightness_item
                    and (item.flags() & QGraphicsItem.ItemIsMovable)):
                self.setDragMode(QGraphicsView.NoDrag)
                self._item_drag = True
        super().mousePressEvent(event)
            
    def mouseMoveEvent(self, event):
        if self.dragging_brightness:
            dy = self.last_mouse_y - event.y() # Up = Positive
            self.last_mouse_y = event.y()
            
            self.brightness_val += dy * 2
            self.brightness_val = max(-255, min(255, self.brightness_val))
            
            self.update_brightness_overlay()
            event.accept()
        else:
            super().mouseMoveEvent(event)
            
    def mouseReleaseEvent(self, event):
        if event.button() == Qt.RightButton:
            self.dragging_brightness = False
            self.setCursor(Qt.ArrowCursor)
            event.accept()
            return
        if event.button() == Qt.LeftButton and getattr(self, "_item_drag", False):
            self._item_drag = False
            self.setDragMode(QGraphicsView.ScrollHandDrag)
        super().mouseReleaseEvent(event)

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("全脊柱拼接与Cobb角测量软件 (Full Spine Stitching & Cobb Angle)")
        self.resize(1200, 800)
        
        icon_path = resource_path('spine_icon.png')
        self.setWindowIcon(QIcon(icon_path))
        self.apply_dark_theme()
        
        # Menu Bar
        menubar = self.menuBar()
        
        # Tools Menu
        tools_menu = menubar.addMenu("工具 (Tools)")
        view_records_action = QAction("查看拼接记录 (View Records)", self)
        view_records_action.triggered.connect(self.show_records)
        tools_menu.addAction(view_records_action)
        
        # Help Menu
        help_menu = menubar.addMenu("帮助 (Help)")
        
        about_action = QAction("关于 (About)", self)
        about_action.triggered.connect(self.show_about)
        help_menu.addAction(about_action)

        # Central Widget
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QHBoxLayout(central_widget)
        main_layout.setContentsMargins(10, 10, 10, 10)
        main_layout.setSpacing(10)

        # Left Panel - File List
        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(0, 0, 0, 0)
        
        # File Operations Group
        file_group = QGroupBox("文件操作")
        file_group_layout = QVBoxLayout()
        
        btn_layout = QHBoxLayout()
        load_btn = QPushButton("刷新")
        load_btn.clicked.connect(self.load_data_files)
        open_file_btn = QPushButton("打开文件")
        open_file_btn.clicked.connect(self.open_dicom_file)
        open_dir_btn = QPushButton("打开文件夹")
        open_dir_btn.clicked.connect(self.open_dicom_dir)
        
        btn_layout.addWidget(load_btn)
        btn_layout.addWidget(open_file_btn)
        btn_layout.addWidget(open_dir_btn)
        
        file_group_layout.addLayout(btn_layout)
        file_group.setLayout(file_group_layout)
        left_layout.addWidget(file_group)

        # List Widget / Tree Widget
        self.file_tree = QTreeWidget()
        self.file_tree.setHeaderLabel("患者列表 (Patient List)")
        self.file_tree.setIconSize(QSize(80, 80))
        self.file_tree.setSelectionMode(QTreeWidget.ExtendedSelection)
        self.file_tree.setFrameShape(QFrame.NoFrame)
        self.file_tree.setAlternatingRowColors(True)
        
        list_group = QGroupBox("患者DICOM列表 (按住Ctrl多选)")
        list_group_layout = QVBoxLayout()
        list_group_layout.addWidget(self.file_tree)
        list_group.setLayout(list_group_layout)
        
        left_layout.addWidget(list_group)
        
        # Right Panel - Workspace
        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(0, 0, 0, 0)
        
        # Toolbar Area (Top Controls)
        controls_layout = QHBoxLayout()
        
        # 1. Stitching Controls
        stitch_group = QGroupBox("拼接与测量")
        stitch_layout = QHBoxLayout()
        
        stitch_btn = QPushButton("一键拼接")
        stitch_btn.clicked.connect(self.stitch_images)
        stitch_btn.setMinimumHeight(30)
        
        auto_measure_btn = QPushButton("测量Cobb角")
        auto_measure_btn.clicked.connect(self.auto_measure_cobb)
        auto_measure_btn.setMinimumHeight(30)
        
        save_measure_btn = QPushButton("保存测量")
        save_measure_btn.clicked.connect(self.save_measurement)
        save_measure_btn.setMinimumHeight(30)
        
        clear_btn = QPushButton("清空画布")
        clear_btn.clicked.connect(self.clear_scene)
        clear_btn.setMinimumHeight(30)
        
        stitch_layout.addWidget(stitch_btn)
        stitch_layout.addWidget(auto_measure_btn)
        stitch_layout.addWidget(save_measure_btn)
        stitch_layout.addWidget(clear_btn)
        stitch_group.setLayout(stitch_layout)
        
        # 2. Display Settings
        display_group = QGroupBox("显示设置")
        display_layout = QHBoxLayout()
        
        self.feather_cb = QCheckBox("平滑融合")
        self.feather_cb.setChecked(True)
        self.feather_cb.stateChanged.connect(self.toggle_feathering)
        
        display_layout.addWidget(self.feather_cb)
        
        display_layout.addWidget(QLabel("平滑度:"))
        self.feather_slider = QSlider(Qt.Horizontal)
        self.feather_slider.setRange(0, 300)
        self.feather_slider.setValue(150)
        self.feather_slider.setFixedWidth(80)
        self.feather_slider.valueChanged.connect(self.change_feather_amount)
        display_layout.addWidget(self.feather_slider)
        
        display_layout.addWidget(QLabel("透明度:"))
        self.opacity_slider = QSlider(Qt.Horizontal)
        self.opacity_slider.setRange(0, 100)
        self.opacity_slider.setValue(80)
        self.opacity_slider.setFixedWidth(80)
        self.opacity_slider.valueChanged.connect(self.change_opacity)
        display_layout.addWidget(self.opacity_slider)
        
        display_layout.addWidget(QLabel(" | ")) # Separator
        
        zoom_in_btn = QPushButton("+")
        zoom_in_btn.setFixedWidth(30)
        zoom_in_btn.clicked.connect(lambda: self.scale_view(1.1))
        
        zoom_out_btn = QPushButton("-")
        zoom_out_btn.setFixedWidth(30)
        zoom_out_btn.clicked.connect(lambda: self.scale_view(0.9))
        
        display_layout.addWidget(QLabel("缩放:"))
        display_layout.addWidget(zoom_in_btn)
        display_layout.addWidget(zoom_out_btn)
        
        display_group.setLayout(display_layout)
        
        # 3. Export
        export_group = QGroupBox("导出")
        export_layout = QHBoxLayout()
        save_btn = QPushButton("保存图片")
        save_btn.clicked.connect(self.save_result)
        save_dicom_btn = QPushButton("保存DICOM")
        save_dicom_btn.clicked.connect(self.save_result_as_dicom)
        
        export_layout.addWidget(save_btn)
        export_layout.addWidget(save_dicom_btn)
        export_group.setLayout(export_layout)
        
        controls_layout.addWidget(stitch_group)
        controls_layout.addWidget(display_group)
        controls_layout.addWidget(export_group)
        controls_layout.addStretch()
        
        right_layout.addLayout(controls_layout)

        # Graphics View
        self.scene = QGraphicsScene()
        self.scene.setBackgroundBrush(QBrush(QColor(30, 30, 30))) # Dark background for scene
        self.scene.selectionChanged.connect(self.on_selection_changed)
        self.view = StitchingGraphicsView(self.scene)
        # DragMode is set in class init
        # self.view.setDragMode(QGraphicsView.ScrollHandDrag)
        self.view.setRenderHint(QPainter.Antialiasing)
        self.view.setRenderHint(QPainter.SmoothPixmapTransform)
        self.view.setFrameShape(QFrame.NoFrame)
        right_layout.addWidget(self.view)

        # Splitter
        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(left_panel)
        splitter.addWidget(right_panel)
        splitter.setStretchFactor(1, 4)
        splitter.setHandleWidth(2)
        
        main_layout.addWidget(splitter)
        
        # Status Bar with Progress
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setVisible(False)
        self.progress_bar.setFixedWidth(200)
        self.status_bar.addPermanentWidget(self.progress_bar)

        # Tools
        self.cobb_tool = CobbAngleTool(self.scene)
        self.view.viewport().installEventFilter(self)

        # Data
        self.dicom_files = []
        self.load_data_files() # Now async

    def show_about(self):
        QMessageBox.about(self, "关于软件", 
                          "全脊柱拼接与Cobb角测量软件 v1.0\n\n"
                          "功能：\n"
                          "- DICOM图像读取与预览\n"
                          "- 智能全脊柱图像拼接\n"
                          "- 自动/手动 Cobb 角测量\n"
                          "- 测量结果分级判定\n\n"
                          "作者: wmw")

    def apply_dark_theme(self):
        dark_qss = """
        QMainWindow {
            background-color: #2b2b2b;
        }
        QWidget {
            color: #e0e0e0;
            background-color: #2b2b2b;
            font-family: "Segoe UI", Arial, sans-serif;
            font-size: 14px;
        }
        QGroupBox {
            border: 1px solid #555;
            border-radius: 5px;
            margin-top: 10px;
            font-weight: bold;
            color: #aaa;
        }
        QGroupBox::title {
            subcontrol-origin: margin;
            subcontrol-position: top left;
            padding: 0 3px;
            left: 10px;
        }
        QPushButton {
            background-color: #3c3c3c;
            border: 1px solid #555;
            border-radius: 4px;
            padding: 5px 10px;
            color: #fff;
        }
        QPushButton:hover {
            background-color: #505050;
        }
        QPushButton:pressed {
            background-color: #2a2a2a;
        }
        QTreeWidget {
            background-color: #000000;
            alternate-background-color: #1a1a1a;
            border: 1px solid #444;
            border-radius: 4px;
            color: #ffffff;
        }
        QTreeWidget::item {
            padding: 5px;
            color: #ffffff;
        }
        QTreeWidget::item:selected {
            background-color: #007acc;
            color: #ffffff;
        }
        QProgressBar {
            border: 1px solid #444;
            border-radius: 5px;
            text-align: center;
        }
        QProgressBar::chunk {
            background-color: #007acc;
            width: 20px;
        }
        QSlider::groove:horizontal {
            border: 1px solid #444;
            height: 6px;
            background: #333;
            margin: 2px 0;
            border-radius: 3px;
        }
        QSlider::handle:horizontal {
            background: #007acc;
            border: 1px solid #007acc;
            width: 14px;
            height: 14px;
            margin: -5px 0;
            border-radius: 7px;
        }
        QCheckBox {
            spacing: 5px;
        }
        QCheckBox::indicator {
            width: 16px;
            height: 16px;
        }
        QSplitter::handle {
            background-color: #444;
        }
        """
        self.setStyleSheet(dark_qss)

    def add_dicom_item(self, fpath):
        # Deprecated: Logic moved to load_data_files with grouping
        pass

    def load_data_files(self):
        self.file_tree.clear()
        self.dicom_files = []
        
        # Search in default directory 'D:\ClearTmp\DicomTmp' first, then 'data'
        default_dir = r"D:\ClearTmp\DicomTmp"
        if os.path.exists(default_dir):
            base_dir = default_dir
        else:
            base_dir = os.path.join(os.path.dirname(__file__), 'data')
            if not os.path.exists(base_dir):
                os.makedirs(base_dir)
            
        self.start_dicom_loading(base_dir)

    def start_dicom_loading(self, directory):
        self.progress_bar.setVisible(True)
        self.progress_bar.setValue(0)
        self.status_bar.showMessage(f"正在扫描DICOM文件: {directory} ...")
        
        self.loader_thread = DicomLoaderThread(directory)
        self.loader_thread.progress.connect(self.update_progress)
        self.loader_thread.finished.connect(self.on_loading_finished)
        self.loader_thread.start()
        
    def update_progress(self, value):
        self.progress_bar.setValue(value)
        
    def on_loading_finished(self, patient_map):
        self.progress_bar.setVisible(False)
        self.status_bar.showMessage("扫描完成", 3000)
        self.populate_tree(patient_map)

    def open_dicom_file(self):
        fnames, _ = QFileDialog.getOpenFileNames(self, "打开DICOM文件", "", "DICOM Files (*.dcm *.DCM);;All Files (*)")
        if fnames:
             # We treat manually opened files as a special "Manual Import" group
             manual_group = {}
             for fname in fnames:
                 ds = pydicom.dcmread(fname, stop_before_pixels=True)
                 pname = "Manual Import"
                 if hasattr(ds, 'PatientName'):
                     pname = str(ds.PatientName).replace('^', ' ').strip()
                 
                 # Try to get Image ID
                 image_id = "Unknown"
                 if hasattr(ds, 'AccessionNumber') and ds.AccessionNumber:
                    image_id = str(ds.AccessionNumber)
                 elif hasattr(ds, 'PatientID'):
                    image_id = str(ds.PatientID)
                 
                 if pname not in manual_group:
                     manual_group[pname] = []
                 manual_group[pname].append((fname, ds, image_id))
             
             self.populate_tree(manual_group, append=True)

    def open_dicom_dir(self):
        dirname = QFileDialog.getExistingDirectory(self, "打开文件夹", "")
        if dirname:
            self.start_dicom_loading(dirname) # Use thread for this too

    def add_dicom_files_from_dir(self, directory, append=False):
        # Deprecated: Logic moved to thread
        pass

    def populate_tree(self, patient_map, append=False):
        # Populate Tree
        for pname, items in patient_map.items():
            # Check if patient item already exists if appending
            parent_item = None
            if append:
                # Naive search
                iterator = QTreeWidgetItemIterator(self.file_tree)
                while iterator.value():
                    item = iterator.value()
                    if item.parent() is None and item.text(0) == pname:
                        parent_item = item
                        break
                    iterator += 1
            
            if not parent_item:
                parent_item = QTreeWidgetItem(self.file_tree)
                parent_item.setText(0, f"{pname} ({len(items)})")
                parent_item.setExpanded(True) # Expand by default
            
            # Sort items by Instance Number if possible, else filename
            def sort_key(x):
                f, d, _ = x
                try:
                    return int(d.InstanceNumber)
                except:
                    return f
            
            items.sort(key=sort_key)
            
            for fpath, ds, image_id in items:
                child = QTreeWidgetItem(parent_item)
                child.setText(0, os.path.basename(fpath))
                child.setData(0, Qt.UserRole, fpath)
                child.setData(0, Qt.UserRole + 1, image_id) # Store Image ID
                
                # Generate thumbnail (might be slow for many files, but necessary)
                img_array = DicomHelper.load_dicom_as_image(fpath)
                if img_array is not None:
                    pixmap = DicomHelper.numpy_to_qpixmap(img_array)
                    # Scale for thumbnail
                    pixmap = pixmap.scaled(80, 80, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                    icon = QIcon(pixmap)
                    child.setIcon(0, icon)

    def stitch_images(self):
        selected_items = self.file_tree.selectedItems()
        if not selected_items:
            QMessageBox.warning(self, "提示", "请先选择要拼接的DICOM图像")
            return

        self.current_stitch_images = []
        
        # Filter out top-level patient items, only take image items
        valid_items = [item for item in selected_items if item.parent() is not None]
        
        # If user selected a patient (parent), maybe they want all images?
        # Let's check: if only parents are selected, select all their children
        if not valid_items:
            # Check parents
            for item in selected_items:
                if item.parent() is None:
                    for i in range(item.childCount()):
                        valid_items.append(item.child(i))
        
        # Sort by visual order (Tree order) to respect sequence?
        # Actually, selectedItems() order is undefined. We should sort them by their index in the tree.
        # But since they might be from different patients (though unlikely for stitching), let's sort by visual position.
        # Simple way: Collect all valid items, sort by parent index then child index?
        # Or just trust the user selection order if we could get it? No, standard Qt doesn't preserve selection order easily.
        # Let's rely on the structure:
        # We need to sort valid_items based on their order in the tree.
        # How?
        # We can traverse the tree and pick if in valid_items.
        
        sorted_valid_items = []
        iterator = QTreeWidgetItemIterator(self.file_tree)
        while iterator.value():
            item = iterator.value()
            if item in valid_items:
                sorted_valid_items.append(item)
            iterator += 1
            
        valid_items = sorted_valid_items

        if not valid_items:
             QMessageBox.warning(self, "提示", "请选择有效的图像文件 (而非仅患者名称)")
             return

        for item in valid_items:
            fpath = item.data(0, Qt.UserRole)
            if fpath:
                img = DicomHelper.load_dicom_as_image(fpath)
                if img is not None:
                    self.current_stitch_images.append(img)
        
        if not self.current_stitch_images:
            return

        # Match Brightness/Contrast to the first image
        self.current_stitch_images = StitcherAlgo.match_brightness_contrast(self.current_stitch_images)

        # Reset View Brightness
        if hasattr(self.view, 'reset_brightness'):
            self.view.brightness_val = 0 # Reset value, overlay updated in refresh

        self.scene.clear()
        self.cobb_tool.lines = []
        
        # Calculate positions using StitcherAlgo
        self.current_positions = StitcherAlgo.calculate_offsets(self.current_stitch_images)
        
        self.refresh_scene_items()
            
        # Adjust scene rect to fit all items
        self.scene.setSceneRect(self.scene.itemsBoundingRect())
        self.view.fitInView(self.scene.sceneRect(), Qt.KeepAspectRatio)

    def toggle_feathering(self):
        if hasattr(self, 'current_stitch_images') and self.current_stitch_images:
             self.refresh_scene_items()
             
    def change_feather_amount(self):
        if hasattr(self, 'current_stitch_images') and self.current_stitch_images:
             self.refresh_scene_items()

    def refresh_scene_items(self):
        self.scene.clear()
        use_feathering = self.feather_cb.isChecked()
        feather_amount = self.feather_slider.value()
        
        images = self.current_stitch_images
        positions = self.current_positions
        
        for i, img in enumerate(images):
            # Apply feathering if enabled
            if use_feathering:
                feather_top = (i > 0)
                feather_bottom = (i < len(images) - 1)
                display_img = StitcherAlgo.apply_feathering(img, top=feather_top, bottom=feather_bottom, feather_height=feather_amount)
            else:
                display_img = img

            pixmap = DicomHelper.numpy_to_qpixmap(display_img)
            item = ImageGraphicsItem(pixmap)
            
            # Restore opacity from slider (default 0.8 or current slider value)
            current_opacity = self.opacity_slider.value() / 100.0
            item.setOpacity(current_opacity)
            
            if i < len(positions):
                 x, y = positions[i]
                 item.setPos(x, y)
            self.scene.addItem(item)
            
        # Restore brightness overlay
        if hasattr(self.view, 'update_brightness_overlay'):
            self.view.update_brightness_overlay()

    def change_opacity(self, value):
        for item in self.scene.selectedItems():
            if isinstance(item, ImageGraphicsItem):
                item.setOpacity(value / 100.0)

    def on_selection_changed(self):
        try:
            # Check if scene still exists and hasn't been deleted C++ side
            if not self.scene:
                return
            items = self.scene.selectedItems()
            if items and isinstance(items[0], ImageGraphicsItem):
                self.opacity_slider.setValue(int(items[0].opacity() * 100))
        except RuntimeError:
            pass # Scene might be deleted

    def scale_view(self, scale_factor):
        self.view.scale(scale_factor, scale_factor)

    def save_record_to_json(self, record):
        try:
             # Path in app dir or exe dir
            if getattr(sys, 'frozen', False):
                application_path = os.path.dirname(sys.executable)
            else:
                application_path = os.path.dirname(os.path.abspath(__file__))
                
            json_path = os.path.join(application_path, "stitch_records.json")
                 
            existing_records = []
            if os.path.exists(json_path):
                try:
                    with open(json_path, 'r', encoding='utf-8') as f:
                        existing_records = json.load(f)
                except:
                    pass
            
            existing_records.append(record)
            
            with open(json_path, 'w', encoding='utf-8') as f:
                json.dump(existing_records, f, ensure_ascii=False, indent=4)
        except Exception as e:
            print(f"Error saving record: {e}")

    def get_current_patient_info(self):
        patient_name = "Unknown"
        image_id = "Unknown"
        
        if hasattr(self, 'current_stitch_images') and self.current_stitch_images:
            selected_items = self.file_tree.selectedItems()
            # Find first valid child item
            first_item = None
            for item in selected_items:
                if item.parent() is not None:
                    first_item = item
                    break
            
            if not first_item and selected_items:
                 # Check if parent selected
                 parent = selected_items[0]
                 if parent.childCount() > 0:
                     first_item = parent.child(0)

            if first_item:
                first_path = first_item.data(0, Qt.UserRole)
                patient_name = DicomHelper.get_patient_name(first_path)
                
                # Retrieve Image ID from item data
                stored_id = first_item.data(0, Qt.UserRole + 1)
                if stored_id:
                    image_id = stored_id
                else:
                    # Fallback to reading file if not stored (e.g. legacy)
                    try:
                        ds = pydicom.dcmread(first_path, stop_before_pixels=True)
                        if hasattr(ds, 'AccessionNumber') and ds.AccessionNumber:
                            image_id = str(ds.AccessionNumber)
                        elif hasattr(ds, 'PatientID'):
                            image_id = str(ds.PatientID)
                    except:
                        pass
        return patient_name, image_id

    def save_result(self):
        # Create a QImage of the scene
        self.scene.clearSelection() # Deselect to avoid saving selection border
        rect = self.scene.itemsBoundingRect()
        image = QImage(rect.size().toSize(), QImage.Format_RGB32)
        image.fill(Qt.transparent)
        
        painter = QPainter(image)
        self.scene.render(painter, target=QRectF(image.rect()), source=rect)
        painter.end()
        
        # Determine default filename from first loaded DICOM
        default_name = "Stitched_Spine"
        patient_name, image_id = self.get_current_patient_info()
        if patient_name != "Unknown":
            default_name = patient_name
        
        desktop_path = os.path.join(os.path.expanduser("~"), "Desktop")
        default_path = os.path.join(desktop_path, f"{default_name}.png")
        
        fname, _ = QFileDialog.getSaveFileName(self, "保存拼接图像", default_path, "PNG Images (*.png);;JPEG Images (*.jpg);;BMP Images (*.bmp)")
        if fname:
            image.save(fname)
            
            # Save record to JSON
            record = {
                "name": patient_name,
                "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "cobb_angle": None, # Image save only, no specific angle measured or guaranteed
                "image_id": image_id
            }
            # If Cobb tool has an angle, maybe we should save it?
            # User requirement: "保存图片的时候也记录到json"
            # It implies logging the event of saving an image.
            # If there is a calculated angle on screen, it might be good to include it, but let's stick to simple logging first.
            # Actually, if the tool is active or has lines, we could try to get the angle.
            # But let's just log "None" for angle to distinguish from explicit measurement save.
            # OR, if the user wants to see the angle in the record, they should have clicked "Save Measurement".
            # Let's keep it simple: Image save -> record existence of image.
            
            self.save_record_to_json(record)
            
            QMessageBox.information(self, "成功", "图像已保存")

    def save_result_as_dicom(self):
        self.scene.clearSelection()
        rect = self.scene.itemsBoundingRect()
        
        # Render scene to QImage (Grayscale preferred for DICOM, but RGB is easier to capture from scene)
        image = QImage(rect.size().toSize(), QImage.Format_RGB32)
        image.fill(Qt.black) # DICOM background usually black
        
        painter = QPainter(image)
        self.scene.render(painter, target=QRectF(image.rect()), source=rect)
        painter.end()
        
        # Convert QImage to numpy array
        width = image.width()
        height = image.height()
        ptr = image.bits()
        ptr.setsize(image.byteCount())
        arr = np.array(ptr).reshape(height, width, 4)  # RGBA
        
        # Convert to grayscale (DICOM X-Ray is usually monochrome)
        gray_arr = cv2.cvtColor(arr, cv2.COLOR_BGRA2GRAY)
        
        # Determine default filename
        default_name = "Stitched_Spine"
        patient_name, _ = self.get_current_patient_info()
        if patient_name != "Unknown":
            default_name = patient_name
        
        desktop_path = os.path.join(os.path.expanduser("~"), "Desktop")
        default_path = os.path.join(desktop_path, f"{default_name}.dcm")
        
        fname, _ = QFileDialog.getSaveFileName(self, "保存DICOM", default_path, "DICOM Files (*.dcm)")
        if not fname:
            return

        try:
            # Create a new DICOM dataset
            file_meta = pydicom.dataset.FileMetaDataset()
            file_meta.MediaStorageSOPClassUID = '1.2.840.10008.5.1.4.1.1.7' # Secondary Capture Image Storage
            file_meta.MediaStorageSOPInstanceUID = '1.2.3' # Dummy UID, should be generated uniquely in prod
            file_meta.ImplementationClassUID = '1.2.3.4' # Dummy
            file_meta.TransferSyntaxUID = pydicom.uid.ExplicitVRLittleEndian

            ds = pydicom.dataset.FileDataset(fname, {}, file_meta=file_meta, preamble=b"\0" * 128)
            
            # Add minimal required tags
            ds.PatientName = default_name # Use the name we found
            ds.PatientID = "123456"
            ds.Modality = "OT"
            ds.SeriesInstanceUID = pydicom.uid.generate_uid()
            ds.SOPInstanceUID = pydicom.uid.generate_uid()
            ds.SOPClassUID = '1.2.840.10008.5.1.4.1.1.7'
            
            # Image properties
            ds.SamplesPerPixel = 1
            ds.PhotometricInterpretation = "MONOCHROME2"
            ds.PixelRepresentation = 0
            ds.HighBit = 7
            ds.BitsStored = 8
            ds.BitsAllocated = 8
            ds.Rows = gray_arr.shape[0]
            ds.Columns = gray_arr.shape[1]
            ds.PixelData = gray_arr.tobytes()
            
            ds.save_as(fname)
            QMessageBox.information(self, "成功", "DICOM文件已保存")
        except Exception as e:
             QMessageBox.critical(self, "错误", f"保存DICOM失败: {e}")

    def enable_measure_tool(self):
        self.cobb_tool.active = True
        self.view.setDragMode(QGraphicsView.NoDrag)
        self.view.setCursor(Qt.CrossCursor)

    def set_images_movable(self, movable):
        """Helper to lock/unlock images"""
        for item in self.scene.items():
            if isinstance(item, ImageGraphicsItem):
                item.setFlag(QGraphicsItem.ItemIsMovable, movable)
                item.setFlag(QGraphicsItem.ItemIsSelectable, movable)

    def auto_measure_cobb(self):
        # 1. Grab the current full image from the scene
        self.scene.clearSelection()
        rect = self.scene.itemsBoundingRect()
        if rect.isEmpty():
            QMessageBox.warning(self, "提示", "请先拼接图像")
            return
            
        # Lock images to prevent accidental dragging during measurement adjustment
        self.set_images_movable(False)
        
        image = QImage(rect.size().toSize(), QImage.Format_RGB32)
        image.fill(Qt.transparent)
        painter = QPainter(image)
        self.scene.render(painter, target=QRectF(image.rect()), source=rect)
        painter.end()
        
        # Convert to numpy for processing
        width = image.width()
        height = image.height()
        ptr = image.bits()
        ptr.setsize(image.byteCount())
        arr = np.array(ptr).reshape(height, width, 4)
        img_bgr = cv2.cvtColor(arr, cv2.COLOR_BGRA2BGR) # Drop alpha for processing
        
        # 2. Calculate centerline
        centerline = AutoCobbAlgo.calculate_spine_centerline(img_bgr)
        
        if not centerline:
            QMessageBox.warning(self, "提示", "未能自动检测到脊柱区域，请使用手动测量。")
            return
            
        # 3. Estimate Cobb Lines
        lines = AutoCobbAlgo.estimate_cobb_angle_points(centerline, img_bgr.shape)
        
        if lines:
            # Clear previous manual lines?
            self.cobb_tool.clear()
            
            # Add the estimated lines to the scene as movable Smart Lines
            for p1, p2 in lines:
                # Adjust coordinates relative to scene rect
                start_pt = (p1[0] + rect.x(), p1[1] + rect.y())
                end_pt = (p2[0] + rect.x(), p2[1] + rect.y())
                
                # Create SmartCobbLine which has handles
                smart_line = SmartCobbLine(start_pt, end_pt, self.scene, self.cobb_tool.calculate_cobb_angle)
                
                # Register with tool to calculate angle
                self.cobb_tool.lines.append(smart_line)
            
            # Trigger calculation but DO NOT show popup yet
            # Just update the text on screen
            self.cobb_tool.calculate_cobb_angle()
            
            # Inform user subtly or just let them adjust
            # QMessageBox.information(self, "自动测量完成", "已生成测量线，请手动调整，然后点击'保存测量'查看结果。") 
        else:
             QMessageBox.warning(self, "提示", "未能计算出有效的测量线，请手动测量。")

    def save_measurement(self):
        angle = self.cobb_tool.calculate_cobb_angle()
        if angle is None:
            QMessageBox.warning(self, "提示", "没有有效的测量数据")
            return
            
        # Grading logic
        grade = "正常 (Normal)"
        if angle < 10:
            grade = "脊柱无侧弯 (No Scoliosis)"
        elif 10 <= angle < 20:
            grade = "轻度侧弯 (Mild Scoliosis)"
        elif 20 <= angle < 40:
            grade = "中度侧弯 (Moderate Scoliosis)"
        else:
            grade = "重度侧弯 (Severe Scoliosis)"
            
        # Save to JSON
        try:
            patient_name, image_id = self.get_current_patient_info()
            
            record = {
                "name": patient_name,
                "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "cobb_angle": round(angle, 2),
                "image_id": image_id
            }
            
            self.save_record_to_json(record)
                
        except Exception as e:
            print(f"Error saving record: {e}")

        QMessageBox.information(self, "测量结果", f"Cobb角: {angle:.1f}°\n判定: {grade}\n\n测量数据已保存。")

    def show_records(self):
        dialog = RecordViewerDialog(self)
        dialog.exec_()

    def clear_scene(self):
        self.scene.clear()
        
        # Reset Brightness
        if hasattr(self.view, 'reset_brightness'):
            self.view.reset_brightness()
            
        self.cobb_tool.lines = []
        self.cobb_tool.text_item = None
        self.view.setDragMode(QGraphicsView.ScrollHandDrag)
        self.view.setCursor(Qt.ArrowCursor)
        # Re-enable movability for any new images added later (though clear removes them)
        # But if we were to just clear tools, we would call set_images_movable(True)
        


    def eventFilter(self, source, event):
        if source == self.view.viewport() and self.cobb_tool.active:
            if event.type() == event.MouseButtonPress:
                if event.button() == Qt.LeftButton:
                    pos = self.view.mapToScene(event.pos())
                    self.cobb_tool.start_drawing(pos)
                    return True
            elif event.type() == event.MouseMove:
                pos = self.view.mapToScene(event.pos())
                self.cobb_tool.update_drawing(pos)
                return True
            elif event.type() == event.MouseButtonRelease:
                if event.button() == Qt.LeftButton:
                    self.cobb_tool.finish_drawing()
                    if not self.cobb_tool.active:
                         self.view.setDragMode(QGraphicsView.ScrollHandDrag)
                         self.view.setCursor(Qt.ArrowCursor)
                    return True
        return super().eventFilter(source, event)

if __name__ == '__main__':
    # Set AppUserModelID for Windows Taskbar Icon
    try:
        myappid = 'com.spine.stitcher.app.v1'
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(myappid)
    except:
        pass

    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec_())
