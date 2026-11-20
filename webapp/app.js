// Klyx AI — клиентская логика Mini App

(function () {
    "use strict";

    const tg = window.Telegram ? window.Telegram.WebApp : null;
    if (tg) {
        tg.ready();
        tg.expand();
    }

    const state = {
        initData: tg ? tg.initData : "",
        currentScreen: "chat",
        sending: false,
        models: [],
        selectedModel: null,
        roles: {
            default: "Без роли",
            programmer: "Программист",
            teacher: "Учитель",
            copywriter: "Копирайтер",
            translator: "Переводчик",
            socrates: "Сократ",
            critic: "Критик",
        },
        styleOptions: {
            length: { short: "Кратко", detailed: "Подробно" },
            expertise: { novice: "Новичок", expert: "Эксперт" },
            tone: { friendly: "Дружелюбно", strict: "Строго" },
        },
    };

    const $ = (id) => document.getElementById(id);

    // -----------------------------------------------------------------------
    // Telegram theme
    // -----------------------------------------------------------------------
    function applyTelegramTheme() {
        if (!tg || !tg.themeParams) return;
        const root = document.documentElement;
        const map = {
            bg_color: "--tg-theme-bg-color",
            text_color: "--tg-theme-text-color",
            hint_color: "--tg-theme-hint-color",
            button_color: "--tg-theme-button-color",
            button_text_color: "--tg-theme-button-text-color",
            secondary_bg_color: "--tg-theme-secondary-bg-color",
            link_color: "--tg-theme-link-color",
        };
        for (const key in map) {
            if (tg.themeParams[key]) {
                root.style.setProperty(map[key], tg.themeParams[key]);
            }
        }
    }
    if (tg) {
        applyTelegramTheme();
        tg.onEvent("themeChanged", applyTelegramTheme);
    }

    // -----------------------------------------------------------------------
    // Toast
    // -----------------------------------------------------------------------
    let toastTimer = null;
    function showToast(text) {
        const el = $("toast");
        el.textContent = text;
        el.classList.add("visible");
        if (toastTimer) clearTimeout(toastTimer);
        toastTimer = setTimeout(() => el.classList.remove("visible"), 2200);
    }

    // -----------------------------------------------------------------------
    // API
    // -----------------------------------------------------------------------
    async function api(path, body) {
        if (!state.initData) {
            throw new Error("Откройте приложение через Telegram.");
        }
        const response = await fetch(path, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(Object.assign({ initData: state.initData }, body || {})),
        });
        let data;
        try {
            data = await response.json();
        } catch (e) {
            throw new Error("Сервер вернул некорректный ответ.");
        }
        if (!response.ok || !data.ok) {
            throw new Error(data.error || "Ошибка запроса.");
        }
        return data;
    }

    // -----------------------------------------------------------------------
    // Tabs
    // -----------------------------------------------------------------------
    function showScreen(name) {
        state.currentScreen = name;
        document.querySelectorAll(".screen").forEach((s) => s.classList.remove("active"));
        const screen = $("screen-" + name);
        if (screen) screen.classList.add("active");
        document.querySelectorAll(".tab-btn").forEach((b) => {
            b.classList.toggle("active", b.dataset.screen === name);
        });
        if (name === "chat") { loadModels(); loadLimits(); }
        if (name === "profile") loadProfile();
        if (name === "tasks") loadTasks();
    }
    document.querySelectorAll(".tab-btn").forEach((btn) => {
        btn.addEventListener("click", () => showScreen(btn.dataset.screen));
    });
    $("btn-refresh").addEventListener("click", () => {
        if (state.currentScreen === "profile") loadProfile();
        else if (state.currentScreen === "tasks") loadTasks();
        else { loadModels(); loadLimits(); }
        showToast("Обновлено");
    });

    // -----------------------------------------------------------------------
    // Chat
    // -----------------------------------------------------------------------
    function appendMessage(text, role, extraClass) {
        const container = $("chat-messages");
        const div = document.createElement("div");
        div.className = "msg " + role + (extraClass ? " " + extraClass : "");
        div.textContent = text;
        container.appendChild(div);
        container.scrollTop = container.scrollHeight;
    }

    function showSkeleton(visible) {
        $("chat-skeleton").classList.toggle("hidden", !visible);
        if (visible) {
            const container = $("chat-messages");
            container.scrollTop = container.scrollHeight;
        }
    }

    async function loadModels() {
        try {
            const data = await api("/api/models");
            state.models = data.models;
            const select = $("model-select");
            const profile = await api("/api/profile");
            state.selectedModel = profile.profile.current_model;
            select.innerHTML = "";
            data.models.forEach((model) => {
                const opt = document.createElement("option");
                opt.value = model;
                opt.textContent = model.split("/").pop().replace(":free", "");
                if (model === state.selectedModel) opt.selected = true;
                select.appendChild(opt);
            });
        } catch (e) {
            console.error(e);
        }
    }

    async function sendMessage(textOverride) {
        if (state.sending) return;
        const input = $("chat-input");
        const text = (textOverride || input.value).trim();
        if (!text) return;

        input.value = "";
        appendMessage(text, "user");
        state.sending = true;
        $("btn-send").disabled = true;
        showSkeleton(true);

        try {
            const data = await api("/api/chat", { message: text });
            showSkeleton(false);
            appendMessage(data.answer, "bot");
            loadLimits();
        } catch (e) {
            showSkeleton(false);
            appendMessage("⚠️ " + e.message, "bot", "error");
        } finally {
            state.sending = false;
            $("btn-send").disabled = false;
        }
    }

    $("btn-send").addEventListener("click", () => sendMessage());
    $("chat-input").addEventListener("keydown", (e) => {
        if (e.key === "Enter") sendMessage();
    });

    document.querySelectorAll("#chat-chips .chip[data-chip]").forEach((chip) => {
        chip.addEventListener("click", () => {
            const text = chip.dataset.chip;
            if (text) sendMessage(text);
        });
    });

    $("btn-word-chat").addEventListener("click", () => {
        const topic = prompt("Тема Word-отчёта:");
        if (!topic) return;
        generateWordReport(topic);
    });

    // -----------------------------------------------------------------------
    // Limits
    // -----------------------------------------------------------------------
    async function loadLimits() {
        try {
            const data = await api("/api/limits");
            const t = data.limits.text;
            const i = data.limits.img2img;
            $("limit-text-label").textContent = t.used + " / " + t.limit;
            $("limit-img-label").textContent = i.used + " / " + i.limit;
            $("limit-text-bar").style.width = Math.min(100, (t.used / t.limit) * 100) + "%";
            $("limit-img-bar").style.width = Math.min(100, (i.used / i.limit) * 100) + "%";
        } catch (e) {
            console.error(e);
        }
    }

    // -----------------------------------------------------------------------
    // Art & Memes
    // -----------------------------------------------------------------------
    document.querySelectorAll(".segment").forEach((seg) => {
        seg.addEventListener("click", () => {
            document.querySelectorAll(".segment").forEach((s) => s.classList.remove("active"));
            seg.classList.add("active");
            document.querySelectorAll(".art-panel").forEach((p) => p.classList.remove("active"));
            const target = $("art-" + seg.dataset.artTab);
            if (target) target.classList.add("active");
            $("art-result").innerHTML = "";
        });
    });

    async function generateArt() {
        const prompt = $("art-prompt").value.trim();
        if (!prompt) { showToast("Введите описание"); return; }
        const result = $("art-result");
        result.innerHTML = `<div class="loading"><div class="skeleton-line w80"></div><div class="skeleton-line w60"></div><div class="skeleton-line w70"></div></div>`;
        try {
            const data = await api("/api/image/txt2img", { prompt: prompt });
            result.innerHTML = "";
            const img = document.createElement("img");
            img.src = data.image;
            result.appendChild(img);
            loadLimits();
        } catch (e) {
            result.innerHTML = "⚠️ " + e.message;
        }
    }
    $("btn-art-generate").addEventListener("click", generateArt);

    function fileToDataUrl(file) {
        return new Promise((resolve, reject) => {
            const reader = new FileReader();
            reader.onload = () => resolve(reader.result);
            reader.onerror = reject;
            reader.readAsDataURL(file);
        });
    }

    async function editArt() {
        const fileInput = $("art-file");
        const prompt = $("art-edit-prompt").value.trim();
        if (!fileInput.files || !fileInput.files[0]) { showToast("Выберите фото"); return; }
        if (!prompt) { showToast("Что изменить?"); return; }
        const result = $("art-result");
        result.innerHTML = `<div class="loading"><div class="skeleton-line w80"></div><div class="skeleton-line w60"></div></div>`;
        try {
            const dataUrl = await fileToDataUrl(fileInput.files[0]);
            const data = await api("/api/image/img2img", { prompt: prompt, image_url: dataUrl });
            result.innerHTML = "";
            const img = document.createElement("img");
            img.src = data.image;
            result.appendChild(img);
            loadLimits();
        } catch (e) {
            result.innerHTML = "⚠️ " + e.message;
        }
    }
    $("btn-art-edit").addEventListener("click", editArt);

    async function generateMeme() {
        const topic = $("meme-topic").value.trim();
        if (!topic) { showToast("Введите тему"); return; }
        const result = $("art-result");
        result.innerHTML = `<div class="loading"><div class="skeleton-line w80"></div><div class="skeleton-line w60"></div></div>`;
        try {
            const data = await api("/api/image/meme", { topic: topic });
            result.innerHTML = "";
            const img = document.createElement("img");
            img.src = data.image;
            result.appendChild(img);
        } catch (e) {
            result.innerHTML = "⚠️ " + e.message;
        }
    }
    $("btn-meme-generate").addEventListener("click", generateMeme);

    async function generateWordReport(topic) {
        showToast("Готовлю Word-отчёт…");
        try {
            const data = await api("/api/word_report", { topic: topic });
            const byteChars = atob(data.file);
            const byteArray = new Uint8Array(byteChars.length);
            for (let i = 0; i < byteChars.length; i++) byteArray[i] = byteChars.charCodeAt(i);
            const blob = new Blob([byteArray], {
                type: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            });
            const url = URL.createObjectURL(blob);
            const link = document.createElement("a");
            link.href = url;
            link.download = data.filename || "klyx_report.docx";
            document.body.appendChild(link);
            link.click();
            document.body.removeChild(link);
            URL.revokeObjectURL(url);
            showToast("Файл готов ✅");
            loadLimits();
        } catch (e) {
            showToast("⚠️ " + e.message);
        }
    }

    // -----------------------------------------------------------------------
    // Tasks
    // -----------------------------------------------------------------------
    async function loadTasks() {
        try {
            const data = await api("/api/tasks/list");
            renderTasks(data.tasks, data.summary);
        } catch (e) {
            console.error(e);
        }
    }

    function renderTasks(tasks, summary) {
        const list = $("task-list");
        list.innerHTML = "";
        tasks.forEach((task) => {
            const item = document.createElement("div");
            item.className = "task-item";

            const cb = document.createElement("div");
            cb.className = "task-checkbox" + (task.is_done ? " done" : "");
            cb.textContent = task.is_done ? "✓" : "";
            cb.addEventListener("click", () => toggleTask(task.id));

            const txt = document.createElement("div");
            txt.className = "task-text" + (task.is_done ? " done" : "");
            txt.textContent = task.text;

            const del = document.createElement("button");
            del.className = "task-delete";
            del.textContent = "🗑";
            del.addEventListener("click", () => deleteTask(task.id));

            item.appendChild(cb);
            item.appendChild(txt);
            item.appendChild(del);
            list.appendChild(item);
        });

        const total = summary.total || 0;
        const done = summary.done || 0;
        $("tasks-progress-text").textContent = done + " / " + total;
        $("tasks-progress-bar").style.width = total ? Math.min(100, (done / total) * 100) + "%" : "0%";
    }

    async function addTask() {
        const input = $("task-input");
        const text = input.value.trim();
        if (!text) return;
        try {
            await api("/api/tasks/add", { text: text });
            input.value = "";
            loadTasks();
        } catch (e) {
            showToast("⚠️ " + e.message);
        }
    }
    $("btn-task-add").addEventListener("click", addTask);
    $("task-input").addEventListener("keydown", (e) => {
        if (e.key === "Enter") addTask();
    });

    async function toggleTask(id) {
        try {
            await api("/api/tasks/toggle", { task_id: id });
            loadTasks();
        } catch (e) {
            showToast("⚠️ " + e.message);
        }
    }

    async function deleteTask(id) {
        try {
            await api("/api/tasks/delete", { task_id: id });
            loadTasks();
        } catch (e) {
            showToast("⚠️ " + e.message);
        }
    }

    $("btn-tasks-clear").addEventListener("click", async () => {
        if (!confirm("Удалить все выполненные задачи?")) return;
        try {
            await api("/api/tasks/clear_done");
            loadTasks();
        } catch (e) {
            showToast("⚠️ " + e.message);
        }
    });

    // -----------------------------------------------------------------------
    // Profile
    // -----------------------------------------------------------------------
    async function loadProfile() {
        try {
            const data = await api("/api/profile");
            const p = data.profile;

            $("profile-name").textContent = p.first_name || "Гость";
            $("profile-id").textContent = "ID: " + p.telegram_id;
            $("profile-avatar").textContent = (p.first_name || "K").charAt(0).toUpperCase();

            renderPillGrid("profile-models", state.models, p.current_model, (value) => {
                setModel(value);
            });
            renderPillGrid("profile-roles", Object.entries(state.roles).map(([k, v]) => ({ value: k, label: v })),
                p.current_role, (value) => { setRole(value); });

            renderPillGrid("style-length",
                Object.entries(state.styleOptions.length).map(([k, v]) => ({ value: k, label: v })),
                p.style, (value) => { setStyle("length", value); });
            renderPillGrid("style-expertise",
                Object.entries(state.styleOptions.expertise).map(([k, v]) => ({ value: k, label: v })),
                p.expertise, (value) => { setStyle("expertise", value); });
            renderPillGrid("style-tone",
                Object.entries(state.styleOptions.tone).map(([k, v]) => ({ value: k, label: v })),
                p.tone, (value) => { setStyle("tone", value); });

            const memList = $("memory-list");
            memList.innerHTML = "";
            p.facts.forEach((f) => {
                const item = document.createElement("div");
                item.className = "memory-item";
                const span = document.createElement("span");
                span.textContent = f.fact;
                const btn = document.createElement("button");
                btn.className = "del";
                btn.textContent = "✕";
                btn.addEventListener("click", () => deleteMemory(f.id));
                item.appendChild(span);
                item.appendChild(btn);
                memList.appendChild(item);
            });

            loadLimits();
        } catch (e) {
            console.error(e);
        }
    }

    function renderPillGrid(containerId, items, current, onClick) {
        const container = $(containerId);
        container.innerHTML = "";
        items.forEach((item) => {
            const value = typeof item === "string" ? item : item.value;
            const label = typeof item === "string" ? item.split("/").pop().replace(":free", "") : item.label;
            const btn = document.createElement("button");
            btn.className = "pill" + (value === current ? " active" : "");
            btn.textContent = label;
            btn.addEventListener("click", () => onClick(value));
            container.appendChild(btn);
        });
    }

    async function setModel(value) {
        try {
            // Нет прямого endpoint для смены модели через Mini App,
            // используем профиль как источник, но сохраняем локально.
            // Чтобы сохранить на сервере, нужно /api/profile/update.
            // Пока просто запомним и покажем.
            state.selectedModel = value;
            showToast("Модель: " + value.split("/").pop().replace(":free", ""));
            loadProfile();
        } catch (e) {
            showToast("⚠️ " + e.message);
        }
    }

    async function setRole(value) {
        showToast("Роль: " + (state.roles[value] || value));
        // Серверный endpoint для сохранения роли отсутствует; аналогично модели.
        // Обновление доступно через бот.
    }

    async function setStyle(dimension, value) {
        showToast(dimension + " → " + value);
    }

    async function deleteMemory(id) {
        try {
            await api("/api/memory/delete", { fact_id: id });
            loadProfile();
        } catch (e) {
            showToast("⚠️ " + e.message);
        }
    }

    $("btn-memory-add").addEventListener("click", async () => {
        const input = $("memory-input");
        const fact = input.value.trim();
        if (!fact) return;
        try {
            await api("/api/memory/add", { fact: fact });
            input.value = "";
            loadProfile();
        } catch (e) {
            showToast("⚠️ " + e.message);
        }
    });

    $("btn-memory-clear").addEventListener("click", async () => {
        if (!confirm("Удалить все факты из памяти?")) return;
        try {
            await api("/api/memory/clear");
            loadProfile();
        } catch (e) {
            showToast("⚠️ " + e.message);
        }
    });

    // -----------------------------------------------------------------------
    // Startup
    // -----------------------------------------------------------------------
    if (state.initData) {
        loadModels();
        loadLimits();
    } else {
        appendMessage("Откройте это приложение через Telegram-бота @klyx_ai_bot.", "bot", "error");
    }
})();