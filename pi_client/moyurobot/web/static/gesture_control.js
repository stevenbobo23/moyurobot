// 手势控制逻辑

let gestureControlEnabled = false;
let hands = null;
let camera = null;
let lastSentTime = 0;
const SEND_INTERVAL = 100; // 发送指令间隔 (ms)

// 机械臂关节范围 (参考 index.html 中的 input 范围)
const JOINTS = {
    pan: { min: -60, max: 60 },
    lift: { min: -55, max: 55 },
    elbow: { min: -50, max: 50 },
    wrist_flex: { min: -70, max: 70 },
    wrist_roll: { min: -70, max: 70 },
    gripper: { min: 0, max: 60 }
};

// 平滑处理
const SMOOTHING_FACTOR = 0.2;
const GRIPPER_SMOOTHING_FACTOR = 0.8; // 夹爪使用更大的平滑因子，响应更快（加快闭合速度）
let smoothedPositions = {
    pan: 0,
    lift: 0,
    gripper: 0
};

async function initGestureControl() {
    console.log("初始化手势控制...");
    
    const videoElement = document.getElementById('gesture-video');
    const canvasElement = document.getElementById('gesture-canvas');
    const canvasCtx = canvasElement.getContext('2d');

    // 初始化拖拽
    const container = document.getElementById('gesture-preview-container');
    const handle = document.getElementById('gesture-drag-handle');
    if (container && handle) {
        makeDraggable(container, handle);
    }

    if (!videoElement || !canvasElement) {
        console.error("未找到手势控制所需的 video 或 canvas 元素");
        return;
    }

    // Chrome 兼容：确保视频元素有必要的属性
    videoElement.setAttribute('autoplay', '');
    videoElement.setAttribute('playsinline', '');
    videoElement.setAttribute('muted', '');
    videoElement.autoplay = true;
    videoElement.playsInline = true;
    videoElement.muted = true;

    hands = new Hands({locateFile: (file) => {
        return `https://image.mycodebro.cn/mediapipe/${file}`;
    }});

    hands.setOptions({
        maxNumHands: 1,
        modelComplexity: 1,
        minDetectionConfidence: 0.5,
        minTrackingConfidence: 0.5
    });

    hands.onResults((results) => {
        onHandsResults(results, canvasCtx, canvasElement);
    });

    // 使用 MediaPipe Camera Utils
    camera = new Camera(videoElement, {
        onFrame: async () => {
            if (gestureControlEnabled) {
                await hands.send({image: videoElement});
            }
        },
        width: 320,
        height: 240
    });
}

