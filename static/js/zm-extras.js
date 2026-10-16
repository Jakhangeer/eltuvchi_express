/* ZooMo mijoz ilovasi: 📦 Shaxsiy buyum yuborish va 🚕 Taxi.
   Admin yoqmagan xizmat umuman ko'rinmaydi. Ishlatish: ZMExtras.mount({initData: function(){...}, bottom: 86}) */
(function () {
  'use strict';
  var S = { init: null, flags: null, map: null, mk: {}, mode: 'A', pts: { A: null, B: null }, timer: null, tab: null, open: false };
  function T(s) { try { return (window.ZMi18n && ZMi18n.t) ? ZMi18n.t(s) : s; } catch (e) { return s; } }
  function e(v) { return String(v == null ? '' : v).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;'); }
  function money(n) { return Math.round(n || 0).toLocaleString('ru-RU') + " so'm"; }
  function post(url, body) {
    body = body || {}; body.init_data = S.init();
    return fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
      .then(function (r) { return r.json().catch(function () { return {}; }).then(function (j) {
        if (!r.ok) { var d = j && j.detail; if (Array.isArray(d)) d = d.map(function (x) { return x.msg; }).join('; '); throw new Error(d || ('Xato ' + r.status)); }
        return j; }); });
  }
  var ST = { new: "Kuryer qidirilmoqda", accepted: 'Kuryer qabul qildi', picked: "Yo'lda", delivered: 'Topshirildi', cancelled: 'Bekor qilindi',
    searching: 'Haydovchi qidirilmoqda', arrived: 'Haydovchi yetib keldi', started: 'Safar davom etmoqda', completed: 'Yakunlandi' };

  function css() {
    if (document.getElementById('zmx-css')) return;
    var st = document.createElement('style'); st.id = 'zmx-css';
    st.textContent = '.zmx-fab{position:fixed;left:12px;z-index:60;display:flex;flex-direction:column;gap:8px}' +
      '.zmx-fab button{background:#4f46e5;color:#fff;border:0;border-radius:999px;padding:10px 14px;font-weight:700;font-size:13px;box-shadow:0 4px 14px rgba(0,0,0,.25)}' +
      '.zmx-ov{position:fixed;inset:0;z-index:90;background:rgba(0,0,0,.5);display:none}.zmx-ov.on{display:block}' +
      '.zmx-sh{position:absolute;left:0;right:0;bottom:0;max-height:94vh;overflow:auto;background:var(--zmx-bg,#fff);color:var(--zmx-fg,#0f172a);border-radius:20px 20px 0 0;padding:14px 14px 28px}' +
      '@media(prefers-color-scheme:dark){.zmx-sh{--zmx-bg:#1e293b;--zmx-fg:#f1f5f9}.zmx-sh input,.zmx-sh textarea,.zmx-sh select{background:#0f172a;color:#f1f5f9;border-color:#475569}}' +
      'html.dark .zmx-sh,body.dark .zmx-sh{--zmx-bg:#1e293b;--zmx-fg:#f1f5f9}' +
      '.zmx-sh h3{font-weight:800;font-size:17px;margin:0 0 8px}.zmx-sh label{display:block;font-size:12px;margin:8px 0 2px;opacity:.8}' +
      '.zmx-sh input,.zmx-sh textarea,.zmx-sh select{width:100%;border:1px solid #cbd5e1;border-radius:10px;padding:8px;font-size:14px;box-sizing:border-box}' +
      '.zmx-btn{background:#4f46e5;color:#fff;border:0;border-radius:12px;padding:11px 14px;font-weight:700;width:100%;margin-top:10px}' +
      '.zmx-btn.sec{background:#e2e8f0;color:#0f172a}.zmx-btn.red{background:#dc2626}' +
      '.zmx-tabs{display:flex;gap:6px;margin-bottom:8px}.zmx-tabs button{flex:1;padding:8px;border-radius:10px;border:1px solid #cbd5e1;background:transparent;color:inherit;font-weight:700;font-size:13px}.zmx-tabs button.on{background:#4f46e5;color:#fff;border-color:#4f46e5}' +
      '.zmx-card{border:1px solid #cbd5e1;border-radius:12px;padding:10px;margin:8px 0;font-size:13px}' +
      '.zmx-map{height:220px;border-radius:12px;margin:6px 0;z-index:0}.zmx-row{display:flex;gap:6px}.zmx-row>*{flex:1}' +
      '.zmx-x{float:right;background:transparent;border:0;font-size:22px;color:inherit}.zmx-err{color:#dc2626;font-size:13px;margin-top:6px}.zmx-code{font-size:26px;font-weight:900;letter-spacing:6px;text-align:center;background:#eef2ff;color:#3730a3;border-radius:12px;padding:8px;margin:6px 0}';
    document.head.appendChild(st);
  }

  function ensureDom() {
    if (document.getElementById('zmx-ov')) return;
    var ov = document.createElement('div'); ov.id = 'zmx-ov'; ov.className = 'zmx-ov';
    ov.innerHTML = '<div class="zmx-sh" id="zmx-sh"></div>';
    ov.addEventListener('click', function (ev) { if (ev.target === ov) close(); });
    document.body.appendChild(ov);
  }
  function close() { S.open = false; document.getElementById('zmx-ov').classList.remove('on'); clearInterval(S.timer); if (S.map) { S.map.remove(); S.map = null; S.mk = {}; } }
  function sh() { return document.getElementById('zmx-sh'); }

  function openSheet(kind) {
    S.kind = kind; S.open = true; S.pts = { A: null, B: null }; S.mode = 'A'; S.tab = null;
    document.getElementById('zmx-ov').classList.add('on');
    render();
    clearInterval(S.timer); S.timer = setInterval(function () { if (S.open && S.tab === 'my') renderMy(); }, 7000);
  }

  function render() {
    var k = S.kind, title = k === 'taxi' ? '🚕 Taxi' : "📦 Shaxsiy buyum yuborish";
    sh().innerHTML = '<button class="zmx-x" id="zmx-close">✕</button><h3>' + title + '</h3><div class="zmx-tabs"><button id="zmx-t-new">' + (k === 'taxi' ? 'Chaqirish' : "Yangi jo'natma") + '</button><button id="zmx-t-my">' + (k === 'taxi' ? 'Safarim' : "Jo'natmalarim") + '</button></div><div id="zmx-body"></div>';
    document.getElementById('zmx-close').onclick = close;
    document.getElementById('zmx-t-new').onclick = function () { S.tab = 'new'; tabs(); renderNew(); };
    document.getElementById('zmx-t-my').onclick = function () { S.tab = 'my'; tabs(); renderMy(); };
    // faol buyurtma bo'lsa, shu yerdan boshlaymiz
    S.tab = 'new'; tabs(); renderNew();
    post(k === 'taxi' ? '/api/taxi/my' : '/api/parcel/my').then(function (r) {
      var act = k === 'taxi' ? (r.ride && ['searching', 'accepted', 'arrived', 'started'].indexOf(r.ride.status) >= 0)
        : (r.items || []).some(function (x) { return ['new', 'accepted', 'picked'].indexOf(x.status) >= 0; });
      if (act && S.open) { S.tab = 'my'; tabs(); renderMy(); }
    }).catch(function () {});
  }
  function tabs() { ['new', 'my'].forEach(function (t) { var b = document.getElementById('zmx-t-' + t); if (b) b.className = S.tab === t ? 'on' : ''; }); }

  // ---------- xarita ----------
  function initMap(needB) {
    if (!window.L) { document.getElementById('zmx-map').innerHTML = '<div class="zmx-card">Xarita yuklanmadi — manzilni matn bilan yozing va «📍 Joylashuvim» ni bosing.</div>'; return; }
    var c = [41.3111, 69.2797];
    S.map = L.map('zmx-map', { zoomControl: true }).setView(c, 12);
    L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', { maxZoom: 19, attribution: '© OSM' }).addTo(S.map);
    S.map.on('click', function (ev) { setPt(S.mode, ev.latlng.lat, ev.latlng.lng); });
    if (navigator.geolocation) navigator.geolocation.getCurrentPosition(function (p) { if (S.map) S.map.setView([p.coords.latitude, p.coords.longitude], 14); }, function () {}, { timeout: 5000 });
  }
  function setPt(m, lat, lng) {
    S.pts[m] = { lat: lat, lng: lng };
    if (S.map && window.L) {
      if (S.mk[m]) S.map.removeLayer(S.mk[m]);
      S.mk[m] = L.marker([lat, lng], { title: m }).addTo(S.map).bindTooltip(m === 'A' ? 'Olish (A)' : 'Borish (B)');
    }
    var h = document.getElementById('zmx-hint'); if (h) h.textContent = (S.pts.A ? 'A ✓ ' : 'A — ') + (S.pts.B ? 'B ✓' : 'B —');
    quote();
  }
  function mode(m) { S.mode = m; ['A', 'B'].forEach(function (x) { var b = document.getElementById('zmx-m' + x); if (b) b.className = 'zmx-btn ' + (x === m ? '' : 'sec'); }); }
  function geoMe() {
    if (!navigator.geolocation) return alert('Joylashuv qo\'llab-quvvatlanmaydi');
    navigator.geolocation.getCurrentPosition(function (p) {
      setPt('A', p.coords.latitude, p.coords.longitude); if (S.map) S.map.setView([p.coords.latitude, p.coords.longitude], 15);
    }, function () { alert('Joylashuvga ruxsat bering yoki xaritadan belgilang'); }, { enableHighAccuracy: true, timeout: 10000 });
  }

  function mapBlock(needB) {
    return '<div class="zmx-row"><button class="zmx-btn" id="zmx-mA" style="margin-top:4px">A — ' + (S.kind === 'taxi' ? 'Qayerdan' : 'Olish joyi') + '</button>' +
      '<button class="zmx-btn sec" id="zmx-mB" style="margin-top:4px">B — ' + (S.kind === 'taxi' ? 'Qayerga' : 'Yetkazish joyi') + '</button></div>' +
      '<button class="zmx-btn sec" id="zmx-geo" style="margin-top:6px">📍 Joylashuvim (A)</button>' +
      '<div id="zmx-map" class="zmx-map"></div><div id="zmx-hint" style="font-size:12px;opacity:.7">A — B —</div>';
  }
  function wireMap() {
    document.getElementById('zmx-mA').onclick = function () { mode('A'); };
    document.getElementById('zmx-mB').onclick = function () { mode('B'); };
    document.getElementById('zmx-geo').onclick = geoMe;
    initMap(); mode('A');
    ['A', 'B'].forEach(function (m) { if (S.pts[m]) setPt(m, S.pts[m].lat, S.pts[m].lng); });
  }

  function quote() {
    var q = document.getElementById('zmx-quote'); if (!q) return;
    var A = S.pts.A, B = S.pts.B;
    if (S.kind === 'parcel') {
      if (!A || !B) { q.textContent = "Narxni ko'rish uchun xaritada A va B ni belgilang"; return; }
      var w = parseFloat((document.getElementById('zmx-w') || {}).value) || 1;
      post('/api/parcel/quote', { pickup_lat: A.lat, pickup_lng: A.lng, drop_lat: B.lat, drop_lng: B.lng, weight_kg: w })
        .then(function (r) { q.innerHTML = '<b>' + money(r.price) + '</b> · ' + r.distance_km + ' km'; }).catch(function (x) { q.textContent = x.message; });
    } else {
      if (!A) { q.textContent = 'Avval A (qayerdan) ni belgilang'; return; }
      var b = { pickup_lat: A.lat, pickup_lng: A.lng }; if (B) { b.drop_lat = B.lat; b.drop_lng = B.lng; }
      post('/api/taxi/quote', b).then(function (r) { q.innerHTML = (r.exact ? '' : 'Eng kam narx: ') + '<b>' + money(r.price) + '</b>' + (r.distance_km ? ' · ' + r.distance_km + ' km' : '') + ' · naqd'; }).catch(function (x) { q.textContent = x.message; });
    }
  }

  // ---------- yangi buyurtma ----------
  function renderNew() {
    if (S.map) { S.map.remove(); S.map = null; S.mk = {}; }
    var b = document.getElementById('zmx-body');
    if (S.kind === 'parcel') {
      b.innerHTML = mapBlock() +
        '<label>Olish manzili</label><input id="zmx-pa" maxlength="300" placeholder="Masalan: Chilonzor 5-kvartal, 12-uy">' +
        '<label>Yetkazish manzili</label><input id="zmx-da" maxlength="300">' +
        '<div class="zmx-row"><div><label>Qabul qiluvchi ismi</label><input id="zmx-rn" maxlength="100"></div><div><label>Telefoni</label><input id="zmx-rp" type="tel" maxlength="20" placeholder="+998..."></div></div>' +
        '<label>Nima yuborasiz?</label><input id="zmx-it" maxlength="300" placeholder="Masalan: hujjatlar, kitob, kalit">' +
        '<div class="zmx-row"><div><label>Og\'irligi (kg)</label><input id="zmx-w" type="number" step="0.1" min="0.1" value="1"></div><div><label>O\'lchami</label><select id="zmx-sz"><option value="small">Kichik</option><option value="medium">O\'rta</option><option value="large">Katta</option></select></div></div>' +
        '<label>Izoh (ixtiyoriy)</label><textarea id="zmx-cm" rows="2" maxlength="300"></textarea>' +
        '<label style="display:flex;gap:8px;align-items:flex-start;opacity:1"><input type="checkbox" id="zmx-ok" style="width:auto;margin-top:3px"><span id="zmx-proh" style="font-size:12px"></span></label>' +
        '<div id="zmx-quote" style="margin-top:8px;font-size:15px"></div><div class="zmx-err" id="zmx-err"></div>' +
        '<button class="zmx-btn" id="zmx-go">Jo\'natmani yuborish</button>';
      document.getElementById('zmx-proh').textContent = (S.flags && S.flags.prohibited) || '';
      wireMap(); quote();
      document.getElementById('zmx-w').oninput = quote;
      document.getElementById('zmx-go').onclick = createParcel;
    } else {
      b.innerHTML = mapBlock() +
        '<label>Qayerdan (manzil/mo\'ljal)</label><input id="zmx-pa" maxlength="300" placeholder="Masalan: Navoiy ko\'chasi 10">' +
        '<label>Qayerga (ixtiyoriy)</label><input id="zmx-da" maxlength="300">' +
        '<div class="zmx-row"><div><label>Yo\'lovchilar</label><select id="zmx-ps"><option>1</option><option>2</option><option>3</option><option>4</option></select></div><div><label>Telefon</label><input id="zmx-ph" type="tel" maxlength="20"></div></div>' +
        '<label>Izoh</label><textarea id="zmx-cm" rows="2" maxlength="300"></textarea>' +
        '<div id="zmx-quote" style="margin-top:8px;font-size:15px"></div><div class="zmx-err" id="zmx-err"></div>' +
        '<button class="zmx-btn" id="zmx-go">🚕 Taxi chaqirish</button>';
      wireMap(); quote();
      document.getElementById('zmx-go').onclick = createRide;
    }
  }
  function val(id) { var x = document.getElementById(id); return x ? x.value.trim() : ''; }
  function err(m) { var x = document.getElementById('zmx-err'); if (x) x.textContent = m || ''; }
  function createParcel() {
    err('');
    if (!S.pts.A || !S.pts.B) return err('Xaritada A va B nuqtalarini belgilang');
    var btn = document.getElementById('zmx-go'); btn.disabled = true;
    post('/api/parcel/create', { pickup_lat: S.pts.A.lat, pickup_lng: S.pts.A.lng, drop_lat: S.pts.B.lat, drop_lng: S.pts.B.lng,
      weight_kg: parseFloat(val('zmx-w')) || 1, pickup_address: val('zmx-pa'), drop_address: val('zmx-da'), receiver_name: val('zmx-rn'),
      receiver_phone: val('zmx-rp'), item_desc: val('zmx-it'), size: val('zmx-sz'), comment: val('zmx-cm'), confirm_allowed: !!document.getElementById('zmx-ok').checked })
      .then(function () { S.tab = 'my'; tabs(); renderMy(); }).catch(function (x) { err(x.message); btn.disabled = false; });
  }
  function createRide() {
    err('');
    if (!S.pts.A) return err('Xaritada A (qayerdan) nuqtasini belgilang');
    var btn = document.getElementById('zmx-go'); btn.disabled = true;
    var b = { pickup_lat: S.pts.A.lat, pickup_lng: S.pts.A.lng, pickup_address: val('zmx-pa') || 'Xaritadagi nuqta', drop_address: val('zmx-da'),
      passengers: parseInt(val('zmx-ps')) || 1, client_phone: val('zmx-ph'), comment: val('zmx-cm') };
    if (S.pts.B) { b.drop_lat = S.pts.B.lat; b.drop_lng = S.pts.B.lng; }
    post('/api/taxi/create', b).then(function () { S.tab = 'my'; tabs(); renderMy(); }).catch(function (x) { err(x.message); btn.disabled = false; });
  }

  // ---------- mening buyurtmalarim ----------
  function renderMy() {
    if (S.map) { S.map.remove(); S.map = null; S.mk = {}; }
    var b = document.getElementById('zmx-body'); if (!b) return;
    if (S.kind === 'taxi') {
      post('/api/taxi/my').then(function (r) {
        var x = r.ride; if (!x) { b.innerHTML = '<div class="zmx-card">Hali safar yo\'q</div>'; return; }
        var h = '<div class="zmx-card"><b>#' + x.id + ' · ' + e(ST[x.status] || x.status) + '</b><br>' + e(x.pickup_address) + (x.drop_address ? ' → ' + e(x.drop_address) : '') + '<br>' + money(x.price) + ' (naqd)';
        if (x.driver) h += '<br>🚗 ' + e(x.driver.name) + ' · ' + e(x.driver.car) + ' · <b>' + e(x.driver.plate) + '</b><br><a href="tel:' + e(x.driver.phone) + '">📞 ' + e(x.driver.phone) + '</a>';
        h += '</div>';
        if (['searching', 'accepted', 'arrived'].indexOf(x.status) >= 0) h += '<button class="zmx-btn red" id="zmx-cx">Bekor qilish</button>';
        b.innerHTML = h;
        var c = document.getElementById('zmx-cx'); if (c) c.onclick = function () { if (confirm('Bekor qilinsinmi?')) post('/api/taxi/cancel', { id: x.id }).then(renderMy).catch(function (z) { alert(z.message); }); };
      }).catch(function (x) { b.innerHTML = '<div class="zmx-err">' + e(x.message) + '</div>'; });
    } else {
      post('/api/parcel/my').then(function (r) {
        var it = r.items || []; if (!it.length) { b.innerHTML = '<div class="zmx-card">Hali jo\'natma yo\'q</div>'; return; }
        b.innerHTML = it.map(function (x) {
          var live = ['new', 'accepted', 'picked'].indexOf(x.status) >= 0;
          return '<div class="zmx-card"><b>#' + x.id + ' · ' + e(ST[x.status] || x.status) + '</b><br>' + e(x.pickup_address) + ' → ' + e(x.drop_address) + '<br>' + e(x.item_desc) + ' · ' + money(x.price) + ' (kuryerga naqd)' +
            (x.courier ? '<br>🛵 ' + e(x.courier.name) + ' · <a href="tel:' + e(x.courier.phone) + '">' + e(x.courier.phone) + '</a>' : '') +
            (live && x.delivery_code ? '<div style="font-size:12px;margin-top:6px">Qabul qiluvchi kuryerga shu kodni aytsin:</div><div class="zmx-code">' + e(x.delivery_code) + '</div>' : '') +
            (['new', 'accepted'].indexOf(x.status) >= 0 ? '<button class="zmx-btn red" data-cx="' + x.id + '">Bekor qilish</button>' : '') + '</div>';
        }).join('');
        b.querySelectorAll('[data-cx]').forEach(function (z) { z.onclick = function () { if (confirm('Bekor qilinsinmi?')) post('/api/parcel/cancel', { id: +z.dataset.cx }).then(renderMy).catch(function (q) { alert(q.message); }); }; });
      }).catch(function (x) { b.innerHTML = '<div class="zmx-err">' + e(x.message) + '</div>'; });
    }
  }

  function mount(opts) {
    S.init = opts.initData;
    fetch('/api/extras/flags').then(function (r) { return r.json(); }).then(function (f) {
      S.flags = f; if (!f.parcel && !f.taxi) return;
      css(); ensureDom();
      var d = document.getElementById('zmx-fab') || document.createElement('div'); d.id = 'zmx-fab'; d.className = 'zmx-fab'; d.style.bottom = ((opts.bottom || 86) + 'px');
      d.innerHTML = (f.taxi ? '<button data-k="taxi">🚕 Taxi</button>' : '') + (f.parcel ? '<button data-k="parcel">📦 Jo\'natma</button>' : '');
      d.onclick = function (ev) { var k = ev.target.dataset && ev.target.dataset.k; if (k) openSheet(k); };
      if (!d.parentNode) document.body.appendChild(d);
    }).catch(function () {});
  }
  window.ZMExtras = { mount: mount };
})();
