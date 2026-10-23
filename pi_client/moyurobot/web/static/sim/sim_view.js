/**
 * SO-ARM100 三维视图（可复用模块）
 *
 * 只做一件事：给一个容器，把机械臂按给定的关节角画出来。它不认识滑块、
 * 不认识 /control，也不自己决定谁在驱动——由宿主页面喂角度进来。
 *
 * 三个消费方共用这一份：
 *   - 控制页的悬浮窗（static/sim/sim_overlay.js）
 *   - 独立仿真页 /sim（static/sim/sim.js，自带滑块）
 * 以前这两处各写一遍 3D + 取景 + 主臂，改一处忘一处，合并成这里。
 *
 * 角度口径：lerobot 用「中点 0」的度数（夹爪走 0-100 归一化），URDF 用「中点
 * 180°」，所以 simDeg = 180 + lerobotDeg。
 */

import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import URDFLoader from "/static/sim/vendor/urdf-loader/URDFLoader.js";
import { LeaderSerial } from "/static/sim/leader_serial.js";

/** 六个关节的定义，行程与 index.html 滑块、gesture_control.js、actions.py 保持一致 */
export const JOINTS = [
  { key: "arm_shoulder_pan.pos", urdf: "Rotation",    id: 1, min: -85,  max: 85,  label: "肩部水平" },
  { key: "arm_shoulder_lift.pos", urdf: "Pitch",      id: 2, min: -100, max: 100, label: "肩部垂直" },
  { key: "arm_elbow_flex.pos",   urdf: "Elbow",       id: 3, min: -100, max: 100, label: "肘关节" },
  { key: "arm_wrist_flex.pos",   urdf: "Wrist_Pitch", id: 4, min: -85,  max: 85,  label: "腕部弯曲" },
  { key: "arm_wrist_roll.pos",   urdf: "Wrist_Roll",  id: 5, min: -70,  max: 70,  label: "腕部旋转" },
  { key: "arm_gripper.pos",      urdf: "Jaw",         id: 6, min: -10,  max: 90,  label: "夹爪", mode: "0-100" },
];

const MODEL_BASES = {
  cdn: "https://image.mycodebro.cn/moyurobot/sim",
  local: "/static/sim/models",
};

const NEUTRAL_DEG = 180;
const OFFSET_KEY = "moyurobot.sim.offsets";

function pickModelBase() {
  if (window.SIM_MODEL_BASE) return window.SIM_MODEL_BASE;
  const h = location.hostname;
  const isLan =
    h === "localhost" || h === "127.0.0.1" || h === "::1" ||
    /^(192\.168\.|10\.|172\.(1[6-9]|2\d|3[01])\.)/.test(h);
  return isLan ? MODEL_BASES.local : MODEL_BASES.cdn;
}

const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));

export class SimView {
  /**
   * @param {HTMLElement} container 尺寸由外部决定（会跟随 ResizeObserver 自适应）
   * @param {object} opts
   *   onStatus(text, kind)          加载状态 ("idle" | "ok" | "warn" | "error")
   *   onAngles(values)              主臂接管时把最新角度回吐给宿主
   *   onLeaderState(connected)
   *   onLeaderStats({hz, rtt, errors, clamped})
   */
  constructor(container, opts = {}) {
    this.container = container;
    this.opts = opts;

    this.values = Object.fromEntries(JOINTS.map((j) => [j.key, 0]));
    this.invert = {};
    // 主臂零点微调：装配错齿、量程不对称导致「主臂在 0、从臂不在 0」时，
    // 在这里补一个常数偏移。正值 = 把这个关节往正方向掰。
    this.offsets = {};
    try {
      Object.assign(this.offsets, JSON.parse(localStorage.getItem(OFFSET_KEY) || "{}"));
    } catch { /* 忽略坏数据 */ }
    this.jointLimits = {};
    this.robot = null;
    this.fittedOnce = false;
    this.fitLocked = false;
    this.paused = false;
    this.disposed = false;
    this.failedUrls = [];
    this.loadedItems = 0;
    this.totalItems = 0;

    this.leader = new LeaderSerial();
    this.leaderTimer = null;
    this.leaderOn = false;
    this.stats = { frames: 0, t0: 0, errors: 0, hz: 0, rtt: 0, clamped: 0 };

    this._initScene();
  }

