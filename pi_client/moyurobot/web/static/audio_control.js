/*
 * 摸鱼遥控车 - 网页端语音对讲
 *
 * 两条通道（都走同一条 WebSocket，文本帧控制 + 二进制帧 PCM）：
 *   1. 监听车端：车端麦克风 -> 浏览器（像云摄像头点开声音）
 *   2. 按住说话：浏览器麦克风 -> 车端扬声器（半双工对讲）
 *
 * 音频格式：S16_LE / 单声道 / 16kHz（服务端可配），浏览器侧做重采样后播放。
 * 上行链路需要浏览器授予麦克风权限，而浏览器只在"安全上下文"（HTTPS 或
 * localhost）才给权限 —— 用局域网 IP + http 打开时只有监听可用。
 */
(function () {
    "use strict";

    var CFG = window.MOYU_AUDIO || {};
    var SAMPLE_RATE = CFG.sample_rate || 16000;
    var CHUNK_MS = CFG.chunk_ms || 100;
    var TX_CHUNK_SAMPLES = Math.round((SAMPLE_RATE * CHUNK_MS) / 1000);
    var RECONNECT_DELAYS = [1000, 2000, 4000, 8000, 10000];

    var state = {
        ws: null,
        connected: false,
        closing: false,
        retryIndex: 0,
        reconnectTimer: null,
        monitorWanted: false,
        pttWanted: false,
        pttHeld: false,
        pttToken: 0,
        canMonitor: true,
        canTalk: false,
        volume: 1.0,
        playbackMuted: false,
        audioCtx: null,
        playGain: null,
        rx: newStream(),
        tx: newStream(),
        txPcm: [],
        micStream: null,
        micSource: null,
        micNode: null,
        micWorklet: null,
        micSink: null,
        captureMode: "",
        serverState: {},
        capabilities: null,
        hello: null,
        lastError: "",
        remoteMeterPeak: 0,
        localMeterPeak: 0,
        uiListen: "",
        uiPtt: ""
    };

    function newStream() {
        return { buf: new Float32Array(0), pos: 0 };
    }

    function el(id) {
        return document.getElementById(id);
    }

    function log(msg) {
        if (window.console && console.log) {
            console.log("[audio] " + msg);
        }
    }

    // ------------------------------------------------------------------ UI --

    function setBadge(text, tone) {
        var badge = el("audio-badge");
        if (!badge) return;
        var tones = {
            gray: "border-gray-700 text-gray-500",
            cyan: "border-tech-500/60 text-tech-400",
            green: "border-green-700 text-green-400",
            red: "border-red-700 text-red-400",
            amber: "border-amber-700 text-amber-400"
        };
        badge.className = "text-[10px] font-mono px-2 py-0.5 rounded border " + (tones[tone] || tones.gray);
        badge.textContent = text;
    }

    function setHint(text) {
        var node = el("audio-hint");
        if (node) node.innerHTML = text || "";
    }

    function setListenButton(on, enabled) {
        var btn = el("audio-listen-btn");
        var label = el("audio-listen-label");
        if (!btn) return;
        var signature = on + "|" + enabled;
        if (state.uiListen === signature) return;
        state.uiListen = signature;
        btn.disabled = !!enabled;
        if (label) label.textContent = on ? "关闭监听" : "监听车端";
        btn.className = on
            ? "h-12 rounded-lg border-2 border-tech-400 bg-tech-600 hover:bg-tech-500 text-white font-bold text-sm flex items-center justify-center gap-2 transition-all active:scale-95 disabled:opacity-40"
            : "h-12 rounded-lg border-2 border-gray-700 bg-gray-800 hover:bg-gray-700 text-gray-300 font-bold text-sm flex items-center justify-center gap-2 transition-all active:scale-95 disabled:opacity-40 disabled:cursor-not-allowed";
    }

    function setPttButton(active, enabled) {
        var btn = el("audio-ptt-btn");
        var label = el("audio-ptt-label");
        if (!btn) return;
        var signature = active + "|" + enabled;
        if (state.uiPtt === signature) return;
        state.uiPtt = signature;
        btn.disabled = !enabled;
        if (label) label.textContent = active ? "正在说话…" : "按住说话";
        btn.className = active
            ? "h-12 rounded-lg border-2 border-red-400 bg-red-600 text-white font-bold text-sm flex items-center justify-center gap-2 transition-all select-none"
            : (enabled
                ? "h-12 rounded-lg border-2 border-gray-600 bg-gray-800 hover:bg-gray-700 text-gray-200 font-bold text-sm flex items-center justify-center gap-2 transition-all active:scale-95 select-none"
                : "h-12 rounded-lg border-2 border-gray-800 bg-gray-900 text-gray-600 font-bold text-sm flex items-center justify-center gap-2 select-none cursor-not-allowed");
    }

    function setMeter(which, level) {
        var node = el(which === "remote" ? "audio-meter-remote" : "audio-meter-local");
        if (!node) return;
        var pct = Math.max(0, Math.min(100, Math.round(level * 140)));
        node.style.width = pct + "%";
    }

    // ------------------------------------------------------- WebSocket 连接 --

    function resolveWsUrl() {
        if (CFG.ws_url) return CFG.ws_url;
        var scheme = window.location.protocol === "https:" ? "wss:" : "ws:";
        var port = CFG.ws_port || 8081;
        return scheme + "//" + window.location.hostname + ":" + port + "/audio";
    }

    function connect() {
        if (state.ws && (state.ws.readyState === 0 || state.ws.readyState === 1)) return;
        var url = resolveWsUrl();
        if (!url) {
            setBadge("未配置", "red");
            return;
        }
        log("连接 " + url);
        try {
            state.ws = new WebSocket(url);
        } catch (e) {
            setBadge("连接失败", "red");
            return;
        }
        state.ws.binaryType = "arraybuffer";
        state.ws.onopen = function () {
            state.connected = true;
            state.retryIndex = 0;
            log("已连接");
            setBadge("已连接", "cyan");
            if (state.monitorWanted) sendJson({ type: "monitor", on: true });
        };
        state.ws.onmessage = function (ev) {
            if (typeof ev.data === "string") {
                handleJson(ev.data);
            } else {
                handleRemotePcm(ev.data);
            }
        };
        state.ws.onclose = function () {
            state.connected = false;
            log("连接断开");
            if (!state.closing && state.monitorWanted) {
                setBadge("重连中…", "amber");
                scheduleReconnect();
            } else {
                setBadge("未连接", "gray");
            }
        };
        state.ws.onerror = function () {
            log("WebSocket 错误");
        };
    }

    function scheduleReconnect() {
        if (state.reconnectTimer) return;
        var delay = RECONNECT_DELAYS[Math.min(state.retryIndex, RECONNECT_DELAYS.length - 1)];
        state.retryIndex += 1;
        state.reconnectTimer = window.setTimeout(function () {
            state.reconnectTimer = null;
            connect();
        }, delay);
    }

    function sendJson(payload) {
        if (state.ws && state.ws.readyState === 1) {
            state.ws.send(JSON.stringify(payload));
            return true;
        }
        return false;
    }

    function handleJson(raw) {
        var msg;
        try {
            msg = JSON.parse(raw);
        } catch (e) {
            return;
        }
        switch (msg.type) {
            case "hello":
                state.hello = msg;
                state.canMonitor = !!msg.can_monitor;
                state.canTalk = !!msg.can_talk;
                state.serverState = msg.state || {};
                if (msg.hints && msg.hints.length) setHint("⚠️ " + msg.hints.join(" "));
                refreshButtons();
                renderDevices(msg.options, msg.devices);
                break;
            case "state":
                state.serverState = msg;
                if (typeof msg.can_talk === "boolean") state.canTalk = msg.can_talk;
                updateFromServerState(msg);
                break;
            case "monitor":
                state.monitorWanted = !!msg.on;
                setListenButton(state.monitorWanted, !state.canMonitor);
                setBadge(state.monitorWanted ? "监听中" : "已连接", state.monitorWanted ? "green" : "cyan");
                break;
            case "ptt":
                state.pttWanted = !!msg.on;
                setPttButton(state.pttWanted, state.canTalk);
                if (state.pttWanted) setBadge("对讲中", "red");
                else if (state.monitorWanted) setBadge("监听中", "green");
                break;
            case "monitor_stopped":
                setPlaybackMuted(false);
                break;
            case "error":
                state.lastError = msg.message || "音频错误";
                setHint("❌ " + state.lastError);
                log("服务端错误: " + state.lastError);
                // 对讲被拒时立刻复位按钮，避免卡在"正在说话"
                if (state.pttWanted) stopPtt(true);
                break;
            case "devices":
                if (msg.devices) renderDevices(null, msg.devices);
                break;
            case "pong":
                log("pong");
                break;
            default:
                break;
        }
    }

    function updateFromServerState(msg) {
        if (typeof msg.level === "number" && state.monitorWanted) {
            setMeter("remote", msg.level);
            window.clearTimeout(state.remoteMeterPeak);
            state.remoteMeterPeak = window.setTimeout(function () { setMeter("remote", 0); }, 250);
        }
        // 有人说话时本地静音，防止车端喇叭的声音再被我方麦克风收回去（回授啸叫）
        setPlaybackMuted(!!msg.ptt);
        if (msg.error && msg.error !== state.lastError) {
            state.lastError = msg.error;
            setHint("❌ " + msg.error);
        }
        refreshButtons();
        if (state.monitorWanted && !state.pttWanted && !msg.ptt) setBadge("监听中", "green");
    }

    function refreshButtons() {
        setListenButton(state.monitorWanted, !state.canMonitor || !state.connected);
        setPttButton(state.pttWanted, state.canTalk && state.connected);
    }

    // ------------------------------------------------------- 车端声音播放 --

    function ensureAudioCtx() {
        if (state.audioCtx) return state.audioCtx;
        var Ctx = window.AudioContext || window.webkitAudioContext;
        if (!Ctx) return null;
        state.audioCtx = new Ctx();
        state.playGain = state.audioCtx.createGain();
        state.playGain.gain.value = state.volume;
        state.playGain.connect(state.audioCtx.destination);
        return state.audioCtx;
    }

    /**
     * 流式重采样：把 inRate 的 Float32 转成 outRate（保留跨包余量，避免抖动）
     * ratio = inRate / outRate（每个输出样本消耗的输入样本数）
     */
    function resampleStreaming(stream, input, ratio) {
        if (input && input.length) {
            var merged = new Float32Array(stream.buf.length + input.length);
            merged.set(stream.buf, 0);
            merged.set(input, stream.buf.length);
            stream.buf = merged;
        }
        var outCount = 0;
        var pos = stream.pos;
        while (pos + ratio + 1 <= stream.buf.length) {
            outCount += 1;
            pos += ratio;
        }
        if (outCount === 0) return new Float32Array(0);

        var out = new Float32Array(outCount);
        pos = stream.pos;
        for (var i = 0; i < outCount; i += 1) {
            var i0 = Math.floor(pos);
            var frac = pos - i0;
            var a = stream.buf[i0];
            var b = stream.buf[i0 + 1];
            out[i] = a + (b - a) * frac;
            pos += ratio;
        }
        var consumed = Math.floor(pos);
        if (consumed > 0) {
            stream.buf = stream.buf.slice(consumed);
            stream.pos = pos - consumed;
        } else {
            stream.pos = pos;
        }
        return out;
    }

    function handleRemotePcm(arrayBuffer) {
        if (!state.monitorWanted) return;
        var ctx = state.audioCtx;
        if (!ctx || ctx.state === "suspended") return;
        var i16 = new Int16Array(arrayBuffer);
        if (!i16.length) return;
        var f32 = new Float32Array(i16.length);
        for (var i = 0; i < i16.length; i += 1) {
            f32[i] = i16[i] / 32768;
        }
        var out = resampleStreaming(state.rx, f32, SAMPLE_RATE / ctx.sampleRate);
        if (out.length) schedulePlayback(out);
    }

    var playCursor = 0;

    function schedulePlayback(f32) {
        var ctx = state.audioCtx;
        if (!ctx || !f32.length) return;
        var buffer = ctx.createBuffer(1, f32.length, ctx.sampleRate);
        if (buffer.copyToChannel) buffer.copyToChannel(f32, 0);
        else buffer.getChannelData(0).set(f32);

        var when = playCursor;
        var now = ctx.currentTime;
        if (when < now) when = now + 0.08;               // 断流后重新对齐
        if (when - now > 1.0) when = now + 0.08;         // 积压太多就丢帧保实时

        var source = ctx.createBufferSource();
        source.buffer = buffer;
        source.connect(state.playGain);
        source.start(when);
        playCursor = when + buffer.duration;
    }

    function setPlaybackMuted(muted) {
        state.playbackMuted = !!muted;
        if (state.playGain) {
            state.playGain.gain.value = muted ? 0 : state.volume;
        }
        if (muted) {
            playCursor = 0;
            state.rx = newStream();
        }
    }

    // ------------------------------------------------------------ 监听开关 --

    function toggleMonitor() {
        if (!state.canMonitor) return;
        if (!state.connected) {
            connect();
            state.monitorWanted = true;
            setListenButton(true, true);
            setBadge("连接中…", "amber");
            return;
        }
        var next = !state.monitorWanted;
        var ctx = ensureAudioCtx();
        if (next && ctx) {
            if (ctx.state === "suspended") ctx.resume();
            playCursor = 0;
            state.rx = newStream();
        }
        if (next && !ctx) {
            setHint("❌ 当前浏览器不支持 Web Audio，无法播放车端声音");
            return;
        }
        state.monitorWanted = next;
        setListenButton(next, false);
        if (next) setBadge("监听中", "green");
        sendJson({ type: "monitor", on: next });
    }

    // ------------------------------------------------------------ 对讲上行 --

    function ensureMic() {
        if (state.micStream) return Promise.resolve(state.micStream);
        if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
            return Promise.reject(new Error(
                "浏览器禁止在非安全上下文使用麦克风：请用 HTTPS 打开（或用 localhost）"
            ));
        }
        return navigator.mediaDevices.getUserMedia({
            audio: {
                channelCount: 1,
                echoCancellation: true,
                noiseSuppression: true,
                autoGainControl: true
            },
            video: false
        }).then(function (stream) {
            state.micStream = stream;
            return stream;
        });
    }

    function startMicPipeline() {
        var ctx = ensureAudioCtx();
        if (!ctx) return Promise.reject(new Error("浏览器不支持 Web Audio"));
        if (ctx.state === "suspended") ctx.resume();
        return ensureMic().then(function (stream) {
            if (state.micNode || state.micWorklet) return;
            state.micSource = ctx.createMediaStreamSource(stream);
            return startCaptureNode(ctx).then(function (mode) {
                state.captureMode = mode;
                state.tx = newStream();
                state.txPcm = [];
            });
        });
    }

    /** 优先 AudioWorklet（独立音频线程），失败则退回 ScriptProcessor */
    function startCaptureNode(ctx) {
        var url = CFG.worklet_url || "/static/audio_capture_worklet.js";
        if (!ctx.audioWorklet || !ctx.audioWorklet.addModule) {
            return Promise.resolve(startScriptProcessor(ctx));
        }
        return ctx.audioWorklet.addModule(url).then(function () {
            var node = new AudioWorkletNode(ctx, "moyu-capture");
            node.port.onmessage = function (ev) {
                onMicFrame(ev.data);
            };
            connectSilently(ctx, node);
            state.micWorklet = node;
            return "worklet";
        }).catch(function (err) {
            log("AudioWorklet 不可用，回退 ScriptProcessor: " + err);
            return startScriptProcessor(ctx);
        });
    }

    function startScriptProcessor(ctx) {
        // 兼容老浏览器；主线程开销在语音场景可接受
        var node = ctx.createScriptProcessor(2048, 1, 1);
        node.onaudioprocess = function (ev) {
            onMicFrame(ev.inputBuffer.getChannelData(0));
        };
        connectSilently(ctx, node);
        state.micNode = node;
        return "script";
    }

    /** 需要一个连接到 destination 的节点才会触发回调，用零增益避免本地回授 */
    function connectSilently(ctx, node) {
        state.micSource.connect(node);
        var sink = ctx.createGain();
        sink.gain.value = 0;
        node.connect(sink);
        sink.connect(ctx.destination);
        state.micSink = sink;
    }

    function stopMicPipeline() {
        try {
            if (state.micWorklet) {
                state.micWorklet.port.onmessage = null;
                state.micWorklet.disconnect();
            }
            if (state.micNode) {
                state.micNode.onaudioprocess = null;
                state.micNode.disconnect();
            }
            if (state.micSink) state.micSink.disconnect();
            if (state.micSource) state.micSource.disconnect();
        } catch (e) {
            /* ignore */
        }
        state.micWorklet = null;
        state.micNode = null;
        state.micSink = null;
        state.micSource = null;
        // 松手就释放麦克风，浏览器地址栏的录音标记会立刻消失
        if (state.micStream) {
            state.micStream.getTracks().forEach(function (t) { t.stop(); });
            state.micStream = null;
        }
        state.txPcm = [];
    }

    function onMicFrame(input) {
        var i;
        var sum = 0;
        for (i = 0; i < input.length; i += 1) {
            sum += input[i] * input[i];
        }
        var level = Math.sqrt(sum / input.length);
        setMeter("local", level);
        window.clearTimeout(state.localMeterPeak);
        state.localMeterPeak = window.setTimeout(function () { setMeter("local", 0); }, 250);

        if (!state.pttWanted) return;
        var ctx = state.audioCtx;
        if (!ctx) return;
        var samples = input instanceof Float32Array ? input : new Float32Array(input);
        var out = resampleStreaming(state.tx, samples, ctx.sampleRate / SAMPLE_RATE);
        if (!out.length) return;
        for (i = 0; i < out.length; i += 1) {
            var v = Math.max(-1, Math.min(1, out[i]));
            state.txPcm.push(v < 0 ? v * 32768 : v * 32767);
        }
        flushTx(false);
    }

    function flushTx(force) {
        if (!state.ws || state.ws.readyState !== 1) {
            state.txPcm = [];
            return;
        }
        while (state.txPcm.length >= TX_CHUNK_SAMPLES) {
            sendChunk(state.txPcm.splice(0, TX_CHUNK_SAMPLES));
        }
        if (force && state.txPcm.length > 80) {
            sendChunk(state.txPcm.splice(0, state.txPcm.length));
        }
    }

    function sendChunk(values) {
        var i16 = new Int16Array(values.length);
        for (var i = 0; i < values.length; i += 1) {
            i16[i] = values[i];
        }
        try {
            state.ws.send(i16.buffer);
        } catch (e) {
            log("发送音频失败: " + e);
        }
    }

    function startPtt() {
        if (!state.canTalk) {
            setHint("ℹ️ 当前控制权不在你手上，无法对讲；刷新页面可重新排队");
            return;
        }
        if (!state.connected) {
            connect();
            setHint("⚠️ 音频通道还没连上，稍后再按住试试");
            return;
        }
        if (state.pttWanted) return;

        state.pttHeld = true;
        var token = (state.pttToken += 1);
        startMicPipeline().then(function () {
            // 快速点按：异步流程可能在松手之后才完成，这里必须放弃
            if (!state.pttHeld || token !== state.pttToken) {
                stopMicPipeline();
                return;
            }
            setPlaybackMuted(true);
            state.pttWanted = true;
            setPttButton(true, true);
            setBadge("对讲中", "red");
            sendJson({ type: "ptt", on: true });
        }).catch(function (err) {
            setHint("❌ " + (err && err.message ? err.message : err));
            state.pttHeld = false;
            state.pttToken += 1;
            stopMicPipeline();
            setPttButton(false, state.canTalk);
            setBadge(state.monitorWanted ? "监听中" : "已连接", state.monitorWanted ? "green" : "cyan");
            setPlaybackMuted(false);
        });
    }

    function stopPtt(silent) {
        state.pttHeld = false;
        state.pttToken += 1;
        if (!state.pttWanted) {
            stopMicPipeline();
            return;
        }
        flushTx(true);
        state.pttWanted = false;
        sendJson({ type: "ptt", on: false });
        stopMicPipeline();
        setPlaybackMuted(false);
        setPttButton(false, state.canTalk);
        setBadge(state.monitorWanted ? "监听中" : "已连接", state.monitorWanted ? "green" : "cyan");
        if (!silent && state.lastError) setHint("❌ " + state.lastError);
    }

    // ------------------------------------------------------------ 设备管理 --

    function renderDevices(options, selected) {
        var caps = (options && options.capture_devices) || (state.capabilities && state.capabilities.capture_devices) || [];
        var play = (options && options.playback_devices) || (state.capabilities && state.capabilities.playback_devices) || [];
        var sel = selected || state.serverState.devices || {};
        fillSelect("audio-capture-device", caps, sel.capture_device);
        fillSelect("audio-playback-device", play, sel.playback_device);
    }

    function fillSelect(id, list, current) {
        var node = el(id);
        if (!node) return;
        if (!list || !list.length) {
            node.innerHTML = '<option value="">（服务端未上报设备）</option>';
            return;
        }
        var html = "";
        for (var i = 0; i < list.length; i += 1) {
            var dev = list[i];
            var id_ = dev.id || "";
            var name = dev.name || id_;
            html += '<option value="' + id_ + '"' + (id_ === (current || "") ? " selected" : "") + ">" + name + "</option>";
        }
        node.innerHTML = html;
    }

    function applyAudioDevices() {
        var custom = el("audio-custom-device");
        var customValue = custom ? custom.value.trim() : "";
        var payload = { type: "select" };
        if (customValue) {
            payload.capture = customValue;
            payload.playback = customValue;
        } else {
            var cap = el("audio-capture-device");
            var play = el("audio-playback-device");
            payload.capture = cap ? cap.value : "";
            payload.playback = play ? play.value : "";
        }
        if (sendJson(payload)) {
            setHint("已切换音频设备：" + (payload.capture || "default") + " / " + (payload.playback || "default"));
        } else {
            setHint("⚠️ 音频通道未连接，无法切换设备");
        }
    }

    // ------------------------------------------------------------ 能力探测 --

    function fetchInfo() {
        return fetch("/audio/info", { credentials: "same-origin" })
            .then(function (r) { return r.json(); })
            .then(function (info) {
                state.capabilities = info.capabilities || null;
                if (info.permissions) {
                    state.canMonitor = !!info.permissions.can_monitor;
                    state.canTalk = !!info.permissions.can_talk;
                }
                if (info.ws_url) CFG.ws_url = info.ws_url;
                if (info.sample_rate) SAMPLE_RATE = info.sample_rate;
                if (info.chunk_ms) CHUNK_MS = info.chunk_ms;
                TX_CHUNK_SAMPLES = Math.round((SAMPLE_RATE * CHUNK_MS) / 1000);

                if (!info.available || !info.running) {
                    setBadge(info.available ? "服务未启动" : "功能已关闭", "red");
                    setHint("❌ " + (info.error || "服务端未启用语音对讲（检查 AUDIO_ENABLED / 端口占用）"));
                    setListenButton(false, true);
                    setPttButton(false, false);
                    return info;
                }
                // 徽标优先级：对讲中 > 监听中 > 已连接 > 能力状态，别把连接状态覆盖掉
                if (!state.connected) {
                    setBadge(info.simulated ? "模拟后端" : "就绪", info.simulated ? "amber" : "cyan");
                }
                if (info.simulated) {
                    setHint("当前是模拟后端：监听会听到 440Hz 测试音，对讲数据不会被播放（真机请装 alsa-utils 并接麦克风/音箱）");
                }
                renderDevices(null, info.capabilities || {});
                connect();
                refreshButtons();
                return info;
            })
            .catch(function (e) {
                setBadge("探测失败", "red");
                setHint("❌ 无法获取音频能力信息：" + e);
            });
    }

    function selfCheckLines() {
        var lines = [];
        var caps = state.capabilities || {};
        var srv = state.serverState || {};

        lines.push("页面安全上下文: " + (window.isSecureContext ? "✅ 是（可用麦克风）" : "❌ 否（http + IP 打开，只能用监听）"));
        lines.push("浏览器麦克风 API: " + (navigator.mediaDevices && navigator.mediaDevices.getUserMedia ? "✅ 可用" : "❌ 不可用，需要 HTTPS/localhost"));
        lines.push("浏览器音频上下文: " + (state.audioCtx ? "✅ " + state.audioCtx.sampleRate + "Hz" : "○ 未创建（点一次监听即可创建）"));
        lines.push("采集方式: " + (state.captureMode ? (state.captureMode === "worklet" ? "✅ AudioWorklet（独立音频线程）" : "⚠️ ScriptProcessor（主线程，兼容模式）") : "○ 未采集"));
        lines.push("WebSocket: " + (state.connected ? "✅ 已连接" : "❌ 未连接") + " " + resolveWsUrl());
        lines.push("服务端后端: " + (srv.backend || CFG.backend || "未知") + (srv.simulated || CFG.simulated ? "（模拟）" : ""));
        lines.push("车端麦克风占用: " + (srv.capture_active ? "✅ 已打开" : "○ 空闲（无人监听时自动释放）"));
        lines.push("已下发音频: " + (srv.monitor_kbytes != null ? srv.monitor_kbytes + " KB" : "—") +
                   " / 已接收对讲: " + (srv.talk_kbytes != null ? srv.talk_kbytes + " KB" : "—"));
        lines.push("监听人数: " + (srv.monitor_listeners != null ? srv.monitor_listeners : "—") +
                   " / 对讲占用: " + (srv.ptt ? "⚠️ 有人正在说话" : "空闲"));
        var captureCount = caps.capture_devices ? caps.capture_devices.length : 0;
        var playCount = caps.playback_devices ? caps.playback_devices.length : 0;
        lines.push("车端设备: 麦克风 " + captureCount + " 个 / 扬声器 " + playCount + " 个");
        lines.push("权限: 监听 " + (state.canMonitor ? "✅" : "❌") + "，对讲 " + (state.canTalk ? "✅" : "❌"));
        if (srv.error) lines.push("最近错误: " + srv.error);
        return lines;
    }

    function runAudioSelfCheck() {
        var box = el("audio-diagnostics");
        if (box) box.textContent = "自检中…";
        fetchInfo().then(function () {
            if (box) box.textContent = selfCheckLines().join("\n");
        });
    }

    // ------------------------------------------------------------ 事件绑定 --

    function bindEvents() {
        var listenBtn = el("audio-listen-btn");
        if (listenBtn) {
            listenBtn.addEventListener("click", toggleMonitor);
        }

        var pttBtn = el("audio-ptt-btn");
        if (pttBtn) {
            var press = function (e) {
                e.preventDefault();
                startPtt();
            };
            var release = function (e) {
                if (e) e.preventDefault();
                stopPtt(false);
            };
            pttBtn.addEventListener("mousedown", press);
            pttBtn.addEventListener("touchstart", press, { passive: false });
            pttBtn.addEventListener("mouseup", release);
            pttBtn.addEventListener("mouseleave", release);
            pttBtn.addEventListener("touchend", release);
            pttBtn.addEventListener("touchcancel", release);
        }

        var vol = el("audio-volume");
        if (vol) {
            vol.addEventListener("input", function () {
                state.volume = parseInt(vol.value, 10) / 100;
                var label = el("audio-volume-value");
                if (label) label.textContent = vol.value;
                if (!state.playbackMuted && state.playGain) {
                    state.playGain.gain.value = state.volume;
                }
            });
        }

        // 按住 T 说话（避开输入框）
        document.addEventListener("keydown", function (e) {
            if (e.repeat || e.metaKey || e.ctrlKey || e.altKey) return;
            var tag = (e.target && e.target.tagName) || "";
            if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
            if (e.key === "t" || e.key === "T") {
                e.preventDefault();
                startPtt();
            }
        });
        document.addEventListener("keyup", function (e) {
            if (e.key === "t" || e.key === "T") stopPtt(false);
        });
        document.addEventListener("visibilitychange", function () {
            if (document.hidden && state.pttWanted) stopPtt(true);
        });
        window.addEventListener("beforeunload", function () {
            state.closing = true;
            if (state.ws) {
                try { state.ws.close(); } catch (err) { /* ignore */ }
            }
        });

        window.applyAudioDevices = applyAudioDevices;
        window.runAudioSelfCheck = runAudioSelfCheck;
    }

    function init() {
        if (!el("audio-panel")) return;
        SAMPLE_RATE = CFG.sample_rate || 16000;
        CHUNK_MS = CFG.chunk_ms || 100;
        TX_CHUNK_SAMPLES = Math.round((SAMPLE_RATE * CHUNK_MS) / 1000);
        CFG.ws_port = CFG.ws_port || 8081;

        setListenButton(false, true);
        setPttButton(false, false);
        bindEvents();
        fetchInfo();
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();
