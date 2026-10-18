#!/usr/bin/env python
"""打招呼的状态机（纯逻辑，不碰摄像头/喇叭/机械臂）

把判定逻辑单独抽出来，是为了能在没有硬件、没有 numpy 的机器上把
「冷却、限速、离开判定、连续帧容忍」这些最容易出错的地方用单测钉死。

状态流转::

    idle ──连续命中──► seeing ──达标──► (触发) ──► cooldown ──冷却结束──► idle
      ▲                                            │
      └──────────── 连续 leave_seconds 无人 ────────┘
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Deque, Dict, Optional

from .config import GreetingConfig, in_active_hours

STATE_IDLE = "idle"
STATE_SEEING = "seeing"
STATE_COOLDOWN = "cooldown"

# 未命中时的扣分。0.5 意味着「容忍一次漏检」：命中的分要两次未命中才抵掉，
# 检测器在逆光/侧身时偶尔丢一帧不会让累积进度归零。
MISS_PENALTY = 0.5
# 得分上限，别让人站着不动时分数无限增长
SCORE_CAP = 8.0


@dataclass
class Observation:
    """一帧的检测结果"""

    present: bool = False
    ratio: float = 0.0      # bbox 高 / 画面高
    centered: bool = True   # bbox 中心是否在画面中央区
    score: float = 0.0      # 检测器置信度（仅用于展示）


@dataclass
class Decision:
    greet: bool = False
    reason: str = ""
    gesture: str = ""
    state: str = STATE_IDLE
    next_allowed_in: float = 0.0


class GreetPolicy:
    """触发判定"""

    def __init__(self, cfg: GreetingConfig, clock=None):
        self.cfg = cfg
        self._clock = clock or time.time
        self.greet_count = 0
        self.last_greet_at: Optional[float] = None
        self.last_person_at: Optional[float] = None
        self.reset()

    # ---------------------------------------------------------------- 状态

    def reset(self) -> None:
        self._score = 0.0
        self._first_hit: Optional[float] = None
        self._cooldown_until = 0.0
        self._reason = "no_person"
        self._greet_times: Deque[float] = deque()
        self.last_person_at = None

    def _state(self, now: float) -> str:
        if now < self._cooldown_until:
            return STATE_COOLDOWN
        if self._score > 0:
            return STATE_SEEING
        return STATE_IDLE

    def _centered_ok(self, centered: bool) -> bool:
        return True if self.cfg.center_zone >= 1.0 else bool(centered)

    def _prune(self, now: float) -> None:
        cutoff = now - 3600.0
        while self._greet_times and self._greet_times[0] < cutoff:
            self._greet_times.popleft()

    # ---------------------------------------------------------------- 判定

    def evaluate(self, now: Optional[float] = None,
                 present: bool = False, ratio: float = 0.0,
                 centered: bool = True) -> Decision:
        now = self._clock() if now is None else float(now)
        cfg = self.cfg

        # 非时段内先把累积清掉：否则 21:59 攒的分数会在 22:00 整点炸出来
        if not in_active_hours(cfg.active_hours, now):
            self._score = 0.0
            self._first_hit = None
            self._reason = "inactive_hours"
            return Decision(reason="inactive_hours", state=STATE_IDLE)

        qualified = (
            bool(present)
            and ratio >= cfg.min_person_ratio
            and self._centered_ok(centered)
        )

        if present:
            self.last_person_at = now
        elif self.last_person_at is not None and now - self.last_person_at >= cfg.leave_seconds:
            # 人走了：清空累积；冷却时间**保留** —— 同一个人来回走动不该被反复招呼
            self._score = 0.0
            self._first_hit = None
            self.last_person_at = None

        if qualified:
            if self._first_hit is None:
                self._first_hit = now
            self._score = min(SCORE_CAP, self._score + 1.0)
        elif self._score > 0:
            self._score = max(0.0, self._score - MISS_PENALTY)
            if self._score == 0.0:
                self._first_hit = None

        # 冷却优先于一切（包括速率限制），避免冷却期里反复算
        if now < self._cooldown_until:
            self._reason = "cooldown"
            return Decision(
                reason="cooldown",
                state=STATE_COOLDOWN,
                next_allowed_in=self._cooldown_until - now,
            )

        if not qualified:
            if not present:
                reason = "no_person"
            elif ratio < cfg.min_person_ratio:
                reason = "too_far"
            else:
                reason = "off_center"
            self._reason = reason
            return Decision(reason=reason, state=self._state(now))

        warming = (
            self._score < cfg.trigger_frames
            or self._first_hit is None
            or (now - self._first_hit) < cfg.trigger_seconds
        )
        if warming:
            self._reason = "warming"
            return Decision(reason="warming", state=STATE_SEEING)

        self._prune(now)
        if len(self._greet_times) >= cfg.max_per_hour:
            # 打满配额：清空累积，等人走开再说
            self._score = 0.0
            self._first_hit = None
            self._reason = "rate_limited"
            return Decision(
                reason="rate_limited",
                state=STATE_SEEING,
                next_allowed_in=max(0.0, (self._greet_times[0] + 3600.0) - now),
            )

        self._mark_greeted(now)
        return Decision(
            greet=True,
            reason="ok",
            gesture=cfg.gesture,
            state=STATE_COOLDOWN,
            next_allowed_in=cfg.cooldown_seconds,
        )

    def _mark_greeted(self, now: float) -> None:
        self._greet_times.append(now)
        self._cooldown_until = now + self.cfg.cooldown_seconds
        self._score = 0.0
        self._first_hit = None
        self._reason = "ok"
        self.greet_count += 1
        self.last_greet_at = now

    def force(self, now: Optional[float] = None) -> Decision:
        """手动触发一次（网页"试一次"/MCP 调试用），绕过检测与限速"""
        now = self._clock() if now is None else float(now)
        self._mark_greeted(now)
        return Decision(
            greet=True,
            reason="manual",
            gesture=self.cfg.gesture,
            state=STATE_COOLDOWN,
            next_allowed_in=self.cfg.cooldown_seconds,
        )

    # ---------------------------------------------------------------- 查询

    def snapshot(self, now: Optional[float] = None) -> Dict[str, Any]:
        now = self._clock() if now is None else float(now)
        self._prune(now)
        return {
            "state": self._state(now),
            "reason": self._reason,
            "score": round(self._score, 2),
            "trigger_frames": self.cfg.trigger_frames,
            "next_allowed_in": round(max(0.0, self._cooldown_until - now), 1),
            "greet_count": self.greet_count,
            "last_greet_at": self.last_greet_at,
            "last_person_at": self.last_person_at,
            "last_hour_count": len(self._greet_times),
            "max_per_hour": self.cfg.max_per_hour,
            "in_active_hours": in_active_hours(self.cfg.active_hours, now),
            "active_hours": self.cfg.active_hours,
        }
