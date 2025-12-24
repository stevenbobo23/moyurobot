function updateStatus() {
    fetch('/status')
        .then(response => response.json())
        .then(data => {
            const statusDiv = document.getElementById('status');
            if (data.connected) {
                statusDiv.textContent = '状态: 已连接 - 可以控制';
                statusDiv.className = 'status connected';
            } else {
                statusDiv.textContent = '状态: 未连接 - 需要重启服务';
                statusDiv.className = 'status disconnected';
            }
        })
        .catch(error => {
            console.error('获取状态失败:', error);
            const statusDiv = document.getElementById('status');
            statusDiv.textContent = '状态: 连接错误 - 需要重启服务';
            statusDiv.className = 'status disconnected';
        });
}

// 错误提醒控制变量
let lastErrorAlertTime = 0;
const ERROR_ALERT_INTERVAL = 50000; // 50秒内只弹一次窗

function sendCommand(command, silent = false) {
    const requestBody = {command: command};
    
    fetch('/control', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
        },
        body: JSON.stringify(requestBody)
    })
    .then(response => response.json())
    .then(data => {
        console.log('命令执行结果:', data);
        if (!data.success) {
            if (!silent) {
                const now = Date.now();
                if (now - lastErrorAlertTime > ERROR_ALERT_INTERVAL) {
                    alert('命令执行失败: ' + data.message);
                    lastErrorAlertTime = now;
                }
            }
            showNotification('命令执行失败: ' + data.message, 'error');
        }
    })
    .catch(error => {
        console.error('发送命令失败:', error);
        if (!silent) {
            const now = Date.now();
            if (now - lastErrorAlertTime > ERROR_ALERT_INTERVAL) {
                alert('发送命令失败: ' + error.message);
                lastErrorAlertTime = now;
            }
        }
        showNotification('发送命令失败: ' + error.message, 'error');
    });
}

function showNotification(message, type) {
    // 通知功能已禁用
    return;
}

// 退出控制
function exitControl() {
    if (confirm('确定要退出控制吗？退出后其他人就可以进入控制界面了。')) {
        fetch('/exit_control', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            }
        })
        .then(response => response.json())
        .then(data => {
            if (data.success) {
                // 跳转到等待页面
                window.location.href = '/wait';
            } else {
                alert('退出控制失败: ' + data.message);
            }
        })
        .catch(error => {
            console.error('退出控制失败:', error);
            alert('退出控制失败: ' + error.message);
        });
    }
}

// 会话倒计时
let sessionCountdownInterval = null;

function formatDuration(seconds) {
    const total = Math.max(0, Math.floor(seconds));
    const mins = String(Math.floor(total / 60)).padStart(2, '0');
    const secs = String(total % 60).padStart(2, '0');
    return `${mins}:${secs}`;
}

function showCountdownOverlay(seconds) {
    const overlay = document.getElementById('countdown-overlay');
    const numberEl = document.getElementById('countdown-number');
    if (overlay && numberEl) {
        numberEl.textContent = seconds;
        overlay.classList.remove('hidden');
    }
}

function hideCountdownOverlay() {
    const overlay = document.getElementById('countdown-overlay');
    if (overlay) {
        overlay.classList.add('hidden');
    }
}

function stopSessionCountdown() {
    const timerEl = document.getElementById('session-timer');
    const valueEl = document.getElementById('session-remaining');
    if (sessionCountdownInterval) {
        clearInterval(sessionCountdownInterval);
        sessionCountdownInterval = null;
    }
    if (timerEl && valueEl) {
        timerEl.style.display = 'none';
        valueEl.textContent = '--';
    }
    hideCountdownOverlay();
}

