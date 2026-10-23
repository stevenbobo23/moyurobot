/**
 * 控制页的仿真悬浮窗
 *
 * 设计原则：**页面上只有一套机械臂控制**。悬浮窗只负责把 3D 画出来，
 * 角度来自页面现有的 .arm-slider；主臂接管时反过来把读数写回那些滑块。
 * 所以不存在「两个滑块控制同一根轴」的问题。
 *
 * 3D 与主臂读取都来自 sim_view.js，和独立页 /sim 共用同一份实现。
 */

import { SimView, JOINTS } from "/static/sim/sim_view.js";

const PANEL_ID = "sim-float";

let view = null;
let panel = null;
let body = null;
let pending = null;
let flushing = false;
let lastLeaderText = "";

function el(tag, style, html) {
  const n = document.createElement(tag);
  if (style) Object.assign(n.style, style);
  if (html != null) n.innerHTML = html;
  return n;
}

/** 页面上那六个滑块，按 data-joint 收集 */
function pageSliders() {
  return [...document.querySelectorAll("input.arm-slider[data-joint]")].map((input) => ({
    input,
    key: input.dataset.joint,
    label: document.getElementById(`${input.id}-value`),
  }));
}

function readPageValues() {
  const out = {};
  for (const s of pageSliders()) out[s.key] = parseFloat(s.input.value) || 0;
  return out;
}

/** 主臂读数写回页面滑块（不触发 change，避免 script.js 重复推送和弹通知） */
function writePageValues(values) {
  for (const s of pageSliders()) {
    const v = values[s.key];
    if (v == null) continue;
    s.input.value = String(v);
    if (s.label) s.label.textContent = v.toFixed(1);
  }
}

// ---------------------------------------------------------------- 推送
/** 只保留最新一帧：网络抖动不会积压成越来越大的延迟 */
function pushToArm(values) {
  if (!window.__simPushEnabled) return;
  pending = { ...values };
  if (!flushing) flush();
}

async function flush() {
  flushing = true;
  while (pending) {
    const payload = pending;
    pending = null;
    try {
      const res = await fetch("/control", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
        signal: AbortSignal.timeout(400),
      });
      const data = await res.json();
      setStatus(data.success ? "接管中" : data.message || "被拒绝", data.success ? "ok" : "warn");
    } catch (err) {
      setStatus("发送失败：" + (err?.message || err), "error");
    }
  }
  flushing = false;
}

// ---------------------------------------------------------------- 面板
function setStatus(text, kind = "idle") {
  const n = document.getElementById("sim-float-status");
  if (!n) return;
  n.textContent = text;
  const color = { ok: "#34d399", warn: "#fbbf24", error: "#f87171", idle: "#6b7280" }[kind] || "#6b7280";
  n.style.color = color;
}

function buildPanel() {
  const wrap = el("div", {
    position: "fixed",
    right: "20px",
    bottom: "20px",
    width: "460px",
    height: "380px",
    minWidth: "300px",
    minHeight: "240px",
    display: "flex",
    flexDirection: "column",
    background: "rgba(11,18,32,.97)",
    border: "1px solid #24303f",
    borderRadius: "12px",
    boxShadow: "0 18px 50px rgba(0,0,0,.55)",
    zIndex: "90",
    overflow: "hidden",
    resize: "both",
  });
  wrap.id = PANEL_ID;

  const head = el("div", {
    display: "flex",
    alignItems: "center",
    justifyContent: "space-between",
    gap: "8px",
    padding: "8px 10px",
    borderBottom: "1px solid #1b2432",
    cursor: "move",
    userSelect: "none",
    flex: "0 0 auto",
    background: "#0d1520",
  });

  const title = el("div", {
    fontSize: "11px",
    letterSpacing: ".08em",
    color: "#6b7686",
    fontWeight: "600",
    whiteSpace: "nowrap",
  }, "机械臂仿真");

  const right = el("div", { display: "flex", alignItems: "center", gap: "6px" });

  const leaderBtn = el("button", {
    fontSize: "10px",
    padding: "4px 9px",
    borderRadius: "999px",
    border: "1px solid #2b3a4d",
    background: "transparent",
    color: "#9aa6b6",
    cursor: "pointer",
    whiteSpace: "nowrap",
  }, "连接主臂");
  leaderBtn.id = "sim-float-leader";

  const closeBtn = el("button", {
    width: "20px",
    height: "20px",
    lineHeight: "1",
    borderRadius: "5px",
    border: "1px solid #2b3a4d",
    background: "transparent",
    color: "#7c8798",
    cursor: "pointer",
    fontSize: "13px",
  }, "×");
  closeBtn.title = "关闭（Esc）";

  right.append(leaderBtn, closeBtn);
  head.append(title, right);

  body = el("div", { position: "relative", flex: "1 1 auto", minHeight: "0", background: "#080c12" });
  const status = el("div", {
    position: "absolute",
    left: "10px",
    top: "8px",
    fontSize: "10px",
    color: "#6b7280",
    pointerEvents: "none",
    zIndex: "2",
  }, "加载模型中…");
  status.id = "sim-float-status";
  const hint = el("div", {
    position: "absolute",
    left: "10px",
    bottom: "8px",
    fontSize: "9.5px",
    color: "#4e5a69",
    pointerEvents: "none",
    zIndex: "2",
  }, "拖拽旋转 · 滚轮缩放");
  body.append(status, hint);

  wrap.append(head, body);
  document.body.appendChild(wrap);

  // 拖动
  let drag = null;
  head.addEventListener("pointerdown", (e) => {
    if (e.target.closest("button")) return;
    const r = wrap.getBoundingClientRect();
    drag = { dx: e.clientX - r.left, dy: e.clientY - r.top };
    wrap.style.right = "auto";
    wrap.style.bottom = "auto";
    wrap.style.left = `${r.left}px`;
    wrap.style.top = `${r.top}px`;
    head.setPointerCapture(e.pointerId);
  });
  head.addEventListener("pointermove", (e) => {
    if (!drag) return;
    wrap.style.left = `${Math.max(0, e.clientX - drag.dx)}px`;
    wrap.style.top = `${Math.max(0, e.clientY - drag.dy)}px`;
  });
  head.addEventListener("pointerup", (e) => {
    drag = null;
    head.releasePointerCapture?.(e.pointerId);
  });

  closeBtn.addEventListener("click", closeSimWindow);
  leaderBtn.addEventListener("click", toggleLeader);

  return wrap;
}

