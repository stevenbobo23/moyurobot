/**
 * 独立仿真页 /sim
 *
 * 3D 渲染与主臂读取都交给 sim_view.js（控制页的仿真悬浮窗也用同一份实现），
 * 这里只负责这一页自己的东西：滑块 UI、推送从臂、归位、读回真臂。
 */

import { SimView, JOINTS } from "/static/sim/sim_view.js";

window.__simBooted = true;

const LS_KEY = "moyurobot.sim.prefs";

const state = {
  values: Object.fromEntries(JOINTS.map((j) => [j.key, 0])),
  invert: Object.fromEntries(JOINTS.map((j) => [j.key, false])),
  pushEnabled: true,
};

// 夹爪默认半开，和 reset_arm 的 50 对齐
state.values["arm_gripper.pos"] = 30;

const $ = (id) => document.getElementById(id);

function loadPrefs() {
  try {
    const p = JSON.parse(localStorage.getItem(LS_KEY) || "{}");
    if (p.invert) Object.assign(state.invert, p.invert);
    if (typeof p.pushEnabled === "boolean") state.pushEnabled = p.pushEnabled;
  } catch { /* 忽略坏数据 */ }
}
function savePrefs() {
  try {
    localStorage.setItem(LS_KEY, JSON.stringify({ invert: state.invert, pushEnabled: state.pushEnabled }));
  } catch { /* 忽略 */ }
}

function setPushState(text, kind) {
  const n = $("push-state");
  if (!n) return;
  n.textContent = text;
  n.dataset.kind = kind;
}

function setLeaderState(text, kind) {
  const n = $("leader-state");
  if (!n) return;
  n.textContent = text;
  n.dataset.kind = kind;
}

// ---------------------------------------------------------------- 视图
const view = new SimView($("viewer"), {
  onStatus: (text, kind) => {
    const n = $("urdf-status");
    if (n) {
      n.textContent = text;
      n.dataset.kind = kind;
    }
    const m = /加载网格 (\d+)\/(\d+)/.exec(text);
    const bar = $("load-bar");
    const fill = $("load-bar-fill");
    if (bar && fill) {
      if (m) {
        bar.style.display = "block";
        fill.style.width = `${Math.round((+m[1] / +m[2]) * 100)}%`;
      } else if (kind === "ok" || kind === "warn") {
        bar.style.display = "none";
      }
    }
    if (kind === "error" && $("load-detail")) {
      $("load-detail").style.display = "block";
    }
  },
  onAngles: (values) => {
    Object.assign(state.values, values);
    refreshSliderUI();
    schedulePush();
  },
  onLeaderStats: ({ hz, rtt, errors, clamped }) => {
    $("leader-hz").textContent = hz.toFixed(1);
    $("leader-rtt").textContent = rtt.toFixed(1);
    $("leader-err").textContent = String(errors);
    const hint = $("leader-clamp");
    if (hint) hint.textContent = clamped ? `${clamped} 轴超行程已夹住` : "";
  },
  onLeaderState: (connected) => {
    const btn = $("btn-leader");
    if (!btn) return;
    btn.textContent = connected ? "断开主臂" : "连接主臂";
    btn.classList.toggle("danger", connected);
    setLeaderState(connected ? "已连接" : "未连接（滑块可用）", connected ? "ok" : "idle");
    refreshSliderUI();
  },
});

window.__simView = view; // 调试用

// ---------------------------------------------------------------- 推送
let pending = null;
let flushing = false;

/** 只保留最新一帧，不排队——网络抖动不会积压成越来越大的延迟 */
function schedulePush() {
  if (!state.pushEnabled) return;
  pending = { ...state.values };
  if (!flushing) flushLoop();
}

async function flushLoop() {
  flushing = true;
  while (pending) {
    const payload = pending;
    pending = null;
    const t0 = performance.now();
    try {
      const res = await fetch("/control", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
        signal: AbortSignal.timeout(400),
      });
      const data = await res.json();
      const ok = data.success;
      setPushState(
        ok ? `往返 ${(performance.now() - t0).toFixed(0)} ms` : data.message || "被拒绝",
        ok ? "ok" : "warn"
      );
    } catch (err) {
      setPushState("发送失败：" + (err?.message || err), "error");
    }
  }
  flushing = false;
}

// ---------------------------------------------------------------- 滑块 UI
const sliderEls = {};

function buildSliders() {
  const host = $("sliders");
  host.innerHTML = "";
  for (const joint of JOINTS) {
    const row = document.createElement("div");
    row.className = "joint-row";
    row.innerHTML = `
      <div class="joint-head">
        <span class="joint-name">${joint.label}</span>
        <span class="joint-val" data-val="${joint.key}">0.0</span>
        <button class="joint-invert" title="反向" data-invert="${joint.key}">±</button>
      </div>
      <input type="range" min="${joint.min}" max="${joint.max}" step="1"
             value="${state.values[joint.key]}" data-key="${joint.key}">
    `;
    const input = row.querySelector("input");
    const valEl = row.querySelector("[data-val]");
    const invBtn = row.querySelector("[data-invert]");

    input.addEventListener("input", () => {
      if (view.leaderConnected) return; // 主臂接管时不接受滑块
      state.values[joint.key] = parseFloat(input.value);
      view.setValues(state.values);
      refreshSliderUI();
      schedulePush();
    });

    invBtn.addEventListener("click", () => {
      state.invert[joint.key] = !state.invert[joint.key];
      invBtn.classList.toggle("on", state.invert[joint.key]);
      view.setInvert(state.invert);
      savePrefs();
    });
    invBtn.classList.toggle("on", state.invert[joint.key]);

    host.appendChild(row);
    sliderEls[joint.key] = { input, valEl };
  }
  refreshSliderUI();
}

