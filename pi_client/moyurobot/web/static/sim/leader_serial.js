/**
 * 主臂串口读取（Web Serial + Feetech STS3215，协议 0）
 *
 * 改编自 bambot 的 feetech.js（MIT，见 vendor/LICENSE-bambot.txt），针对
 * 「网页遥操作」这个用途改了三处：
 *
 *   1. 真 SYNC READ。bambot 的 syncReadPositions() 名字叫 sync，实现其实是
 *      对着每个 ID 各发一条 READ，6 个舵机 = 6 次 USB 往返。USB 转接板常有
 *      1~16ms 的 latency timer，6 次串起来帧率就掉到十几 Hz。这里用广播包
 *      （ID 0xFE）一次问 6 个，硬件连着回 6 帧，只吃一次往返。
 *
 *   2. 事务串行化。bambot 在 setInterval(10ms) 里直接 await 读串口，读慢了
 *      就两拍重叠，半双工总线上两个事务交错，应答会串台。这里所有事务走一条
 *      promise 链，永远只有一个在飞。
 *
 *   3. 读超时不用 Promise.race。bambot 的超时是拿 reader.read() 和定时器
 *      race，超时后那个 read() 仍然挂着，会把下一段数据吞掉（读串口最典型的
 *      bug）。这里改成常驻 pump 循环 + 缓冲区，超时只是「等不到就不等了」，
 *      一个 read 永远只有一份。
 *
 * 另外归一化口径和 lerobot 对齐：连上时读一次 Min/Max_Position_Limit，
 * 用 (raw - mid) * 360 / 4095 换算，和 lerobot 的 DEGREES 模式同源。bambot 用
 * raw * 360 / 4096，只有在舵机量程正好是满程 0~4095 时才和 lerobot 一致，
 * 校准过的主臂会有系统性偏移。
 */

const REG = {
  MIN_POS: 0x09, // 2 字节，紧跟其后就是 MAX_POS(0x0B)，一次读 4 字节拿全
  MAX_POS: 0x0b,
  PRESENT_POS: 0x38, // 2 字节
  TORQUE_ENABLE: 0x28, // 1 字节
};

const MAX_RES = 4095;
const SYNC_READ_INSTR = 0x82;
const READ_INSTR = 0x02;
const WRITE_INSTR = 0x03;
const BROADCAST_ID = 0xfe;

const checksum = (bytes) => {
  let s = 0;
  for (const b of bytes) s += b;
  return ~s & 0xff;
};

/** FF FF ID LEN INSTR PARAM... CK */
function buildPacket(id, instruction, params) {
  const body = [id, params.length + 2, instruction, ...params];
  return new Uint8Array([0xff, 0xff, ...body, checksum(body)]);
}

const pktRead = (id, addr, len) => buildPacket(id, READ_INSTR, [addr, len]);
const pktSyncRead = (ids, addr, len) => buildPacket(BROADCAST_ID, SYNC_READ_INSTR, [addr, len, ...ids]);
const pktWrite = (id, addr, data) => buildPacket(id, WRITE_INSTR, [addr, ...data]);

export class LeaderSerial {
  constructor() {
    this.port = null;
    this.reader = null;
    this.writer = null;
    this.pumping = false;
    this.buf = [];
    this.frames = [];
    this._chain = Promise.resolve();

    /** @type {Map<number, {min:number, max:number, mid:number}>} */
    this.limits = new Map();
    this.ids = [];
    this.lastRtt = 0;
  }

  static get supported() {
    return typeof navigator !== "undefined" && "serial" in navigator;
  }

  get isOpen() {
    return !!this.port && this.pumping;
  }

  async connect(ids, { baudRate = 1000000 } = {}) {
    if (!LeaderSerial.supported) {
      throw new Error("这个浏览器不支持 Web Serial，请用 Chrome 或 Edge（Safari / Firefox / 手机浏览器都不行）");
    }
    if (this.isOpen) return;

    this.ids = [...ids];
    this.port = await navigator.serial.requestPort();
    await this.port.open({ baudRate, dataBits: 8, stopBits: 1, parity: "none" });
    this.writer = this.port.writable.getWriter();
    this.reader = this.port.readable.getReader();
    this.pumping = true;
    this._pump();

    // 先读限位，拿到每个舵机的量程中点；读不到就用满程兜底
    await this.readLimits(this.ids);

    // 主臂必须能用手掰，扭矩一律松开
    for (const id of this.ids) {
      try {
        await this.setTorque(id, false);
      } catch {
        /* 个别舵机不响应不该挡住整体 */
      }
    }
  }

  async disconnect() {
    this.pumping = false;
    // 先掐 reader，pump 里 pending 的那次 read 会以 done 结束
    try { await this.reader?.cancel(); } catch { /* noop */ }
    try { this.reader?.releaseLock(); } catch { /* noop */ }
    try { this.writer?.releaseLock(); } catch { /* noop */ }
    try { await this.port?.close(); } catch { /* noop */ }
    this.port = this.reader = this.writer = null;
    this.buf = [];
    this.frames = [];
  }