function startSessionCountdown(seconds) {
    const timerEl = document.getElementById('session-timer');
    const valueEl = document.getElementById('session-remaining');
    if (!timerEl || !valueEl) return;

    if (sessionCountdownInterval) {
        clearInterval(sessionCountdownInterval);
    }

    let remaining = Math.max(0, Math.floor(seconds));
    if (remaining <= 0) {
        stopSessionCountdown();
        hideCountdownOverlay();
        // 如果剩余时间为0，立即跳转
        showNotification('控制时间已到，正在前往排队页...', 'error');
        setTimeout(() => {
            window.location.href = '/wait';
        }, 3000);
        return;
    }

    timerEl.style.display = 'block';
    valueEl.textContent = formatDuration(remaining);
    
    // 如果剩余时间已经在10秒以内，立即显示提示
    if (remaining <= 10) {
        showCountdownOverlay(remaining);
    } else {
        hideCountdownOverlay();
    }

    sessionCountdownInterval = setInterval(() => {
        remaining -= 1;
        if (remaining <= 0) {
            stopSessionCountdown();
            // 隐藏倒计时提示
            hideCountdownOverlay();
            // 倒计时结束，显示提示并跳转到等待页面
            showNotification('控制时间已到，正在前往排队页...', 'error');
            setTimeout(() => {
                window.location.href = '/wait';
            }, 3000);
        } else {
            valueEl.textContent = formatDuration(remaining);
            // 最后10秒显示全屏倒计时提示
            if (remaining <= 10) {
                showCountdownOverlay(remaining);
            } else {
                hideCountdownOverlay();
            }
        }
    }, 1000);
}

function updateSessionInfo() {
    fetch('/session_info')
        .then(response => response.json())
        .then(data => {
            if (data.is_active_user && data.remaining_seconds > 0) {
                startSessionCountdown(data.remaining_seconds);
            } else {
                stopSessionCountdown();
                // 如果不再是活跃用户或时间已到，跳转到等待页面
                if (data.remaining_seconds <= 0 || !data.is_active_user) {
                    hideCountdownOverlay();
                    showNotification('控制时间已到，正在前往排队页...', 'error');
                    setTimeout(() => {
                        window.location.href = '/wait';
                    }, 3000);
                }
            }
        })
        .catch(error => {
            console.debug('获取会话信息失败:', error);
        });
}

// 键盘控制支持 - 记录按下的按键
const pressedKeys = new Set();

// 按键到按钮ID的映射
const keyToButtonId = {
    'q': 'btn-q',
    'w': 'btn-w',
    'e': 'btn-e',
    'a': 'btn-a',
    's': 'btn-s',
    'd': 'btn-d',
    ' ': 'btn-space',
    'h': 'btn-q',  // H 键也控制左旋转，映射到 Q 按钮
    'j': 'btn-e'   // J 键也控制右旋转，映射到 E 按钮
};

// 高亮按钮
function highlightButton(key) {
    const buttonId = keyToButtonId[key];
    if (buttonId) {
        const button = document.getElementById(buttonId);
        if (button) {
            // 停止按钮使用红色高亮
            if (buttonId === 'btn-space') {
                button.classList.add('bg-red-600', 'text-white', 'shadow-lg', 'shadow-red-500/50');
                button.classList.remove('bg-red-900/30', 'text-red-400');
            } else {
                button.classList.add('bg-tech-500', 'text-white', 'shadow-lg', 'shadow-tech-500/50');
                button.classList.remove('bg-gray-800', 'bg-gray-700', 'text-gray-400');
            }
            // 添加按下效果
            button.style.transform = 'translateY(2px)';
            button.style.borderBottomWidth = '0px';
        }
    }
}

// 取消高亮按钮
function unhighlightButton(key) {
    const buttonId = keyToButtonId[key];
    if (buttonId) {
        const button = document.getElementById(buttonId);
        if (button) {
            // 恢复原始样式
            button.classList.remove('bg-tech-500', 'text-white', 'shadow-lg', 'shadow-tech-500/50');
            button.style.transform = '';
            button.style.borderBottomWidth = '';
            
            // 根据按钮类型恢复原始背景色
            if (buttonId === 'btn-q' || buttonId === 'btn-e') {
                button.classList.add('bg-gray-800', 'text-gray-400');
            } else if (buttonId === 'btn-space') {
                button.classList.add('bg-red-900/30', 'text-red-400');
            } else {
                button.classList.add('bg-gray-700', 'text-white');
            }
        }
    }
}

