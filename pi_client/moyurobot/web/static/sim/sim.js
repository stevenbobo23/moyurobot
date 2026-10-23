/**
 * SO-ARM100 仿真 + 主臂联动 + 推从臂
 *
 * 三条驱动源，优先级从高到低：
 *   1. 主臂（Web Serial）—— 连上就接管，滑块变成只读显示
 *   2. 滑块 —— 没连主臂时手动控制
 *   3. 从臂回读（可选）—— 空闲时把真臂姿态同步到仿真，避免画面和实物不符
 *
 * 角度口径：lerobot 用「中点 0」的度数（夹爪是 0~100），URDF 用「中点 180°」，
 * 所以 simDeg = 180 + lerobotDeg。详见 toSimDeg()。
 */

import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import URDFLoader from "/static/sim/vendor/urdf-loader/URDFLoader.js";
import { LeaderSerial } from "/static/sim/leader_serial.js";

// 给页面里的看门狗用：模块真跑起来了才置位，否则页面上只会一直显示「加载模型中」
window.__simBooted = true;

// ---------------------------------------------------------------- 关节定义
// slider 的 min/max 与 templates/index.html 中现有滑块保持一致，这样仿真和
// 真实从臂的动作范围完全对齐。
const JOINTS = [
  { key: "arm_shoulder_pan.pos", urdf: "Rotation",    id: 1, min: -60, max: 60, label: "肩部水平" },
  { key: "arm_shoulder_lift.pos", urdf: "Pitch",      id: 2, min: -55, max: 55, label: "肩部垂直" },
  { key: "arm_elbow_flex.pos",   urdf: "Elbow",       id: 3, min: -50, max: 50, label: "肘关节" },
  { key: "arm_wrist_flex.pos",   urdf: "Wrist_Pitch", id: 4, min: -70, max: 70, label: "腕部弯曲" },
  { key: "arm_wrist_roll.pos",   urdf: "Wrist_Roll",  id: 5, min: -70, max: 70, label: "腕部旋转" },
  { key: "arm_gripper.pos",      urdf: "Jaw",         id: 6, min: 0,   max: 60, label: "夹爪", mode: "0-100" },
];

const LS_KEY = "moyurobot.sim.prefs";

const state = {
  values: Object.fromEntries(JOINTS.map((j) => [j.key, j.min <= 0 ? 0 : j.min])),
  invert: Object.fromEntries(JOINTS.map((j) => [j.key, false])),
  pushEnabled: true,
  pushUrl: "/control",
  leaderConnected: false,
  followFollower: false,
};

// 夹爪默认半开，和 reset_arm 的 50 对齐
state.values["arm_gripper.pos"] = 30;

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

// ---------------------------------------------------------------- 场景
const canvasHost = document.getElementById("viewer");
const renderer = new THREE.WebGLRenderer({ antialias: true });
renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
renderer.shadowMap.enabled = true;
canvasHost.appendChild(renderer.domElement);

const scene = new THREE.Scene();
scene.background = new THREE.Color(0x0b0f14);

const camera = new THREE.PerspectiveCamera(35, 1, 0.1, 500);
camera.position.set(-1.4, 0.9, 1.4);

const controls = new OrbitControls(camera, renderer.domElement);
controls.target.set(0, 0.18, 0);
controls.enableDamping = true;
controls.dampingFactor = 0.08;

scene.add(new THREE.HemisphereLight(0xbcd4ff, 0x14202c, 1.1));
const key = new THREE.DirectionalLight(0xffffff, 2.2);
key.position.set(3, 6, 4);
key.castShadow = true;
key.shadow.mapSize.set(1024, 1024);
scene.add(key);
const fill = new THREE.DirectionalLight(0x88aaff, 0.7);
fill.position.set(-4, 2, -3);
scene.add(fill);

const grid = new THREE.GridHelper(4, 32, 0x1d2a38, 0x141d27);
grid.position.y = -0.001;
scene.add(grid);

