/* ZooMo: yetkazilgan buyurtmadan keyin baholash (platforma, kuryer, hamkor). Server: main.py "BAHOLASH". */
(function () {
  var T = function (s) { return window.ZMI18n && ZMI18n.t ? ZMI18n.t(s) : s; };
  function esc(v) { return String(v == null ? '' : v).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;'); }
  var cfg, busy = false;
  var CSS = '.zmr-bg{position:fixed;inset:0;z-index:100000;background:rgba(2,6,23,.7);display:flex;align-items:flex-end;justify-content:center;padding:10px;font-family:system-ui,sans-serif}' +
    '.zmr-box{background:#fff;color:#0f172a;border-radius:22px;width:min(440px,100%);max-height:92vh;overflow:auto;padding:18px}' +
    '.zmr-h{font-weight:900;font-size:17px;margin:0 0 4px}.zmr-sub{font-size:12px;color:#64748b;margin:0 0 12px}.zmr-row{margin:10px 0}.zmr-lab{font-size:13px;font-weight:800;margin-bottom:4px}' +
    '.zmr-st{display:flex;gap:4px}.zmr-st button{background:none;border:0;font-size:32px;line-height:1;cursor:pointer;color:#cbd5e1;padding:0 2px}.zmr-st button.on{color:#f59e0b}' +
    '.zmr-ta{width:100%;box-sizing:border-box;border:1px solid #cbd5e1;border-radius:12px;padding:10px;font-size:14px;font-family:inherit;resize:none;background:#fff;color:#0f172a}' +
    '.zmr-btn{border:0;border-radius:12px;padding:12px;font-weight:800;font-size:14px;cursor:pointer}.zmr-ok{background:#f59e0b;color:#fff;flex:1}.zmr-no{background:rgba(148,163,184,.25);color:inherit}' +
    '.zmr-low{background:#fef2f2;color:#991b1b;border-radius:12px;padding:10px;font-size:13px;margin:10px 0}.zmr-err{color:#e11d48;font-size:12px;font-weight:700;min-height:16px}' +
    '@media (prefers-color-scheme:dark){.zmr-box{background:#1e293b;color:#e2e8f0}.zmr-ta{background:#0f172a;color:#e2e8f0;border-color:#475569}.zmr-sub{color:#94a3b8}.zmr-low{background:#450a0a;color:#fecaca}}';
  function call(path, body) {
    return fetch(cfg.base + path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(Object.assign({ init_data: cfg.initData() }, body || {})) })
      .then(function (r) { return r.json().catch(function () { return {}; }).then(function (d) { if (!r.ok) throw new Error(d.detail || T('Xatolik')); return d; }); });
  }
  function dismissed(id) { try { return localStorage.getItem('zm_rate_skip_' + id) === '1'; } catch (e) { return false; } }
  function skip(id) { try { localStorage.setItem('zm_rate_skip_' + id, '1'); } catch (e) {} }

  function show(o) {
    if (document.getElementById('zmr-bg')) return;
    var st = document.getElementById('zmr-css') || document.createElement('style'); st.id = 'zmr-css'; st.textContent = CSS; document.head.appendChild(st);
    var val = { platform: 0, courier: 0, partner: 0 };
    function row(key, label) {
      return '<div class="zmr-row"><div class="zmr-lab">' + esc(label) + '</div><div class="zmr-st" data-k="' + key + '">' +
        [1, 2, 3, 4, 5].map(function (i) { return '<button type="button" data-v="' + i + '" aria-label="' + i + '">★</button>'; }).join('') + '</div></div>';
    }
    var bg = document.createElement('div'); bg.className = 'zmr-bg'; bg.id = 'zmr-bg';
    bg.innerHTML = '<div class="zmr-box" role="dialog" aria-modal="true"><p class="zmr-h">⭐ ' + esc(T("Buyurtmani baholang")) + ' #' + o.id + '</p>' +
      '<p class="zmr-sub">' + esc(T("Fikringiz xizmatni yaxshilashga yordam beradi.")) + '</p>' +
      row('platform', T("ZooMo xizmati")) + (o.has_courier ? row('courier', T("Kuryer") + (o.courier ? ': ' + o.courier : '')) : '') + (o.has_partner ? row('partner', T("Hamkor") + (o.partner ? ': ' + o.partner : '')) : '') +
      '<div class="zmr-low" id="zmr-low" style="display:none">' + esc(T("Afsuski, xizmatimiz sizni qoniqtirmadi. Muammoni 💬 chat (fikr-mulohazalar) bo'limiga yozing — administratsiya tezda yordam beradi.")) + '</div>' +
      '<textarea class="zmr-ta" id="zmr-c" rows="2" maxlength="500" placeholder="' + esc(T("Izoh (ixtiyoriy)")) + '"></textarea><div class="zmr-err" id="zmr-err"></div>' +
      '<div style="display:flex;gap:8px"><button type="button" class="zmr-btn zmr-ok" id="zmr-send">' + esc(T("Yuborish")) + '</button><button type="button" class="zmr-btn zmr-no" id="zmr-later">' + esc(T("Keyinroq")) + '</button></div></div>';
    document.body.appendChild(bg);
    bg.querySelectorAll('.zmr-st').forEach(function (g) {
      g.onclick = function (e) {
        var b = e.target.closest('button'); if (!b) return; var v = +b.dataset.v; val[g.dataset.k] = v;
        g.querySelectorAll('button').forEach(function (x) { x.classList.toggle('on', +x.dataset.v <= v); });
        var low = Object.keys(val).some(function (k) { return val[k] && val[k] <= 3; });
        bg.querySelector('#zmr-low').style.display = low ? 'block' : 'none';
      };
    });
    bg.querySelector('#zmr-later').onclick = function () { skip(o.id); bg.remove(); };
    bg.querySelector('#zmr-send').onclick = function () {
      var err = bg.querySelector('#zmr-err');
      if (!val.platform && !val.courier && !val.partner) { err.textContent = T("Kamida bittasini baholang"); return; }
      call('/rating/submit', { order_id: o.id, platform: val.platform || null, courier: val.courier || null, partner: val.partner || null, comment: bg.querySelector('#zmr-c').value })
        .then(function (r) {
          if (r.low && cfg.onLow) { bg.remove(); cfg.onLow(o.id); }
          else { bg.querySelector('.zmr-box').innerHTML = '<p class="zmr-h" style="text-align:center;padding:18px 0">🙏 ' + esc(T("Rahmat! Bahoyingiz qabul qilindi.")) + '</p>'; setTimeout(function () { bg.remove(); }, 1600); }
        }).catch(function (e) { err.textContent = e.message; });
    };
  }
  function check() {
    if (busy || !cfg) return; busy = true;
    call('/rating/pending').then(function (d) { busy = false; if (d.order && !dismissed(d.order.id)) show(d.order); }).catch(function () { busy = false; });
  }
  window.ZMRating = { mount: function (c) { cfg = c; setTimeout(check, 3000); }, check: check };
})();
