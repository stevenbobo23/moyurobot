#!/usr/bin/env python
"""人体检测

三层降级，按 ``greeting.detector`` 选择（``auto`` 会自动挑最靠前那个可用的）：

1. ``yolo`` + ``onnxruntime``：最快的 CPU 推理路径
2. ``yolo`` + ``cv2.dnn``：**只要有 opencv 就能跑 YOLO**，不需要 onnxruntime
3. ``hog``：OpenCV 自带的 HOG + SVM 行人检测（仅 OpenCV 4.x 有；5.0 已移除该 API）
4. ``none``：关闭检测

**本模块顶层不 import numpy / cv2**：这些库在开发机上不一定有，
单测只测状态机与配置，检测器按需懒加载，import 失败要能优雅降级而不是崩。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from .config import GreetingConfig

logger = logging.getLogger(__name__)

_MODEL_CANDIDATES = (
    "models/yolo11n.onnx",
    "models/yolov8n.onnx",
    "models/yolo11s.onnx",
    "yolo11n.onnx",
)


@dataclass
class Detection:
    """一个人"""

    ratio: float           # bbox 高 / 画面高，用来当「距离」代理
    centered: bool         # 中心是否在画面中央区
    score: float = 0.0     # 置信度
    box: Tuple[int, int, int, int] = (0, 0, 0, 0)


def _project_root() -> Path:
    # moyurobot/greeting/detector.py -> pi_client/
    return Path(__file__).resolve().parents[2]


def _resolve_model_path(configured: str, root: Path) -> Optional[Path]:
    if configured:
        path = Path(configured)
        if not path.is_absolute():
            path = root / path
        return path if path.exists() else None
    for relative in _MODEL_CANDIDATES:
        candidate = root / relative
        if candidate.exists():
            return candidate
    return None


class PersonDetector:
    """检测器门面"""

    def __init__(self, cfg: GreetingConfig, root: Optional[Path] = None):
        self.cfg = cfg
        self.root = Path(root) if root else _project_root()
        self.backend = "none"
        self.error = ""
        self.model_path = ""
        self._input_size = (cfg.input_size, cfg.input_size)

        self._ort_session = None
        self._ort_input = ""
        self._dnn_net = None
        self._hog = None
        self._cv2 = None

        self._load()

    # ---------------------------------------------------------------- 载入

    def _load(self) -> None:
        wanted = (self.cfg.detector or "auto").strip().lower()
        if wanted == "none":
            logger.info("打招呼：检测器已关闭（detector=none）")
            return

        if wanted in ("auto", "yolo", "onnx", "dnn"):
            reason = self._load_yolo(prefer=wanted)
            if self.backend == "yolo":
                return
            if wanted in ("yolo", "onnx", "dnn"):
                logger.warning("打招呼：YOLO 不可用（%s），检测器关闭", reason or "未知原因")
                self.error = reason or "YOLO 不可用"
                return
            logger.info("打招呼：YOLO 不可用（%s），尝试 OpenCV HOG", reason or "未知原因")

        if wanted in ("auto", "hog"):
            if self._load_hog():
                return
            if not self.error:
                self.error = "没有可用的检测后端"

    def _load_yolo(self, prefer: str = "auto") -> str:
        """返回失败原因（空字符串表示成功）"""
        model = _resolve_model_path(self.cfg.model_path, self.root)
        if model is None:
            return "未找到 ONNX 模型（models/yolo11n.onnx）"

        want_ort = prefer in ("auto", "yolo", "onnx")
        want_dnn = prefer in ("auto", "yolo", "dnn")

        if want_ort and self._load_onnxruntime(model):
            return ""
        if want_dnn and self._load_cv2_dnn(model):
            return ""
        return self.error or "YOLO 初始化失败"

    def _load_onnxruntime(self, model: Path) -> bool:
        try:
            import onnxruntime as ort
        except ImportError:
            self.error = "缺少 onnxruntime（可改用 cv2.dnn 后端，无需安装）"
            return False
        try:
            options = ort.SessionOptions()
            options.intra_op_num_threads = 2
            options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            session = ort.InferenceSession(
                str(model), sess_options=options, providers=["CPUExecutionProvider"]
            )
            spec = session.get_inputs()[0]
            self._ort_session = session
            self._ort_input = spec.name
            self._apply_input_shape(list(spec.shape))
            self.backend = "yolo"
            self.model_path = str(model)
            logger.info("打招呼：检测器 = YOLO（onnxruntime，%s）", model.name)
            return True
        except Exception as e:
            self.error = f"onnxruntime 加载失败: {e}"
            self._ort_session = None
            return False

    def _load_cv2_dnn(self, model: Path) -> bool:
        try:
            import cv2
        except ImportError:
            self.error = "缺少 opencv"
            return False
        reader = getattr(cv2.dnn, "readNetFromONNX", None)
        if reader is None:
            self.error = "当前 opencv 没有 dnn.readNetFromONNX"
            return False
        try:
            net = reader(str(model))
            net.setPreferableBackend(cv2.dnn.DNN_BACKEND_OPENCV)
            net.setPreferableTarget(cv2.dnn.DNN_TARGET_CPU)
            self._cv2 = cv2
            self._dnn_net = net
            self.backend = "yolo"
            self.model_path = str(model)
            logger.info("打招呼：检测器 = YOLO（opencv dnn，%s）", model.name)
            return True
        except Exception as e:
            self.error = f"opencv dnn 加载模型失败: {e}"
            self._dnn_net = None
            return False

    def _apply_input_shape(self, shape) -> None:
        height = shape[2] if len(shape) == 4 and isinstance(shape[2], int) else self.cfg.input_size
        width = shape[3] if len(shape) == 4 and isinstance(shape[3], int) else self.cfg.input_size
        self._input_size = (int(width), int(height))

    def _load_hog(self) -> bool:
        try:
            import cv2
        except ImportError as e:
            self.error = f"缺少 opencv: {e}"
            return False

        factory = getattr(cv2, "HOGDescriptor", None)
        if factory is None:
            # OpenCV 5.0 把 objdetect 里的 HOGDescriptor 移除了，这里不能硬用
            self.error = (
                "当前 opencv %s 没有 HOGDescriptor（5.0 起已移除）；"
                "想开检测请放一个 models/yolo11n.onnx" % getattr(cv2, "__version__", "?")
            )
            return False

        try:
            hog = factory()
            hog.setSVMDetector(cv2.HOGDescriptor_getDefaultPeopleDetector())
            self._hog = hog
            self._cv2 = cv2
            self.backend = "hog"
            logger.info("打招呼：检测器 = OpenCV HOG（精度一般，建议换 YOLO）")
            return True
        except Exception as e:
            self.error = f"初始化 HOG 失败: {e}"
            return False

    # ---------------------------------------------------------------- 接口

    @property
    def available(self) -> bool:
        return self.backend != "none"

    def describe(self) -> Dict[str, Any]:
        return {
            "backend": self.backend,
            "model": self.model_path,
            "error": self.error,
            "input_size": list(self._input_size),
        }

    def detect(self, frame) -> Optional[Detection]:
        """返回最可信的一个人；没有则 None。frame 为 RGB uint8"""
        if self.backend == "yolo":
            return self._detect_yolo(frame)
        if self.backend == "hog":
            return self._detect_hog(frame)
        return None

    # ---------------------------------------------------------------- YOLO

    def _detect_yolo(self, frame) -> Optional[Detection]:
        try:
            import numpy as np
        except ImportError:
            return None

        try:
            height, width = frame.shape[:2]
            in_w, in_h = self._input_size
            canvas, scale, (pad_x, pad_y) = _letterbox_canvas(frame, in_w, in_h, np)
            raw = self._infer(canvas, np)
            if raw is None:
                return None
        except Exception as e:
            logger.debug("YOLO 推理失败: %s", e)
            return None

        return _parse_output(raw, width, height, scale, pad_x, pad_y,
                             self.cfg.yolo_conf, self.cfg.center_zone, np)

    def _infer(self, canvas, np):
        if self._ort_session is not None:
            blob = canvas.astype(np.float32) / 255.0
            blob = np.transpose(blob, (2, 0, 1))[None, ...]
            return self._ort_session.run(
                None, {self._ort_input: np.ascontiguousarray(blob)}
            )[0]

        if self._dnn_net is not None:
            cv2 = self._cv2
            in_w, in_h = self._input_size
            # canvas 已是 RGB 且尺寸正确，swapRB=False 保持 RGB
            blob = cv2.dnn.blobFromImage(
                canvas, 1.0 / 255.0, (in_w, in_h), (0, 0, 0), swapRB=False, crop=False
            )
            self._dnn_net.setInput(blob)
            return self._dnn_net.forward()

        return None

    # ---------------------------------------------------------------- HOG

    def _detect_hog(self, frame) -> Optional[Detection]:
        cv2 = self._cv2
        if cv2 is None or self._hog is None:
            return None
        try:
            gray = cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY)
            gray = cv2.equalizeHist(gray)  # 帐篷里偏暗时 HOG 更稳一点
            rects, weights = self._hog.detectMultiScale(
                gray, winStride=(8, 8), padding=(8, 8), scale=self.cfg.hog_scale
            )
        except Exception as e:
            logger.debug("HOG 检测失败: %s", e)
            return None

        if rects is None or len(rects) == 0:
            return None

        height, width = gray.shape[:2]
        best = None
        for index, (x, y, w, h) in enumerate(rects):
            weight = float(weights[index]) if index < len(weights) else 1.0
            if weight <= 0:
                continue
            area = float(w) * float(h)
            if best is None or area > best[0]:
                best = (area, int(x), int(y), int(w), int(h), weight)
        if best is None:
            return None

        _, x, y, w, h, weight = best
        return Detection(
            ratio=max(0.0, min(1.0, h / float(height))),
            centered=_is_centered(x + w / 2.0, width, self.cfg.center_zone),
            score=weight,
            box=(x, y, w, h),
        )


# ---------------------------------------------------------------- 工具函数


def _is_centered(center_x: float, width: int, zone: float) -> bool:
    if zone >= 1.0 or width <= 0:
        return True
    half = width * zone / 2.0
    middle = width / 2.0
    return (middle - half) <= center_x <= (middle + half)


def _letterbox_canvas(frame, in_w: int, in_h: int, np):
    """等比缩放 + 灰边填充，返回 (uint8 RGB canvas, scale, (pad_x, pad_y))"""
    import cv2

    height, width = frame.shape[:2]
    scale = min(in_w / float(width), in_h / float(height))
    new_w = max(1, int(round(width * scale)))
    new_h = max(1, int(round(height * scale)))

    resized = cv2.resize(frame, (new_w, new_h), interpolation=cv2.INTER_LINEAR)
    canvas = np.full((in_h, in_w, 3), 114, dtype=np.uint8)
    pad_x = (in_w - new_w) // 2
    pad_y = (in_h - new_h) // 2
    canvas[pad_y:pad_y + new_h, pad_x:pad_x + new_w] = resized
    return canvas, scale, (pad_x, pad_y)


def _parse_output(raw, width: int, height: int, scale: float,
                  pad_x: int, pad_y: int, conf: float, center_zone: float, np):
    """解析 YOLOv5/v8/v11 的输出，取最可信的那个人"""
    out = np.asarray(raw)
    out = np.squeeze(out)
    if out.ndim != 2:
        return None
    if out.shape[0] < out.shape[1]:
        out = out.T  # (84, 8400) -> (8400, 84)
    if out.shape[1] < 5:
        return None

    # YOLOv5 带 objectness（85 列）；YOLOv8/11 是 4 + 类别数
    if out.shape[1] == 85:
        scores = out[:, 4] * out[:, 5]
    else:
        scores = out[:, 4]

    best = int(np.argmax(scores))
    score = float(scores[best])
    if score < conf:
        return None

    cx, cy, bw, bh = (float(v) for v in out[best, :4])
    cx = (cx - pad_x) / scale
    cy = (cy - pad_y) / scale
    bw = bw / scale
    bh = bh / scale
    if bw <= 1 or bh <= 1:
        return None

    ratio = max(0.0, min(1.0, bh / float(height)))
    return Detection(
        ratio=ratio,
        centered=_is_centered(cx, width, center_zone),
        score=score,
        box=(int(max(0, cx - bw / 2)), int(max(0, cy - bh / 2)), int(bw), int(bh)),
    )