const floor = new THREE.Mesh(
  new THREE.PlaneGeometry(6, 6),
  new THREE.ShadowMaterial({ opacity: 0.35 })
);
floor.rotation.x = -Math.PI / 2;
floor.receiveShadow = true;
scene.add(floor);

function resize() {
  const w = canvasHost.clientWidth;
  const h = canvasHost.clientHeight;
  if (!w || !h) return;
  renderer.setSize(w, h, false);
  camera.aspect = w / h;
  camera.updateProjectionMatrix();
}
new ResizeObserver(resize).observe(canvasHost);
resize();

// ---------------------------------------------------------------- URDF
let robot = null;
const jointLimits = {}; // urdf 名称 -> {lower, upper} 弧度

function setStatus(text, kind = "idle") {
  const el = document.getElementById("urdf-status");
  el.textContent = text;
  el.dataset.kind = kind;
}

const loader = new URDFLoader();
// URDFLoader 的 onComplete 在网格（STL）还没下载完时就回调了，此时包围盒是空的。
// 用 LoadingManager 等所有网格落地，再做归一化和取景。
const manager = new THREE.LoadingManager();
loader.manager = manager;
let fitted = false;
const failedUrls = [];

manager.onStart = (url, loaded, total) => setStatus(`加载网格 0/${total}`);
manager.onProgress = (url, loaded, total) => setStatus(`加载网格 ${loaded}/${total}`);
manager.onError = (url) => {
  failedUrls.push(url);
  console.error("[sim] 资源加载失败:", url);
  setStatus(`有 ${failedUrls.length} 个文件没取到`, "error");
};

// 看门狗：一直卡在「加载中」的时候，把真实原因写到页面上
setTimeout(() => {
  if (fitted) return;
  if (!robot) {
    setStatus("URDF 没取到（/static/sim/models/so101.urdf 404？）", "error");
  } else if (failedUrls.length) {
    setStatus(`网格缺失 ${failedUrls.length} 个，检查 /static/sim/models/assets/`, "error");
  } else {
    setStatus("网格迟迟没到齐（网络慢或路径不对）", "warn");
  }
  const detail = document.getElementById("load-detail");
  if (detail) {
    detail.style.display = "block";
    detail.textContent = failedUrls.length
      ? "失败的文件：\n" + failedUrls.slice(0, 6).join("\n")
      : "如果这是在预览面板里打开的，静态资源走的是 Flask 的 /static/，预览面板取不到——请用 http://<机器人IP>:8080/sim 打开。";
  }
}, 12000);

