/* ZooMo yuklanish animatsiyasi (splash). Sahifaga <link splash.css> va <script splash.js> qo'shish kifoya —
   brauzer sessiyasida BIR MARTA (har sahifa almashganda takrorlanmaydi) ko'rsatiladi.
   window.ZooMoSplash.play() bilan qo'lda ham chaqirsa bo'ladi. */
(function () {
  var KEY = 'zm_splash_done';
  var DURATION = 3300;

  function seen() { try { return sessionStorage.getItem(KEY) === '1'; } catch (e) { return false; } }
  function mark() { try { sessionStorage.setItem(KEY, '1'); } catch (e) {} }

  function particles(canvas, startAt, dur) {
    var ctx = canvas.getContext('2d'); if (!ctx) return;
    var dpr = Math.min(window.devicePixelRatio || 1, 2);
    var w = canvas.clientWidth, h = canvas.clientHeight;
    canvas.width = w * dpr; canvas.height = h * dpr; ctx.scale(dpr, dpr);
    var ps = [], t0 = null, colors = ['#FACC15', '#F59E0B', '#16A34A', '#FDE68A', '#166534'];
    function frame(ts) {
      if (!t0) t0 = ts;
      var e = ts - t0 - startAt;
      ctx.clearRect(0, 0, w, h);
      if (e > -50 && e < dur) {
        var p = Math.max(0, Math.min(1, e / dur));
        // clip-path bilan bir xil egri (cubic-bezier(.5,0,.2,1)) ga yaqin
        var eased = p < .5 ? 2 * p * p : 1 - Math.pow(-2 * p + 2, 2) / 2;
        var x = eased * w;
        for (var i = 0; i < 5; i++) {
          ps.push({ x: x, y: h * (.15 + Math.random() * .7), vx: -20 - Math.random() * 60, vy: (Math.random() - .5) * 60,
                    life: 1, r: 1 + Math.random() * 2.2, c: colors[(Math.random() * colors.length) | 0] });
        }
        // yorqin "supurish" chizig'i
        var g = ctx.createLinearGradient(x - 26, 0, x + 2, 0);
        g.addColorStop(0, 'rgba(250,204,21,0)'); g.addColorStop(1, 'rgba(250,204,21,.55)');
        ctx.fillStyle = g; ctx.fillRect(x - 26, h * .08, 28, h * .84);
      }
      for (var j = ps.length - 1; j >= 0; j--) {
        var q = ps[j]; q.x += q.vx / 60; q.y += q.vy / 60; q.life -= .028;
        if (q.life <= 0) { ps.splice(j, 1); continue; }
        ctx.globalAlpha = q.life; ctx.fillStyle = q.c;
        ctx.beginPath(); ctx.arc(q.x, q.y, q.r, 0, 6.283); ctx.fill();
      }
      ctx.globalAlpha = 1;
      if (e < dur + 900) requestAnimationFrame(frame);
    }
    requestAnimationFrame(frame);
  }

  function play(opts) {
    opts = opts || {};
    if (document.getElementById('zm-splash')) return;
    var el = document.createElement('div');
    el.id = 'zm-splash';
    el.innerHTML =
      '<div class="zm-stage">' +
        '<div class="zm-shadow"></div>' +
        '<div class="zm-ride">' +
          '<div class="zm-trails"><i></i><i></i><i></i></div>' +
          '<div class="zm-img zm-main"></div>' +
          '<div class="zm-wheel zm-w1"></div><div class="zm-wheel zm-w2"></div><div class="zm-wheel zm-w3"></div>' +
          '<div class="zm-flare"></div>' +
        '</div>' +
        '<div class="zm-img zm-text"></div>' +
        '<canvas id="zm-particles"></canvas>' +
      '</div>' +
      '<div class="zm-hint">YUKLANMOQDA…</div>';
    document.body.appendChild(el);
    var stage = el.querySelector('.zm-stage');
    var canvas = el.querySelector('#zm-particles');
    // zarrachalar faqat "DELIVERY SERVICE" qatori bo'ylab (stage kengligida) harakatlanadi
    canvas.style.left = '0'; canvas.style.width = '100%';
    particles(canvas, 1750, 950);

    var closed = false;
    function close() {
      if (closed) return; closed = true; mark();
      el.classList.add('zm-out');
      setTimeout(function () { if (el.parentNode) el.parentNode.removeChild(el); if (opts.onDone) opts.onDone(); }, 600);
    }
    el.addEventListener('click', close);
    setTimeout(close, opts.duration || DURATION);
    return stage;
  }

  window.ZooMoSplash = { play: play, seen: seen };

  function auto() { if (!seen()) play(); }
  if (document.body) auto(); else document.addEventListener('DOMContentLoaded', auto);
})();
