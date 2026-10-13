/*!
 * ZooMo i18n — 7 til: o'zbek (lotin), o'zbek (kirill), rus, ingliz, qozoq, tojik, qirg'iz.
 * Manba til — o'zbek (lotin): sahifadagi matnlar o'zgarishsiz qoladi, tarjima DOM bo'yicha qo'llanadi
 * (matn tugunlari + placeholder/title/aria-label/alt/value), dinamik qo'shilgan elementlar ham
 * MutationObserver orqali tarjima qilinadi. Lug'atlar: /static/i18n/<til>.json
 * O'zbek kirill — lotindan avtomatik transliteratsiya.
 * API: ZMI18n.t(str) · ZMI18n.setLang(code) · ZMI18n.lang() · ZMI18n.translate(root) · ZMI18n.LANGS
 */
(function () {
    'use strict';
    var KEY = 'zm_lang', root = document.documentElement;
    var LANGS = [['uz', "O'zbekcha"], ['uzc', 'Ўзбекча'], ['ru', 'Русский'], ['en', 'English'], ['kk', 'Қазақша'], ['tg', 'Тоҷикӣ'], ['ky', 'Кыргызча']];
    var HTML_LANG = { uz: 'uz', uzc: 'uz-Cyrl', ru: 'ru', en: 'en', kk: 'kk', tg: 'tg', ky: 'ky' };
    var cur = 'uz', dict = {}, rx = [], loaded = {}, observer = null, busy = false;

    function read() { try { var v = localStorage.getItem(KEY); if (v) for (var i = 0; i < LANGS.length; i++) if (LANGS[i][0] === v) return v; } catch (e) {} return 'uz'; }

    /* ---------- normalizatsiya ---------- */
    function apos(s) { return s.replace(/\\'/g, "'").replace(/[ʻʼ‘’`´]/g, "'"); }
    function normKey(s) {
        s = apos(s).replace(/…/g, '...').replace(/\s+/g, ' ').trim();
        return s.replace(/(^|[^A-Za-z\d])\d[\d\s.,]*\d(?![A-Za-z])|(^|[^A-Za-z\d])\d(?![A-Za-z\d])/g, function (m, a, b) { return (a || b || '') + '{n}'; });
    }
    function nums(s) {
        var out = [], re = /(?:^|[^A-Za-z\d])(\d[\d\s.,]*\d|\d)(?![A-Za-z\d])/g, m;
        s = apos(s).replace(/\s+/g, ' ').trim();
        while ((m = re.exec(s))) { var v = m[1]; out.push(v); re.lastIndex = m.index + m[0].length; }
        return out;
    }
    function fill(tpl, vals) { var i = 0; return tpl.replace(/\{n\}/g, function () { return i < vals.length ? vals[i++] : ''; }); }

    /* ---------- o'zbek lotin → kirill ---------- */
    var PROTECT = /(ZooMo|ZOOMO|DELIVERY|SERVICE|P2P|PIN|GPS|STIR|MFO|Telegram|Uzcard|Humo|Visa|Mastercard|Click|Payme|Google|Yandex|2GIS|RawBT|Bluetooth|USB|COM|WhatsApp|Chrome|Edge|Safari|iPhone|iPad|iOS|Android|AirPrint|Windows|Wi-Fi|Excel|Instagram|Facebook|Leaflet|OSRM|Obsidian|Aurora|Sunset|Midnight|Gourmet Express|https?:\/\/\S+|\+?\d[\d\s()-]{6,}|\S+@\S+|\{n\})/g;
    function l2c(s) {
        var parts = s.split(PROTECT), out = '';
        for (var i = 0; i < parts.length; i++) out += i % 2 === 1 ? parts[i] : conv(parts[i]);
        return out;
    }
    function conv(s) {
        s = apos(s);
        var map2 = { "o'": 'ў', "O'": 'Ў', "g'": 'ғ', "G'": 'Ғ', 'sh': 'ш', 'Sh': 'Ш', 'SH': 'Ш', 'ch': 'ч', 'Ch': 'Ч', 'CH': 'Ч', 'yo': 'ё', 'Yo': 'Ё', 'YO': 'Ё', 'yu': 'ю', 'Yu': 'Ю', 'YU': 'Ю', 'ya': 'я', 'Ya': 'Я', 'YA': 'Я', 'ye': 'е', 'Ye': 'Е', 'YE': 'Е', 'ng': 'нг', 'Ng': 'Нг', 'NG': 'НГ', 'ts': 'ц', 'Ts': 'Ц' };
        var map1 = { a: 'а', b: 'б', d: 'д', e: 'е', f: 'ф', g: 'г', h: 'ҳ', i: 'и', j: 'ж', k: 'к', l: 'л', m: 'м', n: 'н', o: 'о', p: 'п', q: 'қ', r: 'р', s: 'с', t: 'т', u: 'у', v: 'в', x: 'х', y: 'й', z: 'з', "'": 'ъ' };
        var vowels = 'aeiouAEIOUоаеиуў', r = '', i = 0, n = s.length;
        while (i < n) {
            var two = s.substr(i, 2), ch = s[i], prev = i > 0 ? s[i - 1] : '';
            var atWordStart = !/[A-Za-z']/.test(prev);
            if (two.toLowerCase() === 'ts' && !atWordStart) { /* "ts" — odatda t+s */ }
            if (map2[two] && !(two.toLowerCase() === 'ts')) {
                // "ng": faqat so'z ichida (nga so'zboshi emas) — oddiy holat
                if (two.toLowerCase() === 'ya' || two.toLowerCase() === 'yu' || two.toLowerCase() === 'yo' || two.toLowerCase() === 'ye') {
                    // y+unli — oldidan unli bo'lsa ham "я/ю/ё/е" ishlatiladi
                }
                r += map2[two]; i += 2; continue;
            }
            var low = ch.toLowerCase();
            if (low === 'e' && atWordStart) { r += ch === 'E' ? 'Э' : 'э'; i++; continue; }
            if (map1[low] !== undefined) { var c = map1[low]; r += (ch !== low && low !== "'") ? c.toUpperCase() : c; i++; continue; }
            if (ch === 'ʻ' ) { r += 'ъ'; i++; continue; }
            r += ch; i++;
        }
        return r;
    }

    /* ---------- lug'at ---------- */
    function build(d) {
        dict = {}; rx = [];
        for (var k in d) {
            var nk = normKey(k); dict[nk] = d[k];
            if (nk.indexOf('{n}') > -1) {
                var esc = nk.replace(/[.*+?^$()|[\]\\]/g, '\\$&').replace(/\\\{n\\\}|\{n\}/g, '(.+?)');
                try { rx.push([new RegExp('^' + esc + '$'), d[k], nk.length]); } catch (e) {}
            }
        }
        rx.sort(function (a, b) { return b[2] - a[2]; });
    }

    var cache = {};
    function t(str) {
        if (str == null || cur === 'uz') return str;
        var s = String(str);
        if (!/[A-Za-zʻ‘’']/.test(s)) return s;
        var lead = s.match(/^\s*/)[0], trail = s.match(/\s*$/)[0], core = s.trim();
        if (!core) return s;
        var ck = cur + '|' + core;
        if (cache[ck] !== undefined) return lead + cache[ck] + trail;
        var res = core;
        if (cur === 'uzc') res = l2c(core);
        else {
            var nk = normKey(core), hit = dict[nk];
            if (hit !== undefined) res = hit.indexOf('{n}') > -1 ? fill(hit, nums(core)) : hit;
            else if (core.length > 2 && core === core.toUpperCase() && core !== core.toLowerCase() && dict[normKey(core.charAt(0) + core.slice(1).toLowerCase())] !== undefined) {
                res = t(core.charAt(0) + core.slice(1).toLowerCase()).toUpperCase();
            } else if (/[:.…!?]$/.test(core) && dict[normKey(core.replace(/[:.…!?]+$/, ''))] !== undefined) {
                res = t(core.replace(/[:.…!?]+$/, '')) + core.match(/[:.…!?]+$/)[0];
            } else {
                var c2 = apos(core).replace(/\s+/g, ' ');
                for (var i = 0; i < rx.length; i++) {
                    var m = rx[i][0].exec(c2);
                    if (m) { res = fill(rx[i][1], m.slice(1)); break; }
                }
            }
        }
        if (Object.keys(cache).length > 4000) cache = {};
        cache[ck] = res;
        return lead + res + trail;
    }

    /* ---------- DOM ---------- */
    var ATTRS = ['placeholder', 'title', 'aria-label', 'alt'];
    var SKIP = { SCRIPT: 1, STYLE: 1, TEXTAREA: 1, CODE: 1, PRE: 1, NOSCRIPT: 1 };
    var origText = new WeakMap(), origAttr = new WeakMap();

    function txtNode(n) {
        var p = n.parentNode; if (!p || SKIP[p.nodeName] || (p.closest && p.closest('[data-no-i18n]'))) return;
        var rec = origText.get(n);
        if (rec && n.data === rec.out) { var v = t(rec.src); if (v !== rec.out) { rec.out = v; n.data = v; } return; }
        var src = n.data; if (!src.trim()) return;
        var out = t(src); origText.set(n, { src: src, out: out });
        if (out !== src) n.data = out;
    }
    function attrs(el) {
        if (!el.getAttribute || (el.closest && el.closest('[data-no-i18n]'))) return;
        var rec = origAttr.get(el) || {};
        ATTRS.concat((el.tagName === 'INPUT' && /^(submit|button)$/.test(el.type)) ? ['value'] : []).forEach(function (a) {
            if (!el.hasAttribute(a)) return;
            var cv = el.getAttribute(a), r = rec[a];
            if (r && cv === r.out) { var nv = t(r.src); if (nv !== r.out) { r.out = nv; el.setAttribute(a, nv); } return; }
            var out = t(cv); rec[a] = { src: cv, out: out }; if (out !== cv) el.setAttribute(a, out);
        });
        origAttr.set(el, rec);
    }
    function walk(node) {
        if (node.nodeType === 3) { txtNode(node); return; }
        if (node.nodeType !== 1 || node.nodeName === 'SCRIPT' || node.nodeName === 'STYLE') return;
        attrs(node);
        if (SKIP[node.nodeName]) return;
        for (var c = node.firstChild; c; c = c.nextSibling) walk(c);
    }
    function translate(rootEl) {
        busy = true;
        try { walk(rootEl || document.body); } finally { busy = false; }
    }
    var titleSrc = null;
    function translateTitle() {
        if (titleSrc === null) titleSrc = document.title;
        document.title = t(titleSrc);
    }
    function observe() {
        if (observer || !window.MutationObserver) return;
        observer = new MutationObserver(function (muts) {
            if (busy || cur === 'uz') return;
            busy = true;
            try {
                muts.forEach(function (m) {
                    if (m.type === 'childList') m.addedNodes.forEach(walk);
                    else if (m.type === 'characterData') txtNode(m.target);
                    else if (m.type === 'attributes') attrs(m.target);
                });
            } finally { busy = false; }
        });
        observer.observe(document.body, { childList: true, subtree: true, characterData: true, attributes: true, attributeFilter: ATTRS });
    }

    /* alert/confirm/prompt xabarlarini ham tarjima qilamiz */
    ['alert', 'confirm', 'prompt'].forEach(function (fn) {
        var orig = window[fn]; if (!orig) return;
        window[fn] = function (msg) { var a = Array.prototype.slice.call(arguments); a[0] = t(msg); return orig.apply(window, a); };
    });

    function load(code, cb) {
        if (code === 'uz' || code === 'uzc') { dict = {}; rx = []; cb(); return; }
        if (loaded[code]) { build(loaded[code]); cb(); return; }
        fetch('/static/i18n/' + code + '.json', { cache: 'force-cache' }).then(function (r) { return r.json(); }).then(function (d) { loaded[code] = d; build(d); cb(); }, function () { dict = {}; rx = []; cb(); });
    }

    function apply(code, first) {
        cur = code; cache = {};
        root.setAttribute('lang', HTML_LANG[code] || 'uz'); root.setAttribute('data-lang', code);
        load(code, function () {
            cache = {};
            var run = function () { if (document.body) { translate(document.body); translateTitle(); observe(); } root.classList.remove('zm-i18n-pending'); };
            if (document.body) run(); else document.addEventListener('DOMContentLoaded', run);
            try { document.dispatchEvent(new CustomEvent('zmlangchange', { detail: { lang: code } })); } catch (e) {}
        });
    }
    function setLang(code) {
        try { localStorage.setItem(KEY, code); } catch (e) {}
        if (code === 'uz') { // asl matnni tiklash uchun — qayta yuklash eng ishonchli yo'l
            if (cur !== 'uz') { location.reload(); return; }
        }
        apply(code);
    }

    window.ZMI18n = { t: t, setLang: setLang, lang: function () { return cur; }, translate: translate, LANGS: LANGS };

    var start = read();
    if (start !== 'uz') {
        root.classList.add('zm-i18n-pending');
        var st = document.createElement('style'); st.textContent = 'html.zm-i18n-pending body{visibility:hidden}';
        (document.head || root).appendChild(st);
        setTimeout(function () { root.classList.remove('zm-i18n-pending'); }, 1500);
    }
    cur = start; root.setAttribute('lang', HTML_LANG[start] || 'uz');
    if (start !== 'uz') {
        if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', function () { apply(start, true); });
        else apply(start, true);
    }
})();
