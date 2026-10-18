/* ZooMo: yashirin ma'lumotlarni (kartalar, rekvizitlar) faqat maxfiy javob bilan ochish. Server: secure_action (sec2fa.py). */
(function () {
  var T = function (s) { return window.ZMI18n && ZMI18n.t ? ZMI18n.t(s) : s; };
  function esc(v) { return String(v == null ? '' : v).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;'); }
  function getJson(url) {
    // zm-secure.js fetch o'rami 428 kelsa oynani ochib, so'rovni qayta yuboradi
    return fetch(url, { credentials: 'same-origin' }).then(function (r) { return r.json().catch(function () { return {}; }).then(function (d) { if (!r.ok) throw new Error(typeof d.detail === 'string' ? d.detail : T('Maxfiy javob tasdiqlanmadi')); return d; }); });
  }
  function lockedView(box, n, onShow) {
    box.innerHTML = '<div class="rounded-xl p-3 mb-2 flex items-center justify-between gap-2" style="background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.08)"><div class="text-xs"><b>🔒 ' + esc(T('Kartalar yashirin')) + '</b><div class="text-[11px] opacity-70">' + n + ' ' + esc(T('ta karta')) + '</div></div>' +
      '<button type="button" class="px-3 py-1.5 rounded-lg text-xs font-bold" style="background:#f59e0b;color:#fff" data-rv>👁 ' + esc(T("Ko'rsatish")) + '</button></div>';
    box.querySelector('[data-rv]').onclick = onShow;
  }
  function cards(opt) {
    var box = document.getElementById(opt.box); if (!box) return;
    var n = parseInt(box.dataset.count || '0', 10);
    if (!n) { box.innerHTML = '<p class="text-xs text-slate-500 mb-2">' + esc(T("Hali karta qo'shilmagan.")) + '</p>'; return; }
    function show() {
      getJson(opt.base + '/cards').then(function (d) {
        box.innerHTML = (d.cards || []).map(function (c) {
          return '<div class="flex items-center justify-between rounded-xl p-2.5 mb-2" style="background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.08)"><div class="min-w-0"><p class="text-xs font-bold truncate">' + esc(c.bank_name) + ' <span class="text-slate-500 font-normal">(' + esc(String(c.card_type || '').toUpperCase()) + ')</span></p>' +
            '<p class="text-[11px] text-slate-400 font-mono">' + esc(c.masked_number) + ' · ' + esc(c.expire) + '</p><p class="text-[10px] text-slate-500 truncate">' + esc(c.card_holder_name) + '</p></div>' +
            '<form action="' + opt.base + '/cards/' + c.id + '/delete" method="POST" class="js-haptic-form" onsubmit="return confirm(\'' + esc(T("Karta o'chirilsinmi?")).replace(/'/g, "\\'") + '\');"><button type="submit" class="text-rose-400 text-xs px-2 py-1">🗑️</button></form></div>';
        }).join('') + '<button type="button" class="text-[11px] opacity-70 underline" data-hide>🙈 ' + esc(T('Yashirish')) + '</button>';
        box.querySelector('[data-hide]').onclick = function () { lockedView(box, n, show); };
      }).catch(function (e) { if (e && e.message !== 'cancel') alert(e.message); });
    }
    lockedView(box, n, show);
  }
  function legal(opt) {
    var btn = document.getElementById(opt.button), form = document.getElementById(opt.form), info = document.getElementById(opt.info);
    var hideBtn = opt.hide ? document.getElementById(opt.hide) : null;
    if (!btn || !form) return;
    var hasData = opt.hasData !== false;   // false: hali kiritilmagan — faqat maxfiy javob tekshiriladi, keyin forma ochiladi
    function openForm(d) {
      Object.keys(d || {}).forEach(function (k) { var el = form.elements[k]; if (el) { el.value = d[k]; el.dispatchEvent(new Event('change', { bubbles: true })); } });
      form.classList.remove('hidden'); if (info) info.classList.add('hidden');
    }
    function closeForm() {
      // yopilganda DOMda ma'lumot qolib ketmasin — maydonlar tozalanadi
      try { form.reset(); form.elements.legal_type && form.elements.legal_type.dispatchEvent(new Event('change', { bubbles: true })); } catch (e) {}
      form.classList.add('hidden'); if (info) info.classList.remove('hidden');
    }
    btn.onclick = function () {
      var p = hasData ? getJson(opt.base + '/legal/data')
        : (window.ZMSec && ZMSec.ensure ? ZMSec.ensure().then(function () { return {}; }) : Promise.resolve({}));
      p.then(openForm).catch(function (e) { if (e && e.message !== 'cancel') alert(e.message); });
    };
    if (hideBtn) hideBtn.onclick = closeForm;
  }
  window.ZMReveal = { cards: cards, legal: legal };
})();