  // ------------------------------------------------------------ 场景

  _initScene() {
    const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    renderer.shadowMap.enabled = true;
    // updateStyle 必须为 true（默认）。传 false 时 three 不写 canvas 的 CSS 尺寸，
    // 浏览器会用属性尺寸当布局尺寸，DPR=2 的屏幕上画布直接变成两倍大盖住旁边的面板。
    renderer.domElement.style.display = "block";
    renderer.domElement.style.width = "100%";
    renderer.domElement.style.height = "100%";
    this.container.appendChild(renderer.domElement);
    this.renderer = renderer;

    const scene = new THREE.Scene();
    this.scene = scene;

    this.camera = new THREE.PerspectiveCamera(35, 1, 0.1, 500);
    this.camera.position.set(-1.4, 0.9, 1.4);

    this.controls = new OrbitControls(this.camera, renderer.domElement);
    this.controls.target.set(0, 0.18, 0);
    this.controls.enableDamping = true;
    this.controls.dampingFactor = 0.08;

    scene.add(new THREE.HemisphereLight(0xbcd4ff, 0x14202c, 1.15));
    const key = new THREE.DirectionalLight(0xffffff, 2.1);
    key.position.set(3, 6, 4);
    key.castShadow = true;
    key.shadow.mapSize.set(1024, 1024);
    scene.add(key);
    const fill = new THREE.DirectionalLight(0x88aaff, 0.65);
    fill.position.set(-4, 2, -3);
    scene.add(fill);

    const grid = new THREE.GridHelper(4, 32, 0x1b2734, 0x131c26);
    grid.position.y = -0.001;
    scene.add(grid);

    const floor = new THREE.Mesh(
      new THREE.PlaneGeometry(6, 6),
      new THREE.ShadowMaterial({ opacity: 0.35 })
    );
    floor.rotation.x = -Math.PI / 2;
    floor.receiveShadow = true;
    scene.add(floor);

    this._ro = new ResizeObserver(() => this.resize());
    this._ro.observe(this.container);
    this.resize();

    this._loop = this._loop.bind(this);
    requestAnimationFrame(this._loop);
  }

  resize() {
    const w = this.container.clientWidth;
    const h = this.container.clientHeight;
    if (!w || !h) return;
    this.renderer.setSize(w, h);
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
  }

  _loop() {
    if (this.disposed) return;
    requestAnimationFrame(this._loop);
    // 暂停时不渲染但仍排队下一帧，恢复时立刻接上
    if (this.paused) return;
    this.controls.update();
    this.renderer.render(this.scene, this.camera);
  }

  /** 悬浮窗收起 / 标签页隐藏时调用，别和白天的视频流抢 GPU */
  setPaused(v) {
    this.paused = !!v;
  }

  // ------------------------------------------------------------ 模型

  async init() {
    const primary = pickModelBase();
    const secondary = primary === MODEL_BASES.cdn ? MODEL_BASES.local : MODEL_BASES.cdn;

    for (const base of [primary, secondary]) {
      this.failedUrls.length = 0;
      try {
        const model = await this._loadURDF(base);
        this.MODEL_BASE = base;
        this._initRobot(model);
        return true;
      } catch (err) {
        console.warn(`[sim] ${base} 取不到 URDF，换下一个：${err?.message || err}`);
      }
    }
    this._status("URDF 两个来源都取不到（CDN 与本地均失败）", "error");
    return false;
  }

