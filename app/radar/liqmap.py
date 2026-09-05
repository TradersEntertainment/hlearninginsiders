"""Likidasyon haritası — "fiyat hangi yöne, ne kadar giderse kim ve kaç $ patlar?"

SAF modül: DB yok, ağ yok. Sayfa (`/t/<sembol>`) pozisyon satırlarını verir,
burası kovalar. Eski hâli pozisyon başına bir çizgiydi (40 ince bar, hepsi
soldan) — bir seviyede NE KADAR birikmiş görünmüyordu, çakışan barlar aşağı
itilip fiyatını yalan söylüyordu, telefonda yazı 4px'e iniyordu.

KOVALAR şimdiye uzaklığın %'sinde SABİT, temiz kenarlı (`SLOT_EDGES`): yakın
bölge ince, uzak bölge kalın (log benzeri) ama sınırlar yuvarlak sayı — etiket
"-1…2%" diye okunur, coinler arası karşılaştırılabilir, ve kademeli okuma
(%2 / %5 / %10) kenarlara TAM oturduğu için kesin hesaplanır.

DÜRÜSTLÜK: kova toplamı yalnız süpürmede GÖRÜLEN pozisyonlar; %`max_dist`'ten
uzak olanlar sayılır ve sayfa söyler (eskiden sessizce yok oluyordu).
"""
from ..telegram.format import px as _px, usd as _usd

# Şimdiye uzaklık (%) kova kenarları. Son kenar yapılandırmadaki azami mesafeye
# (`max_liq_distance_pct`, vars. 50) uzatılır; bu liste tek kaynak.
SLOT_EDGES = (0.0, 0.5, 1.0, 2.0, 3.0, 5.0, 7.5, 10.0, 15.0, 20.0, 30.0, 50.0)
# Kova, yönün toplamının bu payını tek başına taşıyorsa "duvar" (🧲 + kalın).
WALL_SHARE = 0.25
# Bar içinde en fazla bu kadar dilim; fazlası "kalan" dilimine katlanır.
MAX_SEGMENTS = 6
# Bu kadar ya da daha çok ARDIŞIK boş kova tek sönük satıra katlanır.
COLLAPSE_RUN = 3
# En küçük bar (ray genişliğinin %'si) — küçük kova da görünsün.
MIN_WPCT = 2.0
# Kademeli okuma eşikleri ("fiyat %2 giderse toplam kaç $ patlar").
CASCADE_PCTS = (2.0, 5.0, 10.0)
# "Sıradaki" listesinde yön başına kaç pozisyon.
NEXT_N = 4


def _edges(max_dist: float) -> list[float]:
    e = [x for x in SLOT_EDGES if x < max_dist]
    return e + [float(max_dist)]


def _label(lo: float, hi: float, sign: str) -> str:
    """ASCII '-' bilerek: tablo ikizindeki sıralayıcı (`parseCell`) U+2212'yi
    sayı olarak okumuyor. Görünüm için yeterince iyi."""
    f = lambda v: f"{v:g}"
    return f"{sign}{f(lo)}…{f(hi)}%"


