"""Site taraması — her sayfayı masaüstü (1400) ve telefon (390) genişliğinde açar,
tam sayfa ekran görüntüsü alır ve özet tablo basar: HTTP durumu, yatay taşma,
sayfa yüksekliği, JS hatası, panel/SVG/tablo/inline-style sayıları.

    python scripts/survey.py                       # http://127.0.0.1:8093 → /tmp/site
    python scripts/survey.py --base http://... --out /tmp/site2 --only index,coin

Playwright + Chromium gerekir (PLAYWRIGHT_CHROMIUM ile yol verilebilir)."""
import argparse
import asyncio
import json
import os

from playwright.async_api import async_playwright

PAGES = [("index", "/"), ("coin", "/t/SNDK"), ("crypto", "/t/PUMP"),
         ("whale", "/whale/" + os.environ.get("WHALE", "0x9e1aab3312d00000000000000000000000000001")),
         ("devler", "/devler"), ("gecmis", "/gecmis"), ("ai", "/ai"), ("tani", "/tani"),
         ("saatler", "/saatler"), ("kapali", "/kapali"), ("funding", "/funding"),
         ("hacim", "/hacim"), ("orintu", "/orintu"), ("neoldu", "/neoldu?sym=SNDK"),
         ("twap", "/twap"), ("saldiri", "/saldiri"), ("settings", "/settings"),
         ("login", "/login")]
VIEWPORTS = (("d", 1400, 900), ("m", 390, 844))
METRICS = {
    "ovf": "document.documentElement.scrollWidth - document.documentElement.clientWidth",
    "h": "document.documentElement.scrollHeight",
    "panels": "document.querySelectorAll('.panel').length",
    "svg": "document.querySelectorAll('svg.ichart').length",
    "tables": "document.querySelectorAll('table').length",
    "inline": "document.querySelectorAll('main [style]').length",
    "titles": "document.querySelectorAll('main [title]').length",
}


async def main(base, out, only, theme):
    os.makedirs(out, exist_ok=True)
    pages = [p for p in PAGES if not only or p[0] in only]
    chromium = os.environ.get("PLAYWRIGHT_CHROMIUM") or (
        "/opt/pw-browsers/chromium" if os.path.exists("/opt/pw-browsers/chromium") else "")
    rows = []
    async with async_playwright() as p:
        kw = {"args": ["--no-proxy-server"]}
        if chromium:
            kw["executable_path"] = chromium
        b = await p.chromium.launch(**kw)
        for name, path in pages:
            rec = {"page": name}
            for vp, w, h in VIEWPORTS:
                pg = await b.new_page(viewport={"width": w, "height": h})
                if theme:
                    await pg.add_init_script(f"localStorage.setItem('hlr_theme', '{theme}')")
                errs = []
                pg.on("pageerror", lambda e: errs.append(str(e)))
                try:
                    r = await pg.goto(base + path, wait_until="networkidle", timeout=30000)
                    st = r.status if r else 0
                except Exception as e:  # noqa: BLE001
                    st = f"ERR {type(e).__name__}"
                m = {k: await pg.evaluate(v) for k, v in METRICS.items()}
                await pg.screenshot(path=os.path.join(out, f"{name}_{vp}.png"), full_page=True)
                m.update({"st": st, "js": len(errs), "errs": errs[:3]})
                rec[vp] = m
                await pg.close()
            rows.append(rec)
        await b.close()
    print(f"{'sayfa':9} {'st':>4} {'ovf-d':>6} {'ovf-m':>6} {'h-d':>6} {'h-m':>6} "
          f"{'js':>3} {'pan':>4} {'svg':>4} {'tbl':>4} {'inl':>4} {'ttl':>4}")
    for r in rows:
        d, m = r["d"], r["m"]
        print(f"{r['page']:9} {str(d['st']):>4} {d['ovf']:>6} {m['ovf']:>6} {d['h']:>6} "
              f"{m['h']:>6} {d['js'] + m['js']:>3} {d['panels']:>4} {d['svg']:>4} "
              f"{d['tables']:>4} {d['inline']:>4} {d['titles']:>4}")
        for vp in ("d", "m"):
            for e in r[vp]["errs"]:
                print(f"   ⚠ {vp}: {e[:160]}")
    with open(os.path.join(out, "survey.json"), "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8093")
    ap.add_argument("--out", default="/tmp/site")
    ap.add_argument("--only", default="", help="virgülle sayfa adları")
    ap.add_argument("--theme", default="", help="'' (gece) ya da okyanus")
    a = ap.parse_args()
    asyncio.run(main(a.base, a.out, [x for x in a.only.split(",") if x], a.theme))
