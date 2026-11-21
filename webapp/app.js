(function () {
  'use strict';

  /* ---------- Telegram ---------- */
  var tg = window.Telegram && window.Telegram.WebApp;
  var tgUser = null;
  if (tg) {
    try {
      tg.ready();
      tg.expand();
      tg.setHeaderColor('#09090B');
      tg.setBackgroundColor('#09090B');
      if (tg.setBottomBarColor) tg.setBottomBarColor('#09090B');
      tgUser = tg.initDataUnsafe && tg.initDataUnsafe.user ? tg.initDataUnsafe.user : null;
    } catch (e) {}
  }
  function haptic(type) {
    try {
      if (!tg || !tg.HapticFeedback) return;
      if (type === 'ok') tg.HapticFeedback.notificationOccurred('success');
      else tg.HapticFeedback.impactOccurred('light');
    } catch (e) {}
  }

  /* ---------- Helpers ---------- */
  function $(s, r) { return (r || document).querySelector(s); }
  function $$(s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); }
  function icons() { if (window.lucide) window.lucide.createIcons(); }
  function icon(name) { return '<i data-lucide="' + name + '"></i>'; }

  var toastTimer;
  function toast(text) {
    var t = $('#toast');
    t.textContent = text;
    t.classList.add('show');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { t.classList.remove('show'); }, 1800);
  }

  /* ---------- State ---------- */
  var MODELS = [
    { id: 'nemotron-3-ultra-550b-a55b', short: 'nemotron-3-ultra', desc: 'Максимальное качество ответов' },
    { id: 'ling-3.0-flash', short: 'ling-3.0-flash', desc: 'Быстрые ответы на простые запросы' }
  ];
  var LIMIT = 50;
  var state = { model: 0, role: 'none', len: 'detail', memory: true, used: 2, tasks: [] };
  try {
    var saved = JSON.parse(localStorage.getItem('klyx_state') || 'null');
    if (saved) Object.keys(state).forEach(function (k) { if (saved[k] !== undefined) state[k] = saved[k]; });
  } catch (e) {}
  function save() { try { localStorage.setItem('klyx_state', JSON.stringify(state)); } catch (e) {} }

  /* ---------- Splash + stagger ---------- */
  function boot() {
    icons();
    setTimeout(function () {
      $('#splash').classList.add('hide');
      document.body.classList.add('ready');
    }, 650);
  }

  /* ---------- Tabs ---------- */
  $$('#nav button').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var tab = btn.getAttribute('data-tab');
      $$('#nav button').forEach(function (b) { b.classList.toggle('active', b === btn); });
      $$('.view').forEach(function (v) { v.classList.toggle('active', v.id === 'view-' + tab); });
      haptic();
    });
  });

  /* ---------- Segmented controls ---------- */
  function initSeg(seg, onChange) {
    var btns = $$('button', seg);
    function setIdx(i) {
      seg.style.setProperty('--idx', i);
      btns.forEach(function (b, k) { b.classList.toggle('active', k === i); });
    }
    btns.forEach(function (b, i) {
      b.addEventListener('click', function () { setIdx(i); haptic(); if (onChange) onChange(b, i); });
    });
    return setIdx;
  }

  /* ---------- Model selection ---------- */
  function modelCardHTML(m, i) {
    return '<button type="button" class="model-card' + (i === state.model ? ' active' : '') + '" data-i="' + i + '">' +
      '<span><span class="name">' + m.id + '</span><span class="desc">' + m.desc + '</span></span>' +
      '<span class="radio">' + icon('check') + '</span></button>';
  }
  function renderModels() {
    var html = MODELS.map(modelCardHTML).join('');
    $('#modelCards').innerHTML = html;
    $('#sheetList').innerHTML = html;
    $('#modelChipLabel').textContent = MODELS[state.model].short;
    $$('.model-card').forEach(function (c) {
      c.addEventListener('click', function () {
        setModel(parseInt(c.getAttribute('data-i'), 10));
        closeSheet();
      });
    });
    icons();
  }
  function setModel(i) {
    state.model = i; save(); renderModels(); haptic();
    toast('Модель: ' + MODELS[i].short);
  }
  function openSheet() { $('#scrim').classList.add('open'); $('#sheet').classList.add('open'); haptic(); }
  function closeSheet() { $('#scrim').classList.remove('open'); $('#sheet').classList.remove('open'); }
  $('#modelChip').addEventListener('click', openSheet);
  $('#scrim').addEventListener('click', closeSheet);
  $('#planBadge').addEventListener('click', function () { toast('Тариф FREE: ' + LIMIT + ' текстовых запросов'); });

  /* ---------- Chat ---------- */
  var chatInput = $('#chatInput'), sendBtn = $('#sendBtn'), messages = $('#messages'),
      scroller = $('#chatScroll'), busy = false;

  function autoGrow() {
    chatInput.style.height = 'auto';
    chatInput.style.height = Math.min(chatInput.scrollHeight, 120) + 'px';
    sendBtn.disabled = busy || !chatInput.value.trim();
  }
  chatInput.addEventListener('input', autoGrow);
  chatInput.addEventListener('keydown', function (e) {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); }
  });
  sendBtn.addEventListener('click', sendMessage);
  $$('.scenario').forEach(function (s) {
    s.addEventListener('click', function () {
      chatInput.value = s.getAttribute('data-prompt');
      autoGrow(); chatInput.focus(); haptic();
    });
  });

  function scrollDown() { scroller.scrollTop = scroller.scrollHeight; }
  function addMsg(kind, text) {
    var el = document.createElement('div');
    el.className = 'msg ' + kind;
    if (kind === 'ai') {
      var meta = document.createElement('span');
      meta.className = 'msg-meta';
      meta.textContent = MODELS[state.model].short;
      el.appendChild(meta);
      var body = document.createElement('span');
      body.className = 'msg-body';
      body.textContent = text || '';
      el.appendChild(body);
    } else {
      el.textContent = text;
    }
    messages.appendChild(el);
    scrollDown();
    return el;
  }

  function buildReply(q) {
    var intro = { none: '', dev: 'Смотрю как разработчик. ', teacher: 'Объясню шаг за шагом. ' }[state.role] || '';
    var short = intro + 'Принял запрос: «' + (q.length > 60 ? q.slice(0, 60) + '…' : q) + '». Вот главная мысль: начни с самого важного шага и проверь результат на небольшом примере.';
    if (state.len === 'short') return short;
    return short + '\n\n1. Уточни цель и ограничения.\n2. Разбей задачу на небольшие шаги.\n3. Выполни первый шаг и оцени результат.\n4. Скорректируй план и продолжай.' +
      (state.memory ? '\n\nЯ запомнил контекст этого диалога и учту его в следующих ответах.' : '') +
      '\n\nХочешь, разберу любой пункт подробнее?';
  }

  function streamReply(text) {
    var el = addMsg('ai', '');
    var body = $('.msg-body', el), i = 0;
    var timer = setInterval(function () {
      i += 2;
      body.textContent = text.slice(0, i);
      scrollDown();
      if (i >= text.length) {
        clearInterval(timer);
        busy = false; autoGrow(); haptic('ok');
      }
    }, 16);
  }

  function sendMessage() {
    var text = chatInput.value.trim();
    if (!text || busy) return;
    if (state.used >= LIMIT) { toast('Лимит текстовых запросов исчерпан'); return; }
    busy = true;
    $('#emptyState').classList.add('hidden');
    addMsg('user', text);
    chatInput.value = '';
    autoGrow();
    state.used++; save(); renderLimits();
    var typing = document.createElement('div');
    typing.className = 'msg ai';
    typing.innerHTML = '<span class="dots"><i></i><i></i><i></i></span>';
    messages.appendChild(typing);
    scrollDown();
    setTimeout(function () {
      typing.remove();
      streamReply(buildReply(text));
    }, 900);
  }

  $('#resetBtn').addEventListener('click', function () {
    messages.innerHTML = '';
    $('#emptyState').classList.remove('hidden');
    busy = false; chatInput.value = ''; autoGrow();
    $$('#nav button')[0].click();
    toast('Новый чат');
  });

  /* ---------- Art ---------- */
  var MODES = {
    gen:  { label: 'Описание изображения', ph: 'Например: космонавт на закате, стиль synthwave', btn: 'Сгенерировать', done: 'Изображение готово', upload: false },
    edit: { label: 'Что изменить', ph: 'Например: добавь неоновые огни на фон', btn: 'Применить', done: 'Изменения применены', upload: true },
    meme: { label: 'Текст мема', ph: 'Например: когда код заработал с первого раза', btn: 'Создать мем', done: 'Мем готов', upload: false }
  };
  var artMode = 'gen', artRatio = '1:1', artBusy = false;

  initSeg($('#artSeg'), function (b) {
    artMode = b.getAttribute('data-mode');
    var m = MODES[artMode];
    $('#artLabel').textContent = m.label;
    $('#artPrompt').placeholder = m.ph;
    $('#artGoText').textContent = m.btn;
    $('#uploadBtn').hidden = !m.upload;
    $('#artResult').hidden = true;
  });
  $$('#ratioChips .chip').forEach(function (c) {
    c.addEventListener('click', function () {
      $$('#ratioChips .chip').forEach(function (x) { x.classList.toggle('active', x === c); });
      artRatio = c.getAttribute('data-ratio'); haptic();
    });
  });
  $('#uploadBtn').addEventListener('click', function () { $('#artFile').click(); });
  $('#artFile').addEventListener('change', function (e) {
    var f = e.target.files && e.target.files[0];
    $('#uploadText').textContent = f ? f.name : 'Загрузить изображение';
    $('#uploadBtn').classList.toggle('has-file', !!f);
  });

  function hashStr(s) { var h = 0; for (var i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) | 0; return Math.abs(h); }

  function drawArt(prompt) {
    var c = $('#artCanvas'), p = artRatio.split(':'), w = 720, h = Math.round(w * p[1] / p[0]);
    c.width = w; c.height = h;
    var x = c.getContext('2d'), hue = hashStr(prompt) % 360;
    var g = x.createLinearGradient(0, 0, 0, h);
    g.addColorStop(0, 'hsl(' + ((hue + 240) % 360) + ',70%,12%)');
    g.addColorStop(0.6, 'hsl(' + ((hue + 300) % 360) + ',70%,28%)');
    g.addColorStop(1, 'hsl(' + hue + ',80%,45%)');
    x.fillStyle = g; x.fillRect(0, 0, w, h);
    var hor = h * 0.62, r = Math.min(w, h) * 0.24;
    var sg = x.createLinearGradient(0, hor - r * 2, 0, hor);
    sg.addColorStop(0, 'hsl(' + ((hue + 40) % 360) + ',100%,65%)');
    sg.addColorStop(1, 'hsl(' + ((hue + 320) % 360) + ',100%,55%)');
    x.fillStyle = sg; x.beginPath(); x.arc(w / 2, hor - r, r, 0, Math.PI * 2); x.fill();
    x.fillStyle = '#09090B'; x.fillRect(0, hor, w, h - hor);
    x.strokeStyle = 'rgba(165,166,255,.55)'; x.lineWidth = 1.5;
    var i, y;
    for (i = 1; i <= 8; i++) { y = hor + Math.pow(i / 8, 2) * (h - hor); x.beginPath(); x.moveTo(0, y); x.lineTo(w, y); x.stroke(); }
    for (i = -10; i <= 10; i++) { x.beginPath(); x.moveTo(w / 2 + i * 12, hor); x.lineTo(w / 2 + i * w * 0.11, h); x.stroke(); }
    if (artMode === 'meme') {
      x.font = '700 ' + Math.round(w * 0.075) + 'px Inter, sans-serif';
      x.textAlign = 'center'; x.fillStyle = '#fff'; x.strokeStyle = '#000'; x.lineWidth = 6; x.lineJoin = 'round';
      var t = prompt.toUpperCase().slice(0, 40);
      x.strokeText(t, w / 2, h * 0.12, w * 0.92); x.fillText(t, w / 2, h * 0.12, w * 0.92);
    }
  }

  $('#artGo').addEventListener('click', function () {
    var prompt = $('#artPrompt').value.trim();
    if (artBusy) return;
    if (!prompt) { toast('Введи описание'); $('#artPrompt').focus(); return; }
    var btn = $('#artGo');
    artBusy = true; btn.classList.add('loading');
    $('#artGoText').textContent = 'Генерация...';
    haptic();
    setTimeout(function () {
      drawArt(prompt);
      $('#artCaption').textContent = prompt;
      var r = $('#artResult'); r.hidden = false;
      r.style.animation = 'none'; void r.offsetWidth; r.style.animation = '';
      r.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
      btn.classList.remove('loading');
      $('#artGoText').textContent = MODES[artMode].btn;
      artBusy = false; haptic('ok'); toast(MODES[artMode].done);
    }, 1900);
  });

  /* ---------- Tasks ---------- */
  var taskList = $('#taskList'), taskInput = $('#taskInput');

  function renderTasks() {
    taskList.innerHTML = '';
    state.tasks.forEach(function (t) {
      var li = document.createElement('li');
      li.className = 'task' + (t.done ? ' done' : '');
      var toggle = document.createElement('button');
      toggle.type = 'button'; toggle.className = 'check'; toggle.innerHTML = icon('check');
      toggle.setAttribute('aria-label', 'Отметить выполненной');
      var txt = document.createElement('button');
      txt.type = 'button'; txt.className = 'task-text'; txt.textContent = t.text;
      var del = document.createElement('button');
      del.type = 'button'; del.className = 'task-del'; del.innerHTML = icon('x');
      del.setAttribute('aria-label', 'Удалить');
      function flip() { t.done = !t.done; save(); renderTasks(); haptic(); }
      toggle.addEventListener('click', flip);
      txt.addEventListener('click', flip);
      del.addEventListener('click', function () {
        li.classList.add('removing');
        setTimeout(function () {
          state.tasks = state.tasks.filter(function (x) { return x.id !== t.id; });
          save(); renderTasks();
        }, 200);
      });
      li.appendChild(toggle); li.appendChild(txt); li.appendChild(del);
      taskList.appendChild(li);
    });
    var done = state.tasks.filter(function (t) { return t.done; }).length, total = state.tasks.length;
    $('#taskCounter').textContent = done + ' / ' + total;
    $('#taskBar').style.width = (total ? Math.round(done / total * 100) : 0) + '%';
    $('#tasksEmpty').hidden = total > 0;
    $('#clearDone').disabled = done === 0;
    icons();
  }
  function addTask() {
    var text = taskInput.value.trim();
    if (!text) return;
    state.tasks.push({ id: Date.now() + Math.random(), text: text, done: false });
    taskInput.value = ''; save(); renderTasks(); haptic();
  }
  $('#taskAdd').addEventListener('click', addTask);
  taskInput.addEventListener('keydown', function (e) { if (e.key === 'Enter') addTask(); });
  $('#clearDone').addEventListener('click', function () {
    state.tasks = state.tasks.filter(function (t) { return !t.done; });
    save(); renderTasks(); toast('Выполненные удалены');
  });

  /* ---------- Profile ---------- */
  function renderUser() {
    var name = tgUser && tgUser.first_name ? tgUser.first_name : 'Богдан';
    var id = tgUser && tgUser.id ? String(tgUser.id) : '9411200619';
    $('#userName').textContent = name;
    $('#userId').textContent = 'ID: ' + id;
    var av = $('#avatar');
    if (tgUser && tgUser.photo_url) {
      av.innerHTML = '';
      var img = document.createElement('img'); img.src = tgUser.photo_url; img.alt = '';
      img.onerror = function () { av.textContent = name.charAt(0).toUpperCase(); };
      av.appendChild(img);
    } else av.textContent = name.charAt(0).toUpperCase();
    $('#copyId').onclick = function () {
      function ok() {
        var b = $('#copyId'); b.classList.add('ok'); b.innerHTML = icon('check'); icons();
        toast('ID скопирован'); haptic('ok');
        setTimeout(function () { b.classList.remove('ok'); b.innerHTML = icon('copy'); icons(); }, 1400);
      }
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(id).then(ok, fallback);
      } else fallback();
      function fallback() {
        var ta = document.createElement('textarea'); ta.value = id; ta.style.position = 'fixed'; ta.style.opacity = '0';
        document.body.appendChild(ta); ta.select();
        try { document.execCommand('copy'); ok(); } catch (e) { toast('Не удалось скопировать'); }
        ta.remove();
      }
    };
  }
  function renderLimits() {
    $('#limitCounter').textContent = state.used + ' / ' + LIMIT;
    $('#limitBar').style.width = Math.min(100, state.used / LIMIT * 100) + '%';
  }

  var setLenIdx = initSeg($('#lenSeg'), function (b) { state.len = b.getAttribute('data-len'); save(); });
  setLenIdx(state.len === 'short' ? 0 : 1);

  $$('#roleChips .chip').forEach(function (c) {
    c.addEventListener('click', function () { state.role = c.getAttribute('data-role'); save(); markRole(); haptic(); });
  });
  function markRole() {
    $$('#roleChips .chip').forEach(function (c) { c.classList.toggle('active', c.getAttribute('data-role') === state.role); });
  }

  var memToggle = $('#memToggle');
  function markMem() { memToggle.setAttribute('aria-checked', state.memory ? 'true' : 'false'); }
  memToggle.addEventListener('click', function () {
    state.memory = !state.memory; save(); markMem(); haptic();
    toast(state.memory ? 'Память включена' : 'Память выключена');
  });

  /* ---------- Init ---------- */
  $$('.view').forEach(function (v) {
    if (v.id === 'view-chat') return;
    Array.prototype.slice.call(v.children).forEach(function (c, i) {
      c.setAttribute('data-s', ''); c.style.setProperty('--i', i + 2);
    });
  });
  renderUser(); renderLimits(); renderModels(); markRole(); markMem(); renderTasks(); autoGrow();
  if (document.readyState === 'complete') boot(); else window.addEventListener('load', boot);
})();
