/* ZooMo qo'shimcha himoya: maxfiy javob (sevimli ... savollari). Server: sec2fa.py */
(function () {
  var T = function (s) { return window.ZMI18n && ZMI18n.t ? ZMI18n.t(s) : s; };
  var rawFetch = window.fetch.bind(window);
  function esc(v) { return String(v == null ? '' : v).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;'); }
  // server xabarlari: oddiy yoki raqamli ({n}) shablon bo'yicha tarjima
  function tm(m) {
    var t = T(m); if (t !== m) return t;
    var mm = String(m).match(/\d+/); if (!mm) return m;
    var k = String(m).replace(/\d+/, '{n}'), r = T(k); return r !== k ? r.replace('{n}', mm[0]) : m;
  }
  function jget(u) { return rawFetch(u, { credentials: 'same-origin' }).then(j); }
  function jpost(u, b) { return rawFetch(u, { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(b || {}) }).then(j); }
  function j(r) {
    return r.json().catch(function () { return {}; }).then(function (d) {
      if (!r.ok) { var m = d && d.detail; var e = new Error(tm(typeof m === 'string' ? m : (m && m.message) || 'Xatolik')); e.status = r.status; e.code = m && m.code; throw e; }
      return d;
    });
  }

  var CSS = '.zms-bg{position:fixed;inset:0;z-index:100000;background:rgba(2,6,23,.72);display:flex;align-items:center;justify-content:center;padding:14px;font-family:system-ui,-apple-system,sans-serif}' +
    '.zms-box{background:#fff;color:#0f172a;border-radius:20px;width:min(440px,100%);max-height:92vh;overflow:auto;padding:18px;box-shadow:0 20px 60px rgba(0,0,0,.4)}' +
    '@media (prefers-color-scheme:dark){.zms-box{background:#1e293b;color:#e2e8f0}.zms-in{background:#0f172a!important;color:#e2e8f0!important;border-color:#475569!important}.zms-sub{color:#94a3b8!important}}' +
    '.zms-h{font-weight:900;font-size:17px;margin:0 0 6px}.zms-sub{font-size:12px;color:#64748b;margin:0 0 12px;line-height:1.45}' +
    '.zms-in{width:100%;box-sizing:border-box;padding:11px 12px;border:1px solid #cbd5e1;border-radius:12px;font-size:15px;background:#fff;color:#0f172a}' +
    '.zms-row{margin-bottom:10px}.zms-lab{display:flex;justify-content:space-between;align-items:center;font-size:12px;font-weight:700;margin-bottom:4px}' +
    '.zms-btn{border:0;border-radius:12px;padding:12px 14px;font-weight:800;font-size:14px;cursor:pointer}.zms-ok{background:#f59e0b;color:#fff;width:100%}.zms-no{background:rgba(148,163,184,.25);color:inherit}' +
    '.zms-err{color:#e11d48;font-size:12px;font-weight:700;min-height:16px;margin:6px 0}.zms-q{font-size:16px;font-weight:800;margin:6px 0 10px}.zms-tag{font-size:10px;background:#10b98122;color:#059669;border-radius:8px;padding:1px 6px}';
  function ensureCss() { if (document.getElementById('zms-css')) return; var s = document.createElement('style'); s.id = 'zms-css'; s.textContent = CSS; document.head.appendChild(s); }

  function modal(html, cancellable) {
    ensureCss();
    var bg = document.createElement('div'); bg.className = 'zms-bg'; bg.innerHTML = '<div class="zms-box" role="dialog" aria-modal="true">' + html + '</div>';
    document.body.appendChild(bg);
    return bg;
  }

  // ---- Maxfiy javobni kiritish (tekshiruv) ----
  function verifyView(opts) {
    return new Promise(function (resolve, reject) {
      jget('/security/challenge').then(function (ch) {
        var bg = modal('<p class="zms-h">🔐 ' + T("Xavfsizlik tekshiruvi") + '</p><p class="zms-sub">' + T("Maxfiy javobingizni kiriting.") + '</p>' +
          '<div class="zms-q">' + esc(T(ch.question)) + '</div>' +
          '<input class="zms-in" id="zms-ans" type="password" maxlength="300" autocomplete="off" placeholder="' + esc(T("Javobingiz")) + '">' +
          '<label class="zms-sub" style="display:flex;gap:6px;margin:8px 0 0"><input type="checkbox" id="zms-show"> ' + T("Ko'rsatish") + '</label>' +
          '<div class="zms-err" id="zms-err">' + (ch.locked ? esc(T("Ko'p xato urinish. Birozdan keyin qayta urinib ko'ring.")) : '') + '</div>' +
          '<div style="display:flex;gap:8px"><button class="zms-btn zms-ok" id="zms-go">' + T("Tasdiqlash") + '</button>' +
          (opts && opts.noCancel ? '' : '<button class="zms-btn zms-no" id="zms-cancel">' + T("Bekor qilish") + '</button>') + '</div>');
        var inp = bg.querySelector('#zms-ans'), err = bg.querySelector('#zms-err');
        inp.focus();
        bg.querySelector('#zms-show').onchange = function (e) { inp.type = e.target.checked ? 'text' : 'password'; };
        function go() {
          var v = inp.value; if (!v) return;
          jpost('/security/verify', { answer: v }).then(function () { bg.remove(); resolve(); })
            .catch(function (e) {
              err.textContent = e.message; inp.value = '';
              if (e.status === 403) jget('/security/challenge').then(function (c2) { bg.querySelector('.zms-q').textContent = T(c2.question); }).catch(function () {});
            });
        }
        bg.querySelector('#zms-go').onclick = go;
        inp.onkeydown = function (e) { if (e.key === 'Enter') go(); };
        var c = bg.querySelector('#zms-cancel'); if (c) c.onclick = function () { bg.remove(); reject(new Error('cancel')); };
      }).catch(function (e) {
        if (e.code === 'sec_setup') setupView(opts).then(resolve, reject); else { alert(e.message); reject(e); }
      });
    });
  }

  // ---- Telegramga yuborilgan 8 xonali kod (javoblarni o'zgartirish/o'chirish uchun) ----
  function otpView(opts) {
    return new Promise(function (resolve, reject) {
      var bg = modal('<p class="zms-h">📲 ' + T("Telegram kodi") + '</p><p class="zms-sub" id="zms-info">' + T("Xavfsizlik uchun Telegram botdan 8 xonali kod yuboriladi. Javoblarni o'zgartirish yoki o'chirish uchun uni kiriting.") + '</p>' +
        '<input class="zms-in" id="zms-otp" inputmode="numeric" maxlength="8" autocomplete="one-time-code" placeholder="••••••••" style="letter-spacing:6px;text-align:center;font-size:20px" disabled>' +
        '<div class="zms-err" id="zms-err"></div><div style="display:flex;gap:8px"><button class="zms-btn zms-ok" id="zms-send">' + T("Kod yuborish") + '</button>' +
        '<button class="zms-btn zms-ok" id="zms-go" style="display:none">' + T("Tasdiqlash") + '</button><button class="zms-btn zms-no" id="zms-cancel">' + T("Bekor qilish") + '</button></div>');
      var inp = bg.querySelector('#zms-otp'), err = bg.querySelector('#zms-err'), send = bg.querySelector('#zms-send'), go = bg.querySelector('#zms-go');
      function doSend() {
        send.disabled = true; err.textContent = '';
        jpost('/security/otp/send').then(function () {
          inp.disabled = false; inp.focus(); go.style.display = ''; send.textContent = T("Qayta yuborish");
          bg.querySelector('#zms-info').textContent = T("Telegramga 8 xonali kod yuborildi. Uni shu yerga kiriting.");
          setTimeout(function () { send.disabled = false; }, 30000);
        }).catch(function (e) { err.textContent = e.message; send.disabled = false; });
      }
      send.onclick = doSend;
      function check() {
        var v = inp.value.replace(/\D/g, ''); if (v.length !== 8) { err.textContent = T("8 ta raqam kiriting"); return; }
        jpost('/security/otp/verify', { code: v }).then(function () { bg.remove(); resolve(); }).catch(function (e) { err.textContent = e.message; inp.value = ''; });
      }
      go.onclick = check; inp.onkeydown = function (e) { if (e.key === 'Enter') check(); };
      bg.querySelector('#zms-cancel').onclick = function () { bg.remove(); reject(new Error('cancel')); };
    });
  }

  // ---- Maxfiy javoblarni o'rnatish / yangilash ----
  function setupView(opts, st) {
    return new Promise(function (resolve, reject) {
      (st ? Promise.resolve(st) : jget('/security/status')).then(function (s) {
        if (s.has && !s.can_change) { otpView(opts).then(function () { setupView(opts).then(resolve, reject); }, reject); return; }
        var have = {}; (s.mine || []).forEach(function (m) { have[m.category] = m; });
        var rows = s.categories.map(function (c) {
          var h = have[c.key];
          return '<div class="zms-row"><div class="zms-lab"><span>' + esc(T(c.label)) + (h ? ' <span class="zms-tag">✓ ' + T("o'rnatilgan") + '</span>' : '') + '</span>' +
            (h && s.mine.length > 1 ? '<button type="button" class="zms-btn zms-no" style="padding:2px 8px;font-size:11px" data-del="' + h.id + '">🗑</button>' : '') + '</div>' +
            '<input class="zms-in" type="password" maxlength="' + s.max + '" autocomplete="off" data-cat="' + c.key + '" placeholder="' + esc(h ? T("Yangilash uchun yangi javob yozing") : T("Kamida 5 ta belgi")) + '"></div>';
        }).join('');
        var bg = modal('<p class="zms-h">🔐 ' + T("Maxfiy javoblarim") + '</p>' +
          '<p class="zms-sub">' + T("Quyidagilardan xohlaganingizga javob yozing (kamida bittasiga). Javob haqiqiy bo'lishi shart emas — o'zingiz bilgan so'z, raqam, belgi yoki ism-familiya yozing. Kamida 5, ko'pi bilan 300 belgi. Pul yechish, sozlamalar va operator kirishida shu javob so'raladi. Uni hech kimga aytmang.") + '</p>' + rows +
          '<div class="zms-err" id="zms-err"></div><div style="display:flex;gap:8px"><button class="zms-btn zms-ok" id="zms-save">' + T("Saqlash") + '</button>' +
          (opts && opts.noCancel && !s.has ? '' : '<button class="zms-btn zms-no" id="zms-cancel">' + T("Yopish") + '</button>') + '</div>');
        var err = bg.querySelector('#zms-err');
        bg.querySelectorAll('[data-del]').forEach(function (b) {
          b.onclick = function () { if (!confirm(T("O'chirilsinmi?"))) return; jpost('/security/delete/' + b.dataset.del).then(function () { bg.remove(); setupView(opts).then(resolve, reject); }).catch(function (e) { if (e.code === 'sec_otp') { bg.remove(); setupView(opts).then(resolve, reject); } else err.textContent = e.message; }); };
        });
        bg.querySelector('#zms-save').onclick = function () {
          var entries = [], bad = '';
          bg.querySelectorAll('input[data-cat]').forEach(function (i) {
            var v = i.value.trim(); if (!v) return;
            if (v.length < 5) bad = T("Har bir javob kamida 5 ta belgi bo'lsin"); entries.push({ category: i.dataset.cat, answer: v });
          });
          if (bad) { err.textContent = bad; return; }
          if (!entries.length) { err.textContent = T("Kamida bitta javob yozing"); return; }
          jpost('/security/setup', { entries: entries }).then(function () { bg.remove(); resolve(); })
            .catch(function (e) { if (e.code === 'sec_verify') { bg.remove(); verifyView(opts).then(function () { setupView(opts).then(resolve, reject); }, reject); } else if (e.code === 'sec_otp') { bg.remove(); setupView(opts).then(resolve, reject); } else err.textContent = e.message; });
        };
        var c = bg.querySelector('#zms-cancel'); if (c) c.onclick = function () { bg.remove(); reject(new Error('cancel')); };
      }).catch(function (e) { alert(e.message); reject(e); });
    });
  }

  var busy = null;
  function ensure(opts) {
    if (busy) return busy;
    busy = jget('/security/status').then(function (s) {
      if (!s.has) return setupView(opts, s);
      if (!s.unlocked) return verifyView(opts);
    });
    var done = function () { busy = null; }; busy.then(done, done);
    return busy;
  }

  function manage() {
    return ensure().then(function () { return setupView({}); }).catch(function () {});
  }

  function gate(next) {
    ensure({ noCancel: true }).then(function () { location.replace(next || '/admin'); }, function () { location.replace('/login'); });
  }

  // 428 (sec_*) kelsa — oyna ochib, so'rovni qayta yuboramiz
  window.fetch = function (input, init) {
    return rawFetch(input, init).then(function (r) {
      if (r.status !== 428) return r;
      return r.clone().json().then(function (d) {
        var c = d && d.detail && d.detail.code;
        if (!c || String(c).indexOf('sec_') !== 0) return r;
        return ensure().then(function () { return rawFetch(input, init); }, function () { return r; });
      }).catch(function () { return r; });
    });
  };

  // Oddiy forma yuborish: pul yechish, karta, sozlamalar — avval tekshiruv
  var PROT = /\/(partner|courier)\/(withdraw|cards|settings\/update|legal)/;
  document.addEventListener('submit', function (e) {
    var f = e.target; if (!f || !f.getAttribute) return;
    var a = f.getAttribute('action') || ''; if (!PROT.test(a)) return;
    if (f.dataset.secOk === '1') { f.dataset.secOk = ''; return; }
    e.preventDefault(); e.stopImmediatePropagation();
    ensure().then(function () { f.dataset.secOk = '1'; if (f.requestSubmit) f.requestSubmit(); else f.submit(); }, function () {});
  }, true);

  window.ZMSec = { ensure: ensure, manage: manage, gate: gate };
})();
