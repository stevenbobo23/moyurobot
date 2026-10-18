/**
 * 自动招呼面板
 *
 * 只做三件事：轮询 /greeting/status 把状态显示出来；开关；手动试一次。
 * 判定逻辑全在服务端（moyurobot/greeting/policy.py），这里不做任何推断。
 */
(function () {
    "use strict";

    var POLL_MS = 4000;
    var STATE_TEXT = {
        idle: "待机",
        seeing: "有人 / 准备招呼",
        cooldown: "冷却中"
    };
    var REASON_TEXT = {
        no_person: "画面里没人",
        too_far: "人太远",
        off_center: "人不在画面中央",
        warming: "确认中",
        cooldown: "冷却中",
        rate_limited: "本小时次数已用完",
        inactive_hours: "不在时段内",
        ok: "刚刚招呼过"
    };

    var busy = false;
    var timer = null;

    function $(id) {
        return document.getElementById(id);
    }

    function toggleButton(el, enabled, unavailable) {
        if (!el) return;
        if (unavailable) {
            el.textContent = "不可用";
            el.className = "px-3 py-1 text-[10px] font-bold bg-gray-900 border border-gray-800 rounded text-gray-600";
            return;
        }
        if (enabled) {
            el.textContent = "已开启";
            el.className = "px-3 py-1 text-[10px] font-bold bg-tech-600/20 border border-tech-500/60 rounded text-tech-400 hover:bg-tech-600/30 transition-colors";
        } else {
            el.textContent = "已关闭";
            el.className = "px-3 py-1 text-[10px] font-bold bg-gray-800 border border-gray-700 rounded text-gray-400 hover:bg-gray-700 transition-colors";
        }
    }

    function render(data) {
        var enabled = !!data.enabled;
        var unavailable = !data.running && data.error;
        toggleButton($("greeting-toggle"), enabled, unavailable);

        var stateEl = $("greeting-state");
        if (stateEl) {
            if (unavailable) {
                stateEl.textContent = "未启动";
            } else {
                stateEl.textContent = STATE_TEXT[data.state] || (data.state || "--");
            }
            stateEl.className = "text-[11px] font-mono " +
                (data.state === "cooldown" ? "text-amber-400"
                    : data.state === "seeing" ? "text-green-400" : "text-gray-400");
        }

        if ($("greeting-count")) {
            $("greeting-count").textContent = String(data.greet_count || 0);
        }

        var lines = [];
        if (data.error) {
            lines.push("⚠️ " + data.error);
        } else {
            var det = data.detector || {};
            var sp = data.speech || {};
            var cfg = data.config || {};
            lines.push("检测器: " + (det.backend || "none") + (det.model ? " (" + det.model.split("/").pop() + ")" : ""));
            lines.push("语音: " + (sp.resolved || "none") +
                (sp.resolved === "pool" ? " / 本地 " + (sp.pool_count || 0) + " 条"
                    : sp.resolved === "tts" ? " / 云端 TTS" : ""));
            lines.push("时段: " + (cfg.active_hours || "全天") +
                (data.in_active_hours === false ? "（当前不在时段）" : ""));
            if (data.reason) {
                lines.push("判定: " + (REASON_TEXT[data.reason] || data.reason));
            }
            if (data.next_allowed_in > 0) {
                lines.push("再过 " + Math.round(data.next_allowed_in) + "s 可再次招呼");
            }
            if (data.last_error) {
                lines.push("最近错误: " + data.last_error);
            }
            var events = data.events || [];
            if (events.length) {
                lines.push("— 最近 —");
                events.slice(-3).reverse().forEach(function (ev) {
                    lines.push(ev.t + " " + ev.kind + ": " + ev.detail);
                });
            }
        }
        // 只显示最近 7 行，面板高度别被日志撑爆
        if ($("greeting-detail")) {
            $("greeting-detail").textContent = lines.slice(0, 7).join("\n");
        }
    }

    function poll() {
        fetch("/greeting/status", { credentials: "same-origin", cache: "no-store" })
            .then(function (r) { return r.json(); })
            .then(render)
            .catch(function (e) {
                toggleButton($("greeting-toggle"), false, true);
                if ($("greeting-detail")) $("greeting-detail").textContent = "状态获取失败: " + e;
            });
    }

    window.toggleGreeting = function () {
        if (busy) return;
        var el = $("greeting-toggle");
        var currentlyOn = el && el.textContent === "已开启";
        busy = true;
        fetch("/greeting/enabled", {
            method: "POST",
            credentials: "same-origin",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ enabled: !currentlyOn })
        })
            .then(function (r) { return r.json(); })
            .then(function () { poll(); })
            .catch(function (e) { alert("切换失败: " + e); })
            .then(function () { busy = false; });
    };

    window.testGreeting = function (btn) {
        if (btn) {
            btn.disabled = true;
            btn.textContent = "正在打招呼…";
        }
        fetch("/greeting/test", {
            method: "POST",
            credentials: "same-origin",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({})
        })
            .then(function (r) { return r.json(); })
            .then(function (data) {
                if (!data.success) alert("打招呼失败: " + (data.error || "未知原因"));
                setTimeout(poll, 1200);
            })
            .catch(function (e) { alert("请求失败: " + e); })
            .then(function () {
                if (btn) {
                    btn.disabled = false;
                    btn.textContent = "试一次（立刻打招呼）";
                }
            });
    };

    function start() {
        poll();
        if (timer) clearInterval(timer);
        timer = setInterval(poll, POLL_MS);
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", start);
    } else {
        start();
    }
})();