  _loadURDF(base) {
    return new Promise((resolve, reject) => {
      const manager = new THREE.LoadingManager();
      const loader = new URDFLoader(manager);
      this._manager = manager;

      manager.onStart = (url, loaded, total) => {
        this.totalItems = total;
        this._status(`加载网格 0/${total}`);
      };
      manager.onProgress = (url, loaded, total) => {
        this.totalItems = total;
        this.loadedItems = loaded;
        this._status(`加载网格 ${loaded}/${total}`);
        // 先到几个就先摆个临时取景，别让人对着空画面干等；到齐后 onLoad 重算覆盖
        if (!this.fittedOnce && this._countMeshes() >= 3) this._fit({ provisional: true });
      };
      manager.onError = (url) => {
        this.failedUrls.push(url);
        this._status(`有 ${this.failedUrls.length} 个文件没取到`, "error");
      };
      manager.onLoad = () => this._fit();

      loader.load(`${base}/so101.urdf`, resolve, undefined,
        (err) => reject(err || new Error("URDF 加载失败")));
    });
  }

  _countMeshes() {
    let n = 0;
    this.robot?.traverse((c) => {
      if (c.isMesh && c.geometry?.attributes?.position) n++;
    });
    return n;
  }

  _initRobot(model) {
    this.robot = model;
    model.rotation.x = -Math.PI / 2; // ROS 的 Z 朝上 → three 的 Y 朝上
    this.scene.add(model);
    model.updateMatrixWorld(true);

    for (const [name, joint] of Object.entries(model.joints || {})) {
      if (joint.jointType === "revolute" || joint.jointType === "continuous") {
        this.jointLimits[name] = {
          lower: joint.limit?.lower !== undefined ? Number(joint.limit.lower) : -Math.PI,
          upper: joint.limit?.upper !== undefined ? Number(joint.limit.upper) : Math.PI,
        };
      }
    }
    this._fit({ provisional: true });
  }

  /**
   * 归一化尺寸 + 取景。幂等：每次从 scale=1、位移 0 重新量，可以反复调用。
   * 网格逐个到达，先在 3 个时临时取景，到齐（onLoad）再算终态。
   */
  _fit({ provisional = false } = {}) {
    if (!this.robot || this.fitLocked) return;
    if (this._countMeshes() < 3) return;
    if (!provisional) this.fitLocked = true;
    this.fittedOnce = true;

    this.robot.traverse((c) => {
      if (c.isMesh) {
        c.castShadow = true;
        c.receiveShadow = true;
      }
    });

    this.robot.scale.setScalar(1);
    this.robot.position.set(0, 0, 0);
    this.robot.updateMatrixWorld(true);

    // 先摆到当前角度再量。URDF 默认位（全 0）和 180° 中性位姿态差很远，
    // 先量再摆会把取景算小一圈。
    this._applyPose(this.values);
    this.robot.updateMatrixWorld(true);
    const neutralBox = new THREE.Box3().setFromObject(this.robot);

    // 取景要看整个扫掠范围：每个关节单独打到上下限各量一次，包围盒取并集
    const unionBox = new THREE.Box3().copy(neutralBox);
    const probe = { ...this.values };
    for (const joint of JOINTS) {
      for (const v of [joint.min, joint.max]) {
        probe[joint.key] = v;
        this._applyPose(probe);
        this.robot.updateMatrixWorld(true);
        unionBox.union(new THREE.Box3().setFromObject(this.robot));
      }
      probe[joint.key] = this.values[joint.key];
    }
    this._applyPose(this.values);
    this.robot.updateMatrixWorld(true);

    const rawSize = unionBox.getSize(new THREE.Vector3());
    const maxDim = Math.max(rawSize.x, rawSize.y, rawSize.z) || 1;
    const scale = 1.15 / maxDim;
    this.robot.scale.setScalar(scale);
    this.robot.updateMatrixWorld(true);

    const baseCenter = neutralBox.getCenter(new THREE.Vector3());
    this.robot.position.x -= baseCenter.x * scale;
    this.robot.position.z -= baseCenter.z * scale;
    this.robot.position.y -= neutralBox.min.y * scale;
    this.robot.updateMatrixWorld(true);

    const fitSize = rawSize.clone().multiplyScalar(scale);
    const unionCenter = unionBox.getCenter(new THREE.Vector3()).multiplyScalar(scale);
    const fitCenter = new THREE.Vector3(
      unionCenter.x - baseCenter.x * scale,
      fitSize.y * 0.45,
      unionCenter.z - baseCenter.z * scale
    );

    const vFov = THREE.MathUtils.degToRad(this.camera.fov);
    const hFov = 2 * Math.atan(Math.tan(vFov / 2) * Math.max(this.camera.aspect, 0.2));
    const distH = fitSize.y / 2 / Math.tan(vFov / 2);
    const distW = fitSize.x / 2 / Math.tan(hFov / 2);
    const dist = Math.max(distH, distW, 0.5) * 1.22;
    const dir = new THREE.Vector3(-0.52, 0.26, 0.81).normalize();
    this.camera.position.copy(fitCenter).addScaledVector(dir, dist);
    this.camera.near = Math.max(0.01, dist / 200);
    this.camera.far = dist * 30;
    this.camera.updateProjectionMatrix();
    this.controls.target.copy(fitCenter);
    this.controls.update();

    if (provisional) {
      this._status(`加载网格 ${this.loadedItems}/${this.totalItems}（已可预览）`);
      return;
    }
    if (this.failedUrls.length) {
      this._status(`模型已加载，但缺 ${this.failedUrls.length} 个部件`, "warn");
    } else {
      this._status("模型已加载", "ok");
    }
  }