document.addEventListener('keydown', (e) => {
    const key = e.key.toLowerCase();
    const keyMap = {
        'w': 'forward',
        's': 'backward',
        'a': 'left',
        'd': 'right',
        'q': 'rotate_left',
        'e': 'rotate_right',
        'h': 'rotate_left',  // H 键也控制左旋转
        'j': 'rotate_right', // J 键也控制右旋转
        ' ': 'stop'
    };
    
    if (keyMap[key]) {
        e.preventDefault();
        // 只有当按键未被按下时才发送命令（防止连续触发）
        if (!pressedKeys.has(key)) {
            pressedKeys.add(key);
            sendCommand(keyMap[key]);
            // 高亮对应的按钮
            highlightButton(key);
        }
    }
});

// 按键松开时发送停止命令
document.addEventListener('keyup', (e) => {
    const key = e.key.toLowerCase();
    const keyMap = {
        'w': 'forward',
        's': 'backward',
        'a': 'left',
        'd': 'right',
        'q': 'rotate_left',
        'e': 'rotate_right',
        'h': 'rotate_left',  // H 键也控制左旋转
        'j': 'rotate_right', // J 键也控制右旋转
        ' ': 'stop'  // 空格键也处理
    };
    
    if (keyMap[key]) {
        e.preventDefault();
        pressedKeys.delete(key);
        // 取消高亮对应的按钮
        unhighlightButton(key);
        // 按键松开时发送停止命令（除了空格键，因为空格键是点击式命令）
        if (key !== ' ') {
            sendCommand('stop');
        }
    }
});

// 防止空格键滚动页面
document.addEventListener('keydown', (e) => {
    if (e.key === ' ') {
        e.preventDefault();
    }
});

// 定期更新状态
setInterval(updateStatus, 1000);

// 机械臂当前位置
let currentArmPosition = {
    'arm_shoulder_pan.pos': 0,
    'arm_shoulder_lift.pos': 0,
    'arm_elbow_flex.pos': 0,
    'arm_wrist_flex.pos': 0,
    'arm_wrist_roll.pos': 0,
    'arm_gripper.pos': 0
};

// 发送机械臂位置命令
function sendArmPosition(positions) {
    fetch('/control', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
        },
        body: JSON.stringify(positions)
    })
    .then(response => response.json())
    .then(data => {
        if (data.success) {
            showNotification('机械臂位置已更新', 'success');
        } else {
            showNotification('机械臂控制失败: ' + data.message, 'error');
        }
    })
    .catch(error => {
        console.error('发送机械臂命令失败:', error);
        showNotification('发送机械臂命令失败: ' + error.message, 'error');
    });
}

// 更新滑块值显示
function updateSliderValue(sliderId, value) {
    const valueSpan = document.getElementById(sliderId + '-value');
    if (valueSpan) {
        valueSpan.textContent = value;
    }
}

// 通过箭头按钮调整滑块值
let sliderAdjustInterval = null;
let sliderAdjustTimeout = null;

function adjustSlider(sliderId, delta) {
    const slider = document.getElementById(sliderId);
    if (!slider) return;
    
    const min = parseFloat(slider.min);
    const max = parseFloat(slider.max);
    const currentValue = parseFloat(slider.value);
    
    // 计算新值并限制在范围内
    let newValue = currentValue + delta;
    newValue = Math.max(min, Math.min(max, newValue));
    
    // 更新滑块值
    slider.value = newValue;
    
    // 更新显示值
    updateSliderValue(sliderId, Math.round(newValue));
    
    // 获取关节名称并发送命令
    const joint = slider.getAttribute('data-joint');
    if (joint) {
        currentArmPosition[joint] = newValue;
        sendArmPosition(currentArmPosition);
    }
}

// 开始持续调整滑块（按住按钮时）
function startAdjustSlider(sliderId, delta) {
    // 先立即执行一次
    adjustSlider(sliderId, delta);
    
    // 清除之前的定时器
    stopAdjustSlider();
    
    // 延迟200ms后开始持续调整（防止误触）
    sliderAdjustTimeout = setTimeout(() => {
        sliderAdjustInterval = setInterval(() => {
            adjustSlider(sliderId, delta);
        }, 80); // 每80ms调整一次
    }, 200);
}