function fitAndStyle() {
  if (fitted || !robot) return;
  // 至少要有一个带几何体的 Mesh，否则说明网格还没挂上来
  let hasGeometry = false;
  robot.traverse((c) => {
    if (c.isMesh && c.geometry?.attributes?.position) hasGeometry = true;
  });
  if (!hasGeometry) return;
  fitted = true;

  const bodyMat = new THREE.MeshStandardMaterial({
    color: 0xd6dee8, metalness: 0.18, roughness: 0.55,
  });
  const accentMat = new THREE.MeshStandardMaterial({
    color: 0x2c3a4a, metalness: 0.3, roughness: 0.5,
  });
  robot.traverse((c) => {
    if (c.isMesh) {
      c.castShadow = true;
      c.receiveShadow = true;
      // 舵机件偏深色，结构件浅色，层次分得开
      c.material = /sts3215|motor_holder/.test(c.name || "") ? accentMat : bodyMat;
    }
  });

  // 关键顺序：先把关节摆到当前值再量包围盒。URDF 默认位（全 0）和 180° 中性位
  // 的姿态差很远，先量再摆会把取景算小一圈。
  applyToRobot();
  robot.updateMatrixWorld(true);
  const neutralBox = new THREE.Box3().setFromObject(robot);

  // 取景不能只看当前姿态——手臂摆到别的角度会更宽。把每个关节单独打到上下限
  // 各量一次，包围盒取并集，保证任何姿态都不出画。
  const unionBox = new THREE.Box3().copy(neutralBox);
  const probe = { ...state.values };
  for (const joint of JOINTS) {
    for (const v of [joint.min, joint.max]) {
      probe[joint.key] = v;
      setPose(probe);
      robot.updateMatrixWorld(true);
      unionBox.union(new THREE.Box3().setFromObject(robot));
    }
    probe[joint.key] = state.values[joint.key];
  }
  setPose(state.values);
  robot.updateMatrixWorld(true);

  const rawSize = unionBox.getSize(new THREE.Vector3());
  const maxDim = Math.max(rawSize.x, rawSize.y, rawSize.z) || 1;
  const scale = 1.15 / maxDim;
  console.debug("[sim] 包络尺寸", rawSize.toArray().map((v) => +v.toFixed(1)), "→ scale", scale.toFixed(6));
  robot.scale.setScalar(scale);
  robot.updateMatrixWorld(true);

  // 底座落地：用中性姿态的盒子定原点，别用包络（包络的下沿可能不是底座）
  const baseCenter = neutralBox.getCenter(new THREE.Vector3());
  robot.position.x -= baseCenter.x * scale;
  robot.position.z -= baseCenter.z * scale;
  robot.position.y -= neutralBox.min.y * scale;
  robot.updateMatrixWorld(true);

  const fitSize = rawSize.clone().multiplyScalar(scale);
  // 底座钉在原点（物理上它就摆在地上），但取景中心往手臂扫过的体积中心偏，
  // 否则手臂摆到一侧时会贴在画面边上
  const unionCenter = unionBox.getCenter(new THREE.Vector3()).multiplyScalar(scale);
  const fitCenter = new THREE.Vector3(
    unionCenter.x - baseCenter.x * scale,
    fitSize.y * 0.45,
    unionCenter.z - baseCenter.z * scale
  );

  // 标准取景：垂直 fov 反推距离，宽高都算进去，再留边
  const vFov = THREE.MathUtils.degToRad(camera.fov);
  const hFov = 2 * Math.atan(Math.tan(vFov / 2) * Math.max(camera.aspect, 0.2));
  const distH = fitSize.y / 2 / Math.tan(vFov / 2);
  const distW = fitSize.x / 2 / Math.tan(hFov / 2);
  const dist = Math.max(distH, distW, 0.5) * 1.22;
  const dir = new THREE.Vector3(-0.52, 0.26, 0.81).normalize();
  camera.position.copy(fitCenter).addScaledVector(dir, dist);
  camera.near = Math.max(0.01, dist / 200);
  camera.far = dist * 30;
  camera.updateProjectionMatrix();
  controls.target.copy(fitCenter);
  controls.update();
  console.debug("[sim] fitSize", fitSize.toArray().map((v) => +v.toFixed(2)),
    "aspect", camera.aspect.toFixed(2), "dist", dist.toFixed(2));

  if (failedUrls.length) {
    setStatus(`模型已加载，但缺 ${failedUrls.length} 个部件`, "warn");
  } else {
    setStatus("模型已加载", "ok");
  }
  const detail = document.getElementById("load-detail");
  if (detail) {
    if (failedUrls.length) {
      detail.style.display = "block";
      detail.textContent = "缺失的文件：\n" + failedUrls.slice(0, 8).join("\n");
    } else {
      detail.style.display = "none";
    }
  }
  applyToRobot();
}

manager.onLoad = fitAndStyle;

loader.load(
  "/static/sim/models/so101.urdf",
  (model) => {
    robot = model;
    // URDF 是 ROS 惯例（Z 朝上），转成 three 的 Y 朝上
    robot.rotation.x = -Math.PI / 2;
    scene.add(robot);
    robot.updateMatrixWorld(true);

    for (const [name, joint] of Object.entries(robot.joints || {})) {
      if (joint.jointType === "revolute" || joint.jointType === "continuous") {
        jointLimits[name] = {
          lower: joint.limit?.lower !== undefined ? Number(joint.limit.lower) : -Math.PI,
          upper: joint.limit?.upper !== undefined ? Number(joint.limit.upper) : Math.PI,
        };
      }
    }
    fitAndStyle(); // 网格若走缓存同步挂好，这里就能直接收尾
    // 兜底：万一 LoadingManager 没触发（比如缓存命中不计数），1.5 秒后再试
    setTimeout(fitAndStyle, 1500);
  },
  undefined,
  (err) => {
    console.error("URDF 加载失败", err);
    setStatus("模型加载失败：" + (err?.message || err), "error");
  }
);