// ---------------------------------------------------------------- 主臂
async function toggleLeader() {
  const btn = document.getElementById("sim-float-leader");
  if (!view) return;
  if (view.leaderConnected) {
    await view.disconnectLeader();
    btn.textContent = "连接主臂";
    btn.style.background = "transparent";
    btn.style.color = "#9aa6b6";
    for (const s of pageSliders()) {
      s.input.disabled = false;
      s.input.style.opacity = "1";
    }
    setStatus("未连接（滑块可用）", "idle");
    return;
  }
  btn.textContent = "连接中…";
  btn.disabled = true;
  try {
    await view.connectLeader();
    btn.textContent = "断开主臂";
    btn.style.background = "#0c4a6e";
    btn.style.color = "#7dd3fc";
    for (const s of pageSliders()) {
      s.input.disabled = true;
      s.input.style.opacity = ".45";
    }
    setStatus("接管中", "ok");
  } catch (err) {
    btn.textContent = "连接主臂";
    setStatus("主臂连接失败：" + (err?.message || err), "error");
  } finally {
    btn.disabled = false;
  }
}

// ---------------------------------------------------------------- 生命周期
export async function openSimWindow() {
  if (!panel) {
    panel = buildPanel();
    view = new SimView(body, {
      onStatus: (text, kind) => setStatus(text, kind),
      // 主臂接管时：写回页面滑块 + 推给真臂
      onAngles: (values) => {
        writePageValues(values);
        pushToArm(values);
      },
      onLeaderStats: ({ hz, rtt, errors, clamped }) => {
        if (view?.leaderConnected) {
          const extra = clamped ? ` · ${clamped} 轴超行程已夹住` : "";
          const text = `接管中 ${hz.toFixed(0)} Hz · ${rtt.toFixed(0)} ms${extra}`;
          if (text !== lastLeaderText) {
            lastLeaderText = text;
            setStatus(text, clamped ? "warn" : "ok");
          }
        }
        if (errors) console.warn("[sim] 主臂读取错误累计", errors);
      },
      onLeaderState: () => {},
    });

    // 页面滑块一动，3D 立刻跟随（滑块本身就是唯一控制源）
    for (const s of pageSliders()) {
      s.input.addEventListener("input", () => {
        if (view.leaderConnected) return;
        view.setValues(readPageValues());
      });
    }

    await view.init();
    view.setValues(readPageValues());
    window.__simFloatView = view; // 调试用：可在控制台量尺寸、看是否真在跟
  }

  panel.style.display = "flex";
  panel.dataset.open = "1";
  view.setPaused(false);
  // 面板尺寸确定了才取景：首次创建时 init() 里那次 _fit 可能拿到了布局前的
  // aspect，画面会偏小、贴底。这里补一次 resize + 重新取景。
  view.resize();
  view.resetView();
  setStatus(view.leaderConnected ? "接管中" : "模型已加载", "ok");
}

export function closeSimWindow() {
  if (!panel) return;
  if (view?.leaderConnected) toggleLeader();
  panel.style.display = "none";
  panel.dataset.open = "0";
  // 收起就停止渲染，别和白天的视频流抢 GPU
  view?.setPaused(true);
}

export function toggleSimWindow() {
  if (panel && panel.dataset.open === "1") closeSimWindow();
  else openSimWindow();
}

// Esc 关闭
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && panel?.dataset.open === "1") closeSimWindow();
});

// 标签页隐藏时停渲染
document.addEventListener("visibilitychange", () => {
  if (!view) return;
  view.setPaused(document.hidden || panel?.dataset.open !== "1");
});

window.openSimWindow = openSimWindow;
window.closeSimWindow = closeSimWindow;
window.toggleSimWindow = toggleSimWindow;