def _slots_for(side: str, pts: list[dict], edges: list[float], mark: float,
               max_total: float) -> list[dict]:
    """Bir yönün kovaları, ŞİMDİDEN DIŞA doğru (en yakın önce)."""
    sign = "+" if side == "short" else "-"
    side_total = sum(p["notional"] for p in pts)
    out, cum = [], 0.0
    for lo, hi in zip(edges, edges[1:]):
        # [lo, hi) — son kovada hi dahil, aksi hâlde tam sınırdaki pozisyon düşer
        last = hi == edges[-1]
        members = [p for p in pts
                   if lo <= p["_dist"] < hi or (last and p["_dist"] == hi)]
        members.sort(key=lambda p: -p["notional"])
        total = sum(p["notional"] for p in members)
        cum += total
        if side == "short":
            px_lo, px_hi = mark * (1 + lo / 100), mark * (1 + hi / 100)
        else:
            px_lo, px_hi = mark * (1 - hi / 100), mark * (1 - lo / 100)
        s = {"side": side, "lo": lo, "hi": hi, "label": _label(lo, hi, sign),
             "px_lo": px_lo, "px_hi": px_hi, "n": len(members), "total": total,
             "cum": cum, "empty": not members, "collapsed": False,
             "wall": False, "nearest": False, "wpct": 0.0, "segs": [], "tip": ""}
        if members:
            s["px_avg"] = sum(p["liq_px"] * p["notional"] for p in members) / total
            s["wpct"] = max(MIN_WPCT, total / max_total * 100) if max_total else MIN_WPCT
            # Dilimler: en büyük kökte. Paylar kova genişliğinden dağıtılır ki
            # dilim toplamı = kova genişliği (taban uygulanmış hâliyle).
            head = members[:MAX_SEGMENTS - 1] if len(members) > MAX_SEGMENTS else members
            rest = members[len(head):]
            segs = [{"address": p["address"], "notional": p["notional"],
                     "wpct": p["notional"] / total * s["wpct"]} for p in head]
            if rest:
                rn = sum(p["notional"] for p in rest)
                segs.append({"address": "", "notional": rn, "n": len(rest),
                             "wpct": rn / total * s["wpct"]})
            s["segs"] = segs
            s["wall"] = side_total > 0 and total >= WALL_SHARE * side_total
            who = " · ".join(
                f"{p['address'][:6]}..{p['address'][-4:]} {_usd(p['notional'])}"
                + (f" {p['leverage']:g}x" if p.get("leverage") else "")
                for p in members[:3])
            more = f" · +{len(members) - 3} pozisyon daha" if len(members) > 3 else ""
            s["tip"] = (f"{s['label']} · {_px(px_lo)}–{_px(px_hi)} · "
                        f"{len(members)} pozisyon · {_usd(total)}"
                        f" · buraya kadar toplam {_usd(cum)} · {who}{more}")
        out.append(s)
    # En yakın DOLU kova işaretli (etiketi kalın).
    for s in out:
        if not s["empty"]:
            s["nearest"] = True
            break
    # Uçtaki boşlar atılır: grafiğin uzunluğu verinin uzunluğu.
    while out and out[-1]["empty"]:
        out.pop()
    # Aradaki uzun boş koşular tek satıra katlanır — ölçek hissi kalır,
    # 20 boş satır olmaz.
    res, i = [], 0
    while i < len(out):
        j = i
        while j < len(out) and out[j]["empty"]:
            j += 1
        if j - i >= COLLAPSE_RUN:
            res.append({"side": side, "lo": out[i]["lo"], "hi": out[j - 1]["hi"],
                        "label": _label(out[i]["lo"], out[j - 1]["hi"], sign),
                        "empty": True, "collapsed": True, "n": 0, "total": 0.0,
                        "cum": out[j - 1]["cum"], "wall": False, "nearest": False,
                        "wpct": 0.0, "segs": [], "tip": "",
                        "px_lo": min(out[i]["px_lo"], out[j - 1]["px_lo"]),
                        "px_hi": max(out[i]["px_hi"], out[j - 1]["px_hi"])})
            i = j
        elif j > i:
            res.extend(out[i:j])
            i = j
        else:
            res.append(out[i])
            i += 1
    return res


def build(rows: list[dict], mark: float | None, max_dist_pct: float = 50.0) -> dict | None:
    """Sayfa verisi. `rows`: pozisyon satırları (address, side, notional, liq_px,
    leverage, ts). Pozisyon yoksa ya da fiyat yoksa None — sayfa boş-durum yazar.
    """
    if not mark or mark <= 0:
        return None
    max_dist = float(max_dist_pct or 50.0)
    pts, no_liq, far = [], 0, []
    for p in rows:
        liq = p.get("liq_px")
        ntl = float(p.get("notional") or 0)
        if not liq or ntl <= 0 or not p.get("side"):
            no_liq += 1
            continue
        dist = abs(mark - float(liq)) / mark * 100
        q = {**p, "notional": ntl, "liq_px": float(liq), "_dist": dist}
        (pts if dist <= max_dist else far).append(q)
    if not pts:
        return None
    # short liq fiyatın ÜSTÜNDE (fiyat çıkarsa), long liq ALTINDA (düşerse).
    # Yönü liq fiyatının mark'a göre konumu değil pozisyonun tarafı belirler:
    # veri tutarsızsa (long'un liq'i markın üstünde) yine long tarafında kalır
    # ve tooltip fiyatı gösterir — sessizce yön değiştirmekten iyidir.
    ups = [p for p in pts if p["side"] == "short"]
    downs = [p for p in pts if p["side"] == "long"]
    edges = _edges(max_dist)

    def bucket_max(ps):
        m = 0.0
        for lo, hi in zip(edges, edges[1:]):
            last = hi == edges[-1]
            m = max(m, sum(p["notional"] for p in ps
                           if lo <= p["_dist"] < hi or (last and p["_dist"] == hi)))
        return m

    max_total = max(bucket_max(ups), bucket_max(downs))
    up = _slots_for("short", ups, edges, mark, max_total)
    down = _slots_for("long", downs, edges, mark, max_total)

    def cascade(ps):
        return [{"pct": t, "total": sum(p["notional"] for p in ps if p["_dist"] <= t)}
                for t in CASCADE_PCTS]

    def nearest(ps):
        return [{"address": p["address"], "side": p["side"], "notional": p["notional"],
                 "liq_px": p["liq_px"], "dist": p["_dist"],
                 "leverage": p.get("leverage") or 0}
                for p in sorted(ps, key=lambda p: p["_dist"])[:NEXT_N]]

    table = [{"side": s["side"], "label": s["label"], "px_lo": s["px_lo"],
              "px_hi": s["px_hi"], "n": s["n"], "total": s["total"], "cum": s["cum"]}
             for s in list(reversed(up)) + down if not s["collapsed"]]
    ts = [int(p.get("ts") or 0) for p in pts]
    return {
        "now": mark,
        # `up` sayfada ÜSTTEN aşağı akar: en uzak short en üstte, en yakın
        # "şimdi" çizgisinin hemen üstünde. `down` en yakından uzağa.
        "up": list(reversed(up)), "down": down,
        "legend": {"up": {"n": len(ups), "total": sum(p["notional"] for p in ups)},
                   "down": {"n": len(downs), "total": sum(p["notional"] for p in downs)}},
        "cascade": {"up": cascade(ups), "down": cascade(downs)},
        "next": {"up": nearest(ups), "down": nearest(downs)},
        "table": table,
        "meta": {"n": len(pts), "dropped_far": len(far), "dropped_noliq": no_liq,
                 "max_dist": max_dist, "latest_ts": max(ts) if ts else 0,
                 "far_total": sum(p["notional"] for p in far),
                 "wall_share": WALL_SHARE},
    }