/** lerobot 口径的值 → 仿真关节角（弧度） */
function toSimRad(joint, value) {
  const base = 180; // URDF 中性位 = 180°
  const deg = joint.mode === "0-100" ? base + value : base + (state.invert[joint.key] ? -value : value);
  let rad = (deg * Math.PI) / 180;
  const lim = jointLimits[joint.urdf];
  if (lim) rad = Math.min(lim.upper, Math.max(lim.lower, rad));
  return rad;
}

/** 按给定的一组 lerobot 值摆姿态（取景探测和正常渲染共用） */
function setPose(values) {
  if (!robot) return;
  for (const joint of JOINTS) {
    const j = robot.joints[joint.urdf];
    if (j) j.setJointValue(toSimRad(joint, values[joint.key]));
  }
}

function applyToRobot() {
  setPose(state.values);
}

function animate() {
  requestAnimationFrame(animate);
  controls.update();
  renderer.render(scene, camera);
}
animate();

// 调试用：window.__sim 里能拿到场景对象，方便在控制台量尺寸
window.__sim = { scene, camera, controls, state, JOINTS, get robot() { return robot; }, refreshSliderUI, applyToRobot };

// ---------------------------------------------------------------- 推送到从臂
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
      const res = await fetch(state.pushUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
        signal: AbortSignal.timeout(400),
      });
      const data = await res.json();
      if (!data.success) {
        setPushState(data.message || "被拒绝", "warn");
      } else {
        setPushState(`往返 ${(performance.now() - t0).toFixed(0)} ms`, "ok");
      }
    } catch (err) {
      setPushState("发送失败：" + (err?.message || err), "error");
    }
  }
  flushing = false;
}

function setPushState(text, kind) {
  const el = document.getElementById("push-state");
  if (!el) return;
  el.textContent = text;
  el.dataset.kind = kind;
}

// ---------------------------------------------------------------- 滑块 UI
const sliderHost = document.getElementById("sliders");
const sliderEls = {};

function buildSliders() {
  sliderHost.innerHTML = "";
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
      if (state.leaderConnected) return; // 主臂接管时不接受滑块
      state.values[joint.key] = parseFloat(input.value);
      valEl.textContent = state.values[joint.key].toFixed(1);
      applyToRobot();
      schedulePush();
    });

    invBtn.addEventListener("click", () => {
      state.invert[joint.key] = !state.invert[joint.key];
      invBtn.classList.toggle("on", state.invert[joint.key]);
      savePrefs();
      applyToRobot();
    });
    invBtn.classList.toggle("on", state.invert[joint.key]);

    sliderHost.appendChild(row);
    sliderEls[joint.key] = { input, valEl, invBtn };
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
    el.input.disabled = state.leaderConnected;
  }
}

// ---------------------------------------------------------------- 主臂
const leader = new LeaderSerial();
let leaderTimer = null;
let leaderStats = { frames: 0, t0: 0, rtt: 0, errors: 0 };

async function connectLeader() {
  const btn = document.getElementById("btn-leader");
  btn.disabled = true;
  btn.textContent = "连接中…";
  try {
    await leader.connect(JOINTS.map((j) => j.id));
    state.leaderConnected = true;
    leaderStats = { frames: 0, t0: performance.now(), rtt: 0, errors: 0 };
    document.getElementById("btn-leader").textContent = "断开主臂";
    document.getElementById("btn-leader").classList.add("danger");
    const miss = JOINTS.filter((j) => leader.limits.get(j.id)?.fallback).map((j) => j.id);
    setLeaderState(
      miss.length ? `已连接（ID ${miss.join(",")} 未读到量程，按满程处理）` : "已连接",
      miss.length ? "warn" : "ok"
    );
    leaderLoop();
  } catch (err) {
    setLeaderState("连接失败：" + (err?.message || err), "error");
    btn.textContent = "连接主臂";
  } finally {
    btn.disabled = false;
  }
}

