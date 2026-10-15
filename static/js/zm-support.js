/* ZooMo: administratsiya bilan chat (fikr-mulohaza) + majburiy e'lonlar. Server: main.py "CHAT / FIKR-MULOHAZA". */
(function () {
  var T = function (s) { return window.ZMI18n && ZMI18n.t ? ZMI18n.t(s) : s; };
  function esc(v) { return String(v == null ? '' : v).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/\n/g, '<br>'); }
  var CSS = '.zmsp-fab{position:fixed;left:14px;bottom:var(--zmsp-b,14px);z-index:60;width:48px;height:48px;border-radius:50%;border:0;background:#0ea5e9;color:#fff;font-size:21px;box-shadow:0 6px 18px rgba(0,0,0,.25);cursor:pointer}' +
    '.zmsp-badge{position:absolute;top:-4px;right:-4px;min-width:20px;height:20px;border-radius:10px;background:#ef4444;color:#fff;font-size:11px;font-weight:800;display:none;align-items:center;justify-content:center;padding:0 5px}' +
    '.zmsp-box{position:fixed;left:10px;bottom:calc(var(--zmsp-b,14px) + 56px);z-index:61;width:min(380px,calc(100vw - 20px));height:min(70vh,520px);display:none;flex-direction:column;background:#fff;color:#0f172a;border:1px solid rgba(148,163,184,.4);border-radius:18px;box-shadow:0 14px 40px rgba(0,0,0,.28);overflow:hidden;font-size:13px;font-family:system-ui,sans-serif}' +
    '.zmsp-box.open{display:flex}.zmsp-head{padding:10px 14px;font-weight:800;background:#0ea5e9;color:#fff;display:flex;justify-content:space-between;align-items:center}' +
    '.zmsp-msgs{flex:1;overflow-y:auto;padding:10px;display:flex;flex-direction:column;gap:6px;background:#f8fafc}' +
    '.zmsp-m{max-width:82%;padding:8px 11px;border-radius:14px;line-height:1.4;word-break:break-word}.zmsp-u{align-self:flex-end;background:#0ea5e9;color:#fff;border-bottom-right-radius:4px}.zmsp-s{align-self:flex-start;background:#e2e8f0;color:#0f172a;border-bottom-left-radius:4px}' +
    '.zmsp-t{font-size:9px;opacity:.7;margin-top:2px}.zmsp-hint{font-size:11px;color:#64748b;text-align:center;padding:6px 10px}' +
    '.zmsp-in{display:flex;gap:6px;padding:8px;border-top:1px solid #e2e8f0;background:#fff}.zmsp-in textarea{flex:1;resize:none;border:1px solid #cbd5e1;border-radius:12px;padding:8px 10px;font-size:14px;height:40px;font-family:inherit;background:#fff;color:#0f172a}' +
    '.zmsp-in button{border:0;border-radius:12px;background:#0ea5e9;color:#fff;font-weight:800;padding:0 14px;cursor:pointer}' +
    '.zmsp-ann{position:fixed;inset:0;z-index:100001;background:rgba(2,6,23,.8);display:flex;align-items:center;justify-content:center;padding:16px}' +
    '.zmsp-annb{background:#fff;color:#0f172a;border-radius:20px;padding:20px;width:min(420px,100%);box-shadow:0 20px 60px rgba(0,0,0,.5);animation:zmspPulse 1.2s ease-in-out infinite;font-family:system-ui,sans-serif}' +
    '@keyframes zmspPulse{0%,100%{box-shadow:0 0 0 0 rgba(245,158,11,.7)}50%{box-shadow:0 0 0 12px rgba(245,158,11,0)}}.zmsp-annb h3{margin:0 0 8px;font-size:17px;font-weight:900}.zmsp-annb p{margin:0 0 14px;font-size:15px;line-height:1.5;white-space:pre-wrap}' +
    '.zmsp-annb button{width:100%;border:0;border-radius:14px;background:#f59e0b;color:#fff;font-weight:900;font-size:15px;padding:13px;cursor:pointer}' +
    '@media (prefers-color-scheme:dark){.zmsp-box,.zmsp-annb,.zmsp-in{background:#1e293b;color:#e2e8f0}.zmsp-msgs{background:#0f172a}.zmsp-s{background:#334155;color:#e2e8f0}.zmsp-in textarea{background:#0f172a;color:#e2e8f0;border-color:#475569}}';
  var cfg, open = false, lastUnread = 0, seenAnn = {}, beepTimer = null, audioCtx = null, timer = null;

  function call(path, method, body) {
    var o = { credentials: 'same-origin', headers: { 'Content-Type': 'application/json' } };
    if (cfg.client) { o.method = 'POST'; o.body = JSON.stringify(Object.assign({ init_data: cfg.initData() }, body || {})); }
    else { o.method = method; if (method === 'POST') o.body = JSON.stringify(body || {}); }
    return fetch(cfg.base + path, o).then(function (r) { return r.json().catch(function () { return {}; }).then(function (d) { if (!r.ok) throw new Error(d.detail || T('Xatolik')); return d; }); });
  }
  function beep() {
    try {
      audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
      [0, 0.22].forEach(function (t, i) { var o = audioCtx.createOscillator(), g = audioCtx.createGain(); o.frequency.value = i ? 1046 : 784; o.connect(g); g.connect(audioCtx.destination);
        g.gain.setValueAtTime(0.25, audioCtx.currentTime + t); g.gain.exponentialRampToValueAtTime(0.001, audioCtx.currentTime + t + 0.2); o.start(audioCtx.currentTime + t); o.stop(audioCtx.currentTime + t + 0.21); });
    } catch (e) {}
    try { if (navigator.vibrate) navigator.vibrate([200, 100, 200]); } catch (e) {}
  }
  function badge(n) { var b = document.getElementById('zmsp-badge'); if (!b) return; b.textContent = n > 9 ? '9+' : n; b.style.display = n ? 'flex' : 'none'; }
  function time(iso) { try { var d = new Date(iso + (iso.indexOf('Z') < 0 ? 'Z' : '')); return d.toLocaleString([], { hour: '2-digit', minute: '2-digit', day: '2-digit', month: '2-digit' }); } catch (e) { return ''; } }

  function render(msgs) {
    var box = document.getElementById('zmsp-msgs'); if (!box) return;
    var atEnd = box.scrollHeight - box.scrollTop - box.clientHeight < 60;
    box.innerHTML = '<div class="zmsp-hint">' + esc(T("Real muammo, savol yoki fikr-mulohazangizni yozing. Administratsiya shu yerda javob beradi.")) + '</div>' +
      msgs.map(function (m) { return '<div class="zmsp-m ' + (m.sender === 'user' ? 'zmsp-u' : 'zmsp-s') + '">' + esc(m.text) + '<div class="zmsp-t">' + (m.sender === 'staff' ? esc(T('Administratsiya')) + ' · ' : '') + time(m.at) + '</div></div>'; }).join('');
    if (atEnd || !box.dataset.init) { box.scrollTop = box.scrollHeight; box.dataset.init = '1'; }
  }
  function load() { return call('', 'GET').then(function (d) { render(d.messages || []); badge(0); lastUnread = 0; }).catch(function () {}); }
  function send() {
    var ta = document.getElementById('zmsp-ta'), v = ta.value.trim(); if (!v) return; ta.value = '';
    call('/send', 'POST', { text: v }).then(load).catch(function (e) { alert(e.message); ta.value = v; });
  }
  function toggle(force) {
    open = force != null ? force : !open;
    document.getElementById('zmsp-box').classList.toggle('open', open);
    if (open) { load(); setTimeout(function () { var t = document.getElementById('zmsp-ta'); if (t) t.focus(); }, 100); }
  }

  function showAnn(list) {
    var a = list.filter(function (x) { return !seenAnn[x.id]; })[0]; if (!a || document.getElementById('zmsp-ann')) return;
    seenAnn[a.id] = 1;
    var bg = document.createElement('div'); bg.className = 'zmsp-ann'; bg.id = 'zmsp-ann';
    bg.innerHTML = '<div class="zmsp-annb" role="alertdialog" aria-modal="true"><h3>📢 ' + esc(T("Muhim e'lon")) + '</h3><p>' + esc(a.text) + '</p><button type="button">✅ ' + esc(T("Tushundim")) + '</button></div>';
    document.body.appendChild(bg);
    beep(); beepTimer = setInterval(beep, 4000);
    bg.querySelector('button').onclick = function () {
      clearInterval(beepTimer); bg.remove();
      call(cfg.client ? '/ack' : '/ack/' + a.id, 'POST', { id: a.id }).catch(function () {}).then(function () { setTimeout(poll, 300); });
    };
  }
  function poll() {
    (cfg.client ? call('/poll', 'POST') : call('/poll', 'GET')).then(function (d) {
      if (!open) { badge(d.unread || 0); if ((d.unread || 0) > lastUnread) beep(); lastUnread = d.unread || 0; }
      else if (d.unread) load();
      if (d.pending && d.pending.length) showAnn(d.pending);
    }).catch(function () {});
  }

  function mount(c) {
    cfg = c; if (document.getElementById('zmsp-fab')) return;
    var st = document.createElement('style'); st.textContent = CSS; document.head.appendChild(st);
    document.documentElement.style.setProperty('--zmsp-b', (c.bottom || 14) + 'px');
    var fab = document.createElement('button'); fab.id = 'zmsp-fab'; fab.type = 'button'; fab.className = 'zmsp-fab'; fab.setAttribute('aria-label', T('Chat'));
    fab.innerHTML = '💬<span class="zmsp-badge" id="zmsp-badge"></span>'; fab.onclick = function () { toggle(); };
    var box = document.createElement('div'); box.id = 'zmsp-box'; box.className = 'zmsp-box';
    box.innerHTML = '<div class="zmsp-head"><span>' + esc(T("Administratsiya bilan chat")) + '</span><button type="button" id="zmsp-x" style="background:none;border:0;color:#fff;font-size:18px;cursor:pointer">✕</button></div>' +
      '<div class="zmsp-msgs" id="zmsp-msgs"></div><div class="zmsp-in"><textarea id="zmsp-ta" maxlength="1500" placeholder="' + esc(T("Xabar yozing...")) + '"></textarea><button type="button" id="zmsp-send">➤</button></div>';
    document.body.appendChild(fab); document.body.appendChild(box);
    document.getElementById('zmsp-x').onclick = function () { toggle(false); };
    document.getElementById('zmsp-send').onclick = send;
    document.getElementById('zmsp-ta').onkeydown = function (e) { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); } };
    // brauzer ovozni birinchi bosishdan keyin ruxsat beradi
    document.addEventListener('click', function () { try { audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)(); if (audioCtx.state === 'suspended') audioCtx.resume(); } catch (e) {} }, { once: true });
    if (c.ratingUrl) fetch(c.ratingUrl, { credentials: 'same-origin' }).then(function (r) { return r.json(); }).then(function (d) {
      if (d && d.count) { var h = document.createElement('div'); h.className = 'zmsp-hint'; h.id = 'zmsp-rate'; h.textContent = '⭐ ' + T('Sizning bahoyingiz') + ': ' + d.avg + ' (' + d.count + ')'; box.insertBefore(h, document.getElementById('zmsp-msgs')); }
    }).catch(function () {});
    poll(); timer = setInterval(poll, 15000);
  }
  window.ZMSupport = { mount: mount, open: function (prefill) { toggle(true); if (prefill) setTimeout(function () { var t = document.getElementById('zmsp-ta'); if (t && !t.value) { t.value = prefill; t.focus(); } }, 150); } };
})();
