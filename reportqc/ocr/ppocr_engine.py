# -*- coding: utf-8 -*-
"""PP-OCRv4 中文 OCR 引擎（自实现推理，ORT 1.12.1 + OpenCV 4.12）。

设计要点
--------
1. **零新增依赖**：只用 onnxruntime / cv2 / numpy，三者 CT3D 的 pyenv 已内置。
   RapidOCR / PaddleOCR 官方实现依赖 ``pyclipper`` + ``shapely``（DBNet 后处理）
   与 ``paddlepaddle``，本文件全部用 cv2 等价替代，见 :meth:`_unclip`。

2. **算法口径与 rapidocr-onnxruntime 1.4.4 逐一对齐**（该版本的 wheel 自带
   同样版本的 det/rec/cls 模型），包括：
   - det 预处理：``limit_side_len=736, limit_type="min"``，宽高取整到 32 的倍数，
     归一化 ``(x/255 - 0.5) / 0.5``；
   - det 后处理：``thresh=0.3, box_thresh=0.5, unclip_ratio=1.6, use_dilation=True``，
     ``score_mode="fast"``；
   - rec 预处理：高固定 48，宽按 ``ceil(48 * w/h)`` 且 batch 内统一取最大宽高比，
     右侧零 padding；
   - rec 解码：CTC，去 blank（索引 0）+ 去相邻重复，置信度取非 blank 位置概率均值。

3. **字典内嵌在模型里**：``ch_PP-OCRv4_rec_infer.onnx`` 的 metadata 的
   ``character`` 字段就是 6623 字的字符表，无需单独的 ``ppocr_keys_v1.txt``。

4. **模型无需降级**：三个模型本身是 ir_version=8/7、opset=12/11，
   ORT 1.12.1（IR 上限 8）直接加载通过。

Win7 兼容：只用 Python 3.8 语法；无 ``X | None`` 注解、无 walrus、无 match。
"""

import math
import os
import typing


def _ensure_app_cwd():
    """把进程 CWD 归一化到发行包根目录（**必须在 import cv2 之前调用**）。

    为什么需要
    --------
    这个绿色嵌入式 Python 3.8.3 有一套隐性的 CWD 依赖：发行包根目录下的
    ``python3.dll`` / ``unicodedata.pyd`` / ``pyexpat.pyd``（2021-05，与
    ``pyenv`` 配套）与 ``pyenv/DLLs/`` 下的那份**不是同一版本**。Python 解释器
    启动时会从「CWD / sys.path[0]」优先加载 ``unicodedata``，于是：

    * CWD = 发行包根目录 → 加载配套版本 → ``import cv2`` 正常；
    * CWD = 其他目录（例如测试脚本放在别处运行）→ 从 ``pyenv/DLLs`` 加载
      另一版本 → 后续 C 扩展的依赖解析错位，``import cv2`` **直接段错误**
      （exit 139，且不带任何 traceback，极难定位）。

    实测结论
    --------
    * ``os.add_dll_directory(根目录)`` **修不了**（段错误依旧）；
    * 在 import cv2 之前 ``os.chdir(根目录)`` **可以修复**。

    安全性：CT3D 主程序（main.py / ct3d_utils.py / export_utils.py / 各 bridge）
    经 grep 确认**没有任何 os.chdir 调用**，全部走绝对路径，因此本函数把 CWD
    收敛到根目录不会影响主程序行为。
    """
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    try:
        if os.path.abspath(os.getcwd()) != root:
            os.chdir(root)
    except OSError:
        pass
    return root


_ensure_app_cwd()

import cv2                                              # noqa: E402
import numpy as np                                      # noqa: E402
import onnxruntime as ort                               # noqa: E402

__all__ = ["PPOCREngine", "TextLine"]

_HERE = os.path.dirname(os.path.abspath(__file__))
# .../reportqc/ocr/ppocr_engine.py → .../reportqc → 拼上 models/ocr
_MODEL_DIR = os.path.join(os.path.dirname(_HERE), "models", "ocr")

DET_MODEL = "ch_PP-OCRv4_det_infer.onnx"
REC_MODEL = "ch_PP-OCRv4_rec_infer.onnx"


