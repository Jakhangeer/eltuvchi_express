/*!
 * ZooMo kuryer navigatori — to'liq ekran xarita, burilishlar (OSRM), masofa (km), narxlar.
 * Faza 1: kuryer → do'kon (buyurtmani olish), Faza 2: do'kon → mijoz (yetkazish).
 * ZMNav.open(orderId) · ZMNav.close()
 */
(function () {
    'use strict';
    var T = function (s) { return window.ZMI18n && ZMI18n.t ? ZMI18n.t(s) : s; };
    var OSRM = 'https://router.project-osrm.org/route/v1/driving/';
    var S = { map: null, order: null, phase: 1, me: null, layers: {}, watch: null, follow: true, lastRoute: null, lastRouteAt: 0, steps: [], wake: null, timer: null };
    var $ = function (id) { return document.getElementById(id); };

    function hav(a, b) {
        var R = 6371000, r = Math.PI / 180, dLat = (b[0] - a[0]) * r, dLng = (b[1] - a[1]) * r;
        var x = Math.sin(dLat / 2) * Math.sin(dLat / 2) + Math.cos(a[0] * r) * Math.cos(b[0] * r) * Math.sin(dLng / 2) * Math.sin(dLng / 2);
        return 2 * R * Math.atan2(Math.sqrt(x), Math.sqrt(1 - x));
    }
    function fmtKm(m) { return m < 950 ? Math.round(m / 10) * 10 + ' m' : (m / 1000).toFixed(1) + ' km'; }
    function fmtMin(s) { var m = Math.max(1, Math.round(s / 60)); return m < 60 ? m + ' ' + T('daqiqa') : Math.floor(m / 60) + ' ' + T('soat') + ' ' + (m % 60) + ' ' + T('daqiqa'); }
    function money(n) { return String(Math.round(Number(n) || 0)).replace(/\B(?=(\d{3})+(?!\d))/g, ' ') + " " + T("so'm"); }
    function num(v) { var n = parseFloat(v); return isNaN(n) ? null : n; }

    function readOrder(el) {
        var d = el.dataset;
        return {
            id: d.orderId, brand: d.brand, partner: [num(d.pLat), num(d.pLng)], client: [num(d.cLat), num(d.cLng)],
            address: d.address, clientName: d.clientName, clientPhone: d.clientPhone, partnerPhone: d.partnerPhone,
            total: num(d.total) || 0, fee: num(d.fee) || 0, earn: num(d.earn) || 0, cash: d.cash === '1', comment: d.comment || '',
            items: d.items || ''
        };
    }
    function hasPt(p) { return p && p[0] != null && p[1] != null; }
    function target() { return S.phase === 1 ? S.order.partner : S.order.client; }
    function phaseKey() { return 'zm_phase_' + S.order.id; }

    function icon(html, size) { return L.divIcon({ html: html, className: '', iconSize: [size, size], iconAnchor: [size / 2, size / 2] }); }
    var ICON_P = '<div class="zmn-pin" style="background:#f59e0b">🏪</div>';
    var ICON_C = '<div class="zmn-pin" style="background:#2563eb">📍</div>';
    function meIcon(h) { return icon('<div class="zmn-me"><div class="zmn-me-arrow" style="transform:rotate(' + (h || 0) + 'deg)"></div></div>', 34); }

    function ensureMap() {
        if (S.map) return;
        S.map = L.map('zmn-map', { zoomControl: false, attributionControl: false }).setView([41.5504, 60.6318], 14);
        var dark = L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', { maxZoom: 20, subdomains: 'abcd' });
        var light = L.tileLayer('https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png', { maxZoom: 20, subdomains: 'abcd' });
        var streets = L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', { maxZoom: 19 });
        var sat = L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}', { maxZoom: 19 });
        L.control.layers({ '🌑': dark, '☀️': light, '🗺️': streets, '🛰️': sat }, {}, { position: 'topright', collapsed: true }).addTo(S.map);
        if (window.UIMode) UIMode.attachMapTheme(S.map, dark, light); else dark.addTo(S.map);
        S.map.on('dragstart', function () { S.follow = false; $('zmn-follow').classList.remove('on'); });
    }

    function drawStatic() {
        var m = S.map, o = S.order;
        ['p', 'c'].forEach(function (k) { if (S.layers[k]) { m.removeLayer(S.layers[k]); S.layers[k] = null; } });
        if (hasPt(o.partner)) S.layers.p = L.marker(o.partner, { icon: icon(ICON_P, 34) }).addTo(m).bindPopup(o.brand || T("Do'kon"));
        if (hasPt(o.client)) S.layers.c = L.marker(o.client, { icon: icon(ICON_C, 34) }).addTo(m).bindPopup(T('Mijoz'));
    }

    function stepText(st) {
        var mv = st.maneuver || {}, t = mv.type, mod = mv.modifier || '', nm = st.name ? ' — ' + st.name : '';
        var dir = { left: ['⬅️', 'Chapga buring'], right: ['➡️', "O'ngga buring"], 'slight left': ['↖️', 'Biroz chapga'], 'slight right': ['↗️', "Biroz o'ngga"],
                    'sharp left': ['↙️', 'Keskin chapga'], 'sharp right': ['↘️', "Keskin o'ngga"], straight: ['⬆️', "To'g'ri yuring"], uturn: ['↩️', 'Orqaga buriling'] }[mod];
        if (t === 'arrive') return ['🏁', T('Manzilga yetib keldingiz')];
        if (t === 'depart') return ['⬆️', T("Yo'lga chiqing") + nm];
        if (t === 'roundabout' || t === 'rotary' || t === 'roundabout turn') return ['🔄', T("Aylanma yo'lga kiring") + (mv.exit ? ', ' + mv.exit + '-' + T('chiqish') : '') + nm];
        if (dir) return [dir[0], T(dir[1]) + nm];
        return ['⬆️', T('Davom eting') + nm];
    }

    function route(force) {
        var from = S.me || (S.phase === 2 && hasPt(S.order.partner) ? S.order.partner : null), to = target();
        if (!hasPt(to)) { $('zmn-instr').textContent = T('Manzil koordinatalari yo\'q — tashqi navigatordan foydalaning'); renderInfo(null); return; }
        if (!from) { renderInfo(null); return; }
        var now = Date.now();
        if (!force && S.lastRoute && hav(S.lastRoute, from) < 30 && now - S.lastRouteAt < 20000) return;
        S.lastRoute = from; S.lastRouteAt = now;
        var url = OSRM + from[1] + ',' + from[0] + ';' + to[1] + ',' + to[0] + '?overview=full&geometries=geojson&steps=true';
        fetch(url).then(function (r) { return r.ok ? r.json() : null; }).then(function (d) {
            if (!d || !d.routes || !d.routes[0]) { fallback(from, to); return; }
            var rt = d.routes[0];
            if (S.layers.route) S.map.removeLayer(S.layers.route);
            S.layers.route = L.polyline(rt.geometry.coordinates.map(function (c) { return [c[1], c[0]]; }), { color: '#38bdf8', weight: 7, opacity: .9 }).addTo(S.map);
            S.steps = (rt.legs && rt.legs[0] && rt.legs[0].steps) || [];
            S.dist = rt.distance; S.dur = rt.duration;
            renderInfo(rt);
            if (force && !S.follow) S.map.fitBounds(S.layers.route.getBounds(), { padding: [60, 60] });
        }).catch(function () { fallback(from, to); });
        // 2-faza uzunligi (do'kon → mijoz) — jami masofa uchun
        if (S.phase === 1 && hasPt(S.order.partner) && hasPt(S.order.client) && S.leg2 == null) {
            S.leg2 = 0;
            fetch(OSRM + S.order.partner[1] + ',' + S.order.partner[0] + ';' + S.order.client[1] + ',' + S.order.client[0] + '?overview=false').then(function (r) { return r.json(); }).then(function (d) {
                if (d && d.routes && d.routes[0]) { S.leg2 = d.routes[0].distance; renderInfo(); }
            }).catch(function () { S.leg2 = hav(S.order.partner, S.order.client) * 1.3; renderInfo(); });
        }
    }
    function fallback(from, to) {
        var m = hav(from, to) * 1.3;
        if (S.layers.route) S.map.removeLayer(S.layers.route);
        S.layers.route = L.polyline([from, to], { color: '#38bdf8', weight: 5, dashArray: '8 8' }).addTo(S.map);
        S.dist = m; S.dur = m / 8; S.steps = []; renderInfo({ distance: m, duration: m / 8, approx: true });
        $('zmn-arrow').textContent = '⬆️'; $('zmn-instr').textContent = T('Chiziq bo\'yicha manzil tomon yuring'); $('zmn-next').textContent = '';
    }

    function renderInfo(rt) {
        var o = S.order;
        if (rt) { S.dist = rt.distance; S.dur = rt.duration; }
        if (S.dist != null) {
            $('zmn-dist').textContent = (rt && rt.approx ? '~' : '') + fmtKm(S.dist);
            $('zmn-eta').textContent = fmtMin(S.dur || 0);
        } else { $('zmn-dist').textContent = '—'; $('zmn-eta').textContent = ''; }
        // keyingi burilish
        var next = S.steps && S.steps[1] ? S.steps[1] : (S.steps && S.steps[0]);
        if (next) {
            var tx = stepText(next), dm = S.me && next.maneuver && next.maneuver.location ? hav(S.me, [next.maneuver.location[1], next.maneuver.location[0]]) : (S.steps[0] ? S.steps[0].distance : next.distance);
            $('zmn-arrow').textContent = tx[0]; $('zmn-instr').textContent = tx[1]; $('zmn-next').textContent = fmtKm(dm);
        } else if (!hasPt(target())) { $('zmn-arrow').textContent = '📍'; }
        // jami masofa (faza 1: yo'l + do'kon→mijoz)
        var tot = '';
        if (S.phase === 1 && S.dist != null && S.leg2) tot = T('Jami yo\'l') + ': ' + fmtKm(S.dist + S.leg2);
        else if (S.phase === 1 && hasPt(o.partner) && hasPt(o.client)) tot = T("Do'kon → mijoz") + ': ~' + fmtKm(hav(o.partner, o.client) * 1.3);
        $('zmn-total').textContent = tot;
        // yetib keldik?
        var near = S.me && hasPt(target()) && hav(S.me, target()) < 60;
        $('zmn-arrived').classList.toggle('hidden', !near);
        $('zmn-arrived').textContent = S.phase === 1 ? '📦 ' + T("Do'konga yetdingiz — buyurtmani oldim") : '✅ ' + T('Mijozga yetdingiz — yetkazdim');
    }

    function onPos(p) {
        var c = p.coords; S.me = [c.latitude, c.longitude];
        var h = c.heading != null && !isNaN(c.heading) ? c.heading : (S.heading || 0); S.heading = h;
        if (!S.layers.me) S.layers.me = L.marker(S.me, { icon: meIcon(h), zIndexOffset: 1000 }).addTo(S.map);
        else { S.layers.me.setLatLng(S.me); S.layers.me.setIcon(meIcon(h)); }
        if (S.follow) S.map.setView(S.me, Math.max(S.map.getZoom(), 16), { animate: true });
        route(false);
        // serverga ham yuboramiz (admin/hamkor/mijoz kuzatuvi uchun) — sahifaning o'z funksiyasi bor bo'lsa
        renderInfo();
    }

    function buildSheet() {
        var o = S.order, p1 = S.phase === 1;
        $('zmn-phase').textContent = p1 ? '1/2 · ' + T("Do'konga boring") + ': ' + (o.brand || '') : '2/2 · ' + T('Mijozga boring') + ': ' + (o.clientName || '');
        $('zmn-addr').textContent = p1 ? (T("Do'kon") + ': ' + (o.brand || '')) : ('📍 ' + (o.address || ''));
        var rows = '';
        rows += '<div class="zmn-row"><span>' + T('Taom narxi') + '</span><b>' + money(o.total) + '</b></div>';
        rows += '<div class="zmn-row"><span>' + T('Yetkazish narxi') + '</span><b>' + money(o.fee) + '</b></div>';
        rows += '<div class="zmn-row ' + (o.cash ? 'warn' : 'ok') + '"><span>' + (o.cash ? T('Mijozdan olinadi (naqd)') : T("To'langan (karta)")) + '</span><b>' + (o.cash ? money(o.total + o.fee) : money(0)) + '</b></div>';
        rows += '<div class="zmn-row ok"><span>' + T('Sizning haqingiz') + '</span><b>' + money(o.earn) + '</b></div>';
        if (o.comment) rows += '<div class="zmn-note">💬 ' + o.comment.replace(/[<>&]/g, '') + '</div>';
        $('zmn-prices').innerHTML = rows;
        $('zmn-act').textContent = p1 ? '📦 ' + T('Buyurtmani oldim') : '✅ ' + T('Yetkazdim');
        $('zmn-act').className = 'zmn-act ' + (p1 ? 'p1' : 'p2');
        var tel = p1 ? o.partnerPhone : o.clientPhone;
        $('zmn-call').href = tel ? 'tel:' + tel : '#'; $('zmn-call').style.display = tel ? '' : 'none';
        var t = target(), q = hasPt(t) ? t[0] + ',' + t[1] : encodeURIComponent(o.address || '');
        $('zmn-ext-g').href = hasPt(t) ? 'https://www.google.com/maps/dir/?api=1&destination=' + q + '&travelmode=driving' : '#';
        $('zmn-ext-y').href = hasPt(t) ? 'https://yandex.com/maps/?rtext=~' + q + '&rtt=auto' : 'https://yandex.com/maps/?text=' + q;
        $('zmn-ext-2').href = hasPt(t) ? 'https://2gis.uz/?m=' + t[1] + ',' + t[0] + '/17' : '#';
        $('zmn-step1').className = 'zmn-dot' + (p1 ? ' on' : ' done'); $('zmn-step2').className = 'zmn-dot' + (p1 ? '' : ' on');
    }

    function setPhase(n) {
        S.phase = n; try { localStorage.setItem(phaseKey(), String(n)); } catch (e) {}
        S.lastRoute = null; S.steps = []; S.dist = null;
        buildSheet(); drawStatic(); renderInfo();
        route(true);
        var t = target();
        if (S.me && hasPt(t)) S.map.fitBounds([S.me, t], { padding: [90, 90] });
        else if (hasPt(t)) S.map.setView(t, 15);
    }

    function open(orderId) {
        var el = document.querySelector('[data-nav-order="' + orderId + '"]'); if (!el) return;
        if (typeof L === 'undefined') { alert(T('Xarita yuklanmadi — internetni tekshiring')); return; }
        S.order = readOrder(el); S.leg2 = null; S.follow = true; S.me = null; S.heading = 0;
        var ph = 1; try { ph = Number(localStorage.getItem('zm_phase_' + orderId)) === 2 ? 2 : 1; } catch (e) {}
        $('zmn').classList.add('open'); document.body.classList.add('zmn-lock');
        ensureMap(); setTimeout(function () { S.map.invalidateSize(); }, 80);
        $('zmn-follow').classList.add('on');
        setPhase(ph);
        if (navigator.geolocation) S.watch = navigator.geolocation.watchPosition(onPos, function () { $('zmn-instr').textContent = T('GPS signali yo\'q — joylashuvga ruxsat bering'); }, { enableHighAccuracy: true, maximumAge: 2000, timeout: 20000 });
        try { if (navigator.wakeLock) navigator.wakeLock.request('screen').then(function (w) { S.wake = w; }, function () {}); } catch (e) {}
        try { sessionStorage.setItem('zm_nav_auto_' + orderId, '1'); } catch (e) {}
    }
    function close() {
        $('zmn').classList.remove('open'); document.body.classList.remove('zmn-lock');
        if (S.watch != null) { navigator.geolocation.clearWatch(S.watch); S.watch = null; }
        try { if (S.wake) S.wake.release(); } catch (e) {} S.wake = null;
    }
    function action() {
        if (S.phase === 1) { setPhase(2); return; }
        if (!confirm(T('Buyurtma mijozga yetkazildimi?'))) return;
        $('zmn-act').disabled = true;
        fetch('/courier/orders/' + S.order.id + '/delivered', { method: 'POST', credentials: 'same-origin' }).then(function () {
            try { localStorage.removeItem(phaseKey()); } catch (e) {} location.reload();
        }, function () { $('zmn-act').disabled = false; alert(T('Xatolik. Qaytadan urinib ko\'ring.')); });
    }
    function recenter() {
        S.follow = true; $('zmn-follow').classList.add('on');
        if (S.me) S.map.setView(S.me, 17, { animate: true });
        else if (hasPt(target())) S.map.setView(target(), 16);
    }
    function showAll() {
        var pts = []; if (S.me) pts.push(S.me); if (hasPt(S.order.partner)) pts.push(S.order.partner); if (hasPt(S.order.client)) pts.push(S.order.client);
        if (pts.length > 1) { S.follow = false; $('zmn-follow').classList.remove('on'); S.map.fitBounds(pts, { padding: [90, 90] }); }
    }

    document.addEventListener('click', function (e) {
        var b = e.target.closest('[data-zmn]'); if (!b) return;
        var a = b.getAttribute('data-zmn');
        if (a === 'open') open(b.getAttribute('data-id'));
        else if (a === 'close') close(); else if (a === 'act') action(); else if (a === 'follow') recenter(); else if (a === 'all') showAll();
        else if (a === 'arrived') action();
    });
    document.addEventListener('DOMContentLoaded', function () {
        // Buyurtma qabul qilingach navigator o'zi ochiladi (har buyurtma uchun bir marta)
        var first = document.querySelector('[data-nav-order]');
        if (first) { var id = first.getAttribute('data-nav-order'), seen = null; try { seen = sessionStorage.getItem('zm_nav_auto_' + id); } catch (e) {} if (!seen) setTimeout(function () { open(id); }, 700); }
    });

    // ---- Yangi (bo'sh) buyurtmalar: masofalar (km) ----
    function fillDistances() {
        document.querySelectorAll('[data-dist-order]').forEach(function (el) {
            var p = [num(el.dataset.pLat), num(el.dataset.pLng)], c = [num(el.dataset.cLat), num(el.dataset.cLng)], me = [num(el.dataset.myLat), num(el.dataset.myLng)], parts = [];
            if (hasPt(me) && hasPt(p)) parts.push('🧭 ' + T("Sizdan do'konga") + ': ~' + fmtKm(hav(me, p) * 1.3));
            if (hasPt(p) && hasPt(c)) parts.push('📏 ' + T("Do'kon → mijoz") + ': ~' + fmtKm(hav(p, c) * 1.3));
            el.textContent = parts.join(' · ');
        });
    }
    document.addEventListener('DOMContentLoaded', fillDistances);
    window.ZMNav = { open: open, close: close };
})();