async function disconnectLeader() {
  state.leaderConnected = false;
  if (leaderTimer) { clearTimeout(leaderTimer); leaderTimer = null; }
  await leader.disconnect();
  const btn = document.getElementById("btn-leader");
  btn.textContent = "连接主臂";
  btn.classList.remove("danger");
  setLeaderState("未连接（滑块可用）", "idle");
  refreshSliderUI();
}

function setLeaderState(text, kind) {
  const el = document.getElementById("leader-state");
  el.textContent = text;
  el.dataset.kind = kind;
}

const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));

/** 自调度，不用 setInterval：一次读多久就跑多快，绝不让两拍重叠 */
async function leaderLoop() {
  if (!state.leaderConnected) return;
  try {
    const raw = await leader.readPositions();
    let clamped = 0;
    for (const joint of JOINTS) {
      const v = leader.toLerobot(joint.id, raw.get(joint.id), joint.mode);
      if (v == null) continue;
      // 主臂能读到 ±180°，但控制台给的安全行程只有 ±60 这一档。超出就夹住，
      // 保证仿真看到的、发给真臂的，都还在原有安全范围内。
      const c = clamp(v, joint.min, joint.max);
      if (Math.abs(c - v) > 0.5) clamped++;
      state.values[joint.key] = c;
    }
    applyToRobot();
    refreshSliderUI();
    schedulePush();

    leaderStats.frames++;
    const now = performance.now();
    const dt = now - leaderStats.t0;
    if (dt > 800) {
      const hz = (leaderStats.frames / dt) * 1000;
      document.getElementById("leader-hz").textContent = hz.toFixed(1);
      document.getElementById("leader-rtt").textContent = leader.lastRtt.toFixed(1);
      const hint = document.getElementById("leader-clamp");
      if (hint) hint.textContent = clamped ? `${clamped} 轴超行程已夹住` : "";
      leaderStats.frames = 0;
      leaderStats.t0 = now;
    }
    leaderTimer = setTimeout(leaderLoop, 0);
  } catch (err) {
    leaderStats.errors++;
    document.getElementById("leader-err").textContent = String(leaderStats.errors);
    leaderTimer = setTimeout(leaderLoop, 120); // 出错退避，别把总线打死
  }
}

document.getElementById("btn-leader").addEventListener("click", () => {
  if (state.leaderConnected) disconnectLeader();
  else connectLeader();
});

// ---------------------------------------------------------------- 交互绑定
document.getElementById("push-toggle").addEventListener("change", (e) => {
  state.pushEnabled = e.target.checked;
  savePrefs();
  setPushState(state.pushEnabled ? "待推送" : "已暂停", "idle");
});

document.getElementById("btn-home").addEventListener("click", () => {
  if (state.leaderConnected) return;
  for (const j of JOINTS) state.values[j.key] = j.mode === "0-100" ? 30 : 0;
  applyToRobot();
  refreshSliderUI();
  schedulePush();
});

document.getElementById("btn-read-follower").addEventListener("click", async () => {
  // 把真臂当前姿态拉回仿真，避免画面和实物两张皮
  try {
    const res = await fetch("/status");
    const data = await res.json();
    if (data?.current_action) {
      for (const j of JOINTS) {
        if (typeof data.current_action[j.key] === "number") {
          state.values[j.key] = clamp(data.current_action[j.key], j.min, j.max);
        }
      }
      applyToRobot();
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
document.getElementById("push-toggle").checked = state.pushEnabled;
setPushState(state.pushEnabled ? "待推送" : "已暂停", "idle");
setLeaderState(LeaderSerial.supported ? "未连接（滑块可用）" : "此浏览器不支持 Web Serial", LeaderSerial.supported ? "idle" : "error");
if (!LeaderSerial.supported) {
  document.getElementById("btn-leader").disabled = true;
  document.getElementById("btn-leader").title = "请用 Chrome / Edge 打开";
}
