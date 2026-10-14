/* ZooMo AI — hamkor va kuryer bo'limlari (qoida/tarix asosida, tashqi xizmatsiz). */
(function () {
  var T = function (s) { return window.ZMI18n && ZMI18n.t ? ZMI18n.t(s) : s; };
  function esc(v) { return String(v == null ? '' : v).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;'); }
  function $(id) { return document.getElementById(id); }
  function jget(u) { return fetch(u, { credentials: 'same-origin' }).then(function (r) { if (!r.ok) return r.json().then(function (e) { throw new Error(e.detail || 'Xatolik'); }); return r.json(); }); }
  function jpost(u, b) { return fetch(u, { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(b || {}) }).then(function (r) { if (!r.ok) return r.json().then(function (e) { throw new Error(e.detail || 'Xatolik'); }); return r.json(); }); }

  function listen(cb) {
    var SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SR) { alert(T("Bu brauzerda ovoz yozish ishlamaydi. Matn yozing.")); return; }
    var r = new SR(); var l = (localStorage.getItem('zm_lang') || 'uz');
    r.lang = { uz: 'uz-UZ', uzc: 'uz-UZ', ru: 'ru-RU', en: 'en-US', kk: 'kk-KZ', tg: 'tg-TJ', ky: 'ky-KG' }[l] || 'uz-UZ';
    r.onresult = function (e) { cb(e.results[0][0].transcript); };
    r.onerror = function () {};
    try { r.start(); } catch (e) {}
  }

  // ---------- Ovozli navigatsiya (kuryer) ----------
  var voiceOn = false, lastSaid = '', lastAt = 0;
  try { voiceOn = localStorage.getItem('zm_ai_voice') === '1'; } catch (e) {}
  function say(t) {
    if (!('speechSynthesis' in window)) return;
    try {
      var u = new SpeechSynthesisUtterance(t);
      var l = (localStorage.getItem('zm_lang') || 'uz');
      u.lang = { uz: 'ru-RU', uzc: 'ru-RU', ru: 'ru-RU', en: 'en-US', kk: 'kk-KZ', tg: 'ru-RU', ky: 'ru-RU' }[l] || 'ru-RU';
      speechSynthesis.cancel(); speechSynthesis.speak(u);
    } catch (e) {}
  }
  function speakNav(text, dist) {
    if (!voiceOn || !text) return;
    var near = dist != null && dist < 120;
    var key = text + (near ? '|near' : '');
    var now = Date.now();
    if (key === lastSaid || now - lastAt < 6000) return;
    if (dist != null && dist > 400) return;
    lastSaid = key; lastAt = now;
    say((dist != null ? Math.round(dist / 10) * 10 + ' ' + T('metrdan keyin') + ' ' : '') + text);
  }

  // ---------- KURYER ----------
  function mountCourier(root) {
    root.innerHTML =
      '<div class="flex items-center justify-between gap-2 mb-2"><h2 class="text-base font-extrabold">🤖 ZooMo AI</h2>' +
      '<button type="button" id="zai-refresh" class="text-xs font-bold px-2 py-1 rounded-lg bg-slate-500/10">🔄</button></div>' +
      '<p class="text-[11px] text-slate-500 mb-2">' + esc(T('Aqlli yo\'lboshchi: foydali buyurtmalar, birlashtirish va ovozli ko\'rsatma.')) + '</p>' +
      '<div id="zai-tips" class="space-y-2 mb-3"><p class="text-xs text-slate-500">…</p></div>' +
      '<label class="flex items-center gap-2 text-sm font-semibold mb-2 cursor-pointer"><input type="checkbox" id="zai-voice" class="w-4 h-4"> 🔊 ' + esc(T('Ovozli yo\'l ko\'rsatma')) + '</label>' +
      (root.getAttribute('data-led') === '1' ? '<a href="/courier/led" class="block text-center text-sm font-bold py-2.5 rounded-xl text-white" style="background:var(--accent,#f59e0b)">📺 ' + esc(T('Sumka LED ekranini ochish')) + '</a><p class="text-[11px] text-slate-500 mt-1">' + esc(T('Telefon yoki ekranni sumka orqasiga o\'rnating — yaqin do\'konlar reklamasi aylanib turadi.')) + '</p>' : '');
    var cb = $('zai-voice'); cb.checked = voiceOn;
    cb.onchange = function () { voiceOn = cb.checked; try { localStorage.setItem('zm_ai_voice', voiceOn ? '1' : '0'); } catch (e) {} if (voiceOn) say(T('Ovozli ko\'rsatma yoqildi')); };
    function load() {
      jget('/courier/ai/insights').then(function (d) {
        var h = (d.tips || []).map(function (t) { return '<div class="p-2.5 rounded-xl border text-xs font-semibold" style="border-color:rgba(148,163,184,.3)">' + esc(t.icon) + ' ' + esc(t.text) + '</div>'; }).join('');
        $('zai-tips').innerHTML = h || '<p class="text-xs text-slate-500">' + esc(T('Hozircha maslahat yo\'q.')) + '</p>';
      }).catch(function (e) { $('zai-tips').innerHTML = '<p class="text-xs text-slate-500">' + esc(e.message) + '</p>'; });
    }
    $('zai-refresh').onclick = load; load(); setInterval(function () { if (!document.hidden) load(); }, 20000);
  }

  // ---------- HAMKOR ----------
  function mountPartner(root) {
    var ledOn = root.getAttribute('data-led') === '1';
    root.innerHTML =
      '<h2 class="text-base font-extrabold mb-1 text-grad">🤖 ZooMo AI</h2>' +
      '<p class="text-xs text-slate-500 mb-4">' + esc(T('Talab bashorati, avto-menyu va LED reklama.')) + '</p>' +
      '<h3 class="text-sm font-extrabold mb-2">📈 ' + esc(T('Bugungi bashorat')) + '</h3><div id="zai-ins" class="space-y-2 mb-2"></div><div id="zai-bars" class="flex items-end gap-[2px] h-16 mb-5"></div>' +
      '<h3 class="text-sm font-extrabold mb-1">🍽️ ' + esc(T('Avto-menyu')) + '</h3>' +
      '<p class="text-[11px] text-slate-500 mb-2">' + esc(T('Masalan: «Tovuq tugadi» — tovuqli taomlar menyudan yashiriladi; «Tovuq keldi» — qaytadi.')) + '</p>' +
      '<div class="flex gap-2 mb-1"><input id="zai-cmd" type="text" maxlength="120" placeholder="Tovuq tugadi" class="flex-1 min-w-0 p-2.5 border rounded-xl text-sm"><button type="button" id="zai-mic" class="px-3 rounded-xl bg-slate-500/10" aria-label="Mikrofon">🎤</button><button type="button" id="zai-send" class="px-4 rounded-xl text-white font-bold text-sm" style="background:var(--accent,#f59e0b)">OK</button></div>' +
      '<p id="zai-cmd-res" class="text-xs font-semibold mb-5 min-h-[1rem]"></p>' +
      (ledOn ?
        '<h3 class="text-sm font-extrabold mb-1">📺 ' + esc(T('LED reklama so\'rovi')) + '</h3>' +
        '<p class="text-[11px] text-slate-500 mb-2">' + esc(T('Reklama kuryer sumkasining LED ekranida chiqadi. So\'rov yuboring, adminga to\'lov qiling — admin tasdiqlagach reklama efirga chiqadi. Navbatni AI tuzadi.')) + '</p>' +
        '<div id="zai-pay" class="text-[11px] font-semibold mb-2"></div>' +
        '<div class="grid grid-cols-2 gap-2 mb-2"><input id="zai-ltitle" maxlength="60" placeholder="Sarlavha" class="col-span-2 p-2.5 border rounded-xl text-sm"><input id="zai-ltext" maxlength="120" placeholder="Matn (ixtiyoriy)" class="col-span-2 p-2.5 border rounded-xl text-sm">' +
        '<label class="text-xs font-semibold flex items-center gap-2">' + esc(T('Rang')) + ' <input id="zai-lcolor" type="color" value="#f59e0b" class="w-10 h-8 p-0 border-0"></label>' +
        '<label class="text-xs font-semibold flex items-center gap-2">' + esc(T('Radius (km)')) + ' <input id="zai-lrad" type="number" min="0.5" max="30" step="0.5" value="3" class="w-16 p-1.5 border rounded-lg text-sm"></label></div>' +
        '<button type="button" id="zai-ladd" class="w-full py-2.5 rounded-xl text-white font-bold text-sm mb-3" style="background:var(--accent,#f59e0b)">📨 ' + esc(T('Reklama so\'rovini yuborish')) + '</button><div id="zai-leds" class="space-y-2"></div>' : '');
    jget('/partner/ai/insights').then(function (d) {
      $('zai-ins').innerHTML = (d.insights || []).map(function (i) { return '<div class="p-2.5 rounded-xl border text-xs font-semibold" style="border-color:rgba(148,163,184,.3)">' + esc(i.icon) + ' ' + esc(i.text) + '</div>'; }).join('');
      var max = Math.max.apply(null, (d.hours || [0]).concat([1]));
      $('zai-bars').innerHTML = (d.hours || []).map(function (v, h) { return '<div title="' + h + ':00 — ' + v + '" style="flex:1;height:' + Math.max(4, v / max * 100) + '%;background:var(--accent,#f59e0b);opacity:' + (h === d.now_hour ? 1 : .45) + ';border-radius:2px"></div>'; }).join('');
    }).catch(function (e) { $('zai-ins').innerHTML = '<p class="text-xs text-slate-500">' + esc(e.message) + '</p>'; });
    function send() {
      var t = $('zai-cmd').value.trim(); if (!t) return;
      jpost('/partner/ai/command', { text: t }).then(function (r) {
        $('zai-cmd-res').style.color = '#059669';
        $('zai-cmd-res').textContent = (r.available ? '✅ ' + T('Menyuga qaytarildi') : '🙈 ' + T('Menyudan yashirildi')) + ': ' + (r.changed.length ? r.changed.join(', ') : T('o\'zgarish yo\'q')) + (r.alternatives && r.alternatives.length ? ' · ' + T('Muqobil') + ': ' + r.alternatives.join(', ') : '');
        $('zai-cmd').value = '';
      }).catch(function (e) { $('zai-cmd-res').style.color = '#dc2626'; $('zai-cmd-res').textContent = e.message; });
    }
    $('zai-send').onclick = send; $('zai-cmd').onkeydown = function (e) { if (e.key === 'Enter') send(); };
    $('zai-mic').onclick = function () { listen(function (t) { $('zai-cmd').value = t; send(); }); };
    if (!ledOn) return;
    var ST = { pending: '⏳ ' + T('To\'lov va admin tasdig\'i kutilmoqda'), approved: '✅ ' + T('Efirda'), rejected: '⛔ ' + T('Rad etildi'), expired: '⌛ ' + T('Muddati tugagan') };
    function loadLeds() {
      jget('/partner/ai/led').then(function (d) {
        var pc = d.pay_card;
        $('zai-pay').innerHTML = pc ? '💳 ' + esc(T('To\'lov kartasi')) + ': <b>' + esc(pc.card_number) + '</b> · ' + esc(pc.bank_name) + ' · ' + esc(pc.card_holder_name) : '';
        $('zai-leds').innerHTML = (d.ads || []).map(function (a) {
          var st = a.is_active && a.status === 'approved' ? ST.approved : (ST[a.status] || ST.pending);
          var exp = a.expires_at && a.status === 'approved' ? ' · ' + T('gacha') + ' ' + esc(a.expires_at.slice(0, 10)) : '';
          return '<div class="flex items-center gap-2 p-2 rounded-xl border" style="border-color:rgba(148,163,184,.3)"><span style="width:14px;height:14px;border-radius:4px;background:' + esc(a.bg_color) + '"></span><div class="flex-1 min-w-0"><div class="text-xs font-bold truncate">' + esc(a.title) + '</div><div class="text-[11px] text-slate-500">' + st + exp + ' · ' + a.radius_km + ' km</div></div>' +
            (a.status !== 'approved' ? '<button data-d="' + a.id + '" class="text-xs px-2 py-1 rounded-lg bg-red-500/10">🗑</button>' : '') + '</div>';
        }).join('') || '<p class="text-xs text-slate-500">' + esc(T('Reklama yo\'q.')) + '</p>';
      });
    }
    $('zai-leds').onclick = function (e) {
      var b = e.target.closest('button'); if (!b) return;
      if (b.dataset.d && confirm(T('O\'chirilsinmi?'))) jpost('/partner/ai/led/' + b.dataset.d + '/delete').then(loadLeds).catch(function (er) { alert(er.message); });
    };
    $('zai-ladd').onclick = function () {
      jpost('/partner/ai/led', { title: $('zai-ltitle').value, text: $('zai-ltext').value, bg_color: $('zai-lcolor').value, radius_km: parseFloat($('zai-lrad').value) || 3 })
        .then(function () { $('zai-ltitle').value = ''; $('zai-ltext').value = ''; loadLeds(); alert(T('So\'rov yuborildi. Adminga to\'lov qiling — tasdiqlangach reklama chiqadi.')); }).catch(function (e) { alert(e.message); });
    };
    loadLeds();
  }

  // ---------- AI YORDAMCHI (rolga mos qo'llanma + savol-javob) ----------
  function mountHelp(opt) {
    if (document.getElementById('zmh-fab')) return;
    var st = document.createElement('style');
    st.textContent = '.zmh-fab{position:fixed;right:14px;bottom:' + (opt.bottom || 14) + 'px;z-index:60;width:48px;height:48px;border-radius:50%;border:0;background:var(--accent,#f59e0b);color:#fff;font-size:22px;box-shadow:0 6px 18px rgba(0,0,0,.25);cursor:pointer}' +
      '.zmh-box{position:fixed;right:10px;bottom:' + ((opt.bottom || 14) + 56) + 'px;z-index:61;width:min(380px,calc(100vw - 20px));max-height:70vh;display:none;flex-direction:column;background:#fff;color:#0f172a;border:1px solid rgba(148,163,184,.4);border-radius:18px;box-shadow:0 14px 40px rgba(0,0,0,.28);overflow:hidden;font-size:13px}' +
      '.zmh-box.open{display:flex}.zmh-head{padding:10px 14px;font-weight:800;display:flex;justify-content:space-between;align-items:center;background:var(--accent,#f59e0b);color:#fff}' +
      '.zmh-msgs{padding:10px;overflow-y:auto;flex:1;display:flex;flex-direction:column;gap:8px;min-height:120px}' +
      '.zmh-m{padding:8px 10px;border-radius:12px;max-width:92%;white-space:pre-wrap;line-height:1.4}.zmh-b{background:rgba(148,163,184,.18);align-self:flex-start}.zmh-u{background:var(--accent,#f59e0b);color:#fff;align-self:flex-end}' +
      '.zmh-chips{display:flex;flex-wrap:wrap;gap:6px;padding:0 10px 8px}.zmh-chip{border:1px solid rgba(148,163,184,.5);background:transparent;color:inherit;border-radius:999px;padding:4px 9px;font-size:11px;font-weight:700;cursor:pointer}' +
      '.zmh-in{display:flex;gap:6px;padding:8px;border-top:1px solid rgba(148,163,184,.3)}.zmh-in input{flex:1;min-width:0;padding:8px 10px;border:1px solid rgba(148,163,184,.5);border-radius:12px;background:transparent;color:inherit;font-size:13px}.zmh-in button{border:0;border-radius:12px;padding:0 12px;background:rgba(148,163,184,.2);color:inherit;cursor:pointer}' +
      'html[data-mode="dark"] .zmh-box,body.dark .zmh-box,body.theme-dark .zmh-box{background:#1e293b;color:#e2e8f0}';
    document.head.appendChild(st);
    var fab = document.createElement('button'); fab.id = 'zmh-fab'; fab.className = 'zmh-fab'; fab.type = 'button'; fab.setAttribute('aria-label', 'AI yordamchi'); fab.textContent = '💬';
    var box = document.createElement('div'); box.className = 'zmh-box';
    box.innerHTML = '<div class="zmh-head"><span>🤖 ' + esc(T('AI yordamchi')) + '</span><button type="button" class="zmh-x" style="background:none;border:0;color:#fff;font-size:18px;cursor:pointer" aria-label="Yopish">✕</button></div><div class="zmh-msgs"></div><div class="zmh-chips"></div><div class="zmh-in"><input type="text" maxlength="300" placeholder="' + esc(T('Savolingizni yozing...')) + '"><button type="button" class="zmh-mic" aria-label="Mikrofon">🎤</button><button type="button" class="zmh-go">➤</button></div>';
    document.body.appendChild(fab); document.body.appendChild(box);
    var msgs = box.querySelector('.zmh-msgs'), chips = box.querySelector('.zmh-chips'), inp = box.querySelector('input'), first = true;
    function add(t, me) { var d = document.createElement('div'); d.className = 'zmh-m ' + (me ? 'zmh-u' : 'zmh-b'); d.textContent = t; msgs.appendChild(d); msgs.scrollTop = msgs.scrollHeight; }
    function ask(q, silent) {
      if (!silent) add(q, true);
      var body = { q: q }; if (opt.extra) { var e = typeof opt.extra === 'function' ? opt.extra() : opt.extra; for (var k in e) body[k] = e[k]; }
      jpost(opt.url, body).then(function (r) {
        add((r.title && r.matched ? r.title + '\n' : '') + r.text, false);
        chips.innerHTML = (r.topics || []).map(function (t) { return '<button type="button" class="zmh-chip" data-t="' + esc(t) + '">' + esc(t) + '</button>'; }).join('');
      }).catch(function (e) { add(e.message || 'Xatolik', false); });
    }
    function send() { var q = inp.value.trim(); if (!q) return; inp.value = ''; ask(q); }
    fab.onclick = function () { box.classList.toggle('open'); if (box.classList.contains('open') && first) { first = false; ask('', true); } if (box.classList.contains('open')) setTimeout(function () { inp.focus(); }, 50); };
    box.querySelector('.zmh-x').onclick = function () { box.classList.remove('open'); };
    box.querySelector('.zmh-go').onclick = send; inp.onkeydown = function (e) { if (e.key === 'Enter') send(); };
    box.querySelector('.zmh-mic').onclick = function () { listen(function (t) { inp.value = t; send(); }); };
    chips.onclick = function (e) { var b = e.target.closest('.zmh-chip'); if (b) ask(b.dataset.t); };
  }

  window.ZMAI = { mountHelp: mountHelp, mountCourier: mountCourier, mountPartner: mountPartner, speakNav: speakNav, listen: listen };
})();