// 停止持续调整滑块（松开按钮时）
function stopAdjustSlider() {
    if (sliderAdjustTimeout) {
        clearTimeout(sliderAdjustTimeout);
        sliderAdjustTimeout = null;
    }
    if (sliderAdjustInterval) {
        clearInterval(sliderAdjustInterval);
        sliderAdjustInterval = null;
    }
}

// 复位机械臂到初始位置
function resetArmToHome() {
    const homePosition = {
        'arm_shoulder_pan.pos': 0,
        'arm_shoulder_lift.pos': 0,
        'arm_elbow_flex.pos': 0,
        'arm_wrist_flex.pos': 0,
        'arm_wrist_roll.pos': 0,
        'arm_gripper.pos': 0
    };
    
    // 更新滑块位置
    document.getElementById('shoulder-pan').value = 0;
    document.getElementById('shoulder-lift').value = 0;
    document.getElementById('elbow-flex').value = 0;
    document.getElementById('wrist-flex').value = 0;
    document.getElementById('wrist-roll').value = 0;
    document.getElementById('gripper').value = 0;
    
    // 更新显示值
    updateSliderValue('shoulder-pan', 0);
    updateSliderValue('shoulder-lift', 0);
    updateSliderValue('elbow-flex', 0);
    updateSliderValue('wrist-flex', 0);
    updateSliderValue('wrist-roll', 0);
    updateSliderValue('gripper', 0);
    
    // 发送命令
    sendArmPosition(homePosition);
    currentArmPosition = {...homePosition};
}

// 获取当前机械臂位置
function getCurrentArmPosition() {
    fetch('/status')
        .then(response => response.json())
        .then(data => {
            if (data.success && data.current_action) {
                const action = data.current_action;

                // 更新本地状态变量（修复手势控制状态同步问题）
                if (typeof currentArmPosition !== 'undefined') {
                    currentArmPosition['arm_shoulder_pan.pos'] = Math.round(action['arm_shoulder_pan.pos'] || 0);
                    currentArmPosition['arm_shoulder_lift.pos'] = Math.round(action['arm_shoulder_lift.pos'] || 0);
                    currentArmPosition['arm_elbow_flex.pos'] = Math.round(action['arm_elbow_flex.pos'] || 0);
                    currentArmPosition['arm_wrist_flex.pos'] = Math.round(action['arm_wrist_flex.pos'] || 0);
                    currentArmPosition['arm_wrist_roll.pos'] = Math.round(action['arm_wrist_roll.pos'] || 0);
                    currentArmPosition['arm_gripper.pos'] = Math.round(action['arm_gripper.pos'] || 0);
                }
                
                // 更新滑块和显示值
                if (action['arm_shoulder_pan.pos'] !== undefined) {
                    const value = Math.round(action['arm_shoulder_pan.pos']);
                    document.getElementById('shoulder-pan').value = value;
                    updateSliderValue('shoulder-pan', value);
                }
                if (action['arm_shoulder_lift.pos'] !== undefined) {
                    const value = Math.round(action['arm_shoulder_lift.pos']);
                    document.getElementById('shoulder-lift').value = value;
                    updateSliderValue('shoulder-lift', value);
                }
                if (action['arm_elbow_flex.pos'] !== undefined) {
                    const value = Math.round(action['arm_elbow_flex.pos']);
                    document.getElementById('elbow-flex').value = value;
                    updateSliderValue('elbow-flex', value);
                }
                if (action['arm_wrist_flex.pos'] !== undefined) {
                    const value = Math.round(action['arm_wrist_flex.pos']);
                    document.getElementById('wrist-flex').value = value;
                    updateSliderValue('wrist-flex', value);
                }
                if (action['arm_wrist_roll.pos'] !== undefined) {
                    const value = Math.round(action['arm_wrist_roll.pos']);
                    document.getElementById('wrist-roll').value = value;
                    updateSliderValue('wrist-roll', value);
                }
                if (action['arm_gripper.pos'] !== undefined) {
                    const value = Math.round(action['arm_gripper.pos']);
                    document.getElementById('gripper').value = value;
                    updateSliderValue('gripper', value);
                }
                
                showNotification('已获取当前机械臂位置', 'success');
            } else {
                showNotification('获取机械臂位置失败', 'error');
            }
        })
        .catch(error => {
            console.error('获取机械臂位置失败:', error);
            showNotification('获取机械臂位置失败: ' + error.message, 'error');
        });
}