  /** lerobot 口径 → 仿真关节角（弧度）。超出行程时被 URDF 限位夹住 */
  _toRad(joint, value) {
    const deg = NEUTRAL_DEG + (this.invert[joint.key] ? -value : value);
    let rad = (deg * Math.PI) / 180;
    const lim = this.jointLimits[joint.urdf];
    if (lim) rad = clamp(rad, lim.lower, lim.upper);
    return rad;
  }

  /** 指令值对应的实际角度（度），宿主可用它判断是否被夹住 */
  effectiveDeg(key, value) {
    const joint = JOINTS.find((j) => j.key === key);
    if (!joint) return null;
    return (this._toRad(joint, value) * 180) / Math.PI - NEUTRAL_DEG;
  }

  _applyPose(values) {
    if (!this.robot) return;
    for (const joint of JOINTS) {
      const j = this.robot.joints[joint.urdf];
      if (j) j.setJointValue(this._toRad(joint, values[joint.key] ?? 0));
    }
  }

  /** 宿主喂角度进来（唯一入口） */
  setValues(values) {
    Object.assign(this.values, values);
    this._applyPose(this.values);
  }

  setInvert(map) {
    Object.assign(this.invert, map || {});
    this._applyPose(this.values);
  }

  /**
   * 主臂体检：每个关节的原始值、量程、是否校准过、当前换算值
   * 「量程 = 0~4095」= 出厂默认 = 没校准过，此时零点没有物理意义
   */
  getDiagnostics() {
    return JOINTS.map((joint) => {
      const lim = this.leader.limits.get(joint.id) || {};
      const raw = this.lastRaw?.get(joint.id);
      return {
        key: joint.key,
        label: joint.label,
        id: joint.id,
        raw,
        min: lim.min,
        max: lim.max,
        mid: lim.mid,
        fallback: !!lim.fallback,
        uncalibrated: !!lim.uncalibrated,
        value: raw != null ? this.leader.toLerobot(joint.id, raw, joint.mode) : null,
        offset: this.offsets[joint.key] || 0,
      };
    });
  }

  /** 主臂零点微调（单位与滑块一致：度；夹爪是 0-100 的单位） */
  setOffset(key, value) {
    this.offsets[key] = Number(value) || 0;
    this._saveOffsets();
  }

