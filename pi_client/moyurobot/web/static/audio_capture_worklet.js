/*
 * 摸鱼遥控车 - 麦克风采集 AudioWorklet
 *
 * 在独立音频线程里把输入累积到 1024 帧再回传主线程，避免：
 *   1. ScriptProcessorNode 已被废弃的告警；
 *   2. 采集回调抢占主线程 —— 这一页同时还跑着 MediaPipe 手势/人脸识别。
 */
class MoyuCaptureProcessor extends AudioWorkletProcessor {
    constructor() {
        super();
        this.buffer = new Float32Array(1024);
        this.filled = 0;
    }

    process(inputs) {
        const input = inputs[0];
        if (!input || !input.length || !input[0]) {
            return true;
        }
        const channel = input[0];
        for (let i = 0; i < channel.length; i += 1) {
            this.buffer[this.filled] = channel[i];
            this.filled += 1;
            if (this.filled >= this.buffer.length) {
                this.port.postMessage(this.buffer.slice(0));
                this.filled = 0;
            }
        }
        return true;
    }
}

registerProcessor("moyu-capture", MoyuCaptureProcessor);