// 初始化机械臂滑块事件监听
function initArmSliders() {
    const sliders = document.querySelectorAll('.arm-slider');
    console.log(`找到 ${sliders.length} 个机械臂滑块`);
    
    if (sliders.length === 0) {
        console.warn('未找到机械臂滑块，请检查 HTML 中的 class="arm-slider"');
        return;
    }
    
    sliders.forEach(slider => {
        // 更新显示值
        slider.addEventListener('input', function() {
            const value = this.value;
            const sliderId = this.id;
            updateSliderValue(sliderId, value);
        });
        
        // 发送位置命令（当用户释放滑块时）
        slider.addEventListener('change', function() {
            const value = parseFloat(this.value);
            const joint = this.getAttribute('data-joint');
            
            console.log(`滑块 ${this.id} 值改变: ${value}, 关节: ${joint}`);
            
            if (joint) {
                // 更新当前位置
                currentArmPosition[joint] = value;
                
                // 发送完整的机械臂位置（保持其他关节不变）
                console.log('发送机械臂位置:', currentArmPosition);
                sendArmPosition(currentArmPosition);
            } else {
                console.warn(`滑块 ${this.id} 缺少 data-joint 属性`);
            }
        });
        
        // 初始化显示值
        updateSliderValue(slider.id, slider.value);
    });
}

// 前置摄像头控制
let isFrontCameraEnabled = false;

function enableFrontCamera() {
    isFrontCameraEnabled = true;
    const overlay = document.getElementById('front-camera-overlay');
    const img = document.getElementById('front-camera');
    
    if (overlay) overlay.style.display = 'none';
    if (img) {
        img.classList.remove('hidden');
        // 如果有 data-src，则加载视频
        if (img.dataset.src) {
             // 重新初始化视频流
             initVideoStreams();
        }
    }
}

// 初始化视频流
function initVideoStreams() {
    // 获取摄像头状态
    fetch('/cameras')
        .then(response => response.json())
        .then(data => {
            console.log('摄像头状态:', data);
            
            if (data.robot_connected) {
                // 初始化所有摄像头
                data.cameras.forEach(camera => {
                    initSingleVideoStream(camera.name, camera.display_name, camera.connected, camera.frame_available);
                });
            } else {
                showVideoError('机器人未连接，无法显示视频');
            }
        })
        .catch(error => {
            console.error('获取摄像头状态失败:', error);
            showVideoError('无法获取摄像头状态');
        });
}

// 初始化单个视频流
function initSingleVideoStream(cameraName, displayName, isConnected, frameAvailable) {
    const imgElement = document.getElementById(cameraName + '-camera');
    if (!imgElement) {
        console.warn(`找不到摄像头元素: ${cameraName}-camera`);
        return;
    }
    
    // 如果是前置摄像头且未启用，则不加载
    if (cameraName === 'front' && !isFrontCameraEnabled) {
        return;
    }
    
    if (isConnected && frameAvailable) {
        // 设置视频流URL
        const streamUrl = `/video_feed/${cameraName}?t=${Date.now()}`;
        imgElement.src = streamUrl;
        imgElement.style.display = 'block';
        
        // 添加错误处理
        imgElement.onerror = function() {
            console.error(`摄像头 ${cameraName} 视频流加载失败`);
            this.style.display = 'none';
            showCameraError(cameraName, `${displayName}视频流加载失败`);
        };
        
        // 成功加载时隐藏错误信息
        imgElement.onload = function() {
            hideCameraError(cameraName);
        };
        
    } else {
        imgElement.style.display = 'none';
        const reason = !isConnected ? '摄像头未连接' : '摄像头无数据';
        showCameraError(cameraName, `${displayName}: ${reason}`);
    }
}

