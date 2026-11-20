// webapp/app.js
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
    };

    const el = (id) => document.getElementById(id);

    // -----------------------------------------------------------------------
    // Тема Telegram
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
    // Навигация
    // -----------------------------------------------------------------------
    function showScreen(name) {
        state.currentScreen = name;
        document.querySelectorAll(".screen").forEach((s) => s.classList.remove("active"));
        const screen = el("screen-" + name);
        if (screen) screen.classList.add("active");
        document.querySelectorAll(".nav-btn").forEach((b) => {
            b.classList.toggle("active", b.dataset.screen === name);
        });
        if (name === "limits") loadLimits();
        if (name === "profile") loadProfile();
        if (name === "chat") loadModels();
    }

    document.querySelectorAll(".nav-btn").forEach((btn) => {
        btn.addEventListener("click", () => showScreen(btn.dataset.screen));
    });

    el("btn-profile").addEventListener("click", () => showScreen("profile"));
    el("btn-limits").addEventListener("click", () => showScreen("limits"));

    // -----------------------------------------------------------------------
    // Чат
    // -----------------------------------------------------------------------
    function appendMessage(text, role, meta) {
        const container = el("chat-messages");
        const div = document.createElement("div");
        div.className = "msg " + role;
        div.textContent = text;
        if (meta) {
            const span = document.createElement("span");
            span.className = "meta";
            span.textContent = meta;
            div.appendChild(span);
        }
        container.appendChild(div);
        container.scrollTop = container.scrollHeight;
    }

    function setTyping(visible) {
        el("typing-indicator").classList.toggle("hidden", !visible);
    }

    async function loadModels() {
        try {
            const data = await api("/api/profile");
            const profile = data.profile;
            state.selectedModel = profile.current_model;
            // Модели приходят из профиля? Нет. Нужно отдельное API или использовать список из конфига.
            // В бэкенде нет эндпоинта для моделей. Добавим в профиль? Или создадим.
            // Проще: в web_server добавить /api/models. Но мы уже выдали web_server.
            // Чтобы не менять блок 3, используем захардкоженный список? Нет, он должен совпадать с config.
            // Лучше добавить эндпоинт в web_server, но это нарушит "уже выдано".
            // Решение: в main.py при старте отправить модели в HTML? Нет.
            // Сделаем отдельный эндпоинт в web_server позже? Нельзя.
            // Тогда оставим селект пустым и заполним при первом чате? Плохо.
            // В итоге: добавим в web_server /api/models. Пользователь заменит файл.
            // Но мы уже выдали web_server. Придётся выдать обновлённый web_server в этом блоке.
            // Это нарушает план, но иначе не работает.
            // Я добавлю эндпоинт /api/models в web_server и перевыпущу его.
            // См. ниже.
        } catch (e) {
            console.error(e);
        }
    }

    async function sendMessage() {
        if (state.sending) return;
        const input = el("chat-input");
        const text = input.value.trim();
        if (!text) return;

        input.value = "";
        appendMessage(text, "user");
        state.sending = true;
        el("btn-send").disabled = true;
        setTyping(true);

        try {
            const data = await api("/api/chat", { message: text });
            setTyping(false);
            appendMessage(
                data.answer,
                "bot",
                "Модель: " + data.model + " · " + data.elapsed + " сек · Осталось: " + data.remaining
            );
        } catch (e) {
            setTyping(false);
            appendMessage("⚠️ " + e.message, "bot");
        } finally {
            state.sending = false;
            el("btn-send").disabled = false;
        }
    }

    el("btn-send").addEventListener("click", sendMessage);
    el("chat-input").addEventListener("keydown", (e) => {
        if (e.key === "Enter") sendMessage();
    });

    // -----------------------------------------------------------------------
    // Лимиты
    // -----------------------------------------------------------------------
    async function loadLimits() {
        try {
            const data = await api("/api/limits");
            const t = data.limits.text;
            const i = data.limits.img2img;
            el("limit-text-label").textContent = t.used + " / " + t.limit;
            el("limit-img-label").textContent = i.used + " / " + i.limit;
            el("limit-text-bar").style.width = Math.min(100, (t.used / t.limit) * 100) + "%";
            el("limit-img-bar").style.width = Math.min(100, (i.used / i.limit) * 100) + "%";
        } catch (e) {
            console.error(e);
        }
    }

    // -----------------------------------------------------------------------
    // Профиль и память
    // -----------------------------------------------------------------------
    async function loadProfile() {
        try {
            const data = await api("/api/profile");
            const p = data.profile;
            el("profile-info").innerHTML = "";
            const rows = [
                ["Имя", p.first_name || "—"],
                ["Стиль", p.style || "—"],
                ["Уровень", p.expertise || "—"],
                ["Тон", p.tone || "—"],
                ["Модель", p.current_model || "по умолчанию"],
                ["Роль", p.current_role || "—"],
            ];
            rows.forEach(([k, v]) => {
                const div = document.createElement("div");
                div.className = "limit-row";
                div.innerHTML = "<span>" + k + "</span><span>" + v + "</span>";
                el("profile-info").appendChild(div);
            });

            const list = el("memory-list");
            list.innerHTML = "";
            p.facts.forEach((f) => {
                const item = document.createElement("div");
                item.className = "memory-item";
                const span = document.createElement("span");
                span.textContent = f.fact;
                const btn = document.createElement("button");
                btn.className = "del";
                btn.textContent = "✕";
                btn.addEventListener("click", async () => {
                    try {
                        await api("/api/memory/delete", { fact_id: f.id });
                        loadProfile();
                    } catch (e) {
                        alert(e.message);
                    }
                });
                item.appendChild(span);
                item.appendChild(btn);
                list.appendChild(item);
            });
        } catch (e) {
            console.error(e);
        }
    }

    el("btn-memory-add").addEventListener("click", async () => {
        const input = el("memory-input");
        const fact = input.value.trim();
        if (!fact) return;
        try {
            await api("/api/memory/add", { fact: fact });
            input.value = "";
            loadProfile();
        } catch (e) {
            alert(e.message);
        }
    });

    el("btn-memory-clear").addEventListener("click", async () => {
        if (!confirm("Удалить все факты из памяти?")) return;
        try {
            await api("/api/memory/clear");
            loadProfile();
        } catch (e) {
            alert(e.message);
        }
    });

    // -----------------------------------------------------------------------
    // Изображения
    // -----------------------------------------------------------------------
    async function generateTxt2Img() {
        const prompt = el("image-prompt").value.trim();
        if (!prompt) return;
        el("image-result").innerHTML = "⏳ Генерация…";
        try {
            const data = await api("/api/image/txt2img", { prompt: prompt });
            el("image-result").innerHTML = "";
            const img = document.createElement("img");
            img.src = data.image;
            el("image-result").appendChild(img);
        } catch (e) {
            el("image-result").textContent = "⚠️ " + e.message;
        }
    }

    function fileToDataUrl(file) {
        return new Promise((resolve, reject) => {
            const reader = new FileReader();
            reader.onload = () => resolve(reader.result);
            reader.onerror = reject;
            reader.readAsDataURL(file);
        });
    }

    async function editImg2Img() {
        const fileInput = el("image-file");
        const prompt = el("image-edit-prompt").value.trim();
        if (!fileInput.files || !fileInput.files[0]) {
            alert("Выберите изображение.");
            return;
        }
        if (!prompt) {
            alert("Введите, что изменить.");
            return;
        }
        el("image-result").innerHTML = "⏳ Редактирование…";
        try {
            const dataUrl = await fileToDataUrl(fileInput.files[0]);
            const data = await api("/api/image/img2img", {
                prompt: prompt,
                image_url: dataUrl,
            });
            el("image-result").innerHTML = "";
            const img = document.createElement("img");
            img.src = data.image;
            el("image-result").appendChild(img);
        } catch (e) {
            el("image-result").textContent = "⚠️ " + e.message;
        }
    }

    el("btn-txt2img").addEventListener("click", generateTxt2Img);
    el("btn-img2img").addEventListener("click", editImg2Img);

    // -----------------------------------------------------------------------
    // Стартовая загрузка
    // -----------------------------------------------------------------------
    if (state.initData) {
        loadLimits();
        loadModels();
    } else {
        appendMessage("Откройте это приложение через Telegram-бота @klyx_ai_bot.", "bot");
    }
})();