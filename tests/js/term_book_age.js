// 📟 term.js'i küçük bir DOM taklidiyle vm'de yükler; SSE olaylarını sahte saatle sürer (tests/test_term_review.py çağırır).
// Kullanıcı (19.10): "canlı defter canlı değil?" — defter yaşı / bayatlık uyarısı gerçek JS üzerinde pinlenir.
const fs = require('fs'), vm = require('vm');
const src = fs.readFileSync(require('path').join(__dirname, '..', '..', 'app', 'web', 'static', 'term.js'), 'utf8');

class CL { constructor(e){ this.e = e; this.s = new Set(); }
  toggle(c, on){ if(on === undefined) on = !this.s.has(c); on ? this.s.add(c) : this.s.delete(c); this.e._cn(); return on; }
  add(...c){ c.forEach(x => this.s.add(x)); this.e._cn(); } remove(...c){ c.forEach(x => this.s.delete(x)); this.e._cn(); }
  contains(c){ return this.s.has(c); } }
class El {
  constructor(tag){ this.tag = tag; this.children = []; this._t = ''; this.dataset = {}; this.style = { setProperty(){} };
    this.classList = new CL(this); this.title = ''; this.clientHeight = 440; this.scrollTop = 0; this.scrollHeight = 0; this.id = ''; }
  _cn(){ }
  set className(v){ this.classList.s = new Set(String(v).split(/\s+/).filter(Boolean)); }
  get className(){ return [...this.classList.s].join(' '); }
  set textContent(v){ this.children = []; this._t = String(v); }
  get textContent(){ return this._t + this.children.map(c => c.textContent).join(''); }
  appendChild(c){ if(c.tag === '#frag'){ c.children.forEach(x => this.children.push(x)); c.children = []; } else this.children.push(c); return c; }
  insertBefore(c){ return this.appendChild(c); }
  removeChild(c){ this.children = this.children.filter(x => x !== c); }
  get firstChild(){ return this.children[0] || null; } get lastChild(){ return this.children[this.children.length-1] || null; }
  _all(){ let out = []; for(const c of this.children){ out.push(c); out = out.concat(c._all()); } return out; }
  querySelector(s){ return this.querySelectorAll(s)[0] || null; }
  querySelectorAll(s){ if(s.startsWith('.')){ const c = s.slice(1); return this._all().filter(e => e.classList.contains(c)); } return []; }
  getBoundingClientRect(){ return { top: 0 }; }
  addEventListener(){ }
}
const byId = {};
const mk = (id, tag='div') => { const e = new El(tag); e.id = id; byId[id] = e; return e; };
const term = mk('term'); term.dataset = { path: 'PONS', sym: 'PONS', seq: '0', px: '1' };
mk('tladder'); mk('tbookts'); mk('tlive'); mk('tfeed'); mk('tpx'); mk('tchg');
const document = {
  readyState: 'complete', hidden: false, title: '',
  documentElement: { style: { setProperty(){} } },
  getElementById(id){ return byId[id] || null; },
  querySelector(s){ if(s.startsWith('#')) return byId[s.slice(1)] || null; if(s === '.tgrid') return null; return null; },
  querySelectorAll(){ return []; },
  createElement(t){ return new El(t); },
  createDocumentFragment(){ return new El('#frag'); },
  addEventListener(){ },
};
let NOW = 1_760_000_000_000;               // fake browser clock (ms)
const FakeDate = class extends Date { static now(){ return NOW; } };
let ES = null; const IV = [];
class EventSource { constructor(url){ this.url = url; this.L = {}; this.readyState = 1; ES = this; }
  addEventListener(n, f){ this.L[n] = f; } close(){ this.readyState = 2; } }
const ctx = { window: { addEventListener(){} , scrollY: 0 }, document, EventSource, Date: FakeDate, setTimeout: () => 0, setInterval: (f) => { IV.push(f); return 1; },
              clearTimeout(){}, Math, JSON, String, Number, console };
ctx.window.window = ctx.window;
vm.createContext(ctx);
vm.runInContext(src, ctx);
const fire = (n, d) => ES.L[n]({ data: JSON.stringify(d) });
const show = (label, trueAge) => {
  const ts = byId.tbookts, box = byId.tladder;
  console.log(`${label.padEnd(34)} trueAge=${String(trueAge).padStart(4)}s  header="${ts.textContent}"  warn=${ts.classList.contains('warn')}  faded=${box.classList.contains('stale')}`);
};
module.exports = { fire, show, tick: s => { for(let i=0;i<s;i+=2){ NOW += 2000; IV.forEach(f=>f()); } }, now: () => NOW, byId };

// ---- senaryo: çıktı satırları test_term_review.py'de doğrulanır ----
const sec = () => Math.floor(NOW / 1000);
const lv = [[0.2638, 1000, 3]], la = [[0.2639, 900, 2]];
const H = module.exports, out = [];
const snap = (k) => out.push(k + '|' + byId.tbookts.textContent + '|' + byId.tbookts.classList.contains('warn') + '|' + byId.tladder.classList.contains('stale'));
fire('bw', {why: 'HL istek sınırı (429)', now: sec()}); out.push('nobook|' + byId.tladder.textContent + '|' + byId.tbookts.textContent);
let ts0 = sec();
fire('b', {bids: lv, asks: la, ts: ts0, now: ts0, why: ''}); snap('fresh');
H.tick(26); snap('down26');
fire('b', {bids: lv, asks: la, ts: ts0, now: sec(), why: 'HL istek sınırı (429)'}); snap('oldwhy');
H.tick(600); fire('bw', {why: 'X', now: sec()}); H.tick(300); fire('bw', {why: 'X', now: sec()}); snap('bw926');
let t1 = sec(); fire('b', {bids: lv, asks: la, ts: t1, now: t1, why: ''}); H.tick(8);
fire('b', {bids: lv, asks: la, ts: t1, now: sec(), why: ''}); H.tick(5); snap('healthy13');
console.log(out.join('\n'));
