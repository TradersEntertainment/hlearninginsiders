/* 📟 Coin terminali — canlı akış istemcisi (kullanıcı 18.10: "terminal gibi akmalı canlı veriler de ben o sayfadaysam").
 *
 * Sunucu: GET /t/{coin}/live (SSE). Olaylar:
 *   hello {coin, listened, live, seq, px, px_ts}   t {r:[[seq, ts_ms, px, $, 'B'|'A', piyasadan, pasif, twap]], init}
 *   s {n, buy, sell, net, n_addr, twap, label, warn, unknown, buyers, sellers, px, px_ts, listened, live}
 *   b {bids:[[px, sz, n]], asks:[...], ts}   o {lv:[{v,s,px,lo,hi,usd,n,own,mk,gone,unk}], unknown, ts, ...}
 *   x {why, seq}  → akış kapandı (ömür), kaldığı yerden yeniden bağlan.
 * Yumuşak yenileme (base.html) canlı panelleri [data-keep] ile NAKLEDER; bu betik onlara bağlı kalır.
 * Ölçülü dil: tahmin yok; veri yoksa "bilinmiyor" / nedeni yazılır.
 */
(function(){
  'use strict';
  var T = null;            // durum
  var MAX_ROWS = 220;      // akış satırı (DOM)

  function $(s, r){ return (r || document).querySelector(s); }
  function el(tag, cls, txt){
    var e = document.createElement(tag);
    if(cls) e.className = cls;
    if(txt != null) e.textContent = txt;
    return e;
  }
  function usd(v){
    v = +v || 0;
    var a = Math.abs(v), s = v < 0 ? '-' : '';
    if(a >= 1e9) return s + '$' + (a / 1e9).toFixed(2) + 'B';
    if(a >= 1e6) return s + '$' + (a / 1e6).toFixed(a >= 1e7 ? 1 : 2) + 'M';
    if(a >= 1e3) return s + '$' + (a / 1e3).toFixed(a >= 1e4 ? 0 : 1) + 'K';
    return s + '$' + Math.round(a);
  }
  function susd(v){ v = +v || 0; return (v > 0.5 ? '+' : '') + (Math.abs(v) < 0.5 ? '$0' : usd(v)); }
  function px(p){
    p = +p;
    if(!p) return '—';
    if(p >= 10000) return Math.round(p).toLocaleString('en-US');
    if(p < 1e-4){ var d = 4 - Math.floor(Math.log10(p)); return p.toFixed(d).replace(/0+$/, '').replace(/\.$/, ''); }
    return String(+p.toPrecision(5));
  }
  function hhmmss(ms){
    var d = new Date(ms);
    return d.toLocaleTimeString('tr-TR', {hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
                                          timeZone: 'Europe/Istanbul'});
  }
  function short(a){ return a ? a.slice(0, 6) + '…' + a.slice(-4) : '—'; }
  function alink(a, cls){
    if(!a) return el('span', 'dim', '—');
    var x = el('a', cls || 'ad', short(a));
    x.href = '/whale/' + a + (T.k || '');
    x.title = a + ' · cüzdan karnesi (🐋)';
    return x;
  }
  // 📜 işlemler paneli: adres doğrudan hypurrscan'e (yeni sekme). Kullanıcı (19.10): "oradan bir adrese basarsak
  // direkt hypurrscane atsın işlem sahibinin bizim sitede analiz etmeye gerek yok"
  function hlink(a, cls){
    if(!a) return el('span', 'dim', '—');
    var x = el('a', cls || 'ad', short(a));
    x.href = 'https://hypurrscan.io/address/' + a;
    x.target = '_blank';
    x.rel = 'noopener';
    x.title = a + ' · hypurrscan ↗';
    return x;
  }

  // ---- durum / bağlantı ----
  function status(kind, text){
    var s = $('#tlive');
    if(!s) return;
    s.className = 'tlive ' + kind;
    s.textContent = text;
  }

  function connect(){
    if(!T || T.es || T.dead) return;
    if(typeof EventSource === 'undefined'){ status('off', '○ tarayıcı canlı akışı desteklemiyor'); return; }
    var q = T.k ? T.k + '&' : '?';
    var es = new EventSource('/t/' + T.path + '/live' + q + 'seq=' + (T.seq || 0));
    T.es = es;
    status('wait', '◌ bağlanıyor');
    es.addEventListener('hello', function(e){
      var d = JSON.parse(e.data);
      T.fail = 0;
      if(d.seq < T.seq) T.seq = 0;                     // sunucu yeniden başladı: sıra baştan (sunucu da sıfırlar)
      T.listened = d.listened;
      if(d.listened === false) status('off', '○ bu coin canlı akışta dinlenmiyor');
      else if(!d.live) status('off', '○ HL akışı şu an kopuk — veri bilinmiyor');
      else status('on', '● canlı');
      if(d.px) price(d.px, d.px_ts || 0, true, !d.px_ts);
    });
    es.addEventListener('t', function(e){ trades(JSON.parse(e.data)); });
    es.addEventListener('s', function(e){ flow(JSON.parse(e.data)); });
    // alındığı an kitabın KENDİSİNDE (d.at): okunamadı olayı eski defteri tutarken yaşı sıfırlamasın (inceleme 19.10)
    es.addEventListener('b', function(e){ var d = JSON.parse(e.data); d.at = Date.now(); T.book = d; ladder(); });
    es.addEventListener('bw', function(e){ bookFail(JSON.parse(e.data)); });
    es.addEventListener('o', function(e){ T.own = JSON.parse(e.data); ladder(); owners(); });
    es.addEventListener('x', function(){
      es.close(); T.es = null;
      if(!document.hidden) setTimeout(connect, 250);   // gizli sekme: geri gelince bağlanır (sunucu akışı boşa açık kalmasın)
    });
    es.onerror = function(){
      if(es.readyState === 2){
        // kapandı (503 tavanı / sunucu yok): üst üste hatada bekleme katlanır (4 → 8 → 16 → 30 sn)
        T.es = null; T.fail = (T.fail || 0) + 1;
        status('wait', '◌ yeniden bağlanıyor');
        setTimeout(connect, Math.min(30000, 4000 * Math.pow(2, T.fail - 1)));
      }
      else status('wait', '◌ yeniden bağlanıyor');
    };
  }

  // ---- fiyat: üst bant, sekme başlığı, grafiğin son mumu ----
  // `noCandle`: işlem olmayan fiyat (sayfanın mark'ı / eski canlı fiyat) — üst bant yazılır, mum OYNAMAZ (yoksa "şimdi"
  // damgalı sahte fiyat son mumu uzatıp atlanmış saat sanılıyor, grafik her yenilemede yeniden çekiliyordu).
  function price(p, ms, quiet, noCandle){
    p = +p;
    if(!(p > 0)) return;
    var prev = T.px;
    T.px = p;
    var e = $('#tpx');
    if(e){
      e.textContent = px(p);
      if(!quiet && prev && p !== prev){
        e.classList.remove('up', 'dn');
        void e.offsetWidth;
        e.classList.add(p > prev ? 'up' : 'dn');
      }
    }
    var c = $('#tchg');
    if(c && T.prev > 0){
      var ch = (p / T.prev - 1) * 100;
      c.textContent = (ch > 0 ? '+' : '') + ch.toFixed(2) + '%';
      c.className = ch > 0 ? 'long' : (ch < 0 ? 'short' : 'dim');
    }
    document.title = (T.tpre || '') + T.sym + ' ' + px(p) + (T.tsuf || '') + ' · HL Radar';   // 🐋/🚨 ve 📅 kalsın
    if(!noCandle && ms && typeof pxLive === 'function') pxLive(p, Math.floor(ms / 1000));
    if(!T.distT) T.distT = setTimeout(function(){ T.distT = null; distances(); }, 800);
  }

  // ---- işlem akışı ----
  function trades(d){
    var rows = (d.r || []).filter(function(r){ return r[0] > T.seq; });
    if(!rows.length) return;
    T.seq = rows[rows.length - 1][0];
    var box = $('.t-tape .tb'), list = $('#tfeed');
    if(!list) return;
    if(d.init && list.dataset.srv) { list.textContent = ''; delete list.dataset.srv; }
    var keep = box && box.scrollTop > 4, h0 = list.scrollHeight;
    T.feed = list;
    var frag = document.createDocumentFragment();
    for(var i = rows.length - 1; i >= 0; i--){
      var r = rows[i];
      T.sizes.push(r[3]);
      if(T.sizes.length > 400) T.sizes.shift();
      frag.appendChild(tradeRow(r, !d.init));
    }
    list.insertBefore(frag, list.firstChild);
    if(keep && box) box.scrollTop += list.scrollHeight - h0;   // okuyan kullanıcının yeri kaymasın (ÖNCE telafi:
    while(list.children.length > MAX_ROWS) list.removeChild(list.lastChild);   // alttan kırpılan satır yeri oynatmaz)
    // canlı mum paketin HER işlemini görsün (yalnız sonuncusu değil: paket içi tepe / dip kaybolmasın)
    if(typeof pxLive === 'function') rows.forEach(function(r){ pxLive(r[2], Math.floor(r[1] / 1000), true); });
    var last = rows[rows.length - 1];
    price(last[2], last[1]);
    var cnt = $('#tcount');
    if(cnt){ T.n += rows.length; cnt.textContent = T.n + ' işlem (sayfa açıldığından beri)'; }
  }

  function bigCut(){
    var s = T.sizes.slice().sort(function(a, b){ return a - b; });
    if(s.length < 20) return 10000;
    return Math.max(1000, s[Math.floor(s.length * 0.9)]);
  }

  function tradeRow(r, fresh){
    var buy = r[4] === 'B';
    var tr = el('div', 'tr ' + (buy ? 'b' : 's') + (r[3] >= bigCut() ? ' big' : '') + (fresh ? ' new' : ''));
    tr.dataset.seq = r[0];
    tr.appendChild(el('span', 'tm', hhmmss(r[1])));
    tr.appendChild(el('span', 'sd', buy ? 'ALIŞ' : 'SATIŞ'));
    tr.appendChild(el('span', 'us', usd(r[3]) + (r[7] ? ' 🔹' : '')));
    tr.appendChild(el('span', 'pr', px(r[2])));
    var who = el('span', 'wh');
    who.appendChild(hlink(r[5]));
    if(r[6]){ who.appendChild(el('span', 'dim', ' → ')); who.appendChild(hlink(r[6], 'ad mk')); }
    tr.appendChild(who);
    tr.title = (buy ? 'Piyasadan ALIŞ' : 'Piyasadan SATIŞ') + ' ' + usd(r[3]) + ' @ ' + px(r[2]) +
               (r[7] ? ' · HL TWAP alt emri' : '') + ' · sol: piyasadan alan/satan, sağ: defterdeki emri dolan';
    return tr;
  }

  // ---- 15 dk akış ----
  function flow(d){
    T.flowSeen = true;
    bookAge();                                         // defter olayı gelmese de yaş/uyarı ilerlesin
    if(d.listened != null) T.listened = d.listened;
    if(d.px) price(d.px, d.px_ts || 0, true, !d.px_ts);
    if(d.live === false) status('off', '○ HL akışı şu an kopuk — veri bilinmiyor');
    else if(T.listened !== false && T.es) status('on', '● canlı');
    // Ölçüm yoksa (dinlenmiyor / akış kopuk) $0 yazılmaz: "—" ve nedeni (ölçülü dil: veri yoksa bilinmiyor)
    var unk = T.listened === false || !!d.unknown;
    var set = function(id, v, cls){ var e = document.getElementById(id); if(e){ e.textContent = v; if(cls != null) e.className = 'v ' + cls; } };
    set('fbuy', unk ? '—' : usd(d.buy));
    set('fsell', unk ? '—' : usd(d.sell));
    set('fnet', unk ? '—' : susd(d.net), unk ? 'dim' : (d.net > 0 ? 'long' : (d.net < 0 ? 'short' : 'dim')));
    set('fn', unk ? '—' : d.n + ' / ' + d.n_addr);
    var lab = $('#flabel');
    if(lab) lab.textContent = d.label + (d.twap && !unk ? ' · 🔹 TWAP ' + usd(d.twap) : '');
    var why = T.listened === false ? 'bu coin canlı akışta dinlenmiyor — bilinmiyor' : d.warn;
    var w = $('#fwarn');
    if(w){ w.textContent = why ? '⚠️ ' + why : ''; w.hidden = !why; }
    who('#fbuyers', d.buyers, 'long', unk);
    who('#fsellers', d.sellers, 'short', unk);
  }

  function who(sel, rows, cls, unk){
    var box = $(sel);
    if(!box) return;
    box.textContent = '';
    if(!rows || !rows.length){ box.appendChild(el('div', 'dim', unk ? 'bilinmiyor' : 'yok')); return; }
    rows.forEach(function(f){
      var r = el('div', 'wr');
      r.appendChild(alink(f[0]));
      r.appendChild(el('b', cls, susd(f[3])));
      if(f[5]) r.appendChild(el('span', 'tw', '🔹'));
      r.title = 'alış ' + usd(f[1]) + ' · satış ' + usd(f[2]) + ' · ' + (f[4] >= 0.5 ? 'piyasadan %' + f[4] : 'yalnız pasif (defterdeki emri doldu)') +
                (f[5] ? ' · HL TWAP dilimi var' : '');
      box.appendChild(r);
    });
  }

  // ---- canlı defter merdiveni ----
  function ownAt(side, p){
    var o = T.own;
    if(!o || !o.lv) return null;
    var hit = null;
    o.lv.forEach(function(l){
      if(l.s === side && l.v === 'r' && Math.abs(l.px - p) <= Math.abs(p) * 1e-9 && (l.own.length || l.mk.length)) hit = l;
    });
    return hit;
  }

  // Defter bayatsa SÖYLE: eski defter "canlı" gibi durmasın (kullanıcı 19.10: defter 12:29:35'te kalmış, işlemler
  // 12:36'yı gösteriyordu). Yaş = sunucu saatiyle (b.now − b.ts) + alındığından beri geçen (b.at); 2 sn'de bir ve her
  // 's' olayında tazelenir (akış koparsa da ilerler). Eşik: sağlıklı akışta defter en çok ~13 sn eskir (4 sn önbellek ×
  // 2 + olay aralığı + tamsayı yuvarlama) — 12 sn sağlıklı defteri ara ara "⚠️" gösteriyordu (inceleme 19.10).
  var BOOK_STALE = 20;
  function ago(s){ s = Math.max(0, Math.round(s)); return s < 90 ? s + ' sn' : Math.round(s / 60) + ' dk'; }
  function bookAge(){
    var b = T && T.book, ts = $('#tbookts'), box = $('#tladder');
    if(!b || !ts) return;
    var age = (b.now ? b.now - b.ts : 0) + (Date.now() - (b.at || Date.now())) / 1000;
    var stale = age > BOOK_STALE;
    if(box) box.classList.toggle('stale', stale);
    ts.classList.toggle('warn', stale);
    ts.textContent = 'defter ' + hhmmss(b.ts * 1000) + (stale ? ' · ⚠️ ' + ago(age) + ' önce — ' + (b.why || 'yenilenmedi') : '');
    ts.title = stale ? 'Defter bu süredir yeniden okunamadı; gösterilen son okunan defter' + (b.why ? ' · neden: ' + b.why : '') : '';
  }
  // gösterilecek defter yok ('bw'): eski defter ekrandaysa onu tutup nedeni ekle (yaşı kendi b.at'iyle sürer), yoksa yaz
  function bookFail(d){
    if(T.book){ T.book.why = d.why || T.book.why; bookAge(); return; }
    var box = $('#tladder'), ts = $('#tbookts');
    if(box){ box.textContent = ''; box.appendChild(el('div', 'dim', 'Defter okunamadı' + (d.why ? ' — ' + d.why : '') + ' · birazdan yeniden denenir')); }
    if(ts){ ts.textContent = '⚠️ defter okunamadı'; ts.classList.add('warn'); ts.title = d.why || ''; }
  }

  function ladder(){
    var box = $('#tladder');
    var b = T.book;
    if(!box || !b) return;
    // kademe sayısı panele göre: orta fiyat satırı hep görünür kalsın (dar ekranda alışlar aşağı kaçmasın)
    var fitN = Math.floor(((box.clientHeight || 440) - 26) / 18 / 2);
    var N = Math.max(4, Math.min(12, fitN || 12));
    var asks = (b.asks || []).slice(0, N), bids = (b.bids || []).slice(0, N);
    var mx = 0;
    asks.concat(bids).forEach(function(l){ var u = l[0] * l[1]; if(u > mx) mx = u; });
    var frag = document.createDocumentFragment();
    var row = function(l, side){
      var u = l[0] * l[1];
      var r = el('div', 'lr ' + (side === 'ask' ? 'a' : 'b'));
      var bar = el('i', 'lb');
      bar.style.width = (mx ? Math.max(1, u / mx * 100) : 0) + '%';
      r.appendChild(bar);
      r.appendChild(el('span', 'lp', px(l[0])));
      r.appendChild(el('span', 'lu', usd(u)));
      r.appendChild(el('span', 'ln', l[2] ? l[2] + '' : ''));
      var o = ownAt(side, l[0]);
      var oc = el('span', 'lo');
      if(o){
        if(o.own.length){ oc.appendChild(el('span', 'ok', '✅')); oc.appendChild(alink(o.own[0][0])); }
        else { oc.appendChild(el('span', 'dim', 'dolan ')); oc.appendChild(alink(o.mk[0][0])); }
      }
      r.appendChild(oc);
      r.title = (side === 'ask' ? 'SATIŞ' : 'ALIŞ') + ' ' + px(l[0]) + ' · ' + usd(u) + ' · ' + l[2] + ' emir';
      return r;
    };
    for(var i = asks.length - 1; i >= 0; i--) frag.appendChild(row(asks[i], 'ask'));
    var mid = el('div', 'lmid');
    if(asks.length && bids.length){
      var sp = asks[0][0] - bids[0][0], m = (asks[0][0] + bids[0][0]) / 2;
      mid.textContent = 'orta ' + px(m) + ' · makas ' + px(sp) + ' (%' + (sp / m * 100).toFixed(3) + ')';
    } else mid.textContent = 'defterin bir yönü boş';
    frag.appendChild(mid);
    bids.forEach(function(l){ frag.appendChild(row(l, 'bid')); });
    box.textContent = '';
    box.appendChild(frag);
    bookAge();
  }

  // en kalın kademeler + sahipler (gruplu görünüm, yön başına en kalın)
  function owners(){
    var box = $('#tthick'), o = T.own;
    if(!box || !o || !o.lv) return;
    box.textContent = '';
    if(!o.lv.length){
      box.appendChild(el('div', 'dim', o.err && o.err.length ? 'Defter okunamadı' + (o.why ? ' (' + o.why + ')' : '') + ' — birazdan yeniden denenir'
                                                          : 'defterde kademe yok'));
      return;
    }
    ['bid', 'ask'].forEach(function(side){
      var lv = o.lv.filter(function(l){ return l.s === side && l.v === 'g'; })
                   .sort(function(a, b){ return b.usd - a.usd; })[0];
      if(!lv) return;
      var r = el('div', 'tk ' + (side === 'bid' ? 'b' : 'a'));
      r.appendChild(el('b', side === 'bid' ? 'long' : 'short', (side === 'bid' ? '🟢 ' : '🔴 ') + px(lv.px)));
      r.appendChild(el('span', '', ' ' + usd(lv.usd) + ' (' + lv.n + ' emir) '));
      if(lv.own.length){
        lv.own.slice(0, 2).forEach(function(x){ r.appendChild(el('span', 'ok', '✅')); r.appendChild(alink(x[0])); r.appendChild(el('span', 'dim', ' ' + usd(x[1]) + ' ')); });
        if(lv.unk >= 1000) r.appendChild(el('span', 'dim', '· bulunamayan ' + usd(lv.unk)));
      } else if(o.unknown) r.appendChild(el('span', 'dim', 'sahip bilinmiyor (açık emirler sorulamadı)'));
      else r.appendChild(el('span', 'dim', 'sahip bulunamadı'));
      if(lv.mk.length){ r.appendChild(el('span', 'dim', ' · dolan ')); r.appendChild(alink(lv.mk[0][0])); }
      box.appendChild(r);
    });
  }

  // ---- liq / pozisyon mesafeleri: canlı fiyatla ----
  function distances(){
    if(!(T.px > 0)) return;
    document.querySelectorAll('[data-liqpx]').forEach(function(r){
      var lq = +r.dataset.liqpx;
      if(!(lq > 0)) return;
      var d = Math.abs(T.px - lq) / T.px * 100;
      var c = r.querySelector('.dist');
      if(c){ c.textContent = '%' + d.toFixed(1); c.classList.toggle('warn', d < 5); }
    });
  }

  // ızgara ekrana tam otursun: üstündeki başlık + bant yüksekliği ölçülür (menü satır sayısı ekrana göre değişir)
  function fit(){
    var g = document.querySelector('.tgrid');
    if(!g) return;
    var top = g.getBoundingClientRect().top + window.scrollY;
    document.documentElement.style.setProperty('--term-top', Math.round(top + 10) + 'px');
  }
  window.addEventListener('resize', function(){ clearTimeout(T && T.fitT); if(T) T.fitT = setTimeout(fit, 120); });

  function init(){
    var root = document.getElementById('term');
    if(!root) return;
    fit();
    if(T && T.root === root) return;                   // yumuşak yenilemede aynı terminal
    if(T && T.es){ try{ T.es.close(); }catch(e){} }
    T = {root: root, path: root.dataset.path, k: root.dataset.k || '', sym: root.dataset.sym,
         seq: +root.dataset.seq || 0, prev: +root.dataset.prev || 0, px: +root.dataset.px || 0,
         tpre: root.dataset.tpre || '', tsuf: root.dataset.tsuf || '',
         sizes: [], n: 0, book: null, own: null, es: null, fail: 0};
    if(T.px) price(T.px, 0, true, true);
    T.feed = $('#tfeed');
    document.querySelectorAll('#tfeed .tr[data-usd]').forEach(function(r){ T.sizes.push(+r.dataset.usd); });
    connect();
    if(!window.__termAgeT) window.__termAgeT = setInterval(bookAge, 2000);   // akış kopukken de defter yaşı ilerlesin
  }

  // yumuşak yenileme sonrası: üst bant sunucudan yeniden geldi — canlı fiyatı geri yaz
  window.termAfterRefresh = function(){
    if(!T) return init();
    var root = document.getElementById('term');
    if(root && root !== T.root){
      T.root = root; T.prev = +root.dataset.prev || T.prev;
      T.tpre = root.dataset.tpre || ''; T.tsuf = root.dataset.tsuf || '';   // 🚨 yenilemede değişebilir
      // akış akmıyorken sunucunun yeni fiyatı esas (eski canlı fiyat üst bandı / başlığı / mesafeleri donduruyordu)
      if(!window.termLive() && +root.dataset.px > 0) T.px = +root.dataset.px;
    }
    // işlem bandı sunucudan yeniden geldiyse (akış yokken nakledilmedi) sıra onun sırasına çekilir — yoksa akış
    // geri gelince aynı işlemler ikinci kez eklenirdi
    var f = document.getElementById('tfeed');
    if(f && f !== T.feed){
      T.feed = f;
      if(root) T.seq = Math.max(T.seq, +root.dataset.seq || 0);
      T.sizes = [];
      f.querySelectorAll('.tr[data-usd]').forEach(function(r){ T.sizes.push(+r.dataset.usd); });
    }
    if(T.px) price(T.px, 0, true, true);
    distances();
  };
  // akış GERÇEKTEN akıyor mu (base.html yumuşak yenilemesi canlı panelleri yalnız o zaman nakleder)
  window.termLive = function(){ return !!(T && T.es && T.es.readyState === 1 && T.flowSeen); };
  document.addEventListener('visibilitychange', function(){
    if(!T) return;
    if(document.hidden){ T.hiddenAt = Date.now(); return; }
    // gizliyken akış kapandıysa arada akmayan işlemler grafikte yok: bir dakikayı aştıysa mumlar yeniden çekilir
    if(!T.es && T.hiddenAt && Date.now() - T.hiddenAt > 60000 && typeof pxReload === 'function') pxReload(false);
    T.hiddenAt = 0;
    if(!T.es) connect();
  });
  if(document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