# ── Entry haritası: "nereden açtılar, kim kârda kim zararda?" ────────────────
# Aynı kova kenarları, aynı satır dili; fark: yön = girişin şimdiye GÖRE konumu
# (üstü/altı), taraf değil — bir satırda iki taraf birden (short sola, long sağa).
# Şimdinin ÜSTÜNDEN giren long zararda / short kârda; altından girenlerde tersi.
TOP_N = 4


def _entry_slots(direction: str, pts: list[dict], edges: list[float], mark: float,
                 max_total: float) -> list[dict]:
    sign = "+" if direction == "up" else "-"
    out = []
    for lo, hi in zip(edges, edges[1:]):
        last = hi == edges[-1]
        members = [p for p in pts if lo <= p["_adist"] < hi or (last and p["_adist"] == hi)]
        members.sort(key=lambda p: -p["notional"])
        longs = [p for p in members if p["side"] == "long"]
        shorts = [p for p in members if p["side"] == "short"]
        lt = sum(p["notional"] for p in longs)
        st = sum(p["notional"] for p in shorts)
        if direction == "up":
            px_lo, px_hi = mark * (1 + lo / 100), mark * (1 + hi / 100)
        else:
            px_lo, px_hi = mark * (1 - hi / 100), mark * (1 - lo / 100)

        def segs(ps, total):
            if not ps or not max_total:
                return []
            head = ps[:MAX_SEGMENTS - 1] if len(ps) > MAX_SEGMENTS else ps
            rest = ps[len(head):]
            w = max(MIN_WPCT, total / max_total * 100)
            out_ = [{"address": p["address"], "notional": p["notional"],
                     "wpct": p["notional"] / total * w} for p in head]
            if rest:
                rn = sum(p["notional"] for p in rest)
                out_.append({"address": "", "notional": rn, "n": len(rest),
                             "wpct": rn / total * w})
            return out_

        s = {"dir": direction, "lo": lo, "hi": hi, "label": _label(lo, hi, sign),
             "px_lo": px_lo, "px_hi": px_hi,
             "n_long": len(longs), "n_short": len(shorts),
             "long_total": lt, "short_total": st, "n": len(members),
             "segs_long": segs(longs, lt), "segs_short": segs(shorts, st),
             "empty": not members, "collapsed": False, "tip": ""}
        if members:
            who = " · ".join(
                f"{p['address'][:6]}..{p['address'][-4:]} "
                f"{'LONG' if p['side'] == 'long' else 'SHORT'} {_usd(p['notional'])} @{_px(p['entry_px'])}"
                for p in members[:3])
            more = f" · +{len(members) - 3} pozisyon daha" if len(members) > 3 else ""
            sides = " · ".join(t for t in (
                f"LONG {_usd(lt)} ({len(longs)})" if longs else "",
                f"SHORT {_usd(st)} ({len(shorts)})" if shorts else "") if t)
            s["tip"] = f"{s['label']} · {_px(px_lo)}–{_px(px_hi)} · {sides} · {who}{more}"
        out.append(s)
    while out and out[-1]["empty"]:
        out.pop()
    res, i = [], 0
    while i < len(out):
        j = i
        while j < len(out) and out[j]["empty"]:
            j += 1
        if j - i >= COLLAPSE_RUN:
            res.append({"dir": direction, "lo": out[i]["lo"], "hi": out[j - 1]["hi"],
                        "label": _label(out[i]["lo"], out[j - 1]["hi"], sign),
                        "empty": True, "collapsed": True, "n": 0,
                        "n_long": 0, "n_short": 0, "long_total": 0.0, "short_total": 0.0,
                        "segs_long": [], "segs_short": [], "tip": "",
                        "px_lo": min(out[i]["px_lo"], out[j - 1]["px_lo"]),
                        "px_hi": max(out[i]["px_hi"], out[j - 1]["px_hi"])})
            i = j
        elif j > i:
            res.extend(out[i:j])
            i = j
        else:
            res.append(out[i])
            i += 1
    return res