class TextLine(typing.NamedTuple):
    """一行识别结果。``confidence`` 是该行所有非 blank 字符的平均概率。"""

    text: str
    confidence: float
    box: typing.Any          # np.ndarray, shape (4, 2)，原图坐标


class PPOCREngine(object):
    """PP-OCRv4 检测 + 识别引擎。

    Parameters
    ----------
    det_path / rec_path:
        模型路径，默认取 ``reportqc/models/ocr/`` 下的标准文件名。
    scale:
        推理前对图像的放大倍数。PACS 报告字号常偏小，2x 能明显提升识别率，
        代价是耗时约翻倍。默认 ``2.0``。
    text_score:
        低于该置信度的识别行直接丢弃（视为噪声/花屏）。默认 ``0.5``。
    rec_batch_num:
        识别批大小。默认 ``6``。
    intra_op:
        ORT 线程数，``-1`` 为自动。
    """

    def __init__(
        self,
        det_path=None,
        rec_path=None,
        scale=2.0,
        text_score=0.5,
        rec_batch_num=6,
        intra_op=-1,
    ):
        det_path = det_path or os.path.join(_MODEL_DIR, DET_MODEL)
        rec_path = rec_path or os.path.join(_MODEL_DIR, REC_MODEL)

        if not os.path.isfile(det_path):
            raise FileNotFoundError("OCR 检测模型缺失: %s" % det_path)
        if not os.path.isfile(rec_path):
            raise FileNotFoundError("OCR 识别模型缺失: %s" % rec_path)

        opt = ort.SessionOptions()
        opt.intra_op_num_threads = intra_op
        opt.inter_op_num_threads = 1
        opt.log_severity_level = 3

        so = ort.SessionOptions()
        so.intra_op_num_threads = intra_op
        so.inter_op_num_threads = 1
        so.log_severity_level = 3
        self._det = ort.InferenceSession(det_path, so, providers=["CPUExecutionProvider"])
        self._rec = ort.InferenceSession(rec_path, so, providers=["CPUExecutionProvider"])

        self._det_in = self._det.get_inputs()[0].name
        self._det_out = self._det.get_outputs()[0].name
        self._rec_in = self._rec.get_inputs()[0].name
        self._rec_out = self._rec.get_outputs()[0].name

        # 字符表从 rec 模型的 metadata 里读。
        # **必须与官方口径一致地在首尾各插入一个特殊字符**：
        #   索引 0   -> "blank"（CTC 空位，解码时跳过）
        #   索引 1..N -> metadata 里的原始字符
        #   索引 N+1 -> " "（空格）
        # 漏掉这一步会让所有 argmax 索引整体偏移 1 位 —— 表现为置信度极高
        # （模型很确信）但识别结果全是形近错字，极难排查。
        meta = self._rec.get_modelmeta().custom_metadata_map or {}
        raw = meta.get("character") or ""
        base = raw.splitlines()
        if len(base) < 10:
            raise RuntimeError("OCR 模型未内嵌字符表，无法解码")
        self.character = ["blank"] + base + [" "]
        self._ignored = {0, len(self.character) - 1}

        # ---- 与 rapidocr 1.4.4 config.yaml 对齐的默认参数 ----
        self.scale = float(scale)
        self.text_score = float(text_score)
        self.rec_batch_num = max(1, int(rec_batch_num))

        self.det_limit_side_len = 736
        self.det_thresh = 0.3
        self.box_thresh = 0.5
        self.unclip_ratio = 1.6
        self.det_use_dilation = True
        self.max_side_len = 2000
        self.min_side_len = 30

        self.rec_img_shape = (3, 48, 320)   # C, H, W

    # ------------------------------------------------------------------
    # 对外主入口
    # ------------------------------------------------------------------
    def __call__(self, img):
        """对一张 BGR 图像做 OCR，返回按阅读顺序排好的 :class:`TextLine` 列表。

        返回的 ``box`` 坐标基于**原始传入图像**（已按 ``scale`` 反算回去），
        调用方若是在放大后的图上定位，需自行换算。
        """
        if img is None:
            return []
        arr = np.asarray(img)
        if arr.ndim == 2:                      # 灰度 → BGR
            arr = cv2.cvtColor(arr, cv2.COLOR_GRAY2BGR)
        if arr.shape[2] == 4:                  # BGRA → BGR
            arr = cv2.cvtColor(arr, cv2.COLOR_BGRA2BGR)
        if arr.dtype != np.uint8:
            arr = np.clip(arr, 0, 255).astype(np.uint8)

        work = self._maybe_upscale(arr)
        boxes, scores = self._detect(work)
        if not len(boxes):
            return []

        boxes = self._sort_boxes(boxes)
        crops = [self._rotate_crop(work, b) for b in boxes]
        crops = [c for c in crops if c is not None and c.size]
        if not crops:
            return []

        recs = self._recognize(crops)
        results = []
        for (txt, conf), box in zip(recs, boxes):
            if not txt or conf < self.text_score:
                continue
            # box 坐标还原到原始图尺度
            box = box.astype(np.float32) / self.scale
            results.append(TextLine(txt, float(conf), box))
        return results

    # ------------------------------------------------------------------
    # 放大
    # ------------------------------------------------------------------
    def _maybe_upscale(self, img):
        if abs(self.scale - 1.0) < 1e-6:
            return img
        interp = cv2.INTER_CUBIC if self.scale > 1.0 else cv2.INTER_AREA
        return cv2.resize(
            img, None, fx=self.scale, fy=self.scale, interpolation=interp
        )

    # ------------------------------------------------------------------
    # det：预处理
    # ------------------------------------------------------------------
    def _det_prepare(self, img):
        h, w = img.shape[:2]

        # max_side_len / min_side_len 归一（对齐官方 preprocess）
        if max(h, w) > self.max_side_len:
            ratio = float(self.max_side_len) / max(h, w)
            img = cv2.resize(img, None, fx=ratio, fy=ratio, interpolation=cv2.INTER_AREA)
            h, w = img.shape[:2]
        if min(h, w) < self.min_side_len:
            ratio = float(self.min_side_len) / min(h, w)
            img = cv2.resize(img, None, fx=ratio, fy=ratio, interpolation=cv2.INTER_CUBIC)
            h, w = img.shape[:2]

        ratio = 1.0
        if min(h, w) < self.det_limit_side_len:
            if h < w:
                ratio = float(self.det_limit_side_len) / h
            else:
                ratio = float(self.det_limit_side_len) / w

        resize_h = int(round(int(h * ratio) / 32) * 32)
        resize_w = int(round(int(w * ratio) / 32) * 32)
        if resize_h <= 0 or resize_w <= 0:
            return None, 0.0
        resized = cv2.resize(img, (resize_w, resize_h))

        norm = resized.astype(np.float32) / 255.0
        norm = (norm - 0.5) / 0.5
        norm = norm.transpose((2, 0, 1))[np.newaxis, :]
        return np.ascontiguousarray(norm), ratio

    # ------------------------------------------------------------------
    # det：推理
    # ------------------------------------------------------------------
    def _detect(self, img):
        blob, ratio = self._det_prepare(img)
        if blob is None:
            return np.zeros((0, 4, 2), np.int32), []
        pred = self._det.run([self._det_out], {self._det_in: blob})[0]
        return self._db_post(pred, img.shape[:2], ratio)

    # ------------------------------------------------------------------
    # det：DB 后处理（cv2 等价实现，无 pyclipper / shapely）
    # ------------------------------------------------------------------
    def _db_post(self, pred, ori_shape, ratio):
        """DB 后处理。

        ``pred`` 的 H/W 是 **32 对齐后的 blob 尺寸**，与 ``ori_shape`` 不等
        （例如 900x720 的图会被 resize 到 1440x1792）。因此每个框必须先把
        坐标从 blob 尺度线性映射回 ``ori_shape``，否则框会大面积偏移、
        裁出来的内容对不上，识别结果就是乱码。
        """
        src_h, src_w = ori_shape
        pred = pred[:, 0, :, :]
        mask = (pred > self.det_thresh).astype(np.uint8)[0]
        if self.det_use_dilation:
            mask = cv2.dilate(mask, np.array([[1, 1], [1, 1]], np.uint8))

        bitmap_h, bitmap_w = mask.shape
        found = cv2.findContours(
            (mask * 255).astype(np.uint8), cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE
        )
        contours = found[0] if len(found) == 2 else found[1]

        boxes, scores = [], []
        for contour in contours[:1000]:
            points, sside = self._mini_box(contour)
            if sside < 3:
                continue
            score = self._box_score(pred[0], points.reshape(-1, 2))
            if score < self.box_thresh:
                continue
            box = self._unclip(points)
            box, sside = self._mini_box(box)
            if sside < 5:
                continue

            # blob 尺度 → ori_shape（det 输入图）尺度
            box = box.copy()
            box[:, 0] = np.clip(np.round(box[:, 0] / bitmap_w * src_w), 0, src_w)
            box[:, 1] = np.clip(np.round(box[:, 1] / bitmap_h * src_h), 0, src_h)
            boxes.append(box.astype(np.int32))
            scores.append(float(score))
        if not boxes:
            return np.zeros((0, 4, 2), np.int32), []
        return np.array(boxes, dtype=np.int32), scores

    @staticmethod
    def _mini_box(contour):
        """最小外接旋转矩形 → 按 (左上, 右上, 右下, 左下) 排序的 4 点。"""
        rect = cv2.minAreaRect(contour)
        pts = sorted(cv2.boxPoints(rect).tolist(), key=lambda p: p[0])
        if pts[1][1] > pts[0][1]:
            i1, i4 = 0, 1
        else:
            i1, i4 = 1, 0
        if pts[3][1] > pts[2][1]:
            i2, i3 = 2, 3
        else:
            i2, i3 = 3, 2
        box = np.array([pts[i1], pts[i2], pts[i3], pts[i4]], dtype=np.float32)
        return box, min(rect[1])

    @staticmethod
    def _box_score(bitmap, box):
        """fast 模式：框内概率图均值。"""
        h, w = bitmap.shape[:2]
        xmin = int(np.clip(np.floor(box[:, 0].min()), 0, w - 1))
        xmax = int(np.clip(np.ceil(box[:, 0].max()), 0, w - 1))
        ymin = int(np.clip(np.floor(box[:, 1].min()), 0, h - 1))
        ymax = int(np.clip(np.ceil(box[:, 1].max()), 0, h - 1))
        m = np.zeros((ymax - ymin + 1, xmax - xmin + 1), dtype=np.uint8)
        poly = box.copy()
        poly[:, 0] -= xmin
        poly[:, 1] -= ymin
        cv2.fillPoly(m, poly.reshape(1, -1, 2).astype(np.int32), 1)
        return float(cv2.mean(bitmap[ymin:ymax + 1, xmin:xmax + 1], m)[0])

    def _unclip(self, box):
        """等距外扩 —— 替代 pyclipper 的 Vatti 裁剪膨胀。

        DBNet 原版语义：``distance = area * unclip_ratio / perimeter``，
        即外扩 distance 个像素。对矩形而言等价于宽高各加 ``2 * distance``，
        用 ``cv2.boxPoints`` 重建即可，对规整横排文字与原版结果一致。
        """
        rect = cv2.minAreaRect(box.reshape(-1, 1, 2).astype(np.float32))
        (_, _), (w, h), angle = rect
        area = w * h
        peri = 2.0 * (w + h)
        if peri <= 0:
            return box
        d = area * self.unclip_ratio / peri
        expanded = ((rect[0][0], rect[0][1]), (w + 2 * d, h + 2 * d), angle)
        return cv2.boxPoints(expanded).astype(np.float32)

    # ------------------------------------------------------------------
    # 文本框排序：从上到下、从左到右
    # ------------------------------------------------------------------
    @staticmethod
    def _sort_boxes(dt_boxes):
        if not len(dt_boxes):
            return dt_boxes
        ordered = sorted(dt_boxes.tolist(), key=lambda x: (x[0][1], x[0][0]))
        n = len(ordered)
        for i in range(n - 1):
            for j in range(i, -1, -1):
                if abs(ordered[j + 1][0][1] - ordered[j][0][1]) < 10 and \
                        ordered[j + 1][0][0] < ordered[j][0][0]:
                    ordered[j], ordered[j + 1] = ordered[j + 1], ordered[j]
                else:
                    break
        return np.array(ordered, dtype=np.int32)

    # ------------------------------------------------------------------
    # rec：透视裁剪
    # ------------------------------------------------------------------
    @staticmethod
    def _rotate_crop(img, points):
        p = points.astype(np.float32)
        w = int(max(np.linalg.norm(p[0] - p[1]), np.linalg.norm(p[2] - p[3])))
        h = int(max(np.linalg.norm(p[0] - p[3]), np.linalg.norm(p[1] - p[2])))
        if w <= 0 or h <= 0:
            return None
        std = np.array(
            [[0, 0], [w, 0], [w, h], [0, h]], dtype=np.float32
        )
        try:
            M = cv2.getPerspectiveTransform(p, std)
        except cv2.error:
            return None
        dst = cv2.warpPerspective(
            img, M, (w, h),
            borderMode=cv2.BORDER_REPLICATE, flags=cv2.INTER_CUBIC,
        )
        if dst.shape[0] * 1.0 / max(dst.shape[1], 1) >= 1.5:
            dst = np.rot90(dst)
        return dst

    # ------------------------------------------------------------------
    # rec：识别 + CTC 解码
    # ------------------------------------------------------------------
    def _recognize(self, img_list):
        width_list = [im.shape[1] / float(im.shape[0]) for im in img_list]
        order = np.argsort(np.array(width_list))

        results = [("", 0.0)] * len(img_list)
        imgC, imgH, imgW = self.rec_img_shape

        for beg in range(0, len(img_list), self.rec_batch_num):
            end = min(len(img_list), beg + self.rec_batch_num)
            # batch 内统一宽度：取 (固定上限, batch 内最大宽高比) 的较大者。
            # 报告单行文字宽高比可达 20:1，若死用固定的 imgW=320 会把文字
            # 横向压扁到 1/3，识别结果全是乱码 —— 必须动态放宽。
            max_wh = imgW / float(imgH)
            for k in range(beg, end):
                h, w = img_list[order[k]].shape[:2]
                max_wh = max(max_wh, w / float(h))

            batch = []
            for k in range(beg, end):
                batch.append(self._norm_rec(img_list[order[k]], max_wh, imgH, imgW, imgC))
            blob = np.concatenate(batch).astype(np.float32)

            preds = self._rec.run([self._rec_out], {self._rec_in: blob})[0]
            decoded = self._ctc_decode(preds)
            for j, one in enumerate(decoded):
                results[order[beg + j]] = one
        return results

    def _norm_rec(self, img, max_wh_ratio, imgH, imgW, imgC):
        """裁剪图 → rec 网络输入。

        ``img_width`` 按 batch 内最大宽高比动态计算（对齐官方口径），
        单个图的宽高比超出时缩到 ``img_width``，否则按比例缩放后右侧补零。
        """
        img_width = int(imgH * max_wh_ratio)
        h, w = img.shape[:2]
        ratio = w / float(h)
        resized_w = img_width if math.ceil(imgH * ratio) > img_width \
            else int(math.ceil(imgH * ratio))
        resized_w = max(1, min(resized_w, img_width))
        resized = cv2.resize(img, (resized_w, imgH)).astype(np.float32)
        resized = resized.transpose((2, 0, 1)) / 255.0
        resized -= 0.5
        resized /= 0.5
        out = np.zeros((imgC, imgH, img_width), dtype=np.float32)
        out[:, :, 0:resized_w] = resized
        return out[np.newaxis, :]

    def _ctc_decode(self, preds):
        """CTC 解码：argmax → 去 blank(索引0) → 去相邻重复。"""
        idx = preds.argmax(axis=2)
        prob = preds.max(axis=2)
        n_char = len(self.character)
        out = []
        for b in range(idx.shape[0]):
            chars, confs = [], []
            prev = 0
            for t in range(idx.shape[1]):
                c = int(idx[b, t])
                if c != 0 and c != prev and c < n_char:
                    chars.append(self.character[c])
                    confs.append(float(prob[b, t]))
                prev = c
            text = "".join(chars).strip()
            score = float(sum(confs) / len(confs)) if confs else 0.0
            out.append((text, score))
        return out
