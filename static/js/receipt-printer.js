/*!
 * ZooMo chek printer moduli (58mm / 80mm termal printerlar, ESC/POS).
 *
 * ULANISH USULLARI
 *   ble     — Bluetooth Low Energy (Web Bluetooth): Chrome/Edge — Android, Windows, macOS, ChromeOS
 *   serial  — USB yoki kompyuterga ulangan (juftlangan) klassik Bluetooth printer (Web Serial: Chrome/Edge, kompyuter)
 *   rawbt   — Android'da "RawBT" ilovasi orqali (klassik Bluetooth printerlar uchun eng qulay yo'l)
 *   system  — tizim chop etish oynasi (window.print) — har qanday o'rnatilgan printer, iPhone/iPad AirPrint
 * Chek rasm (raster) ko'rinishida chiqariladi — shuning uchun o'zbek (o‘ g‘), kirill, tojik, qirg'iz
 * harflari har qanday printerda to'g'ri chiqadi.
 *
 * API:  ZMPrinter.printOrder(order)  ·  ZMPrinter.connect(kind)  ·  ZMPrinter.test()
 *       ZMPrinter.mountSettings(el)  ·  ZMPrinter.bindAutoPrint(rootEl)
 */
(function () {
    var IS_IOS = /iPad|iPhone|iPod/.test(navigator.userAgent) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
    'use strict';
    var KEY = 'zm_printer';
    var BLE_SERVICES = [
        '000018f0-0000-1000-8000-00805f9b34fb', '0000ffe0-0000-1000-8000-00805f9b34fb', '0000ff00-0000-1000-8000-00805f9b34fb',
        '0000fee7-0000-1000-8000-00805f9b34fb', 'e7810a71-73ae-499d-8c15-faa9aef0c3f2', '49535343-fe7d-4ae5-8fa9-9fafd205e455',
        '0000a002-0000-1000-8000-00805f9b34fb', '00001101-0000-1000-8000-00805f9b34fb', '0000ae30-0000-1000-8000-00805f9b34fb'
    ];

    function T(s) { return window.ZMI18n && ZMI18n.t ? ZMI18n.t(s) : s; }
    function load() {
        var d = { transport: 'system', width: 58, auto: true, copies: 1, baud: 9600, name: '', bleId: '' };
        try { var o = JSON.parse(localStorage.getItem(KEY) || '{}'); for (var k in o) d[k] = o[k]; } catch (e) {}
        return d;
    }
    var S = load();
    function save() { try { localStorage.setItem(KEY, JSON.stringify(S)); } catch (e) {} }

    var bleDevice = null, bleChar = null, serialPort = null, serialWriter = null;
    var listeners = [];
    function emit() { for (var i = 0; i < listeners.length; i++) try { listeners[i](); } catch (e) {} }

    function sleep(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
    function fmt(n) { n = Math.round(Number(n) || 0); return String(n).replace(/\B(?=(\d{3})+(?!\d))/g, ' '); }

    /* ==================== CHEK RASMI (canvas) ==================== */
    function wrap(ctx, text, maxW) {
        var words = String(text).split(/\s+/), lines = [], cur = '';
        for (var i = 0; i < words.length; i++) {
            var test = cur ? cur + ' ' + words[i] : words[i];
            if (ctx.measureText(test).width <= maxW || !cur) cur = test; else { lines.push(cur); cur = words[i]; }
        }
        if (cur) lines.push(cur);
        // juda uzun so'zlarni belgilar bo'yicha bo'lamiz
        var out = [];
        lines.forEach(function (l) {
            while (ctx.measureText(l).width > maxW && l.length > 1) {
                var cut = l.length; while (cut > 1 && ctx.measureText(l.slice(0, cut)).width > maxW) cut--;
                out.push(l.slice(0, cut)); l = l.slice(cut);
            }
            out.push(l);
        });
        return out;
    }

    function renderReceipt(o, width) {
        var W = width === 80 ? 576 : 384, sc = W / 384, pad = Math.round(8 * sc), fs = Math.round(22 * sc), lh = Math.round(28 * sc);
        var cv = document.createElement('canvas'); cv.width = W; cv.height = 4000;
        var c = cv.getContext('2d'), y = pad;
        var FONT = '"Plus Jakarta Sans","Segoe UI",Arial,"Noto Sans",sans-serif';
        c.fillStyle = '#fff'; c.fillRect(0, 0, W, cv.height); c.fillStyle = '#000'; c.textBaseline = 'top';
        function font(sz, b) { c.font = (b ? '800 ' : '600 ') + sz + 'px ' + FONT; }
        function center(t, sz, b) { font(sz, b); wrap(c, t, W - pad * 2).forEach(function (l) { c.textAlign = 'center'; c.fillText(l, W / 2, y); y += Math.round(sz * 1.25); }); }
        function left(t, sz, b) { font(sz, b); wrap(c, t, W - pad * 2).forEach(function (l) { c.textAlign = 'left'; c.fillText(l, pad, y); y += Math.round(sz * 1.25); }); }
        function two(a, b2, sz, bold) { // chapda matn, o'ngda summa
            font(sz, bold); c.textAlign = 'right'; var rw = c.measureText(b2).width;
            var lines = wrap(c, a, W - pad * 2 - rw - 10);
            lines.forEach(function (l, i) { c.textAlign = 'left'; c.fillText(l, pad, y); if (i === lines.length - 1) { c.textAlign = 'right'; c.fillText(b2, W - pad, y); } y += Math.round(sz * 1.25); });
        }
        function dash() { y += 3; c.fillRect(pad, y, W - pad * 2, 2); y += 9; }

        center('ZOOMO', Math.round(36 * sc), true);
        center('DELIVERY SERVICE', Math.round(15 * sc), true);
        if (o.brand) { y += 4; center(o.brand, Math.round(24 * sc), true); }
        dash();
        center(T('Buyurtma') + ' #' + o.id, Math.round(30 * sc), true);
        if (o.time) center(o.time, fs, false);
        dash();
        if (o.client_name) left(T('Mijoz') + ': ' + o.client_name, fs, true);
        if (o.client_phone) left(T('Tel') + ': ' + o.client_phone, fs, true);
        if (o.address) left(T('Manzil') + ': ' + o.address, fs, false);
        if (o.comment) { y += 3; left(T('Izoh') + ': ' + o.comment, fs, true); }
        dash();
        (o.items || []).forEach(function (it) {
            left(it.name, fs, true);
            two(it.qty + ' x ' + fmt(it.price), fmt(it.qty * it.price), fs, false);
            y += 3;
        });
        dash();
        if (o.delivery_fee) two(T('Yetkazish'), fmt(o.delivery_fee), fs, false);
        if (o.discount) two(T('Chegirma'), '-' + fmt(o.discount), fs, false);
        two(T('JAMI'), fmt(o.total) + " " + T("so'm"), Math.round(28 * sc), true);
        if (o.payment) { y += 3; center(T("To'lov") + ': ' + o.payment, fs, true); }
        dash();
        center(T('Rahmat!'), fs, true);
        y += pad;
        var H = y + 4;
        var out = c.getImageData(0, 0, W, H);
        return { w: W, h: H, data: out.data };
    }

    /* ==================== ESC/POS ==================== */
    function escpos(img, copies) {
        var wBytes = img.w >> 3, bytes = [0x1b, 0x40, 0x1b, 0x61, 0x00]; // init, left
        var rows = img.h, BAND = 96;
        for (var y0 = 0; y0 < rows; y0 += BAND) {
            var h = Math.min(BAND, rows - y0);
            bytes.push(0x1d, 0x76, 0x30, 0x00, wBytes & 255, wBytes >> 8, h & 255, h >> 8);
            for (var y = 0; y < h; y++) {
                for (var xb = 0; xb < wBytes; xb++) {
                    var v = 0;
                    for (var b = 0; b < 8; b++) {
                        var i = ((y0 + y) * img.w + xb * 8 + b) * 4;
                        var lum = img.data[i] * 0.3 + img.data[i + 1] * 0.59 + img.data[i + 2] * 0.11;
                        if (lum < 150 && img.data[i + 3] > 40) v |= (0x80 >> b);
                    }
                    bytes.push(v);
                }
            }
        }
        bytes.push(0x1b, 0x64, 0x04, 0x1d, 0x56, 0x42, 0x00); // feed 4 + kesish
        var one = bytes, all = [];
        for (var k = 0; k < Math.max(1, copies || 1); k++) all = all.concat(one);
        return new Uint8Array(all);
    }

    /* ==================== TRANSPORTLAR ==================== */
    async function bleEnsure(interactive) {
        if (!navigator.bluetooth) throw new Error(T(IS_IOS ? 'iPhone/iPad da Bluetooth printer uchun App Store\'dan bepul «Bluefy» brauzerini o\'rnatib, shu sahifani o\'sha yerda oching. Yoki «Tizim» (AirPrint) ni tanlang.' : 'Bu brauzer Bluetooth printerni qo\'llab-quvvatlamaydi (Chrome yoki Edge kerak)'));
        if (bleChar && bleDevice && bleDevice.gatt.connected) return;
        if (!bleDevice && navigator.bluetooth.getDevices) {
            try {
                var known = await navigator.bluetooth.getDevices();
                for (var i = 0; i < known.length; i++) if (known[i].id === S.bleId) bleDevice = known[i];
            } catch (e) {}
        }
        if (!bleDevice) {
            if (!interactive) throw new Error(T('Printer ulanmagan'));
            bleDevice = await navigator.bluetooth.requestDevice({ acceptAllDevices: true, optionalServices: BLE_SERVICES });
            S.bleId = bleDevice.id; S.name = bleDevice.name || 'Bluetooth'; save();
            bleDevice.addEventListener('gattserverdisconnected', function () { bleChar = null; emit(); });
        }
        var server = await bleDevice.gatt.connect();
        var services = await server.getPrimaryServices(), found = null;
        for (var s = 0; s < services.length && !found; s++) {
            var chars = await services[s].getCharacteristics();
            for (var c = 0; c < chars.length; c++) if (chars[c].properties.write || chars[c].properties.writeWithoutResponse) { found = chars[c]; break; }
        }
        if (!found) throw new Error(T('Printerda yozish xizmati topilmadi'));
        bleChar = found; emit();
    }
    async function bleWrite(data) {
        var cs = 180, i = 0;
        while (i < data.length) {
            var chunk = data.slice(i, i + cs);
            try {
                if (bleChar.properties.writeWithoutResponse) await bleChar.writeValueWithoutResponse(chunk); else await bleChar.writeValue(chunk);
                i += cs; await sleep(25);
            } catch (e) {
                if (cs > 20) { cs = 20; continue; }
                throw e;
            }
        }
    }
    async function serialEnsure(interactive) {
        if (!navigator.serial) throw new Error(T('Bu brauzer USB/COM printerni qo\'llab-quvvatlamaydi (kompyuterda Chrome yoki Edge kerak)'));
        if (serialWriter) return;
        if (!serialPort) {
            var ports = await navigator.serial.getPorts();
            if (ports.length) serialPort = ports[0];
            else if (interactive) serialPort = await navigator.serial.requestPort();
            else throw new Error(T('Printer ulanmagan'));
        }
        try { await serialPort.open({ baudRate: Number(S.baud) || 9600 }); } catch (e) { if (!/already open/i.test(String(e))) throw e; }
        serialWriter = serialPort.writable.getWriter(); S.name = 'USB / COM'; save(); emit();
    }
    function toB64(bytes) { var s = ''; for (var i = 0; i < bytes.length; i += 8192) s += String.fromCharCode.apply(null, bytes.subarray(i, i + 8192)); return btoa(s); }

    function systemPrint(o) {
        return new Promise(function (resolve) {
            var w = S.width === 80 ? '80mm' : '58mm', f = document.createElement('iframe');
            f.style.cssText = 'position:fixed;right:0;bottom:0;width:0;height:0;border:0;';
            document.body.appendChild(f);
            var d = f.contentWindow.document, esc = function (x) { return String(x == null ? '' : x).replace(/[&<>]/g, function (m) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;' }[m]; }); };
            var rows = (o.items || []).map(function (it) { return '<tr><td colspan="2"><b>' + esc(it.name) + '</b></td></tr><tr><td>' + it.qty + ' x ' + fmt(it.price) + '</td><td class="r">' + fmt(it.qty * it.price) + '</td></tr>'; }).join('');
            d.open();
            d.write('<!doctype html><html><head><meta charset="utf-8"><style>@page{size:' + w + ' auto;margin:2mm}body{width:' + (S.width === 80 ? '72mm' : '50mm') + ';margin:0;font:12px/1.35 Arial,sans-serif;color:#000}h1{margin:0;text-align:center;font-size:20px}.c{text-align:center}hr{border:0;border-top:1px dashed #000;margin:5px 0}table{width:100%;border-collapse:collapse}.r{text-align:right}</style></head><body>' +
                '<h1>ZOOMO</h1><div class="c"><b>DELIVERY SERVICE</b></div>' + (o.brand ? '<div class="c"><b>' + esc(o.brand) + '</b></div>' : '') + '<hr>' +
                '<div class="c" style="font-size:16px"><b>' + esc(T('Buyurtma')) + ' #' + esc(o.id) + '</b></div><div class="c">' + esc(o.time || '') + '</div><hr>' +
                (o.client_name ? '<div><b>' + esc(T('Mijoz')) + ':</b> ' + esc(o.client_name) + '</div>' : '') + (o.client_phone ? '<div><b>' + esc(T('Tel')) + ':</b> ' + esc(o.client_phone) + '</div>' : '') +
                (o.address ? '<div>' + esc(T('Manzil')) + ': ' + esc(o.address) + '</div>' : '') + (o.comment ? '<div><b>' + esc(T('Izoh')) + ': ' + esc(o.comment) + '</b></div>' : '') + '<hr>' +
                '<table>' + rows + '</table><hr><table>' + (o.delivery_fee ? '<tr><td>' + esc(T('Yetkazish')) + '</td><td class="r">' + fmt(o.delivery_fee) + '</td></tr>' : '') +
                '<tr><td><b>' + esc(T('JAMI')) + '</b></td><td class="r"><b>' + fmt(o.total) + ' ' + esc(T("so'm")) + '</b></td></tr></table>' +
                (o.payment ? '<div class="c"><b>' + esc(T("To'lov")) + ': ' + esc(o.payment) + '</b></div>' : '') + '<hr><div class="c"><b>' + esc(T('Rahmat!')) + '</b></div></body></html>');
            d.close();
            setTimeout(function () { try { f.contentWindow.focus(); f.contentWindow.print(); } catch (e) {} setTimeout(function () { f.remove(); resolve(); }, 1500); }, 250);
        });
    }

    /* ==================== ASOSIY ==================== */
    async function printOrder(o, opts) {
        opts = opts || {};
        var tr = S.transport;
        if (tr === 'system') return systemPrint(o);
        var img = renderReceipt(o, S.width), data = escpos(img, S.copies);
        if (tr === 'rawbt') { window.location.href = 'rawbt:base64,' + toB64(data); await sleep(600); return; }
        if (tr === 'ble') { await bleEnsure(!!opts.interactive); await bleWrite(data); return; }
        if (tr === 'serial') {
            await serialEnsure(!!opts.interactive);
            await serialWriter.write(data);
            return;
        }
    }
    async function connect(kind) {
        S.transport = kind; save();
        if (kind === 'ble') await bleEnsure(true);
        else if (kind === 'serial') await serialEnsure(true);
        emit();
    }
    function demoOrder() {
        return { id: 'TEST', time: new Date().toLocaleString(), brand: 'ZooMo Test', client_name: 'Test Mijoz', client_phone: '+998 90 123 45 67',
                 address: "Toshkent sh., Amir Temur ko'chasi 1", comment: "Sinov cheki — o'g'il, qo'shimcha sous", items: [{ name: 'Lavash (katta)', qty: 2, price: 32000 }, { name: 'Cola 0.5L', qty: 1, price: 8000 }],
                 delivery_fee: 10000, total: 82000, payment: T('Naqd') };
    }
    function test() { return printOrder(demoOrder(), { interactive: true }); }

    // Buyurtma kartochkasidagi data-* atributlaridan chek ma'lumotini o'qiydi
    function readCard(card) {
        var d = card.dataset, items = [];
        card.querySelectorAll('li[data-name]').forEach(function (li) { items.push({ name: li.dataset.name, qty: Number(li.dataset.qty) || 1, price: Number(li.dataset.price) || 0 }); });
        return { id: d.orderId, time: d.time, brand: d.brand, client_name: d.clientName, client_phone: d.clientPhone, address: d.address,
                 comment: d.comment, items: items, delivery_fee: Number(d.deliveryFee) || 0, discount: Number(d.discount) || 0, total: Number(d.total) || 0, payment: d.payment };
    }
    function shouldAuto() { return !!S.auto; }

    /* "Qabul qilish" tugmasi bosilganda — chek chiqarib, so'ng formani yuboradi */
    function bindAutoPrint(root) {
        (root || document).addEventListener('submit', function (e) {
            var f = e.target;
            if (!f || f.__zmPrinted) return;
            var st = f.querySelector('input[name="new_status"]');
            if (!st || st.value !== 'accepted_by_partner' || !shouldAuto()) return;
            var card = f.closest('[data-order-id]'); if (!card) return;
            e.preventDefault(); e.stopImmediatePropagation();
            var btn = f.querySelector('button[type="submit"]'), old = btn && btn.textContent;
            if (btn) { btn.disabled = true; btn.textContent = '🖨 …'; }
            var done = function () { f.__zmPrinted = true; if (btn) { btn.disabled = false; btn.textContent = old; } if (window.ZMLive && f.closest('[data-live]')) ZMLive.submitForm(f); else f.submit(); };
            var order; try { order = readCard(card); } catch (err) { return done(); }
            var guard = setTimeout(done, 9000); // printer javob bermasa ham buyurtma qabul qilinadi
            printOrder(order, { interactive: true }).then(function () { clearTimeout(guard); done(); }, function (err) {
                clearTimeout(guard);
                try { alert(T('Chek chiqarilmadi') + ': ' + (err && err.message ? err.message : err) + '\n' + T('Buyurtma baribir qabul qilinadi.')); } catch (x) {}
                done();
            });
        }, true);
    }
    function printCard(btn) {
        var card = btn.closest('[data-order-id]'); if (!card) return;
        var order = readCard(card);
        printOrder(order, { interactive: true }).catch(function (err) { alert(T('Chek chiqarilmadi') + ': ' + (err && err.message ? err.message : err)); });
    }

    /* ==================== SOZLAMALAR OYNASI ==================== */
    function mountSettings(el) {
        if (!el) return;
        function status() {
            if (S.transport === 'system') return T('Tizim chop etish oynasi');
            if (S.transport === 'rawbt') return 'RawBT (Android)';
            if (S.transport === 'ble') return (bleChar && bleDevice && bleDevice.gatt.connected ? '🟢 ' : '⚪ ') + (S.name || T('Printer ulanmagan'));
            if (S.transport === 'serial') return (serialWriter ? '🟢 ' : '⚪ ') + (S.name || T('Printer ulanmagan'));
            return '';
        }
        function draw() {
            var seg = function (k, label) { return '<button type="button" data-tr="' + k + '" class="zmp-b' + (S.transport === k ? ' on' : '') + '">' + label + '</button>'; };
            el.innerHTML =
                '<div class="zmp-st">🖨 <b>' + T('Holat') + ':</b> <span>' + status() + '</span></div>' +
                '<div class="zmp-grid">' + seg('ble', '🔵 Bluetooth') + seg('serial', '🔌 USB / COM') + seg('rawbt', '📱 RawBT') + seg('system', '🖥 ' + T('Tizim')) + '</div>' +
                '<div class="zmp-row"><label>' + T('Qog\'oz kengligi') + '</label><select data-k="width"><option value="58"' + (S.width == 58 ? ' selected' : '') + '>58 mm</option><option value="80"' + (S.width == 80 ? ' selected' : '') + '>80 mm</option></select></div>' +
                '<div class="zmp-row"><label>' + T('Nusxalar soni') + '</label><select data-k="copies">' + [1, 2, 3].map(function (n) { return '<option' + (S.copies == n ? ' selected' : '') + '>' + n + '</option>'; }).join('') + '</select></div>' +
                '<label class="zmp-chk"><input type="checkbox" data-k="auto"' + (S.auto ? ' checked' : '') + '> ' + T('Buyurtma qabul qilinganda avtomatik chek chiqarish') + '</label>' +
                '<div class="zmp-grid2"><button type="button" data-act="connect" class="zmp-b on">' + T('Printerni ulash') + '</button><button type="button" data-act="test" class="zmp-b">' + T('Sinov cheki') + '</button></div>' +
                (IS_IOS ? '<p class="zmp-hint">⚠️ ' + T('iPhone/iPad da Safari va Chrome Bluetooth printerni qo\'llamaydi. Bepul «Bluefy» brauzerini o\'rnating yoki «Tizim» (AirPrint) ni tanlang.') + '</p>' : '') + '<p class="zmp-hint">' + T('Bluetooth/USB — Chrome yoki Edge brauzerida ishlaydi. iPhone/iPad uchun «Tizim» (AirPrint) ni tanlang. Android\'da klassik Bluetooth printer uchun RawBT ilovasini o\'rnating.') + '</p>';
            if (window.ZMI18n && ZMI18n.translate) ZMI18n.translate(el);
        }
        el.addEventListener('click', function (e) {
            var b = e.target.closest('button'); if (!b) return;
            if (b.dataset.tr) { S.transport = b.dataset.tr; save(); draw(); }
            else if (b.dataset.act === 'connect') {
                var k = S.transport;
                if (k === 'ble' || k === 'serial') connect(k).then(draw, function (err) { alert(T('Ulanmadi') + ': ' + (err && err.message ? err.message : err)); });
                else if (k === 'rawbt') alert(T('RawBT ilovasini Android telefonga o\'rnating va Bluetooth printerni shu ilovada ulang. Keyin «Sinov cheki» ni bosing.'));
                else alert(T('Tizim printeri: chop etish oynasida o\'zingizning printeringizni tanlang.'));
            } else if (b.dataset.act === 'test') test().catch(function (err) { alert(T('Chek chiqarilmadi') + ': ' + (err && err.message ? err.message : err)); });
        });
        el.addEventListener('change', function (e) {
            var k = e.target.dataset.k; if (!k) return;
            S[k] = e.target.type === 'checkbox' ? e.target.checked : (isNaN(e.target.value) ? e.target.value : Number(e.target.value)); save();
        });
        listeners.push(draw); draw();
        document.addEventListener('zmlangchange', draw);
    }

    var st = document.createElement('style');
    st.textContent = '.zmp-st{font-size:12px;margin-bottom:10px;opacity:.9}.zmp-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:6px;margin-bottom:10px}.zmp-grid2{display:grid;grid-template-columns:1fr 1fr;gap:6px;margin-top:10px}' +
        '.zmp-b{padding:9px 8px;border-radius:10px;border:1px solid rgba(148,163,184,.4);background:transparent;color:inherit;font-size:12px;font-weight:700;cursor:pointer}.zmp-b.on{background:linear-gradient(135deg,var(--accent),var(--accent-dark));color:#fff;border-color:transparent}' +
        '.zmp-row{display:flex;justify-content:space-between;align-items:center;gap:8px;margin:6px 0;font-size:12px}.zmp-row select{background:transparent;color:inherit;border:1px solid rgba(148,163,184,.4);border-radius:8px;padding:5px 8px}.zmp-row select option{color:#000}' +
        '.zmp-chk{display:flex;gap:8px;align-items:flex-start;font-size:12px;margin-top:6px}.zmp-hint{font-size:10.5px;opacity:.6;margin-top:10px;line-height:1.45}';
    document.head.appendChild(st);

    window.ZMPrinter = { settings: S, printOrder: printOrder, connect: connect, test: test, mountSettings: mountSettings, bindAutoPrint: bindAutoPrint, printCard: printCard, renderReceipt: renderReceipt, escpos: escpos };
})();