def build_entry(rows: list[dict], mark: float | None) -> dict | None:
    """Entry haritası verisi. `rows`: pozisyon satırları (address, side, notional,
    entry_px, leverage, ts). Giriş fiyatı bilinen pozisyon yoksa None.

    Uzak girişler DÜŞMEZ: son kova kenarı gözlenen en uzak girişe uzatılır
    (uzun süredir taşınan kazanan/kaybeden pozisyonlar tam da ilginç olanlar)."""
    if not mark or mark <= 0:
        return None
    pts, no_entry = [], 0
    for p in rows:
        e = p.get("entry_px")
        ntl = float(p.get("notional") or 0)
        if not e or ntl <= 0 or p.get("side") not in ("long", "short"):
            no_entry += 1
            continue
        d = (float(e) - mark) / mark * 100          # işaretli: + üstte, − altta
        pts.append({**p, "notional": ntl, "entry_px": float(e), "_dist": d, "_adist": abs(d)})
    if not pts:
        return None
    max_dist = max(SLOT_EDGES[-1], max(p["_adist"] for p in pts))
    edges = _edges(max_dist)
    ups = [p for p in pts if p["_dist"] > 0]
    downs = [p for p in pts if p["_dist"] <= 0]

    def bucket_max(ps):
        m = 0.0
        for lo, hi in zip(edges, edges[1:]):
            last = hi == edges[-1]
            mem = [p for p in ps if lo <= p["_adist"] < hi or (last and p["_adist"] == hi)]
            m = max(m, sum(p["notional"] for p in mem if p["side"] == "long"),
                    sum(p["notional"] for p in mem if p["side"] == "short"))
        return m

    max_total = max(bucket_max(ups), bucket_max(downs))
    up = _entry_slots("up", ups, edges, mark, max_total)
    down = _entry_slots("down", downs, edges, mark, max_total)

    def top(side):
        ps = sorted((p for p in pts if p["side"] == side), key=lambda p: -p["notional"])[:TOP_N]
        return [{"address": p["address"], "notional": p["notional"], "entry_px": p["entry_px"],
                 "dist": p["_dist"], "leverage": p.get("leverage") or 0,
                 # long zararda: giriş şimdinin üstünde; short zararda: altında
                 "under": (p["_dist"] > 0) if side == "long" else (p["_dist"] < 0)}
                for p in ps]

    longs = [p for p in pts if p["side"] == "long"]
    shorts = [p for p in pts if p["side"] == "short"]
    lu = [p for p in longs if p["_dist"] > 0]
    su = [p for p in shorts if p["_dist"] < 0]
    table = [{"label": s["label"], "px_lo": s["px_lo"], "px_hi": s["px_hi"],
              "n_long": s["n_long"], "long_total": s["long_total"],
              "n_short": s["n_short"], "short_total": s["short_total"]}
             for s in list(reversed(up)) + down if not s["collapsed"]]
    ts = [int(p.get("ts") or 0) for p in pts]
    return {
        "now": mark, "up": list(reversed(up)), "down": down,
        "legend": {"long": {"n": len(longs), "total": sum(p["notional"] for p in longs)},
                   "short": {"n": len(shorts), "total": sum(p["notional"] for p in shorts)}},
        "top": {"long": top("long"), "short": top("short")},
        "table": table,
        "meta": {"n": len(pts), "no_entry": no_entry, "max_dist": max_dist,
                 "latest_ts": max(ts) if ts else 0,
                 "long_under": sum(p["notional"] for p in lu), "n_long_under": len(lu),
                 "short_under": sum(p["notional"] for p in su), "n_short_under": len(su)},
    }