// 显示摄像头错误信息
function showCameraError(cameraName, message) {
    const videoItem = document.querySelector(`#${cameraName}-camera`).closest('.video-item');
    if (videoItem) {
        let errorDiv = videoItem.querySelector('.camera-error');
        if (!errorDiv) {
            errorDiv = document.createElement('div');
            errorDiv.className = 'camera-error';
            errorDiv.style.cssText = `
                background-color: #f8d7da;
                color: #721c24;
                padding: 20px;
                border-radius: 5px;
                text-align: center;
                font-weight: bold;
                border: 2px solid #f5c6cb;
            `;
            videoItem.appendChild(errorDiv);
        }
        errorDiv.textContent = message;
        errorDiv.style.display = 'block';
    }
}

// 隐藏摄像头错误信息
function hideCameraError(cameraName) {
    const videoItem = document.querySelector(`#${cameraName}-camera`).closest('.video-item');
    if (videoItem) {
        const errorDiv = videoItem.querySelector('.camera-error');
        if (errorDiv) {
            errorDiv.style.display = 'none';
        }
    }
}

// 显示通用视频错误
function showVideoError(message) {
    const videoSection = document.querySelector('.video-section');
    if (videoSection) {
        let errorDiv = videoSection.querySelector('.video-error');
        if (!errorDiv) {
            errorDiv = document.createElement('div');
            errorDiv.className = 'video-error';
            errorDiv.style.cssText = `
                background-color: #f8d7da;
                color: #721c24;
                padding: 15px;
                border-radius: 5px;
                text-align: center;
                font-weight: bold;
                border: 2px solid #f5c6cb;
                margin-top: 10px;
            `;
            videoSection.appendChild(errorDiv);
        }
        errorDiv.textContent = message;
        errorDiv.style.display = 'block';
    }
}

// 定期检查摄像头状态
function checkCameraStatus() {
    fetch('/cameras')
        .then(response => response.json())
        .then(data => {
            if (data.robot_connected) {
                data.cameras.forEach(camera => {
                    const imgElement = document.getElementById(camera.name + '-camera');
                    if (imgElement) {
                        // 如果是前置摄像头且未启用，则跳过检查
                        if (camera.name === 'front' && !isFrontCameraEnabled) {
                            return;
                        }

                        if (camera.connected && camera.frame_available) {
                            if (imgElement.style.display === 'none') {
                                // 摄像头恢复了，重新加载视频流
                                initSingleVideoStream(camera.name, camera.display_name, camera.connected, camera.frame_available);
                            }
                        } else {
                            imgElement.style.display = 'none';
                            const reason = !camera.connected ? '摄像头未连接' : '摄像头无数据';
                            showCameraError(camera.name, `${camera.display_name}: ${reason}`);
                        }
                    }
                });
            }
        })
        .catch(error => {
            console.debug('检查摄像头状态失败:', error);
        });
}

// 页面加载完成后初始化
document.addEventListener('DOMContentLoaded', () => {
    updateStatus();
    initArmSliders();
    initVideoStreams(); // 初始化视频流
    console.log('LeKiwi HTTP Controller 已加载');
    console.log('键盘控制: W(前进) S(后退) A(左转) D(右转) Q(左旋转) E(右旋转) 空格(停止)');
    console.log('机械臂控制: 使用滑块调节各关节位置');
    
    // 页面加载时自动执行复位
    setTimeout(() => {
        if (typeof resetArmToHome === 'function') {
            console.log('页面加载完成，执行机械臂复位...');
            resetArmToHome();
        }
    }, 1000); // 等待1秒，确保机器人连接状态已更新
    
    // 定期检查摄像头状态（5秒一次）
    setInterval(checkCameraStatus, 5000);
    updateSessionInfo();
    setInterval(updateSessionInfo, 5000);
});
