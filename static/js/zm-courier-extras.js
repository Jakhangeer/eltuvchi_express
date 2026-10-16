/* Kuryer paneli: 📦 shaxsiy jo'natmalar (admin yoqsa) va 🚕 taxi haydovchi sahifasiga havola. */
(function () {
  'use strict';
  function e(v) { return String(v == null ? '' : v).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;'); }
  function money(n) { return Math.round(n || 0).toLocaleString('ru-RU') + " so'm"; }
  function api(u, m, b) {
    var o = { method: m || 'GET', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' } }; if (b) o.body = JSON.stringify(b);
    return fetch(u, o).then(function (r) { return r.json().catch(function () { return {}; }).then(function (j) {
      if (!r.ok) { var d = j && j.detail; if (Array.isArray(d)) d = d.map(function (x) { return x.msg; }).join('; '); throw new Error(d || ('Xato ' + r.status)); } return j; }); });
  }
  var timer = null;
  function css() {
    if (document.getElementById('zmcx-css')) return;
    var s = document.createElement('style'); s.id = 'zmcx-css';
    s.textContent = '.zmcx-fab{position:fixed;left:12px;bottom:86px;z-index:60;display:flex;flex-direction:column;gap:8px}.zmcx-fab button,.zmcx-fab a{background:#4f46e5;color:#fff;border:0;border-radius:999px;padding:10px 14px;font-weight:700;font-size:13px;text-decoration:none;text-align:center;box-shadow:0 4px 14px rgba(0,0,0,.25)}' +
      '.zmcx-ov{position:fixed;inset:0;z-index:90;background:rgba(0,0,0,.5);display:none}.zmcx-ov.on{display:block}.zmcx-sh{position:absolute;left:0;right:0;bottom:0;max-height:92vh;overflow:auto;background:#fff;color:#0f172a;border-radius:20px 20px 0 0;padding:14px 14px 28px}' +
      '@media(prefers-color-scheme:dark){.zmcx-sh{background:#1e293b;color:#f1f5f9}}.zmcx-card{border:1px solid #cbd5e1;border-radius:12px;padding:10px;margin:8px 0;font-size:13px}.zmcx-b{background:#4f46e5;color:#fff;border:0;border-radius:10px;padding:9px 12px;font-weight:700;margin:6px 6px 0 0}.zmcx-b.g{background:#16a34a}.zmcx-x{float:right;background:transparent;border:0;font-size:22px;color:inherit}';
    document.head.appendChild(s);
  }
  function render(body) {
    Promise.all([api('/courier/parcels/available'), api('/courier/parcels/mine')]).then(function (r) {
      var av = r[0].items || [], mine = r[1].items || [], h = '<h4 style="font-weight:800">Mening jo\'natmalarim</h4>';
      h += mine.length ? mine.map(function (x) {
        return '<div class="zmcx-card"><b>#' + x.id + ' · ' + (x.status === 'accepted' ? 'Olishga boring' : "Yo'lda") + '</b><br>📍 ' + e(x.pickup_address) + '<br>🏁 ' + e(x.drop_address) + '<br>👤 ' + e(x.receiver_name) + ' · <a href="tel:' + e(x.receiver_phone) + '">' + e(x.receiver_phone) + '</a><br>' + e(x.item_desc) + ' · ' + money(x.price) + ' (naqd olinadi)<br>' +
          (x.status === 'accepted' ? '<button class="zmcx-b" data-a="picked" data-id="' + x.id + '">Buyumni oldim</button>' : '<button class="zmcx-b g" data-a="delivered" data-id="' + x.id + '">Topshirdim (kod)</button>') + '</div>';
      }).join('') : '<div class="zmcx-card">Faol jo\'natma yo\'q</div>';
      h += '<h4 style="font-weight:800;margin-top:12px">Yangi jo\'natmalar</h4>';
      h += av.length ? av.map(function (x) {
        return '<div class="zmcx-card"><b>#' + x.id + '</b> · ' + (x.distance_km || '?') + ' km · ' + money(x.price) + '<br>📍 ' + e(x.pickup_address) + '<br>🏁 ' + e(x.drop_address) + '<br>' + e(x.item_desc) + ' · ' + (x.weight_kg || 1) + ' kg<br><button class="zmcx-b" data-a="accept" data-id="' + x.id + '">Qabul qilish</button></div>';
      }).join('') : '<div class="zmcx-card">Hozircha yangi jo\'natma yo\'q</div>';
      body.innerHTML = h;
    }).catch(function (x) { body.innerHTML = '<div class="zmcx-card">' + e(x.message) + '</div>'; });
  }
  function open() {
    var ov = document.getElementById('zmcx-ov');
    if (!ov) {
      ov = document.createElement('div'); ov.id = 'zmcx-ov'; ov.className = 'zmcx-ov';
      ov.innerHTML = '<div class="zmcx-sh"><button class="zmcx-x" id="zmcx-close">✕</button><h3 style="font-weight:800;font-size:17px">📦 Shaxsiy jo\'natmalar</h3><div id="zmcx-body"></div></div>';
      document.body.appendChild(ov);
      ov.addEventListener('click', function (ev) {
        var t = ev.target;
        if (t === ov || t.id === 'zmcx-close') { ov.classList.remove('on'); clearInterval(timer); return; }
        var a = t.dataset && t.dataset.a; if (!a) return;
        var id = t.dataset.id, body = document.getElementById('zmcx-body');
        if (a === 'delivered') {
          var c = prompt('Qabul qiluvchi aytgan 4 xonali kodni kiriting:'); if (!c) return;
          api('/courier/parcels/' + id + '/delivered', 'POST', { code: c.trim() }).then(function () { render(body); }).catch(function (x) { alert(x.message); });
        } else api('/courier/parcels/' + id + '/' + a, 'POST').then(function () { render(body); }).catch(function (x) { alert(x.message); });
      });
    }
    ov.classList.add('on'); var b = document.getElementById('zmcx-body'); render(b);
    clearInterval(timer); timer = setInterval(function () { if (ov.classList.contains('on')) render(b); }, 10000);
  }
  function mount() {
    fetch('/api/extras/flags').then(function (r) { return r.json(); }).then(function (f) {
      if (!f.parcel && !f.taxi) return;
      css(); var d = document.createElement('div'); d.className = 'zmcx-fab';
      d.innerHTML = (f.parcel ? '<button id="zmcx-open">📦 Jo\'natmalar</button>' : '') + (f.taxi ? '<a href="/taxi/driver/page">🚕 Taxi haydovchi</a>' : '');
      document.body.appendChild(d); var o = document.getElementById('zmcx-open'); if (o) o.onclick = open;
    }).catch(function () {});
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', mount); else mount();
})();