async function toggleGestureControl() {
    // 如果人脸控制开启中，先关闭它
    if (typeof faceControlEnabled !== 'undefined' && faceControlEnabled) {
        if (typeof toggleFaceControl === 'function') {
            await toggleFaceControl();
        }
    }

    gestureControlEnabled = !gestureControlEnabled;
    const btn = document.getElementById('gesture-toggle-btn');
    const previewContainer = document.getElementById('gesture-preview-container');
    const videoElement = document.getElementById('gesture-video');
    
    if (gestureControlEnabled) {
        // 先执行复位
        if (typeof resetArmToHome === 'function') {
            resetArmToHome();
        }
        
        // 等待复位完成后再开启手势控制
        setTimeout(async () => {
            // 尝试同步当前位置
            if (typeof getCurrentArmPosition === 'function') {
                getCurrentArmPosition();
            }
            
            console.log("开启手势控制");
            btn.classList.add('bg-green-600', 'text-white', 'border-green-500');
            btn.classList.remove('bg-gray-800', 'text-gray-400', 'border-gray-700');
            btn.innerHTML = '<span>✋</span> 正在控制';
            
            // Chrome 兼容：先显示容器和视频元素（Chrome 需要元素可见才能访问摄像头）
            previewContainer.classList.remove('hidden');
            if (videoElement) {
                // 临时显示视频元素，确保 Chrome 可以检测到
                videoElement.classList.remove('hidden');
                // 确保必要的属性已设置
                videoElement.setAttribute('autoplay', '');
                videoElement.setAttribute('playsinline', '');
                videoElement.setAttribute('muted', '');
            }
            
            try {
                if (!camera) {
                    await initGestureControl();
                }
                
                // 启动摄像头
                await camera.start();
                
                // 等待视频流开始（Chrome 需要）
                await new Promise((resolve, reject) => {
                    if (!videoElement) {
                        resolve();
                        return;
                    }
                    
                    const timeout = setTimeout(() => {
                        reject(new Error('摄像头启动超时'));
                    }, 5000);
                    
                    const onLoadedMetadata = () => {
                        clearTimeout(timeout);
                        console.log("视频流已加载，分辨率:", videoElement.videoWidth, 'x', videoElement.videoHeight);
                        // 延迟隐藏视频元素，确保 Chrome 已经获取到流
                        setTimeout(() => {
                            if (videoElement && videoElement.readyState >= 2) {
                                videoElement.classList.add('hidden');
                            }
                        }, 300);
                        resolve();
                    };
                    
                    const onError = (e) => {
                        clearTimeout(timeout);
                        console.error("视频加载错误:", e);
                        reject(new Error('摄像头访问失败，请检查权限设置'));
                    };
                    
                    if (videoElement.readyState >= 2) {
                        // 已经加载完成
                        onLoadedMetadata();
                    } else {
                        videoElement.addEventListener('loadedmetadata', onLoadedMetadata, { once: true });
                        videoElement.addEventListener('error', onError, { once: true });
                    }
                });
                
                showNotification('手势控制已开启', 'success');
            } catch (error) {
                console.error("启动手势控制失败:", error);
                showNotification('启动失败: ' + error.message, 'error');
                gestureControlEnabled = false;
                btn.classList.remove('bg-green-600', 'text-white', 'border-green-500');
                btn.classList.add('bg-gray-800', 'text-gray-400', 'border-gray-700');
                btn.innerHTML = '<span>✋</span> 试试手势控制';
                previewContainer.classList.add('hidden');
                if (videoElement) {
                    videoElement.classList.add('hidden');
                }
            }
        }, 500); // 等待500ms让复位命令发送完成
    } else {
        console.log("关闭手势控制");
        if (camera) {
            // 尝试停止摄像头以释放资源
            try {
                await camera.stop();
            } catch (e) {
                console.error("停止摄像头失败:", e);
            }
        }
        
        btn.classList.remove('bg-green-600', 'text-white', 'border-green-500');
        btn.classList.add('bg-gray-800', 'text-gray-400', 'border-gray-700');
        btn.innerHTML = '<span>✋</span> 手势';
        
        previewContainer.classList.add('hidden');
    }
}

function onHandsResults(results, canvasCtx, canvasElement) {
    if (!gestureControlEnabled) return;

    // 绘制预览
    canvasCtx.save();
    canvasCtx.clearRect(0, 0, canvasElement.width, canvasElement.height);
    canvasCtx.drawImage(results.image, 0, 0, canvasElement.width, canvasElement.height);
    
    if (results.multiHandLandmarks && results.multiHandLandmarks.length > 0) {
        const landmarks = results.multiHandLandmarks[0];
        
        // 绘制骨架 - 线条更细，点更小
        drawConnectors(canvasCtx, landmarks, HAND_CONNECTIONS, {color: '#00FF00', lineWidth: 1});
        drawLandmarks(canvasCtx, landmarks, {color: '#FF0000', radius: 2, lineWidth: 0.5});
        
        // 解析手势并控制机械臂
        processGesture(landmarks);
    }
    canvasCtx.restore();
}

