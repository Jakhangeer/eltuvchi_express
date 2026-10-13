/*!
 * ZooMo Live — sahifani QAYTA YUKLAMASDAN, sezdirmay yangilab turish.
 *
 *  - [data-live="nom"] belgisi qo'yilgan bloklar har bir necha soniyada serverdan yangidan olinib, FAQAT o'zgargan bo'lsa almashtiriladi.
 *  - Foydalanuvchi shu blok ichida yozayotgan bo'lsa (input/textarea/select fokusda), yoki fayl tanlagan bo'lsa — almashtirilmaydi (keyingi safar uriniladi).
 *  - Blok ichidagi formalar (buyurtmani qabul qilish va h.k.) sahifani yangilamasdan, fon rejimida yuboriladi.
 *  - Aktiv bo'lim (tab), scroll, ochiq <details> holati saqlanadi.
 *  Foydalanish:  ZMLive.start({ interval: 4000 })   ·   ZMLive.refresh(true)   ·   hodisa: document 'zm:live-updated'
 */
(function () {
    'use strict';
    var hashes = {}, busy = false, timer = null, stopped = false, cfg = { interval: 5000 }, lastOk = 0, errCount = 0;

    function hash(s) { var h = 5381, i = s.length; while (i) h = (h * 33) ^ s.charCodeAt(--i); return (h >>> 0).toString(36) + ':' + s.length; }

    function blocked(el) {
        var a = document.activeElement;
        if (a && el.contains(a) && /^(INPUT|TEXTAREA|SELECT)$/.test(a.tagName) && a.type !== 'button' && a.type !== 'submit' && a.type !== 'checkbox' && a.type !== 'radio') return true;
        var files = el.querySelectorAll('input[type=file]');
        for (var i = 0; i < files.length; i++) if (files[i].files && files[i].files.length) return true;
        if (el.querySelector('[data-live-hold]')) return true;
        return false;
    }

    function openDetailsState(el) {
        var arr = []; el.querySelectorAll('details').forEach(function (d, i) { if (d.open) arr.push(i); }); return arr;
    }
    function restoreDetails(el, arr) { var ds = el.querySelectorAll('details'); arr.forEach(function (i) { if (ds[i]) ds[i].open = true; }); }

    function swap(el, html) {
        var sc = [], scrollers = el.querySelectorAll('*');
        scrollers.forEach(function (n, i) { if (n.scrollTop > 0) sc.push([i, n.scrollTop]); });
        var own = el.scrollTop, det = openDetailsState(el);
        el.innerHTML = html;
        el.setAttribute('data-live-swapped', '1');
        restoreDetails(el, det);
        var now = el.querySelectorAll('*'); sc.forEach(function (p) { if (now[p[0]]) now[p[0]].scrollTop = p[1]; });
        el.scrollTop = own;
    }

    function refresh(force) {
        if (busy || stopped) return Promise.resolve(false);
        if (document.hidden && !force) return Promise.resolve(false);
        busy = true;
        return fetch(location.pathname + location.search, {
            credentials: 'same-origin', cache: 'no-store', headers: { 'X-Requested-With': 'zm-live', 'Accept': 'text/html' }
        }).then(function (res) {
            if (res.redirected && /^\/login/.test(new URL(res.url).pathname) && !/^\/login/.test(location.pathname)) { stopped = true; return null; } // sessiya tugagan
            if (res.status === 401 || res.status === 403) { stopped = true; return null; }
            if (!res.ok) throw new Error('HTTP ' + res.status);
            return res.text();
        }).then(function (txt) {
            if (!txt) return false;
            errCount = 0; lastOk = Date.now();
            var doc = new DOMParser().parseFromString(txt, 'text/html');
            var changed = [];
            doc.querySelectorAll('[data-live]').forEach(function (n) {
                var key = n.getAttribute('data-live'), html = n.innerHTML, h = hash(html);
                var cur = document.querySelector('[data-live="' + key + '"]');
                if (!cur) return;
                if (hashes[key] === undefined) { hashes[key] = h; return; }      // birinchi marta — faqat asos (baseline)
                if (hashes[key] === h) return;
                if (!force && blocked(cur)) return;                               // foydalanuvchi yozayotgan bo'lsa — keyinroq
                if (force && cur.querySelector('input[type=file]') && blocked(cur)) return;
                swap(cur, html); hashes[key] = h; changed.push(key);
            });
            if (changed.length) {
                document.dispatchEvent(new CustomEvent('zm:live-updated', { detail: { keys: changed } }));
                if (window.ZMI18n && ZMI18n.translate) { try { changed.forEach(function (k) { ZMI18n.translate(document.querySelector('[data-live="' + k + '"]')); }); } catch (e) {} }
            }
            return changed.length > 0;
        }).catch(function (e) {
            errCount++; if (errCount === 1) console.warn('ZMLive:', e && e.message);
            return false;
        }).then(function (r) { busy = false; return r; });
    }

    // Blok ichidagi formalarni sahifani yangilamasdan yuborish
    function submitForm(form, submitter) {
        var method = (form.getAttribute('method') || 'GET').toUpperCase();
        var fd;
        try { fd = new FormData(form, submitter || undefined); } catch (e) { fd = new FormData(form); if (submitter && submitter.name) fd.append(submitter.name, submitter.value); }
        var action = form.getAttribute('action') || location.pathname;
        var opts = { method: method, credentials: 'same-origin', headers: { 'X-Requested-With': 'zm-live', 'Accept': 'text/html,application/json' }, redirect: 'follow' };
        if (method !== 'GET') opts.body = fd;
        return fetch(action, opts).then(function (res) {
            if (res.ok) return null;
            return res.json().then(function (j) { return (j && j.detail) ? (typeof j.detail === 'string' ? j.detail : JSON.stringify(j.detail)) : 'Xatolik (' + res.status + ')'; }, function () { return 'Xatolik (' + res.status + ')'; });
        }).then(function (err) {
            if (err) { alert(err); return false; }
            return refresh(true).then(function () { return true; });
        }).catch(function () { alert("Aloqa xatosi — qayta urinib ko'ring"); return false; });
    }

    document.addEventListener('submit', function (e) {
        var f = e.target;
        if (!f || e.defaultPrevented || !f.closest || !f.closest('[data-live]') || f.hasAttribute('data-no-soft') || f.target === '_blank') return;
        var m = (f.getAttribute('method') || 'GET').toUpperCase();
        if (m === 'GET') return;
        e.preventDefault();
        var btn = e.submitter || f.querySelector('button[type="submit"]'), old = btn ? btn.disabled : false;
        if (btn) btn.disabled = true;
        submitForm(f, e.submitter).then(function () { if (btn) btn.disabled = old; });
    });

    function start(o) {
        if (o) for (var k in o) cfg[k] = o[k];
        if (timer) clearInterval(timer);
        refresh(true);                                     // asosiy (baseline) holatni olish
        timer = setInterval(function () { refresh(false); }, cfg.interval);
        document.addEventListener('visibilitychange', function () { if (!document.hidden) refresh(false); });
        window.addEventListener('online', function () { refresh(false); });
    }

    window.ZMLive = { start: start, refresh: refresh, submitForm: submitForm, lastOk: function () { return lastOk; } };
})();