  getOffsets() {
    return { ...this.offsets };
  }

  clearOffsets() {
    this.offsets = {};
    this._saveOffsets();
  }

  _saveOffsets() {
    try {
      localStorage.setItem(OFFSET_KEY, JSON.stringify(this.offsets));
    } catch { /* 忽略 */ }
  }

  resetView() {
    this.fitLocked = false;
    this.fittedOnce = false;
    this._fit();
  }

  // ------------------------------------------------------------ 主臂

  get leaderConnected() {
    return this.leaderOn;
  }

  async connectLeader() {
    await this.leader.connect(JOINTS.map((j) => j.id));
    this.leaderOn = true;
    this.stats = { frames: 0, t0: performance.now(), errors: 0, hz: 0, rtt: 0, clamped: 0 };

    const raw = JOINTS.filter((j) => this.leader.limits.get(j.id)?.uncalibrated);
    if (raw.length) {
      const names = raw.map((j) => `${j.label}(ID${j.id})`).join("、");
      this.opts.onLeaderWarn?.(
        `${names} 的量程还是出厂默认 0~4095，说明主臂没做过 lerobot 校准——` +
        `零点对不上是必然的，靠软件偏移只能压住一部分`
      );
    }

    this.opts.onLeaderState?.(true);
    this._leaderLoop();
    return this.leader.limits;
  }

  async disconnectLeader() {
    this.leaderOn = false;
    if (this.leaderTimer) {
      clearTimeout(this.leaderTimer);
      this.leaderTimer = null;
    }
    await this.leader.disconnect();
    this.opts.onLeaderState?.(false);
  }

  /** 自调度，不用 setInterval：一次读多久就跑多快，两拍永不重叠 */
  async _leaderLoop() {
    if (!this.leaderOn || this.disposed) return;
    try {
      const raw = await this.leader.readPositions();
      this.lastRaw = raw;
      let clamped = 0;
      for (const joint of JOINTS) {
        let v = this.leader.toLerobot(joint.id, raw.get(joint.id), joint.mode);
        if (v == null) continue;
        v += this.offsets[joint.key] || 0; // 零点微调
        // 主臂能读到 ±180°，但滑块行程只有 ±100 这一档，超出就夹住
        const c = clamp(v, joint.min, joint.max);
        if (Math.abs(c - v) > 0.5) clamped++;
        this.values[joint.key] = c;
      }
      this._applyPose(this.values);
      this.opts.onAngles?.({ ...this.values });

      this.stats.frames++;
      const now = performance.now();
      const dt = now - this.stats.t0;
      if (dt > 600) {
        this.stats.hz = (this.stats.frames / dt) * 1000;
        this.stats.rtt = this.leader.lastRtt;
        this.stats.clamped = clamped;
        this.stats.frames = 0;
        this.stats.t0 = now;
        this.opts.onLeaderStats?.({ ...this.stats });
      }
      this.leaderTimer = setTimeout(() => this._leaderLoop(), 0);
    } catch (err) {
      this.stats.errors++;
      this.opts.onLeaderStats?.({ ...this.stats });
      this.leaderTimer = setTimeout(() => this._leaderLoop(), 120); // 出错退避
    }
  }

  // ------------------------------------------------------------ 收尾

  _status(text, kind = "idle") {
    this.opts.onStatus?.(text, kind);
  }

  dispose() {
    this.disposed = true;
    this.disconnectLeader().catch(() => {});
    this._ro?.disconnect();
    this.controls?.dispose();
    this.robot?.traverse((c) => {
      if (c.isMesh) {
        c.geometry?.dispose();
        const m = c.material;
        (Array.isArray(m) ? m : [m]).forEach((x) => x?.dispose?.());
      }
    });
    this.renderer?.dispose();
    this.renderer?.domElement?.remove();
    this.scene?.clear();
  }
}