function processGesture(landmarks) {
    const now = Date.now();
    if (now - lastSentTime < SEND_INTERVAL) return;

    // 关键点索引:
    // 0: 手腕
    // 4: 拇指指尖
    // 8: 食指指尖
    // 9: 中指指根
    
    // 1. 手掌中心位置控制 Shoulder Pan (左右) 和 Shoulder Lift (上下)
    // 使用关键点 9 (中指指根) 作为手掌中心大概位置
    const handX = landmarks[9].x; // 0.0 - 1.0 (左-右)
    const handY = landmarks[9].y; // 0.0 - 1.0 (上-下)
    
    // 映射 X 到 Pan (-100 到 100)
    // 假设有效控制区域是 0.2 - 0.8
    let targetPan = mapRange(handX, 0.8, 0.2, JOINTS.pan.min, JOINTS.pan.max); // 镜像：手向右移，画面是左，我们希望机器人向右
    targetPan = clamp(targetPan, JOINTS.pan.min, JOINTS.pan.max);
    
    // 映射 Y 到 Lift (-100 到 100)
    // 画面上方是 0，下方是 1。手向上移 (Y变小)，机器人应该向上 (Lift 变小，负值)
    // 手向下移 (Y变大)，机器人应该向下 (Lift 变大，正值)
    let targetLift = mapRange(handY, 0.2, 0.8, JOINTS.lift.min, JOINTS.lift.max);
    targetLift = clamp(targetLift, JOINTS.lift.min, JOINTS.lift.max);

    // 2. 拇指和食指距离控制 Gripper (夹爪)
    const thumbTip = landmarks[4];
    const indexTip = landmarks[8];
    const distance = Math.sqrt(
        Math.pow(thumbTip.x - indexTip.x, 2) + 
        Math.pow(thumbTip.y - indexTip.y, 2)
    );
    
    // 距离大概在 0.02 (闭合) 到 0.2 (张开) 之间
    // 调整映射范围，让响应更敏感，加快夹爪速度
    // Gripper 0 是闭合，60 是张开（新范围）
    let targetGripper = mapRange(distance, 0.03, 0.15, 0, 60);
    targetGripper = clamp(targetGripper, 0, 60);

    // 平滑处理
    smoothedPositions.pan = lerp(smoothedPositions.pan, targetPan, SMOOTHING_FACTOR);
    smoothedPositions.lift = lerp(smoothedPositions.lift, targetLift, SMOOTHING_FACTOR);
    // 夹爪使用更大的平滑因子，响应更快
    smoothedPositions.gripper = lerp(smoothedPositions.gripper, targetGripper, GRIPPER_SMOOTHING_FACTOR);

    // 构建指令对象
    // 注意：这里只控制了部分关节，其他关节保持当前值或者归零
    // 为了更安全，我们应该获取当前其他关节的值，或者只发送变化的关节
    
    // 更新全局变量 currentArmPosition (在 script.js 中定义)
    if (typeof currentArmPosition !== 'undefined') {
        currentArmPosition['arm_shoulder_pan.pos'] = Math.round(smoothedPositions.pan);
        currentArmPosition['arm_shoulder_lift.pos'] = Math.round(smoothedPositions.lift);
        currentArmPosition['arm_gripper.pos'] = Math.round(smoothedPositions.gripper);
        
        // 更新 UI 滑块
        updateSliderUI('shoulder-pan', currentArmPosition['arm_shoulder_pan.pos']);
        updateSliderUI('shoulder-lift', currentArmPosition['arm_shoulder_lift.pos']);
        updateSliderUI('gripper', currentArmPosition['arm_gripper.pos']);

        // 发送指令
        sendArmPosition(currentArmPosition);
        lastSentTime = now;
    }
}

function mapRange(value, inMin, inMax, outMin, outMax) {
    return (value - inMin) * (outMax - outMin) / (inMax - inMin) + outMin;
}

function clamp(value, min, max) {
    return Math.min(Math.max(value, min), max);
}

function lerp(start, end, amt) {
    return (1 - amt) * start + amt * end;
}

function updateSliderUI(id, value) {
    const slider = document.getElementById(id);
    const valueDisplay = document.getElementById(id + '-value');
    if (slider) slider.value = value;
    if (valueDisplay) valueDisplay.textContent = value;
}

function makeDraggable(element, handle) {
    let pos1 = 0, pos2 = 0, pos3 = 0, pos4 = 0;
    
    if (handle) {
        handle.onmousedown = dragMouseDown;
    } else {
        element.onmousedown = dragMouseDown;
    }

    function dragMouseDown(e) {
        e = e || window.event;
        // 如果点击的是按钮或其子元素，不进行拖拽
        if (e.target.closest('button') || e.target.closest('input') || e.target.closest('a')) {
            return;
        }
        e.preventDefault();
        // 获取鼠标光标启动位置:
        pos3 = e.clientX;
        pos4 = e.clientY;
        document.onmouseup = closeDragElement;
        document.onmousemove = elementDrag;
    }

    function elementDrag(e) {
        e = e || window.event;
        e.preventDefault();
        // 计算光标新位置:
        pos1 = pos3 - e.clientX;
        pos2 = pos4 - e.clientY;
        pos3 = e.clientX;
        pos4 = e.clientY;
        
        // 如果是第一次拖动，且位置是基于 bottom/right 的，需要转换
        const rect = element.getBoundingClientRect();
        if (element.style.top === "" || element.style.top === "auto") {
             element.style.bottom = "auto";
             element.style.right = "auto";
             element.style.left = rect.left + "px";
             element.style.top = rect.top + "px";
        }

        // 设置元素新位置:
        element.style.top = (element.offsetTop - pos2) + "px";
        element.style.left = (element.offsetLeft - pos1) + "px";
    }

    function closeDragElement() {
        // 停止移动时解绑:
        document.onmouseup = null;
        document.onmousemove = null;
    }
}

