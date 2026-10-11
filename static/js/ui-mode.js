/*!
 * UI Mode — Light / Dark rejim boshqaruvchisi (barcha 5 panel uchun umumiy)
 *
 * ULASH: har bir sahifaning <head> qismiga, BOSHQA skriptlardan oldin:
 *     <script src="/static/js/ui-mode.js"></script>
 * (sinxron yuklanadi — sahifa "chaqnab" qolmasligi uchun rejim darhol qo'llanadi)
 *
 * ISHLATISH:
 *   - <button data-mode-toggle>  → bosilganda Light ⇄ Dark almashadi
 *     (ichida <span data-mode-icon></span> bo'lsa, ☀️/🌙 shu yerga yoziladi)
 *   - Agar sahifada hech qanday [data-mode-toggle] bo'lmasa, o'ng yuqori
 *     burchakda suzuvchi tugma AVTOMATIK qo'shiladi.
 *   - JS API:  UIMode.get() | UIMode.set('light'|'dark') | UIMode.toggle()
 *   - Hodisa:  document.addEventListener('uimodechange', e => e.detail.mode)
 *   - Xarita:  UIMode.attachMapTheme(map, darkLayer, lightLayer)
 */
(function () {
    'use strict';

    var KEY = 'ui_mode';
    // Birinchi marta kirgan foydalanuvchi uchun standart rejim: 'light' yoki 'dark'
    var DEFAULT_MODE = 'light';
    var root = document.documentElement;

    function read() {
        try {
            var v = localStorage.getItem(KEY);
            if (v === 'light' || v === 'dark') return v;
        } catch (e) { /* localStorage yopiq bo'lishi mumkin */ }
        return DEFAULT_MODE;
    }

    function paintChrome(mode) {
        root.setAttribute('data-mode', mode);
        root.style.colorScheme = mode;
        var color = mode === 'light' ? '#F8FAFC' : '#05060A';
        try {
            var meta = document.querySelector('meta[name="theme-color"]');
            if (!meta && document.head) {
                meta = document.createElement('meta');
                meta.setAttribute('name', 'theme-color');
                document.head.appendChild(meta);
            }
            if (meta) meta.setAttribute('content', color);
            var tg = window.Telegram && window.Telegram.WebApp;
            if (tg) {
                if (tg.setHeaderColor) tg.setHeaderColor(color);
                if (tg.setBackgroundColor) tg.setBackgroundColor(color);
            }
        } catch (e) { /* Telegram eski versiyada bo'lsa — e'tiborsiz */ }
    }

    function syncButtons(mode) {
        var btns = document.querySelectorAll('[data-mode-toggle]');
        for (var i = 0; i < btns.length; i++) {
            var b = btns[i];
            var icon = b.querySelector('[data-mode-icon]') || b;
            // Bosilganda O'TILADIGAN rejim belgisi ko'rsatiladi
            var glyph = mode === 'light' ? '🌙' : '☀️';
            if (icon === b && b.children.length) { /* ichki elementlarni buzmaymiz */ } else { icon.textContent = glyph; }
            b.setAttribute('aria-pressed', mode === 'dark' ? 'true' : 'false');
            b.setAttribute('title', mode === 'light' ? 'Tungi rejimga o\u2018tish' : 'Kunduzgi rejimga o\u2018tish');
        }
    }

    function set(mode, opts) {
        if (mode !== 'light' && mode !== 'dark') return;
        var changed = root.getAttribute('data-mode') !== mode;
        try { localStorage.setItem(KEY, mode); } catch (e) {}
        paintChrome(mode);
        syncButtons(mode);
        if (changed && !(opts && opts.silent)) {
            try { document.dispatchEvent(new CustomEvent('uimodechange', { detail: { mode: mode } })); } catch (e) {}
        }
    }

    function get() { return root.getAttribute('data-mode') || read(); }
    function toggle() { set(get() === 'light' ? 'dark' : 'light'); }

    /* Xarita fonini rejimga moslab beradi: Light → Voyager (yorug'), Dark → Dark Matter.
       Foydalanuvchi qatlam menyusidan sxema/sputnikni tanlagan bo'lsa, tegmaydi. */
    function attachMapTheme(map, darkLayer, lightLayer) {
        function apply(mode) {
            if (mode === 'light' && map.hasLayer(darkLayer)) { map.removeLayer(darkLayer); lightLayer.addTo(map); }
            else if (mode === 'dark' && map.hasLayer(lightLayer)) { map.removeLayer(lightLayer); darkLayer.addTo(map); }
        }
        if (get() === 'light') lightLayer.addTo(map); else darkLayer.addTo(map);
        document.addEventListener('uimodechange', function (e) { apply(e.detail.mode); });
    }

    // 1) Darhol qo'llaymiz (birinchi paint'dan oldin — "chaqnash" bo'lmaydi)
    paintChrome(read());

    window.UIMode = { get: get, set: set, toggle: toggle, attachMapTheme: attachMapTheme,
                      lightTiles: 'https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png' };

    // 2) DOM tayyor bo'lgach tugmalarni ulaymiz
    function boot() {
        if (!document.querySelector('[data-mode-toggle]') && !window.UI_MODE_NO_FAB) {
            var st = document.createElement('style');
            st.textContent =
                '.uim-fab{position:fixed;top:14px;right:14px;z-index:9999;width:42px;height:42px;border-radius:14px;' +
                'border:1px solid rgba(148,163,184,.35);background:rgba(255,255,255,.85);color:#0F172A;font-size:18px;' +
                'cursor:pointer;backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px);' +
                'box-shadow:0 4px 20px rgba(0,0,0,.08);transition:transform .15s ease,box-shadow .2s ease}' +
                '.uim-fab:hover{box-shadow:0 8px 26px rgba(0,0,0,.14)}.uim-fab:active{transform:scale(.92)}' +
                'html[data-mode="dark"] .uim-fab{background:rgba(255,255,255,.08);color:#fff;border-color:rgba(255,255,255,.14)}';
            document.head.appendChild(st);
            var fab = document.createElement('button');
            fab.type = 'button';
            fab.className = 'uim-fab';
            fab.setAttribute('data-mode-toggle', '');
            document.body.appendChild(fab);
        }
        document.addEventListener('click', function (e) {
            var t = e.target.closest && e.target.closest('[data-mode-toggle]');
            if (t) { e.preventDefault(); toggle(); if (navigator.vibrate) navigator.vibrate(10); }
        });
        syncButtons(get());
    }
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
    else boot();
})();