function refreshSliderUI() {
  for (const joint of JOINTS) {
    const el = sliderEls[joint.key];
    if (!el) continue;
    const v = state.values[joint.key];
    el.valEl.textContent = v.toFixed(1);
    el.input.value = String(Math.min(joint.max, Math.max(joint.min, v)));
    el.input.disabled = view.leaderConnected;

    // 指令值和 URDF 能表达的角度不完全一致（肘 +100、夹爪 -10 都超），被夹住时标黄
    const eff = view.effectiveDeg(joint.key, v);
    const clamped = eff != null && Math.abs(eff - v) > 0.5;
    el.valEl.dataset.clamped = clamped ? "1" : "0";
    el.valEl.title = clamped ? `模型限位只到 ${eff.toFixed(1)}°` : "";
  }
}

// ---------------------------------------------------------------- 零点微调
function buildOffsets() {
  const host = $("offsets");
  host.innerHTML = "";
  for (const joint of JOINTS) {
    const row = document.createElement("div");
    row.className = "offset-row";
    row.innerHTML = `
      <span class="offset-name">${joint.label}</span>
      <button data-dir="-1" title="−1（Shift 为 −10）">−</button>
      <span class="offset-val" data-o="${joint.key}">0.0</span>
      <button data-dir="1" title="+1（Shift 为 +10）">+</button>
    `;
    const valEl = row.querySelector("[data-o]");
    const paint = () => {
      const v = view.getOffsets()[joint.key] || 0;
      valEl.textContent = v.toFixed(1);
      valEl.style.color = Math.abs(v) > 0.05 ? "#fbbf24" : "#38bdf8";
    };
    row.querySelectorAll("button").forEach((btn) => {
      btn.addEventListener("click", (e) => {
        const step = (e.shiftKey ? 10 : 1) * Number(btn.dataset.dir);
        const cur = view.getOffsets()[joint.key] || 0;
        view.setOffset(joint.key, Math.round((cur + step) * 10) / 10);
        paint();
      });
    });
    paint();
    host.appendChild(row);
  }
}

$("btn-clear-offsets").addEventListener("click", () => {
  view.clearOffsets();
  buildOffsets();
});

// ---------------------------------------------------------------- 绑定
$("btn-leader").addEventListener("click", async () => {
  const btn = $("btn-leader");
  btn.disabled = true;
  try {
    if (view.leaderConnected) {
      await view.disconnectLeader();
    } else {
      btn.textContent = "连接中…";
      await view.connectLeader();
      const miss = JOINTS.filter((j) => view.leader.limits.get(j.id)?.fallback).map((j) => j.id);
      setLeaderState(
        miss.length ? `已连接（ID ${miss.join(",")} 未读到量程）` : "已连接",
        miss.length ? "warn" : "ok"
      );
    }
  } catch (err) {
    setLeaderState("连接失败：" + (err?.message || err), "error");
    btn.textContent = "连接主臂";
  } finally {
    btn.disabled = false;
  }
});

$("push-toggle").addEventListener("change", (e) => {
  state.pushEnabled = e.target.checked;
  savePrefs();
  setPushState(state.pushEnabled ? "待推送" : "已暂停", "idle");
});

$("btn-home").addEventListener("click", () => {
  if (view.leaderConnected) return;
  for (const j of JOINTS) state.values[j.key] = j.mode === "0-100" ? 30 : 0;
  view.setValues(state.values);
  refreshSliderUI();
  schedulePush();
});

$("btn-read-follower").addEventListener("click", async () => {
  try {
    const res = await fetch("/status");
    const data = await res.json();
    if (data?.current_action) {
      for (const j of JOINTS) {
        const v = data.current_action[j.key];
        if (typeof v === "number") {
          state.values[j.key] = Math.min(j.max, Math.max(j.min, v));
        }
      }
      view.setValues(state.values);
      refreshSliderUI();
      setPushState("已同步真臂姿态", "ok");
    }
  } catch (err) {
    setPushState("读取失败：" + (err?.message || err), "error");
  }
});

// ---------------------------------------------------------------- 启动
loadPrefs();
buildSliders();
buildOffsets();
view.setInvert(state.invert);
$("push-toggle").checked = state.pushEnabled;
setPushState(state.pushEnabled ? "待推送" : "已暂停", "idle");
setLeaderState("加载模型…", "idle");
view.init().then((ok) => {
  if (!ok) return;
  view.setValues(state.values);
  if (!view.leaderConnected) setLeaderState("未连接（滑块可用）", "idle");
});