  // ---------------------------------------------------------------- 底层

  async _pump() {
    try {
      while (this.pumping) {
        const { value, done } = await this.reader.read();
        if (done) break;
        if (value && value.length) {
          for (const b of value) this.buf.push(b);
          this._extract();
        }
      }
    } catch (err) {
      if (this.pumping) console.warn("[leader] 串口读取中断:", err);
    }
  }

  /** 把缓冲区里能切的完整状态帧切出来，推进 this.frames */
  _extract() {
    for (;;) {
      let start = -1;
      for (let i = 0; i + 1 < this.buf.length; i++) {
        if (this.buf[i] === 0xff && this.buf[i + 1] === 0xff) { start = i; break; }
      }
      if (start < 0) { this.buf.length = 0; return; }
      if (start > 0) this.buf.splice(0, start);
      if (this.buf.length < 4) return;

      const len = this.buf[3];
      const total = 4 + len; // FF FF ID LEN + (ERR + DATA + CK)
      if (this.buf.length < total) return;

      const frame = this.buf.splice(0, total);
      if (checksum(frame.slice(2, total - 1)) !== frame[total - 1]) continue; // 校验错，丢
      this.frames.push({
        id: frame[2],
        err: frame[4],
        data: frame.slice(5, total - 1),
      });
    }
  }

  _waitFrames(n, timeoutMs) {
    const deadline = performance.now() + timeoutMs;
    return new Promise((resolve) => {
      const tick = () => {
        if (this.frames.length >= n) return resolve(this.frames.splice(0, n));
        if (performance.now() >= deadline) return resolve(this.frames.splice(0, this.frames.length));
        setTimeout(tick, 1);
      };
      tick();
    });
  }

  /** 一条事务：清缓冲 → 发包 → 等 n 帧。所有调用都排在同一条链上 */
  _tx(packet, n, timeoutMs = 40) {
    const run = async () => {
      this.buf.length = 0;
      this.frames.length = 0;
      const t0 = performance.now();
      await this.writer.write(packet);
      const frames = await this._waitFrames(n, timeoutMs);
      this.lastRtt = performance.now() - t0;
      return frames;
    };
    const next = this._chain.then(run, run);
    // 出错也要把链断开，否则一次异常会毒死后续所有事务
    this._chain = next.catch(() => {});
    return next;
  }

  // ---------------------------------------------------------------- 业务

  async readLimits(ids) {
    for (const id of ids) {
      const frames = await this._tx(pktRead(id, REG.MIN_POS, 4), 1, 40).catch(() => []);
      const f = frames[0];
      if (!f || f.data.length < 4) {
        this.limits.set(id, { min: 0, max: MAX_RES, mid: MAX_RES / 2, fallback: true });
        continue;
      }
      const min = (f.data[1] << 8) | f.data[0];
      const max = (f.data[3] << 8) | f.data[2];
      this.limits.set(id, { min, max, mid: (min + max) / 2, fallback: false });
    }
    return this.limits;
  }

  /** 一次广播读回所有 ID 的原始位置 */
  async readPositions(ids = this.ids) {
    const frames = await this._tx(pktSyncRead(ids, REG.PRESENT_POS, 2), ids.length, 40);
    const raw = new Map();
    for (const f of frames) {
      if (f.data.length >= 2) raw.set(f.id, (f.data[1] << 8) | f.data[0]);
    }
    return raw;
  }

  async setTorque(id, enable) {
    await this._tx(pktWrite(id, REG.TORQUE_ENABLE, [enable ? 1 : 0]), 1, 40);
  }

  /** raw → 角度，口径和 lerobot 的 DEGREES 一致 */
  angleOf(id, raw) {
    if (raw == null) return null;
    const lim = this.limits.get(id);
    const mid = lim ? lim.mid : MAX_RES / 2;
    return ((raw - mid) * 360) / MAX_RES;
  }

  /**
   * raw → lerobot 口径的关节值。
   * mode='degrees' → 与 lerobot MotorNormMode.DEGREES 同源（中点 0，±180）
   * mode='0-100'   → 与 lerobot MotorNormMode.RANGE_0_100 同源（夹爪用这个）
   */
  toLerobot(id, raw, mode) {
    if (raw == null) return null;
    const lim = this.limits.get(id);
    const min = lim ? lim.min : 0;
    const max = lim ? lim.max : MAX_RES;
    if (mode === "0-100") {
      if (max === min) return 0;
      return ((raw - min) / (max - min)) * 100;
    }
    const mid = (min + max) / 2;
    return ((raw - mid) * 360) / MAX_RES;
  }

  /** 一次性读回 {id: 角度} ，供仿真和从臂共用 */
  async readAngles(ids = this.ids) {
    const raw = await this.readPositions(ids);
    const angles = new Map();
    for (const [id, value] of raw) angles.set(id, this.angleOf(id, value));
    return { raw, angles, rtt: this.lastRtt };
  }
}
