/*!
 * ZooMo UI — rang mavzusi (accent), fon, kunduzgi/tungi rejim va til tanlash oynasi.
 * Barcha panellarda bir xil. ulash: <script src="/static/js/zm-ui.js"></script> (ui-mode.js dan keyin)
 *   <button data-zm-settings="all|lang|theme"></button>  — sarlavhaga qo'yiladi; bo'lmasa suzuvchi tugma.
 * JS API: ZMTheme.setAccent('emerald') | ZMTheme.get() | ZMTheme.ACCENTS
 */
(function () {
    // Zaxira: themes.css yuklanmagan/eskirgan keshda bo'lsa ham tugma va oyna ishlasin
    (function () {
        try {
            var CRIT = ".zm-set-btn { display: inline-flex; align-items: center; justify-content: center; width: 38px; height: 38px; flex-shrink: 0; border-radius: 12px; border: 1px solid rgba(255,255,255,.28); background: rgba(255,255,255,.14); color: #fff; font-size: 17px; line-height: 1; cursor: pointer; -webkit-tap-highlight-color: transparent; transition: transform .15s ease, background-color .2s ease; } .zm-set-btn:hover { background: rgba(255,255,255,.26); } .zm-set-btn:active { transform: scale(.92); } .zm-set-btn.zm-fab { position: fixed; top: 12px; right: 12px; z-index: 9990; background: rgba(255,255,255,.9); color: #0F172A; border-color: rgba(148,163,184,.4); box-shadow: 0 4px 20px rgba(0,0,0,.12); } html[data-mode=\"dark\"] .zm-set-btn.zm-fab { background: rgba(30,32,48,.9); color: #fff; border-color: rgba(255,255,255,.14); } .zm-pop-back { position: fixed; inset: 0; z-index: 99998; background: rgba(2,6,23,.35); display: none; } .zm-pop-back.open { display: block; } .zm-pop { position: fixed; z-index: 99999; width: min(330px, calc(100vw - 20px)); max-height: calc(100vh - 24px); overflow: auto; background: #fff; color: #0F172A; border: 1px solid #E2E8F0; border-radius: 18px; padding: 14px; box-shadow: 0 24px 60px -12px rgba(15,23,42,.35); font-family: 'Plus Jakarta Sans', 'Inter', -apple-system, sans-serif; display: none; text-align: left; } .zm-pop.open { display: block; animation: zmPop .18s ease both; } @keyframes zmPop { from { opacity: 0; transform: translateY(-6px) scale(.98); } to { opacity: 1; transform: none; } } html[data-mode=\"dark\"] .zm-pop { background: #14151f; color: #e5e7eb; border-color: rgba(255,255,255,.1); } .zm-pop h4 { font-size: 11px; font-weight: 800; letter-spacing: .06em; text-transform: uppercase; opacity: .6; margin: 12px 2px 7px; } .zm-pop h4:first-child { margin-top: 0; } .zm-row { display: flex; gap: 6px; } .zm-seg { flex: 1; padding: 9px 6px; border-radius: 11px; border: 1px solid rgba(148,163,184,.35); background: transparent; color: inherit; font-weight: 700; font-size: 13px; cursor: pointer; } .zm-seg.on { background: linear-gradient(135deg, var(--accent), var(--accent-dark)); color: #fff; border-color: transparent; } .zm-sw { display: grid; grid-template-columns: repeat(5, 1fr); gap: 9px; padding: 2px; } .zm-sw button { aspect-ratio: 1; border-radius: 50%; border: 3px solid transparent; cursor: pointer; box-shadow: 0 2px 8px rgba(0,0,0,.25), inset 0 0 0 2px rgba(255,255,255,.55); transition: transform .12s ease; } .zm-sw button:hover { transform: scale(1.1); } .zm-sw button.on { border-color: #0F172A; transform: scale(1.08); } html[data-mode=\"dark\"] .zm-sw button.on { border-color: #fff; } .zm-bgs { display: grid; grid-template-columns: repeat(4, 1fr); gap: 6px; } .zm-bgs button { height: 40px; border-radius: 10px; border: 2px solid transparent; cursor: pointer; font-size: 10px; font-weight: 800; color: #fff; text-shadow: 0 1px 3px rgba(0,0,0,.7); } .zm-bgs button.on { border-color: var(--accent); } .zm-langs { display: grid; grid-template-columns: 1fr 1fr; gap: 6px; } .zm-langs button { text-align: left; padding: 8px 10px; border-radius: 10px; border: 1px solid rgba(148,163,184,.35); background: transparent; color: inherit; font-size: 12.5px; font-weight: 600; cursor: pointer; } .zm-langs button.on { background: var(--accent-soft); border-color: var(--accent); font-weight: 800; } @media (max-width: 520px) { .zm-pop { left: 10px !important; right: 10px !important; top: auto !important; bottom: 10px; width: auto; } }";
            var st = document.createElement('style'); st.id = 'zm-critical'; st.textContent = CRIT;
            (document.head || document.documentElement).appendChild(st);
            var has = false, ls = document.getElementsByTagName('link');
            for (var i = 0; i < ls.length; i++) if ((ls[i].getAttribute('href') || '').indexOf('themes.css') > -1) has = true;
            if (!has) { var l = document.createElement('link'); l.rel = 'stylesheet'; l.href = '/static/css/themes.css?v=3'; (document.head || document.documentElement).appendChild(l); }
        } catch (e) {}
    })();
    'use strict';
    var KEY = 'zm_accent', root = document.documentElement;
    window.UI_MODE_NO_FAB = true; // ui-mode.js ning o'z suzuvchi tugmasi o'rniga umumiy oyna ishlatiladi

    var ACCENTS = [
        ['zoomo',   '#FFC21A', '#D98E00', '#FDE68A', '#A16207'],
        ['indigo',  '#6366f1', '#4338ca', '#c7d2fe', '#4338ca'],
        ['blue',    '#3b82f6', '#1d4ed8', '#bfdbfe', '#1d4ed8'],
        ['sky',     '#0ea5e9', '#0369a1', '#bae6fd', '#0369a1'],
        ['cyan',    '#06b6d4', '#0e7490', '#a5f3fc', '#0e7490'],
        ['teal',    '#14b8a6', '#0f766e', '#99f6e4', '#0f766e'],
        ['emerald', '#10b981', '#047857', '#a7f3d0', '#047857'],
        ['lime',    '#84cc16', '#4d7c0f', '#d9f99d', '#4d7c0f'],
        ['amber',   '#f59e0b', '#b45309', '#fde68a', '#b45309'],
        ['orange',  '#f97316', '#c2410c', '#fed7aa', '#c2410c'],
        ['red',     '#ef4444', '#b91c1c', '#fecaca', '#b91c1c'],
        ['rose',    '#f43f5e', '#be123c', '#fecdd3', '#be123c'],
        ['pink',    '#ec4899', '#be185d', '#fbcfe8', '#be185d'],
        ['fuchsia', '#d946ef', '#a21caf', '#f5d0fe', '#a21caf'],
        ['violet',  '#8b5cf6', '#6d28d9', '#ddd6fe', '#6d28d9']
    ];
    var BGS = [
        ['obsidian', 'Obsidian', 'linear-gradient(135deg,#05060a,#1a1b2e)'],
        ['aurora',   'Aurora',   'linear-gradient(135deg,#03110f,#14b8a6)'],
        ['sunset',   'Sunset',   'linear-gradient(135deg,#0e0610,#f43f5e)'],
        ['midnight', 'Midnight', 'linear-gradient(135deg,#040816,#3b82f6)']
    ];
    var DEFAULT_ACCENT = 'indigo';

    function find(k) { for (var i = 0; i < ACCENTS.length; i++) if (ACCENTS[i][0] === k) return ACCENTS[i]; return null; }
    function hexToRgb(h) { var n = parseInt(h.slice(1), 16); return [(n >> 16) & 255, (n >> 8) & 255, n & 255]; }
    function read() {
        try { var v = localStorage.getItem(KEY); if (v && find(v)) return v; } catch (e) {}
        // Eski hamkor mavzusidan ko'chirish
        try {
            var old = JSON.parse(localStorage.getItem('partner_theme') || 'null');
            if (old && old.main) for (var i = 0; i < ACCENTS.length; i++) if (ACCENTS[i][1].toLowerCase() === String(old.main).toLowerCase()) return ACCENTS[i][0];
        } catch (e) {}
        return DEFAULT_ACCENT;
    }
    function apply(k) {
        var a = find(k) || find(DEFAULT_ACCENT), rgb = hexToRgb(a[1]), s = root.style;
        s.setProperty('--accent', a[1]); s.setProperty('--accent-dark', a[2]);
        s.setProperty('--accent-soft', 'rgba(' + rgb.join(',') + ',.16)');
        s.setProperty('--accent-ink', a[3]); s.setProperty('--accent-ink-l', a[4]);
        root.setAttribute('data-accent', a[0]);
        var sw = document.querySelectorAll('.zm-sw button');
        for (var i = 0; i < sw.length; i++) sw[i].classList.toggle('on', sw[i].dataset.k === a[0]);
        return a[0];
    }
    function setAccent(k) {
        k = apply(k);
        try { localStorage.setItem(KEY, k); } catch (e) {}
        try { document.dispatchEvent(new CustomEvent('zmaccentchange', { detail: { accent: k } })); } catch (e) {}
    }
    function setBg(name) {
        if (name === 'obsidian') root.removeAttribute('data-bg'); else root.setAttribute('data-bg', name);
        try { localStorage.setItem('panel_bg', name === 'obsidian' ? '' : name); } catch (e) {}
        var b = document.querySelectorAll('.zm-bgs button');
        for (var i = 0; i < b.length; i++) b[i].classList.toggle('on', b[i].dataset.k === name);
    }
    apply(read()); // sahifa chizilishidan oldin

    window.ZMTheme = { ACCENTS: ACCENTS, setAccent: setAccent, get: function () { return root.getAttribute('data-accent'); }, setBg: setBg };

    // ---------- Oyna ----------
    var pop, back;
    function T(s) { return window.ZMI18n && ZMI18n.t ? ZMI18n.t(s) : s; }
    function build(kind) {
        var h = '';
        var mode = window.UIMode ? UIMode.get() : 'dark';
        if (kind !== 'lang') {
            h += '<h4 data-zm-t>Rejim</h4><div class="zm-row">' +
                 '<button class="zm-seg' + (mode === 'light' ? ' on' : '') + '" data-mode="light">☀️ <span>Kunduzgi</span></button>' +
                 '<button class="zm-seg' + (mode === 'dark' ? ' on' : '') + '" data-mode="dark">🌙 <span>Tungi</span></button></div>';
            h += '<h4>Rang mavzusi</h4><div class="zm-sw">';
            for (var i = 0; i < ACCENTS.length; i++) h += '<button type="button" data-k="' + ACCENTS[i][0] + '" title="' + ACCENTS[i][0] + '" style="background:linear-gradient(135deg,' + ACCENTS[i][1] + ',' + ACCENTS[i][2] + ')"></button>';
            h += '</div>';
            if (kind === 'all' && document.querySelector('[data-bg-capable], body[data-ui="glass"]')) {
                h += '<h4>Fon (tungi rejim)</h4><div class="zm-bgs">';
                for (var j = 0; j < BGS.length; j++) h += '<button type="button" data-k="' + BGS[j][0] + '" style="background:' + BGS[j][2] + '">' + BGS[j][1] + '</button>';
                h += '</div>';
            }
        }
        if (window.ZMI18n) {
            h += '<h4>Til</h4><div class="zm-langs">';
            var L = ZMI18n.LANGS, cur = ZMI18n.lang();
            for (var k = 0; k < L.length; k++) h += '<button type="button" data-lang="' + L[k][0] + '" class="' + (L[k][0] === cur ? 'on' : '') + '">' + L[k][1] + '</button>';
            h += '</div>';
        }
        return h;
    }
    function ensurePop() {
        if (pop) return;
        back = document.createElement('div'); back.className = 'zm-pop-back';
        pop = document.createElement('div'); pop.className = 'zm-pop'; pop.setAttribute('data-zm-pop', '');
        document.body.appendChild(back); document.body.appendChild(pop);
        back.addEventListener('click', close);
        pop.addEventListener('click', function (e) {
            var b = e.target.closest('button'); if (!b) return;
            if (b.dataset.mode && window.UIMode) { UIMode.set(b.dataset.mode); refresh(); }
            else if (b.closest('.zm-sw') && b.dataset.k) setAccent(b.dataset.k);
            else if (b.closest('.zm-bgs') && b.dataset.k) setBg(b.dataset.k);
            else if (b.dataset.lang && window.ZMI18n) { ZMI18n.setLang(b.dataset.lang); refresh(); }
        });
        document.addEventListener('keydown', function (e) { if (e.key === 'Escape') close(); });
    }
    var curKind = 'all', curBtn = null;
    function refresh() {
        pop.innerHTML = build(curKind);
        apply(root.getAttribute('data-accent') || read());
        var bg = root.getAttribute('data-bg') || 'obsidian', bs = pop.querySelectorAll('.zm-bgs button');
        for (var i = 0; i < bs.length; i++) bs[i].classList.toggle('on', bs[i].dataset.k === bg);
        if (window.ZMI18n && ZMI18n.translate) ZMI18n.translate(pop);
    }
    function open(btn, kind) {
        ensurePop(); curKind = kind || 'all'; curBtn = btn; refresh();
        pop.classList.add('open'); back.classList.add('open');
        var r = btn.getBoundingClientRect(), w = Math.min(330, window.innerWidth - 20);
        var ph = pop.offsetHeight;
        pop.style.top = Math.max(8, Math.min(r.bottom + 8, window.innerHeight - ph - 8)) + 'px';
        pop.style.left = Math.max(10, Math.min(window.innerWidth - w - 10, r.right - w)) + 'px';
    }
    function close() { if (pop) { pop.classList.remove('open'); back.classList.remove('open'); } }
    window.ZMSettings = { open: open, close: close };

    function boot() {
        var mounts = document.querySelectorAll('[data-zm-settings]');
        if (!mounts.length) {
            var f = document.createElement('button'); f.type = 'button'; f.className = 'zm-set-btn zm-fab';
            f.setAttribute('data-zm-settings', 'all'); document.body.appendChild(f); mounts = [f];
        }
        for (var i = 0; i < mounts.length; i++) (function (m) {
            m.classList.add('zm-set-btn'); m.setAttribute('type', 'button');
            if (!m.textContent.trim()) m.textContent = '🎨';
        })(mounts[i]);
        apply(read());
    }
    // Delegatsiya: tugma keyin qo'shilsa yoki boshqa skript bosishni to'ssa ham ishlaydi
    document.addEventListener('click', function (e) {
        var m = e.target && e.target.closest ? e.target.closest('[data-zm-settings]') : null;
        if (!m) return;
        e.preventDefault(); e.stopPropagation();
        if (pop && pop.classList.contains('open') && curBtn === m) { close(); return; }
        open(m, m.getAttribute('data-zm-settings') || 'all');
    }, true);
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot); else boot();
})();
