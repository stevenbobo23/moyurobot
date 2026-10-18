#!/usr/bin/env python
"""自动打招呼（检测到人就说话 + 点头/摇头）

模块划分：

- ``config``   配置（仅标准库，支持环境变量覆盖）
- ``policy``   触发状态机（纯逻辑，可离线单测）
- ``detector`` 人体检测（YOLO ONNX / OpenCV HOG，懒加载 numpy/cv2）
- ``speech``   语音来源与播放（本地 wav 池 / 云端 TTS，复用对讲喇叭）
- ``worker``   后台线程、帧共享、资源仲裁与状态查询

用法::

    from moyurobot.greeting import start_greeter
    start_greeter(service)
"""

from .config import GreetingConfig, SpeechConfig, in_active_hours
from .policy import Decision, GreetPolicy, Observation
from .speech import SpeechLibrary, decode_wav
from .worker import (
    FrameBroker,
    GreeterWorker,
    get_frame_broker,
    get_greeter,
    start_greeter,
    stop_greeter,
)

__all__ = [
    "GreetingConfig",
    "SpeechConfig",
    "in_active_hours",
    "GreetPolicy",
    "Observation",
    "Decision",
    "SpeechLibrary",
    "decode_wav",
    "FrameBroker",
    "GreeterWorker",
    "start_greeter",
    "stop_greeter",
    "get_greeter",
    "get_frame_broker",
]
