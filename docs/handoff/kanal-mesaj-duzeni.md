# Kanal mesajları — ilk 3-4 satır düzeni (açık iş ZZ1)

Kullanıcının isteği: **her kanal mesajında önemli olan her şey ilk 3-4 satırda olsun** (Telegram bildirim önizlemesi ve telefonda ilk bakış). TWAP mesajları bu düzene `e49e472`'de geçti; aşağıdakiler kalanlar.

**Kaynak:** 14.10.2026 çok-ajanlı denetim (56 mesaj biçimi; 35 düzeltilecek, 21 zaten uygun). Denetim `e49e472` üzerindeki koda yapıldı; o tarihten sonra `app/` değişmedi (`a47f0e4` yalnız testler), yani dosya:satır atıfları bu dosya yazıldığında geçerliydi — yine de düzenlemeden önce kodu açıp doğrula.

**Notlar:** Alanlar denetim ajanlarının çıktısıdır (İngilizce). Aynı `kind` birden çok kez geçebilir (ör. `track`): aynı bildirim türünün farklı mesaj biçimleri. Önceki uygulama denemesi bunları 5 gruba bölüp ayrı worktree'lerde düzenliyordu; disk dolunca durduruldu, hiçbiri commit'lenmedi.

**Önerilen akış:** grup grup uygula → `python tests/run.py` → biçim başına gerçekçi örnek mesajı yazdırıp ilk 4 satırı gözle kontrol et → çok-ajanlı inceleme → commit/push. `tests/test_wording.py` ölçülü dil kurallarını da korur.

## İçindekiler

1. [`new_big` — new_big_position_alert](#1-new_big)
2. [`liqmap` — liq_cluster_alert](#2-liqmap)
3. [`anomaly` — anomaly_alert](#3-anomaly)
4. [`earnings` — earnings_report](#4-earnings)
5. [`eval` — eval_report](#5-eval)
6. [`whale_fill` — whale_fill_alert](#6-whale_fill)
7. [`health` — health_bulk](#7-health)
8. [`channel` — hot_hours_channel](#8-channel)
9. [`listing` — new_listing_alert](#9-listing)
10. [`wall` — wall_alert](#10-wall)
11. [`sticky` — sticky_alert](#11-sticky)
12. [`track` — sticky_note](#12-track)
13. [`track` — sticky_end](#13-track)
14. [`acct` — acct_started](#14-acct)
15. [`acct` — acct_events](#15-acct)
16. [`slice` — slice_summary](#16-slice)
17. [`slice` — slice_events](#17-slice)
18. [`liqattack` — liq_attack_alert](#18-liqattack)
19. [`cryptoliq` — crypto_liq_alert](#19-cryptoliq)
20. [`cryptoliq (split-mode photo caption)` — inline](#20-cryptoliq-split-mode-photo-caption)
21. [`cryptoliq` — crypto_liq_closed](#21-cryptoliq)
22. [`cryptovol` — crypto_vol_alert](#22-cryptovol)
23. [`cryptovol / equityvol (split-mode photo caption)` — short_caption](#23-cryptovol--equityvol-split-mode-photo-caption)
24. [`equityvol` — equity_vol_alert](#24-equityvol)
25. [`offhours` — offhours_move](#25-offhours)
26. [`lowvol` — lowvol_alert](#26-lowvol)
27. [`track` — track_offer](#27-track)
28. [`track` — track_step](#28-track)
29. [`track` — track_closed](#29-track)
30. [`digest` — digest](#30-digest)
31. [`strat` — strat_stop](#31-strat)
32. [`openmove` — openmove_report](#32-openmove)
33. [`movewin` — movewin_report](#33-movewin)
34. [`sim` — sim_opened](#34-sim)
35. [`sim` — sim_closed](#35-sim)

Zaten uygun olanlar: `liq`, `liq`, `health`, `health`, `health`, `health`, `wall`, `pattern`, `wake`, `track`, `track`, `track`, `track`, `strat`, `strat`, `movewin`, `movewin`, `sim`, `public_digest`, `wake`, `test`


---

## 1. new_big

**Biçimlendirici / çağrı yeri:** new_big_position_alert (app/telegram/format.py:200). Called from app/radar/autoscan.py:139 in _alert_new_big; notifier.send('new_big') at :142 goes to the owner chat and the public fan-out, which appends FOOT at the end. Also triggered by the web page's autoscan.kick (app/web/routes.py:749).

**Şu anki ilk 4 satır:**
```
🆕🐋 YENİ BÜYÜK POZİSYON — SNDK
🟢LONG $8.5M @62.35 5x │ 10dk önce açıldı
👤 0xaaaa..aaaa │ skor 73
  └ pozisyon 0h önce açıldı, hesap 3h önce fonlanmış, hesapta tek pozisyon bu, dev pozisyon ($8.5M), 🎯 büyük poz, ≤ 72 saatlik pencerede açıldı, OI'nin %6.2'i, 🧹 piyasadan agresif topladı (%82 taker)   [199 visible chars, about 5-6 lines on a phone; line 5 = '📅 Dikkat: 2026-10-16 (AMC) earnings var!', then ✅ PROPR'da listeli, then the disclaimer]
```

**Olması gerekenler (essentials):**
  - What happened: a newly opened big position (headline)
  - Coin (symbol)
  - Side (LONG/SHORT)
  - Size in $ (notional)
  - Size compared with normal: the only relative measure in the data is the 'OI'nin %X'i' score reason, present when the position is at least 5% of OI. The $8M floor is not passed to the formatter.
  - When: how long ago it was opened
  - Who: address, plus score (score >= 70 makes it critical priority)
  - Context: upcoming earnings date with AMC/BMO, or 'no upcoming earnings on calendar' (key for this alert type, which is earnings/insider-oriented and mostly fires for normal stocks)
  - Position state: entry price and leverage

**Sorunlar:**
  - Line 4 is the score-reasons line and has no length limit. In the realistic example it is 199 visible chars, which wraps to about 5-6 lines on a phone. So the visible top 3-4 lines end inside the reasons list, and the earnings line plus PROPR land around visual line 9-10.
  - Earnings context is on line 5, after the long reasons line, so it is never in the top 4. This covers both cases: '📅 Dikkat: <date> (AMC/BMO) earnings var!' and 'ℹ️ Takvimde yaklaşan earnings kaydı yok'. It is the main context for this alert type.
  - Size compared with normal is missing from the top. The only relative measure ('OI'nin %6.2'i') sits at the end of the 199-char reasons line.
  - The reasons repeat line 2. 'pozisyon 0h önce açıldı' repeats the age and even contradicts line 2's '10dk önce açıldı', because the scorer rounds to hours. 'dev pozisyon ($8.5M)' repeats the size. '🎯 büyük poz, ≤ 72 saatlik pencerede açıldı' repeats 'new and big'. With the defaults (floor $8M >= huge $5M; fresh window 24h <= combo 72h), all three appear in essentially every new_big alert. That is about 95 chars of duplication in the most valuable line.
  - Unknown leverage renders as a bare '?' on line 2 ('@62.35 ? │'), which is unclear.
  - Minor: the entry price and leverage take space on line 2 although they are details. The more decision-relevant relative size and earnings context are below the fold.

**Önerilen ilk 4 satır:**
```
🆕🐋 YENİ BÜYÜK POZİSYON — SNDK
🟢LONG $8.5M · OI'nin %6.2'i │ 10dk önce açıldı
📅 Dikkat: 2026-10-16 (AMC) earnings var!   (no event: 'ℹ️ Takvimde yaklaşan earnings kaydı yok')
👤 0xaaaa..aaaa │ skor 73 │ giriş 62.35 5x
```

**Değişiklik planı:**

Edit only app/telegram/format.py:200-222 (new_big_position_alert). The signature (coin, p, event) and line 1 stay unchanged, because the digest/son-bildirimler first-line previews at format.py:2401 and bot.py:1259 read line 1. Rendered with the realistic context in the scratchpad (/tmp/claude-0/-home-user-hlearninginsiders/49de93d8-1c8e-5cfb-b772-3a1b67b2789e/scratchpad/audit/new_big.py for the current output, new_big_prop.py and run_prop.py for the proposal). Proposed top lines are 29/46/40/41 visible chars.
(1) Move the json.loads(score_reasons) try/except block above the `lines` list.
(2) Pull the OI share out of the reasons: `oi = next((r for r in reasons if r.startswith("OI'nin %")), "")`.
(3) Drop the reasons that repeat line 2: (r.startswith("pozisyon ") and r.endswith("önce açıldı")), and r.startswith(("dev pozisyon (", "büyük pozisyon (", "🎯 büyük poz,", "🦉 büyük poz,")). `rest` = everything else except `oi`. This is dedupe/merge only; no new data.
(4) LINE 2 becomes f"{side} <b>{usd(p['notional'])}</b>" + (f" · {oi}" if oi else "") + f" │ {age_str(p.get('opened_ts'))} önce açıldı". The entry price and leverage leave this line.
(5) LINE 3: move the existing event/no-event line (old lines 214-218), with its wording unchanged, from after the reasons to index 2.
(6) LINE 4 becomes f"👤 {alink(p['address'])} │ skor {p.get('score') or 0} │ giriş {px(p['entry_px'])}" + (f" {p['leverage']:.0f}x" if p.get('leverage') else ""). The entry price and leverage move here from line 2 ('giriş' is the label format.py already uses at :617/:881/:970/:1078). Unknown leverage is omitted instead of showing a bare '?'.
(7) LINE 5+: '  └ ' + ', '.join(rest), only if rest is not empty. It stays directly under the skor line, so the └ tree still points at the score. Then PROPR_NOTE when listed, then DISCLAIMER; fan-out FOOT is still appended after by fanout.deliver.
No caller changes (autoscan.py:139/142, routes.py:749). Optional follow-up: add a rendering test that pins line order (line 2 contains side, $ and 'önce açıldı'; line 3 starts with 📅 or ℹ️ Takvimde; line 4 starts with 👤; 'pozisyon 0h önce açıldı' and 'dev pozisyon (' are absent).

**Etkilenen testler:**
  - No existing assertion needs changing: no test checks this formatter's text.
  - tests/test_alertgate.py:136-137 swaps the formatter for a stub `lambda coin, p, ev`. The signature must stay (coin, p, event). The :145 assert ('8500000' in the stub output) is unaffected.
  - tests/test_wording.py:110-112 (test_source_strings_measured) scans format.py source strings. The new literals ("OI'nin %", 'pozisyon ', 'önce açıldı', 'dev pozisyon (', 'büyük pozisyon (', '🎯 büyük poz,', '🦉 büyük poz,', 'giriş ') contain no forbidden pattern, so it should still pass.
  - Suggested new test (none exists yet), e.g. in tests/test_wording.py near test_rendered_messages (:139) or tests/test_alertgate.py: render new_big_position_alert with an event and the scorer-style reasons, then assert the first-4-line order and the removed duplicates.


---

## 2. liqmap

**Biçimlendirici / çağrı yeri:** liq_cluster_alert (app/telegram/format.py:251; caller app/radar/liqwatch.py:310 -> notifier.send('liqmap') :314, owner chat + fan-out)

**Şu anki ilk 4 satır:**
```
🧲 LİKİDASYON DUVARI — CL
Fiyatın ALTINDA: 🟠 LONG liq toplam $60.0M · 2 pozisyon · ort. mesafe %0.8
Bölge: 99.10 – 99.20 (şimdi 100.00)
En büyüğü: $35.0M liq 99.20 (%0.8) 0xaaaa..aaaa
```

**Olması gerekenler (essentials):**
  - What + coin: a liquidation wall (many liq prices clustered near price) on CL
  - Side of the positions that would be liquidated (🟠 LONG / 🔵 SHORT) and where the wall sits relative to price (ALTINDA / ÜSTÜNDE)
  - Size: total liq notional ($60.0M) and number of positions (2)
  - Proximity and current state: zone lo–hi (99.10 – 99.20), current price (şimdi 100.00), average distance (%0.8)
  - Measured consequence: if price crosses the whole zone, ~$total forced SATIŞ (long wall) / ALIŞ (short wall)
  - Who: largest member's notional, liq price, distance and address
  - Vs threshold: for index/commodity/FX (non_equity) the gate figures (≤%1 içinde $60.0M, eşik $50.0M). Equity alerts carry no threshold figure because alert_min is not in the cluster dict, and adding it would be new data
  - Context, can go below the fold: opposite-side liq (⚖️ iki yönlü sıkışma), PROPR line, disclaimer. Time: this is a 'now' snapshot (şimdi px) with no duration concept

**Sorunlar:**
  - Line 1 has only the coin. Side and size first appear on line 2. fanout._headline (app/telegram/fanout.py:155) uses the FIRST line as the free morning-digest headline, so the digest shows just 'LİKİDASYON DUVARI — CL' with no side and no $.
  - Line 2 is 73 visible chars and wraps to 2+ lines on a phone. It packs location, side, total, count and avg distance into one line.
  - The measured consequence (💥 '...~$60.0M zorunlu SATIŞ tetiklenir') is line 5, below the 4-line fold. This is the line that says what the wall means for flow.
  - The only threshold comparison (📐 endeks/emtia/FX kapısı: ≤%1 içinde $60.0M (eşik $50.0M)) is line 6, or line 7 when ⚖️ is present, because it is appended after the ⚖️ opposite-side line.
  - Redundancy: total $ is printed twice (line 2 and 💥), 'toplam' appears twice, and 'tetiklenir' adds length without information.
  - Minor/optional: a single-position wall (allowed when total ≥ 3× liq_cluster_min_usd, liqwatch.find_clusters) renders 'Bölge: X – X' and '1 pozisyon' plus 'En büyüğü', which is redundant.

**Önerilen ilk 4 satır:**
```
🧲 LİKİDASYON DUVARI — CL · 🟠 LONG liq $60.0M
Fiyatın ALTINDA: 99.10 – 99.20 (şimdi 100.00) · ort. %0.8
💥 fiyat bölgenin tamamını geçerse ~$60.0M zorunlu SATIŞ
👤 2 pozisyon · en büyüğü $35.0M liq 99.20 (%0.8) 0xaaaa..aaaa
```

**Değişiklik planı:**

Only app/telegram/format.py liq_cluster_alert (lines 251-281) changes. No caller change (liqwatch.py:310-314). The fan-out FOOT is still appended after the disclaimer. Rendered in scratch (scratchpad/audit/liqmap_prop.py) at 44/57/55/61 visible chars for CL and 46/59/53/61 for SNDK short; the current line 2 is 73.
1) L1 (current :264): `f"🧲 <b>LİKİDASYON DUVARI — {sym}</b> · {yon} liq <b>{usd(c['total'])}</b>"`. This moves side and total up from old L2. Keep `</b>` right after {sym} so the test substring still matches.
2) L2: merge old L2 (:265-266) and old L3 'Bölge' (:267) into `f"Fiyatın {nerede}: <b>{px(lo)} – {px(hi)}</b> (şimdi {px(c['mark'])}) · ort. %{c['avg_dist']:.1f}"`. Drop the 'Bölge:' label and shorten 'ort. mesafe' to 'ort.'. Side and total have moved to L1 and the count moves to L4.
3) L3: move the 💥 line up from position 5. Trim `etki` (:257-259) by dropping 'toplam' and 'tetiklenir': `f"fiyat bölgenin tamamını geçerse ~{usd(c['total'])} zorunlu <b>ALIŞ</b>"` / `<b>SATIŞ</b>`. Meaning unchanged, still a measured mechanic.
4) L4: old 'En büyüğü:' line (:268-269) becomes `f"👤 {c['count']} pozisyon · en büyüğü {usd(top['notional'])} liq {px(top['liq_px'])} (%{abs(top['dist']):.1f}) {alink(top['address'])}"`. The count moves here from old L2.
5) Below the fold: swap the two conditional blocks so the `if c.get("gate")` 📐 line comes BEFORE the `if c.get("other_total")` ⚖️ line, putting the threshold comparison right under line 4. Keep the 📐 text verbatim (asserted exactly, and test_wiring greps 'endeks/emtia/FX kapısı' in format.py). Then PROPR_NOTE and DISCLAIMER as now.
6) Update the docstring to cite the 14.10 rule (line 1: what/side/size, line 2: where/now, line 3: consequence, line 4: who; details below).
Optional: print px(lo) once when lo == hi (single-position wall).
No new data, no new I/O, no predictive wording. The test_wording FORBIDDEN patterns are unaffected because words are only removed.

**Etkilenen testler:**
  - tests/test_liqcluster.py:121 — asserts '🧲 <b>LİKİDASYON DUVARI — CL</b>' in t and '$60.0M' in t. Still passes if `</b>` stays right after the symbol on L1. Optionally strengthen: t.split('\n')[0] contains '🟠 LONG liq <b>$60.0M</b>', and '💥' is within the first 4 lines.
  - tests/test_liqcluster.py:122 — exact 📐 string 'endeks/emtia/FX kapısı: ≤%1 içinde $60.0M (eşik $50.0M)'. Passes if the text is kept verbatim (only its position moves).
  - tests/test_liqcluster.py:138 — 'SNDK' in and '📐' not in the equity message. Unaffected.
  - tests/test_liqcluster.py:155 — greps 'endeks/emtia/FX kapısı' in format.py source. Unaffected if the text is kept.
  - tests/test_fanout.py:276 — hand-written sample payload '🧲 <b>LİKİDASYON DUVARI — CL</b>\n…' that the formatter does not produce. No change required (line 280 only checks items[0], the cryptoliq one). Optionally refresh the sample to the new L1 '🧲 <b>LİKİDASYON DUVARI — CL</b> · 🟠 LONG liq <b>$60.0M</b>' for realism.


---

## 3. anomaly

**Biçimlendirici / çağrı yeri:** anomaly_alert (app/telegram/format.py:295-306); caller app/radar/anomaly.py:184, send :187 (owner chat + fan-out; fanout.py:82 only appends FOOT at the bottom)

**Şu anki ilk 4 satır:**
```
📡 ANOMALİ — MRVL
📅 Earnings yaklaşıyor: 2026-10-15 (AMC)
  ⚠️ OI 24 saatte +$3.1M (+%22 → $17.2M)
  ⚠️ OI son 4 saatte +$2.6M (hızlı birikim)
```

**Olması gerekenler (essentials):**
  - What kind of anomaly: OI build-up and/or extreme funding (the trigger lines)
  - Which coin (symbol in the header)
  - Size compared with normal: OI $ increase, % increase and the current OI total (for example +$6.0M (+%60 → $16.0M)); the threshold floor is not passed to the formatter, so it is not shown
  - Time window: 24 hours and/or the last 4 hours
  - Funding sign and which side is paying ('shortlar/longlar ödüyor'). This is the only side information; an OI increase has no direction
  - Context: upcoming earnings date + AMC/BMO, which is the reason the threshold is lower and the alert is high priority
  - Who: unknown by design (no address); nothing to show

**Sorunlar:**
  - With earnings coming up and all 3 triggers firing (oi24 + oi4 + funding are all possible only when has_event, anomaly.py:150-170), the funding line (the only side info, 'shortlar ödüyor') ends up on line 5. The first 4 lines then do not cover everything.
  - The earnings date takes a whole line of its own (line 2, '📅 Earnings yaklaşıyor: …') and pushes the measured data down.
  - When hour_hint is unknown the line ends in '(?)' ('📅 Earnings yaklaşıyor: 2026-10-16 (?)'). This adds noise and no information.
  - The follow hint '🔍 Detay için: /scan X' comes BEFORE '✅ PROPR'da listeli'. That differs from the TWAP convention from e49e472 (PROPR line first, then the follow/🔔/👁 hint, then the disclaimer). In the test case (no event) these two lines take lines 3-4.
  - The trigger lines start with a 2-space indent ('  ⚠️ …'), which wastes width on a phone.
  - Minor and left as is: the header 'ANOMALİ' does not say OI or funding. Line 2 always says it, and the test asserts 'ANOMALİ — SNDK', so there is no wording change.
  - Test case (SNDK, oi24 only, no event) renders fine: 1) 📡 ANOMALİ — SNDK 2) ⚠️ OI 24 saatte +$6.0M (+%60 → $16.0M) 3) 🔍 Detay için: /scan SNDK 4) ✅ PROPR'da listeli. Only the earnings + multi-trigger case breaks the rule.

**Önerilen ilk 4 satır:**
```
📡 ANOMALİ — MRVL · 📅 earnings 2026-10-15 (AMC)
⚠️ OI 24 saatte +$3.1M (+%22 → $17.2M)
⚠️ OI son 4 saatte +$2.6M (hızlı birikim)
⚠️ funding aşırı: -0.0420%/h (shortlar ödüyor)
```

**Değişiklik planı:**

Only app/telegram/format.py:295-306 (anomaly_alert) changes; anomaly.py stays as is (trigger strings and send at :187 are unchanged).
1) MERGE the earnings line into the header: head = f"📡 <b>ANOMALİ — {symbol}</b>"; if event: head += f" · 📅 earnings <b>{event['date_et']}</b>" + (f" ({hint})" if hint else ""), where hint = {"amc":"AMC","bmo":"BMO"}.get(event.get("hour_hint") or ""). This removes the separate '📅 Earnings yaklaşıyor: …' line and drops the '(?)' when the hint is unknown. Keep the bold span 'ANOMALİ — {symbol}' as is and put the suffix after </b>.
2) Trigger lines follow straight after the header: f"⚠️ {t}" (drop the 2-space indent). At most 3 triggers exist (oi24, oi4, funding), so header + triggers always fit in 4 lines or fewer.
3) MOVE PROPR_NOTE (if is_listed) ABOVE '🔍 Detay için: /scan {symbol}', then DISCLAIMER last. This matches the TWAP order: details, PROPR, follow hint, disclaimer.
Resulting lengths (no tags): header with earnings up to about 46 characters, trigger lines 38-46. Test case: 1) 📡 ANOMALİ — SNDK 2) ⚠️ OI 24 saatte +$6.0M (+%60 → $16.0M) 3) ✅ PROPR'da listeli 4) 🔍 Detay için: /scan SNDK.
Rendered in scratch: /tmp/claude-0/-home-user-hlearninginsiders/49de93d8-1c8e-5cfb-b772-3a1b67b2789e/scratchpad/audit/anomaly_render.py (current) and anomaly_proposed.py (proposed; it asserts that both existing test strings still match).
Optional: add a small formatter unit test with an event and 3 triggers, asserting 'earnings' is on line 1 and 'funding aşırı' is within the first 4 lines.

**Etkilenen testler:**
  - No assertion needs updating. tests/test_alertgate.py:105 ("ANOMALİ — SNDK" in text) still passes because the bold span is unchanged and the SNDK test seeds no earnings event
  - No assertion needs updating. tests/test_alertgate.py:106 ('OI 24 saatte +$6.0M (+%60 → $16.0M)' in text and 'hacim' not in text) still passes because the trigger string is unchanged and nothing adds 'hacim'. Checked by an assert in the scratch proposed render (pytest is not installed in the env, so the suite was not run)


---

## 4. earnings

**Biçimlendirici / çağrı yeri:** earnings_report (app/telegram/format.py:309). Callers: app/radar/report.py:66 run_stage (stages t1/pre, send at :69, fan-out kind 'earnings' → 'hisse' group), app/web/routes.py:983 ('ondemand', send at :986), plus app/telegram/bot.py:1083 (/scan, 'ondemand', sent straight to the requesting chat; this caller was not in the task list). Scratch renderers: /tmp/claude-0/-home-user-hlearninginsiders/49de93d8-1c8e-5cfb-b772-3a1b67b2789e/scratchpad/audit/earnings_render.py (current output) and proto.py (proposed layout).

**Şu anki ilk 4 satır:**
```
🕐 IREN earnings — ⏰ ~1 saat kaldı
📅 2026-10-14 · 14.10 19:17 TSİ (12:17 ET) (gün içi (piyasa açıkken)) │ EPS beklentisi 0.12
⚠️ ⚠️ kaynak çelişkisi: nasdaq 2026-10-15 diyor
✅ PROPR'da listeli
```

**Olması gerekenler (essentials):**
  - WHAT + COIN: <SYM> earnings pre-report and its stage (⏰ ~1 saat kaldı / 🕐 erken pencere), or '🔍 <SYM> — anlık tarama' for on-demand
  - WHEN: report time in TSİ, the ☀️/🌙/🕐 window icon, and whether the time is exact or approximate (yaklaşık)
  - CURRENT STATE: mark price with its 24h change, OI with its 24h change, and the ⚠️ marker at |ΔOI| ≥ 50% (this is the 'vs normal' measure)
  - POSITIONING: long/short split of the scanned pool with its total ($)
  - WHO / HOW BIG: the largest position: score badge (🚨 ≥70 / ⚠️ ≥50), address, side, size, age, flags (🆕TAZE / 🎯sicil)
  - CAVEAT (first line after the block): the calendar note about a date/time conflict or 'saat belirsiz — sabah da olabilir'

**Sorunlar:**
  - Positioning and 'who' are far down. The long/short pool balance is line 5-6. The first position row comes after a blank line and the '🐋 En büyük pozisyonlar:' header, at line 8-9 (t1/pre) or line 6-7 (ondemand). The main point of this report, who holds what and how big, is outside the first 4 lines.
  - The PROPR note uses prime space: line 3 (t1/pre without a note), line 4 (with a note), line 2 (ondemand). The convention set in e49e472 puts PROPR below the essentials and short details.
  - The date/time line is about 99 visible characters, so it wraps 2-3 times on a phone. It shows the date twice (date_et and the TSİ date), nests parentheses ('(gün içi (piyasa açıkken))' / '(akşam, kapanış sonrası, yaklaşık)'), and has EPS tacked on. The TSİ time, which is the essential part, sits mid-line.
  - The summary line (_summary_block, format.py:138) is about 104 visible characters: Mark, 24h, OI, ΔOI, Funding plus a parenthetical, Vol. It wraps 2-3 times and buries the ΔOI ⚠️ marker mid-line. In practice the phone's first 4 visual lines show only the header and the date line.
  - The calendar note renders with ⚠️ twice ('⚠️ ⚠️ kaynak çelişkisi…'). Every note built in app/earnings/calendar.py:394/397/410-420 already starts with ⚠️ (the 'saat belirsiz' one starts with a leading space, so it becomes '⚠️  ⚠️ saat belirsiz…'), and format.py:327 adds another.
  - In t1/pre the time (the 'when') is on line 2, not in the headline. The public morning digest takes only the first line as its headline (fanout.py:155 _headline), so the time is lost there.
  - Minor: rows are sorted by notional (scanner.py:283), so list item #1 can be an MM or vault (🤖MM, score 0). That entry is not the 'who' a reader is looking for.

**Önerilen ilk 4 satır:**
```
🌙 <b>IREN</b> earnings — ⏰ ~1 saat kaldı · <b>14.10 23:05 TSİ</b>   (+' (yaklaşık)' when not exact; ondemand keeps '🔍 <b>IREN</b> — anlık tarama')
📊 Mark <b>14.82</b> (+3.4% 24h) │ OI $21.4M (24h +62.0% ⚠️)
⚖️ Taranan havuzda: %78 long / %22 short ($5.1M)   (no rows → '🐋 Havuzda açık pozisyon bulunamadı (havuz henüz dar olabilir).' and no line 4)
🐋 En büyük: 🚨<b>78</b> 0x1a2b..9a0b 🟢LONG <b>$3.2M</b> · aç:5h · 🆕TAZE
```

**Değişiklik planı:**

All changes are in app/telegram/format.py. The signature stays the same and there is no new data or I/O. Measured visible widths of the proposed lines are 52 / 53 / 48 / 60 characters (64 for 'pre' with '(yaklaşık)'), against 99 and 104 today.

1) _summary_block (format.py:138-155, used only by earnings_report): split into two helpers.
 (a) _summary_block(summ) returns only the price and OI part: "📊 Mark <b>{px}</b> ({pct} 24h) │ OI {oi_part}". Keep oi_part with its ' (24h ±x% ⚠️)' threshold logic.
 (b) A new _funding_line(summ) returns "📊 Funding {fund_part}" with the existing (shortlar/longlar ödüyor) text, plus " │ Vol {usd}" when day_volume is set.

2) Header (format.py:315-330).
 - t1/pre line 1: f"{ann['icon']} <b>{sym}</b> earnings — {stage_txt} · <b>{ann['tsi']} TSİ</b>" + ("" if ann['exact'] else " (yaklaşık)").
 - ondemand line 1 is unchanged.
 - The remaining date pieces go into a `det` list shown below the block.
   - First, if a note exists: n = event['note'].strip(); show n if it already starts with '⚠️', otherwise f'⚠️ {n}'. This fixes the double ⚠️.
   - Then f"📅 {event['date_et']}" + (f" · {ann['et']} ET" if ann.get('et') else "") + f" · {ann['when_txt']}" + (f" │ EPS beklentisi {eps}" if eps). This removes the nested parentheses; 'yaklaşık' now lives in line 1.
 - ondemand has an empty det.

3) Remove the PROPR append to head (format.py:329-330).

4) Build parts (format.py:331-345) in this order:
 [L1,
  _summary_block(summ),
  (rows ? '⚖️ Taranan havuzda: …' : '🐋 Havuzda açık pozisyon bulunamadı …'),
  (rows ? top_line : nothing),
  *det,
  _funding_line(summ),
  (PROPR_NOTE if is_listed(...)),
  '\n🐋 <b>En büyük pozisyonlar:</b>' + the unchanged pos_line list (rows[:10]),
  reasons, clusters, peers,
  DISCLAIMER].
 top_line uses top = next((p for p in rows if not p.get('entity')), rows[0]) and renders:
 f"🐋 En büyük{' (MM/vault hariç)' if top is not rows[0] else ''}: {score_badge(top.get('score'))} {alink(top['address'])} {_side_badge(top['side'])} <b>{usd(top['notional'])}</b> · aç:{age_str(top.get('opened_ts'))}" + (f" · {fl}" if (fl := _flags(top)) else "").
 These are existing row fields and existing helpers. _side_badge is module-level at about format.py:388, so it resolves at call time.
 The full list below keeps item #1, so the largest position appears again in the list. This is intentional: the same pattern as the TWAP 'who' line.

5) Add a docstring note: 'İlk 4 satır: ne + ne zaman · fiyat+OI(24h) · havuz dengesi · en büyük pozisyon; not/tarih/EPS/funding/PROPR/liste altta. Tahmin yok.'

6) The callers (report.py:66, routes.py:983, bot.py:1083) need no change.

Wording check: nothing new trips tests/test_wording.py FORBIDDEN. 'havuz henüz dar olabilir' is already on its ALLOW list, and 'EPS beklentisi' does not match the \bbeklenen\b pattern.

Suggested new test (optional): render a t1 event with exact_ts, a note, summ and 3 rows (MM first), then assert:
 - line 0 contains '⏰ ~1 saat kaldı' and 'TSİ'
 - line 1 starts with '📊 Mark' and does not contain 'Funding'
 - line 2 starts with '⚖️'
 - line 3 starts with '🐋 En büyük' and has no '🤖MM'
 - the text has a single '⚠️ kaynak' (no '⚠️ ⚠️')
 - PROPR_NOTE's index is greater than 3

**Etkilenen testler:**
  - tests/test_propr_sync.py:299 — asserts PROPR_NOTE NOT in earnings_report(para:IREN, 'ondemand', {}, []). Still passes; no update needed.
  - tests/test_propr_sync.py:300 — asserts PROPR_NOTE IN earnings_report(xyz:IREN, 'ondemand', {}, []). Still passes, because the line is only moved (now after the funding line); no update needed.
  - No other test asserts on earnings_report text: grep for 'anlık tarama', 'Taranan havuzda', 'EPS beklentisi', 'En büyük pozisyonlar' and '_summary_block' finds no test hits. tests/test_coin_render.py:96 'Şüphe nedenleri' is the web page, not this formatter.
  - tests/test_wording.py scans the format.py source. The new strings ('En büyük', '(MM/vault hariç)', 'Funding') contain no forbidden pattern.


---

## 5. eval

**Biçimlendirici / çağrı yeri:** eval_report (app/telegram/format.py:2289), caller app/earnings/evaluator.py:212, sent at :213 as notifier.send("eval", ...). This is owner-chat only: "eval" is not in PUBLIC_KINDS, it has normal priority, and during quiet hours only line 1 reaches the morning digest's "En önemli 3'ü".

**Şu anki ilk 4 satır:**
```
📉 NVDA earnings sonucu: fiyat -6.3%
(blank line, from the "\n✅" prefix)
✅ Yönü tutanlar:
  0x1111..1111 SHORT $2.4M
```

**Olması gerekenler (essentials):**
  - Which coin, and that this is the post-earnings result (header)
  - Measured price move % and direction (pre-earnings to T+24h), and whether it cleared the threshold (counted in the record or not)
  - Tally of positions ≥ eval_min_notional: how many matched the direction and how many did not
  - Who: the largest positions that matched, with address, side and size
  - State changes and current state: addresses added to the watchlist, and how many/which addresses have since closed their position

**Sorunlar:**
  - Rendered with move -6.3%, 5 qualifying positions (2 matched, 3 did not), 1 added to the watchlist, 2 closed. Line 2 is an empty line because "\n✅ <b>Yönü tutanlar:</b>" starts with \n, and line 3 is just a heading. That uses up 2 of the 4 top lines.
  - The miss count "❌ Yönü tutmayan: 3 adres" lands on line 6. With 3 hitters (move +4.1%) it lands on line 7. The reader cannot see the tally in the top 4.
  - The number of addresses that matched is never printed. The reader has to count list rows, and the list is cut at right[:8], so a count of rows can be lower than the real number.
  - "⭐ Watchlist'e eklendi (2+ kez yönü tuttu)" is a lasting change (that address will trigger future alerts) but it lands on line 8, after a second blank line.
  - "🚪 Pozisyonunu kapatanlar" (current state) lands on line 9.
  - The quiet-hours digest shows only line 1 ("fiyat -6.3%"), so the morning summary has no tally. This is minor; the proposed layout keeps line 1 as is, so the problem remains.
  - The below-threshold branch (header / 'Hareket eşiğin altında — sicile işlenmedi.' / closers) and the no-price-data branch already fit in 3 lines and 1 line. They are fine.
  - Outside the reorder-only scope, so not proposed: the message never shows the earnings date (event['date_et'] is in hand), the threshold value (cfg.eval_move_threshold), or the measurement window (T+24h).

**Önerilen ilk 4 satır:**
```
📉 <b>NVDA</b> earnings sonucu: fiyat -6.3%
✅ Yönü tutan: 2 · ❌ tutmayan: 3 · 🚪 kapatan: 2
✅ 0x1111..1111 SHORT $2.4M · 0x3333..3333 SHORT $650K
⭐ <b>Watchlist'e eklendi:</b> 0x3333..3333  (if nothing was added to the watchlist, this line is "🚪 Pozisyonunu kapatanlar: …")
```

**Değişiklik planı:**

Only the above-threshold branch of app/telegram/format.py eval_report (lines 2299-2315) changes. The `move_pct is None` branch (2292-2294) and the below-threshold branch (2297-2298 plus the closers line) stay as they are; they already fit in 3 lines. I mock-rendered this plan in a scratch copy (scratchpad/audit/eval_proposed.py) without touching the repo.

Build two lists, head and tail.
1. head[0] stays exactly as line 2296 (arrow, symbol, "earnings sonucu: fiyat", pct).
2. Merge the count from the "Yönü tutanlar" heading (2303), the miss line at 2308 ("❌ Yönü tutmayan: N adres") and the closed count (from 2313) into head[1]: f"✅ Yönü tutan: {len(right)} · ❌ tutmayan: {len(wrong)}" + (f" · 🚪 kapatan: {len(closed)}" if closed else ""). This uses len(right), not the number of rows shown, so it stays correct with the right[:8] cap.
3. head[2], only if right is non-empty: the existing hitter row format (alink, SHORT/LONG, usd(notional)) for right[:2], joined with " · " and prefixed "✅ ". Results are already ordered by notional DESC because the snapshot query uses ORDER BY notional DESC, so these are the largest. This line is about 53 visible characters with 2 addresses; if that is too wide on a phone, use right[:1].
4. head[3], only if promoted: move line 2310 up and drop its leading "\n" and its "(2+ kez yönü tuttu)" note: "⭐ <b>Watchlist'e eklendi:</b> " + ", ".join(alink(a) for a in promoted).
5. Put the existing closers line (2313, "🚪 Pozisyonunu kapatanlar: …") in head if len(head) < 4, otherwise in tail.
6. tail gets, in this order: the full "✅ <b>Yönü tutanlar:</b>" list (right[:8], the existing rows from 2303-2306, with the leading "\n" removed), but only if len(right) > 2; the closers line if it did not fit in head; and, if promoted, the rule note moved from 2310 as "<i>⭐ Watchlist: 2+ kez yönü tuttu</i>".
7. Join as head + ([""] + tail if tail else []) + [DISCLAIMER]. The only blank line is now the one between the top block and the details.

No new data and no new I/O; all wording reuses existing labels. I ran the new literals through tests/test_wording.hits() and none of them hit a forbidden pattern.

**Etkilenen testler:**
  - No test checks eval_report's output text, so no assertion needs updating.
  - tests/test_wording.py:110 test_source_strings_measured scans the literals in app/telegram/format.py and app/earnings/evaluator.py (SOURCES at :64-67). It must still pass; I checked the new strings 'Yönü tutan', 'tutmayan', 'kapatan', 'Watchlist'e eklendi:' and 'Watchlist: 2+ kez yönü tuttu' and none hit a forbidden pattern.
  - Optional: add a rendered-message check of eval_report to tests/test_wording.py test_rendered_messages. It would assert that strip_tags(text).split('\n')[1] contains 'Yönü tutan:' and 'tutmayan:', and that lines[:4] contain the watchlist line when promoted is non-empty.


---

## 6. whale_fill

**Biçimlendirici / çağrı yeri:** whale_fill_alert (app/telegram/format.py:349) + shared what_happened (format.py:1360); caller app/hl/collector.py:696, sent at :701 via Notifier.send to the owner chat plus fan-out (fanout.py:82 adds only the FOOT line at the bottom, no buttons). Only stock coins reach it (collector.py:504 skips crypto/spot). Scratch renders: /tmp/claude-0/-home-user-hlearninginsiders/49de93d8-1c8e-5cfb-b772-3a1b67b2789e/scratchpad/audit/whale_fill.py (current) and whale_fill_proposed.py (proposed layout).

**Şu anki ilk 4 satır:**
```
🎯 SİCİLLİ BALİNA SNDK'E DÖNDÜ — SNDK
Bu adresin daha önceki bir SNDK bilançosu öncesindeki pozisyon yönü, sonraki fiyat yönüyle aynı çıktı — şimdi bu hissede yeni işlem yaptı
🟢 ALIŞ pozisyon $2.0M @ 480.00
👤 0xaaaa..aaaa
```

**Olması gerekenler (essentials):**
  - Alert type: a whale with a past correct pre-earnings call is back on this coin (🎯), or a plain big trade (🐋)
  - Coin (SNDK)
  - Side (ALIŞ/SATIŞ), size ($) and average price
  - Who: the address
  - Why it matters (🎯 mode): the record, how many times its direction held vs. did not
  - How it was filled: swept as taker or rested as maker, and whether one order was split into N parts
  - Market state now: the OI reading (yeni LONG açılmış / el değiştirmiş / belirsiz…)
  - Size against the flow: net buy/sell in the last 5 minutes and how many addresses traded (the only 'vs normal' context the message has)
  - When: real time, in the last 5-minute window

**Sorunlar:**
  - In 🎯 mode, line 2 is a 137-character rule explanation ('Bu adresin daha önceki bir … bilançosu …'). It wraps to about 4 rows on a phone and pushes everything else down. It explains the rule, so it belongs below the top lines.
  - The heading repeats the symbol ('SNDK'E DÖNDÜ — SNDK') and hardcodes the suffix 'E (format.py:357), which gives wrong Turkish for TSLA'E or NVDA'E. Side, size and price sit alone on line 3.
  - The record line ('🎯 Sicil: …', format.py:376), the reason the 🎯 alert exists at all, appears at line 5–7, below the taker and parts lines.
  - The taker/maker line and the N-parts line take two separate lines (format.py:367-374), both below line 4.
  - The OI reading and the 5-minute flow from what_happened appear only at lines 8–9 in 🎯 mode and 5–6 in 🐋 mode. The market state and the size against the flow are never in the first 4 lines. The OI line is also 104–115 characters.
  - Bug: the taker line hardcodes 'agresif alım' even when the fill is a SATIŞ (format.py:369).
  - The size is mislabelled. In 🐋 mode 'ALIŞ pozisyon $X' is the batch/trade total, not a position. In 🎯 mode collector.py:684-688 replaces it with the clearinghouse position size, so the parts line's '(toplam yukarıda)' (format.py:374) points at a number that is not the order total.
  - The whale's own current position and label ('LONG $2.0M · yeni long açtı') appear only in the brief's address rows (about line 10), repeating the address shown on line 4.
  - Not fixable by reordering: the message never states the alert threshold or any 'vs normal' reference. The floor from alertgate.fill_floor at collector.py:506 is not passed in. This is optional follow-up and needs a new parameter.
  - Minor: unlike sibling channel alerts there is no DISCLAIMER line at the bottom (optional).

**Önerilen ilk 4 satır:**
```
🎯 SİCİLLİ BALİNA DÖNDÜ · SNDK · 🟢 ALIŞ pozisyon $2.0M @ 480.00
👤 0xaaaa..aaaa · 🎯 Sicil: 2 kez yönü tuttu / 1 kez tutmadı
🧹 Piyasadan süpürdü (agresif alım, %85 taker) · 🧩 3 parçaya bölünmüş tek emir
📈 Yeni LONG açılmış · son 5dk net +$2.1M (4 adres)
```

**Değişiklik planı:**

Change whale_fill_alert (format.py:349-380). No new data and no new I/O. Rendered line lengths: 62/58/77/50 visible characters in 🎯 mode; in 🐋 mode, for example, 54/14/41/47.

L1, merging the heading and the act line: f"{head} · <b>{sym}</b> · {act} pozisyon <b>{usd(notional)}</b> @ {px(price)}"
- head = "🎯 <b>SİCİLLİ BALİNA DÖNDÜ</b>" when is_watch, else "🐋 <b>BÜYÜK İŞLEM</b>".
- This removes the duplicate '— SNDK' and the hardcoded 'E suffix.
- Optional wording fix: in 🐋 mode drop the word 'pozisyon' (the number there is the trade total).

L2, the address with the record merged in: who = f"👤 {alink(addr)}"
- if hits or misses: who += f" · 🎯 Sicil: {hits} kez yönü tuttu / {misses} kez tutmadı" (the existing text, moved up from format.py:376).
- Optional: in 🐋 mode with no record, append the whale's own row from brief['top'][0] when its address equals addr: ' · 📍 LONG $2.0M · yeni long açtı'.

L3, taker/maker and parts merged with ' · ', and the line is omitted when both are empty:
- The taker part becomes side-aware: 'agresif alım' if side == 'buy' else 'agresif satış'. This fixes the format.py:369 bug.
- The parts text becomes '🧩 {n} parçaya bölünmüş tek emir'. This drops '(toplam yukarıda)', which is false in 🎯 mode.

L4, a compact headline from the brief:
- Add a pure helper next to what_happened (around format.py:1360): brief_headline(b) -> f"{_verdict_icon(v)} <b>{V}</b>" + (f" · son {minutes}dk net <b>±{usd(|net|)}</b> ({n_addr} adres)" if n_addr).
- It returns None when b is None. It may also return None when the verdict is 'belirsiz' and n_addr == 0.
- It reads only fields already in the brief, so the other callers of what_happened can reuse it (crypto_vol_alert:1834, equity_vol_alert:1865, offhours_move:1942).

Below line 4, in order:
1. The 🎯 rule note (old line 2), unchanged, optionally in <i>.
2. what_happened(brief), unchanged: the full OI line with its reason and the measured window, the 👥 flow line and the address rows.
3. PROPR_NOTE.
4. Optional: DISCLAIMER.

what_happened itself is not changed, so the other kinds are unaffected. The rendered text passes the forbidden-wording scan in tests/test_wording.py, and the 'Sicil: …' substring is kept exactly.

**Etkilenen testler:**
  - tests/test_wording.py:158 — asserts 'Sicil: 2 kez yönü tuttu / 1 kez tutmadı' in whale. Still passes because the substring is kept verbatim on L2; no update needed.
  - tests/test_wording.py:168-171 — runs the forbidden-wording check on the rendered whale text. Still passes: the new strings ('SİCİLLİ BALİNA DÖNDÜ', 'agresif satış', 'parçaya bölünmüş tek emir', 'net … adres') contain no forbidden pattern. The source scan over format.py also stays clean.
  - Recommended new assertions (none exist today): sym, '$2.0M' and 'Sicil:' appear in the first 4 lines; a sell with taker_ratio 0.85 renders 'agresif satış'; the rule note ('bilançosu öncesindeki') sits below line 4.


---

## 7. health

**Biçimlendirici / çağrı yeri:** health_bulk (app/telegram/format.py:432; caller app/health.py:248, sent as notify kind "health" with priority high, key health:bulk; it goes to admins/subscribers who turned on notify_health, not to a public channel)

**Şu anki ilk 4 satır:**
```
🌐 GENİŞ KESİNTİ — 3 görev aynı anda sessizleşti: canlı işlem akışı (WS), anomali dedektörü, likidasyon radarı.  [110 chars; with 8 tasks it is 207 chars]
Aynı turda sessizleştiler; ortak neden ölçülmedi (Hyperliquid API erişimini kontrol et) — görevleri yeniden başlattım, düzelince tek tek haber veririm.  [151 chars]
(none: the message has only 2 logical lines)
(none)
```

**Olması gerekenler (essentials):**
  - What happened: a wide outage, with several bot tasks going silent in the same watchdog round
  - How big: how many tasks (N; this message is only sent when N >= 3)
  - Which tasks went silent (the TASK_TR names)
  - Current state and action taken: the watchdog restarted them and will report each one as it recovers
  - What to check: the common cause was not measured, so check Hyperliquid API access

**Sorunlar:**
  - All the essentials are in the text, but they sit in two very long logical lines (110-207 chars and 151 chars). On a phone these wrap to about 6-9 visual lines. The current state ('görevleri yeniden başlattım, düzelince tek tek haber veririm') is the last clause of the message, so it ends up around visual lines 5-9, after the full task list and the cause note.
  - Line 1 puts the whole task list inline right after the headline. With 8 tasks it grows to 207 chars and pushes everything else down. The variable-length list should sit on its own line after the fixed-size facts (count and state).
  - It repeats itself: 'aynı anda sessizleşti' in line 1 and 'Aynı turda sessizleştiler' in line 2 say the same thing.
  - Line 2 packs three separate things into one sentence: the timing note, the unmeasured cause with the check hint, and the action and follow-up. The most important part, the action and current state, comes last.
  - Accuracy issue, not layout, outside the reorder-only scope: 'görevleri yeniden başlattım' is stated unconditionally. The caller (app/health.py:233-238) has restarted[n], which can be False when respawn fails. health_down shows 'yeniden başlatamadım' in that case, but health_bulk does not. Fixing this would need an extra argument, so it is only noted here.
  - Duration is not in the message: the caller has checks[n]['silent'], but the formatter is not given it. This is not added, because the rules allow no new data.

**Önerilen ilk 4 satır:**
```
🌐 <b>GENİŞ KESİNTİ</b> — <b>{N} görev</b> aynı turda sessizleşti
Görevleri yeniden başlattım; düzelince tek tek haber veririm.
Görevler: {lst}
<i>Ortak neden ölçülmedi — Hyperliquid API erişimini kontrol et.</i>
```

**Değişiklik planı:**

This only reorders and merges existing text in health_bulk (app/telegram/format.py:432-436). Nothing new is fetched or added.
(1) Line 1: keep the headline '🌐 <b>GENİŞ KESİNTİ</b> — ' and the count, bolded as '<b>{len(names)} görev</b>'. Merge 'aynı anda sessizleşti' with 'Aynı turda sessizleştiler' into one phrase, 'aynı turda sessizleşti', and drop the duplicate. Move the task list out of line 1, so line 1 stays about 45 chars no matter how many tasks there are.
(2) Line 2: move the action and current-state clause from the end of the old line 2 up to here: 'Görevleri yeniden başlattım; düzelince tek tek haber veririm.' (about 60 chars).
(3) Line 3: the task list goes on its own line: f'Görevler: {lst}'. This is the same ', '.join(_task(n) ...) string as before, with the trailing period dropped.
(4) Line 4: the cause note from the middle of the old line 2 becomes its own italic line: '<i>Ortak neden ölçülmedi — Hyperliquid API erişimini kontrol et.</i>'. The parentheses become a dash; the wording stays measured.
Join the four lines with '\n'. After this, what happened, how many, current state, which tasks and what to check are each on lines 1-4, and lines 1, 2 and 4 are short on a phone. Only line 3 grows with N, and it comes last among the essentials, so it cannot push the state out of view.
Optional follow-up, outside the reorder-only scope: pass restarted from app/health.py:248 so line 2 can say 'yeniden başlatamadım' for failed respawns, as health_down does.
No caller change is needed: the signature stays health_bulk(names) and the call at app/health.py:248 does not change.

**Etkilenen testler:**



---

## 8. channel

**Biçimlendirici / çağrı yeri:** hot_hours_channel (app/telegram/format.py:482)

**Şu anki ilk 4 satır:**
```
🕐 BU SAATİN GEÇMİŞ KARNESİ — TSİ 16:00
Son ~90 günde bu saatte eşikleri geçen hisseler (örnek ≥ 40 · ort ≥ +%0.10 · pozitif saat ≥ %55):

SNDK 🌙 — bu saat ort +0.41% · %61 pozitif saat · 55 örnek · ✅ PROPR
```

**Olması gerekenler (essentials):**
  - What: this hour's historical scorecard (past measurement, not a signal) — header 'BU SAATİN GEÇMİŞ KARNESİ'
  - When: TSİ hour (16:00) and the lookback window (son ~90 gün)
  - Which coins: the symbols that passed (sorted by avg desc, so the strongest come first); the direction is up only (positive avg)
  - How big: per-coin avg return for this hour, % positive hours and sample n
  - Compared with what: the thresholds (örnek ≥ 40 · ort ≥ +%0.10 · pozitif saat ≥ %55)
  - Below the first 4 lines: caveat 'geçmiş karne; bugünkü saatin yönünü ölçmez', the 🌙 legend, candle source, multiple-testing ('şansla') note, PROPR listing and disclaimer

**Sorunlar:**
  - Line 3 is an empty spacer, so one of the 4 lines carries nothing. With 2 or more entries (auto broadcast sends up to 8), only the first coin row reaches the first 4 lines. In the 3-coin example, NVDA and MU are on lines 5-6.
  - Line 2 is a 97-char criteria line that comes before any data. On a phone (~40 chars a row) it wraps to about 3 rows, so the first coin row starts around visual row 5-6. Its first 30 chars ('Son ~90 günde bu saatte eşikleri geçen hisseler') repeat what the header says; only '~90 gün' is new.
  - Each coin row ends with ' · ✅ PROPR' (+10 chars per row, 65-67 chars total, so it wraps on a phone). The user's rule puts the PROPR line below the essentials, and the TWAP redo in e49e472 placed it under the first 4 lines.
  - Each row repeats 'bu saat ort' even though the header already says 'BU SAATİN'.
  - The footnote is a single 203-char italic line that mixes the 🌙 legend, the data source, the 'şansla' note and the key caveat 'geçmiş karne; bugünkü saatin yönünü ölçmez' at its very end. That is fine below the fold, but the caveat should lead the footnote, not trail it.
  - Minor, outside the reorder: the threshold numbers are hard-coded literals ('40', '+%0.10', '%55') instead of hourstats.MIN_N / HOT_MEAN / HOT_WIN (app/radar/hourstats.py:152-154). They drift silently if the constants change. Optional fix: format them from those constants (no new I/O).

**Önerilen ilk 4 satır:**
```
🕐 <b>BU SAATİN GEÇMİŞ KARNESİ</b> — TSİ 16:00 · son ~90 gün
Eşiği geçenler (örnek ≥ 40 · ort ≥ +%0.10 · pozitif saat ≥ %55):
<b>SNDK</b> 🌙 — ort <b>+0.41%</b> · %61 pozitif saat · 55 örnek
<b>NVDA</b> — ort <b>+0.22%</b> · %58 pozitif saat · 62 örnek   (with 1 entry, line 4 = '✅ <b>PROPR'da listeli:</b> SNDK' if listed, otherwise the footnote)
```

**Değişiklik planı:**

All edits are in app/telegram/format.py hot_hours_channel (lines 482-495). Wording, data and I/O stay the same; the edit only moves and merges existing text.
1) Header (line 484): add ' · son ~90 gün' after 'TSİ {tsi_now:02d}:00'. This moves '~90 gün' out of the old line 2.
2) Old line 2 (line 485): replace 'Son ~90 günde bu saatte eşikleri geçen hisseler (örnek ≥ 40 · ort ≥ +%0.10 · pozitif saat ≥ %55):' with the shorter merge 'Eşiği geçenler (örnek ≥ 40 · ort ≥ +%0.10 · pozitif saat ≥ %55):' (64 chars). Delete the trailing "" element (the blank spacer), so coin rows start on line 3.
3) Coin rows (lines 489-491): change 'bu saat ort' to 'ort', since the header carries 'BU SAATİN'. Remove the inline '{propr}' suffix. Instead, collect listed symbols into a list (same is_listed(h.get('coin') or sym) call, no new I/O). Rows become about 47-49 chars.
4) Right after the loop, if any symbols were collected, append one line: "✅ <b>PROPR'da listeli:</b> " + ', '.join(listed). This reuses the PROPR_NOTE wording.
5) Footnote (lines 492-494): move 'Geçmiş karne; bugünkü saatin yönünü ölçmez' to the front, then 🌙 legend · '1 saatlik mumlardan' · 'çok sayıda hisse × saat hücresi taranır; eşiği şansla geçen hücreler de olur'. Drop 'son ~90 günün', which is now in the header. Keep the leading '\n' (blank line) and the <i>.
6) Keep DISCLAIMER last.
7) Optional: build the threshold numbers from hourstats.MIN_N / HOT_MEAN / HOT_WIN instead of literals.
I prototyped this at /tmp/claude-0/-home-user-hlearninginsiders/49de93d8-1c8e-5cfb-b772-3a1b67b2789e/scratchpad/audit/hot_hours_proposed.py. The rendered output passes tests.test_wording.hits() with []. The current render script is hot_hours.py in the same folder.
Result: what, when and window are on line 1; thresholds on line 2; the two strongest coins with avg, win% and n on lines 3-4; PROPR, caveat and footnote follow. Neither caller needs a change (app/main.py:288, app/web/routes.py:1621): the signature is unchanged.

**Etkilenen testler:**
  - tests/test_wording.py:169-170 — `assert "GEÇMİŞ KARNESİ" in hot and "şansla" in hot` still passes after the change (both strings are kept), so no update is required.
  - tests/test_wording.py:170 (suggested addition, not required): pin the new layout, e.g. `ls = hot.split("\n"); assert ls[2].startswith("<b>SNDK</b>") and "PROPR" not in ls[2] and "son ~90 gün" in ls[0]`. Note that the test entry has no 'coin' key, so is_listed('SNDK') decides whether a PROPR line appears.
  - tests/test_wording.py source-string scan (SOURCES includes app/telegram/format.py): the new literals ('Eşiği geçenler', "PROPR'da listeli:", the reordered footnote) contain no forbidden patterns; checked with hits() on the prototype render.


---

## 9. listing

**Biçimlendirici / çağrı yeri:** new_listing_alert (app/telegram/format.py:499). Caller: app/main.py:103 universe_loop, notifier.send("listing", ...). It goes to the owner only: "listing" is in KINDS (app/notify.py:52) and is left out of PUBLIC_KINDS on purpose. Items come from app/hl/universe.py:67 as {coin, symbol, max_leverage}.

**Şu anki ilk 4 satır:**
```
🆕 YENİ HİSSE LİSTELENDİ
Hyperliquid'de kaldıraçlı işlem görmeye başladı — radar bunları taramaya şimdi başladı:

• IREN · max 10x · ✅ PROPR'da listeli
```

**Olması gerekenler (essentials):**
  - What: a new stock perp was listed on Hyperliquid and leveraged trading has started
  - Which symbol(s): IREN, or every symbol when several are listed at once
  - How big: max leverage per symbol (max 10x). This alert has no notional, volume or threshold.
  - Who/where: the Hyperliquid venue. No address is involved.
  - When: now, at the time the message is sent. The formatter has no other time data.
  - Current state: the radar has just started scanning it, and candle and track-record history for the symbol is still thin

**Sorunlar:**
  - The headline has no symbol: line 1 is only '🆕 YENİ HİSSE LİSTELENDİ'. The notification preview and chat list show line 1 plus the long explainer, so the reader cannot see which stock was listed without opening the message.
  - With one item, the symbol and its leverage (the core facts) first appear on line 4, after an explanation line and a blank line.
  - Line 3 is blank, so one of the four top lines is wasted.
  - Line 2 is a roughly 89-character explanation ending in ':' that wraps to 2-3 lines on a phone. Explanation fills the top of the message.
  - With several items (example: IREN, CRWV, HOOD), only the first symbol is within lines 1-4. The rest, up to 12 bullets plus '…ve N tane daha', sit on lines 5-16, so the count in the headline is the only top-4 hint of what was listed.
  - The 'current state' caveat ('geçmiş mum ve sicil verisi henüz az') is the very last line, after a blank line.
  - Number agreement: 'radar bunları' is plural even when n==1, and the footer 'bu sembolde' is singular even when n>1.
  - With one item, the PROPR marker is inline on the top-4 symbol line and ahead of the state caveat. The house style (e49e472) puts PROPR_NOTE as its own line below the essentials.

**Önerilen ilk 4 satır:**
```
🆕 <b>YENİ HİSSE LİSTELENDİ</b> — <b>IREN</b> · max 10x   (n>1: 🆕 <b>3 YENİ HİSSE LİSTELENDİ</b> — <b>IREN</b>, <b>CRWV</b>, <b>HOOD</b>; first 3 symbols, then ' +N' if more)
Hyperliquid'de kaldıraçlı işlem görmeye başladı
<i>Radar taramaya şimdi başladı — geçmiş mum ve sicil verisi henüz az.</i>
(blank) → then n==1: PROPR_NOTE line only if is_listed. n>1: the existing bullets '• <b>SYM</b> · max Nx · ✅ PROPR'da listeli' (≤12) + '…ve N tane daha'
```

**Değişiklik planı:**

This only edits new_listing_alert (app/telegram/format.py:499-516). It adds no new data or I/O and changes nothing in main.py or the notify key.
1) Headline (line 1). If n==1, use head = "🆕 <b>YENİ HİSSE LİSTELENDİ</b> — <b>{sym}</b>" and append f" · max {int(lev)}x" when lev is set. This moves the existing bullet content (symbol and leverage) up into the headline. If n>1, use head = f"🆕 <b>{n} YENİ HİSSE LİSTELENDİ</b> — " + ", ".join(f"<b>{esc(sym)}</b>" for the first 3 items), plus f" +{n-3}" when n>3. These symbol names are already in the bullets. Capping at 3 keeps the line short on a phone.
2) Line 2. Split the old explainer at the dash and keep only the first half: "Hyperliquid'de kaldıraçlı işlem görmeye başladı". Drop the trailing ':' and the blank line that followed it.
3) Line 3. Merge the second half of the old explainer with the old footer caveat into one italic line: "<i>Radar taramaya şimdi başladı — geçmiş mum ve sicil verisi henüz az.</i>". This replaces both 'radar bunları taramaya şimdi başladı:' and the final '<i>Yeni listeleme — bu sembolde …</i>' line, which also fixes both number-agreement slips because neither pronoun is needed. Remove the old last-line append.
4) Line 4 is blank. Everything below it is detail:
   - n==1: drop the bullet, since line 1 now carries the symbol and leverage. Append PROPR_NOTE as its own line if is_listed(coin or symbol). This follows the e49e472 style, where the PROPR line sits below the essentials.
   - n>1: keep the existing bullet loop unchanged (• <b>SYM</b> · max Nx · inline PROPR, ≤12, plus '…ve {n-12} tane daha').
Rendered result for n==1: [🆕 YENİ HİSSE LİSTELENDİ — IREN · max 10x | Hyperliquid'de kaldıraçlı işlem görmeye başladı | Radar taramaya şimdi başladı — geçmiş mum ve sicil verisi henüz az. | (blank) | ✅ PROPR'da listeli].
Rendered result for n=3: [🆕 3 YENİ HİSSE LİSTELENDİ — IREN, CRWV, HOOD | Hyperliquid'de … başladı | Radar taramaya … henüz az. | (blank) | • IREN · max 10x · ✅ PROPR'da listeli | • CRWV · max 5x · ✅ PROPR'da listeli | • HOOD].
Optional: add tests/test_listing_format.py, which tests/run.py picks up automatically. It should assert that for n==1 the symbol and 'max 10x' are in line 1 and PROPR_NOTE comes after line 3, that for n=5 line 1 contains 3 symbols and '+2', and that the caveat is line 3. Render scratch: /tmp/claude-0/-home-user-hlearninginsiders/49de93d8-1c8e-5cfb-b772-3a1b67b2789e/scratchpad/audit/listing.py

**Etkilenen testler:**



---

## 10. wall

**Biçimlendirici / çağrı yeri:** wall_alert (app/telegram/format.py:518-547); caller app/radar/bookwall.py:269, sent at :272 via Notifier.send("wall") to the owner chat and fan-out (fan-out adds FOOT at the bottom only). Rendered with scratch script /tmp/claude-0/-home-user-hlearninginsiders/49de93d8-1c8e-5cfb-b772-3a1b67b2789e/scratchpad/audit/wall_render.py, using the test context from tests/test_propr_sync.py:295-298 plus a realistic SPCX ask wall ($202M, owner matched, opp depth $9.5M, 24h vol $85M). Proposed layout rendered in .../audit/wall_proposed.py (scratch only, repo not edited).

**Şu anki ilk 4 satır:**
```
🧱 DEV EMİR DUVARI — SPCX
fiyatın hemen ÜSTÜNDE SATIŞ duvarı (short/satmak isteyen)
$202.0M · 201.30–205.80 aralığı (fiyata %0.3)
📊 karşı taraf derinliğinin 21.3 katı · 24h hacme oranı %238
```

**Olması gerekenler (essentials):**
  - What happened: a very large resting order wall is on the book now (the alert fires when it is detected)
  - Coin and side: SATIŞ (ask) above the price or ALIŞ (bid) below it
  - Size: $ notional of the wall
  - Where: price range of the wall and its distance to the price (%)
  - Size compared with normal: multiple of the opposite side's depth and share of 24h volume (📊 line, shown only when available)
  - Who: the matched owner address, or 'unknown, the book is anonymous'
  - Follow-up hint (below the top 4): a separate message comes when the wall leaves the book, and the radar does not tell a fill from a cancel

**Sorunlar:**
  - WHO is outside the first 4 lines. The owner line (👤) is line 5 whenever the 📊 line exists, which is almost always because day_volume is normally present. It moves up to line 4 only when there are no 📊 facts.
  - Line 1 has neither the side nor the size: '🧱 DEV EMİR DUVARI — SPCX'. This line is the Telegram notification preview. It is also the only text the fan-out morning digest shows: fanout._headline (app/telegram/fanout.py:155) takes the first line of the payload. So the digest entry says only 'DEV EMİR DUVARI — SPCX', with no side and no $.
  - Side (line 2) and $ size (line 3) are split across two lines. Lines 2 and 3 together carry what one header could say.
  - Line 2 spends its space on an interpretive parenthetical, '(short/satmak isteyen)' / '(long/almak isteyen)'. It repeats SATIŞ/ALIŞ, and 'short'/'long' is not measured: an ask wall can be a reduce-only long close, which sticky_alert keeps separate ('bu emir LONG'u boşaltıyor').
  - Minor: the italic follow-up hint (line 6) comes before the PROPR line (line 7). In the TWAP redo (e49e472) the PROPR line comes first and the 🔔 follow hints come after it.
  - Observation only, out of scope: the message does not say how long the wall has been on the book. book_walls.first_ts exists, but it is not passed to the formatter, and showing it would be new data.

**Önerilen ilk 4 satır:**
```
🧱 <b>DEV SATIŞ DUVARI</b> — SPCX · <b>$202.0M</b>   (bid: '🧱 DEV ALIŞ DUVARI — BTC · $61.5M'; ~32-35 chars)
fiyatın hemen <b>ÜSTÜNDE</b> · 201.30–205.80 aralığı (fiyata %0.3)   (~57-59 chars; 'aralığı' can be dropped to get ~51)
📊 karşı taraf derinliğinin <b>21.3 katı</b> · 24h hacme oranı %238   (unchanged; conditional)
👤 Sahibi (emirler eşleşti): 0x1234..5678  /  👤 Sahibi bilinmiyor — defter anonim, tanıdığımız balinalarla eşleşmedi   (unchanged; becomes line 3 when there is no 📊 line)
```

**Değişiklik planı:**

All changes are inside wall_alert in app/telegram/format.py:518-547. No new data and no new I/O. wall_gone is left as is.
1) Lines 521-524: replace the `where` sentence with two small values: side_w = "SATIŞ" if w["side"]=="ask" else "ALIŞ", and rel = "ÜSTÜNDE" if ask else "ALTINDA".
2) Line 525, new header (merges the side word from old line 2 and the notional from old line 3): f"🧱 <b>DEV {side_w} DUVARI</b> — {sym} · <b>{usd(w['notional'])}</b>".
3) Lines 526-528, new line 2 (merges the location from old line 2 with the range and distance from old line 3): f"fiyatın hemen <b>{rel}</b> · {px(w['px_lo'])}–{px(w['px_hi'])} aralığı (fiyata %{w['dist_pct']:.1f})". Drop the '(short/satmak isteyen)' / '(long/almak isteyen)' parenthetical: it repeats SATIŞ/ALIŞ and 'short/long' is unmeasured intent. If a strict reorder-only rule applies, append ' (satmak isteyen)' / ' (almak isteyen)' to the end of line 2 instead, without 'short/long'.
4) Lines 529-537 (📊 facts line) unchanged; it becomes line 3.
5) Lines 538-543 (👤 owner line) unchanged; it becomes line 4, or line 3 when there are no facts. Optional phone trim, not required: shorten the unknown-owner text to '👤 Sahibi bilinmiyor (defter anonim)' and move the rest below.
6) Lines 544-546: swap the order so the PROPR_NOTE block (if is_listed) comes before the italic 'Duvar defterden kalkınca ayrıca haber veririm …' hint, which becomes the last line. This matches the TWAP layout (PROPR, then follow hints). The fan-out FOOT is still appended after it by fanout.deliver.
Effects:
- Line 1 now carries coin, side and $. That fixes the notification preview and the fan-out digest headline (fanout._headline).
- The first 4 lines now carry what, coin, side, size, where, size compared with normal, and who.
- Wording stays measured; there is no forbidden pattern from tests/test_wording.py in the new strings.
- Scratch render checked: 31-35 / 57-59 / 57-59 / 40-70 chars per line.

**Etkilenen testler:**
  - tests/test_propr_sync.py:297 — `PROPR_NOTE not in wall_alert(para:STX)`: checks presence only, unaffected by the reorder, no update needed
  - tests/test_propr_sync.py:298 — `PROPR_NOTE in wall_alert(xyz:IREN)`: checks presence only, unaffected, no update needed
  - tests/test_wording.py:115 (test_source_strings_measured) scans every string in format.py: the new f-strings ('DEV {} DUVARI', 'fiyatın hemen … aralığı (fiyata %…)') contain no forbidden patterns, still passes
  - No existing assertion checks wall_alert line order or the 'DEV EMİR DUVARI' / 'short/satmak isteyen' text, so nothing needs updating. Optional new assertion near tests/test_propr_sync.py:298: the first line contains 'ALIŞ' and '$2.0M', and '👤' is within the first 4 lines.


---

## 11. sticky

**Biçimlendirici / çağrı yeri:** sticky_alert (app/telegram/format.py:635; helpers _sticky_head :569, _sticky_owner :585, _sticky_pos :601; caller app/radar/stickywall.py:858 -> notifier.send('sticky', chat_id=chat_for(coin)) :861, crypto channel)

**Şu anki ilk 4 satır:**
```
🧲 YAPIŞKAN DUVAR — SAND · SATIŞ
Tek emir $1.5M (23.25M SAND) @ 0.06265 — en iyi satışın 5 seviye gerisinde
📊 24s hacme oranı %10.5 · OI'ye oranı %25 · karşı tarafın görünen derinliğinin 100+ katı
🔁 ilk görüşten beri 1 dk · bakışlarda en az 1 kez yer değiştirdi (0.06264–0.06265)
```

**Olması gerekenler (essentials):**
  - what: YAPIŞKAN DUVAR, a single (n=1) giant resting order glued near the best price (header)
  - coin + side: SAND · SATIŞ/ALIŞ
  - size: $ notional of the single order + price (@ px)
  - vs threshold/normal: 24s hacme oranı % (the gate is >= $1M AND >= 2% of 24h volume); where it sits relative to best (en iyi satışın N seviye gerisinde / fiyatında)
  - how long + stickiness evidence: ilk görüşten beri X, bakışlarda en az N kez yer değiştirdi / aynı fiyattaydı
  - who: owner (Sahibi = verified by open order / Sahip adayı = flow-only / Sahibi bulunamadı)
  - current state / effect: what this order does to the owner's position (bu emir SHORT açıyor / büyütüyor, LONG'u boşaltıyor ...); secondary: amount eaten so far

**Sorunlar:**
  - WHO is missing from the first 4 lines: the owner line (👤 Sahibi … vault "drkmttr") is line 5, and when the owner is unknown, 'Sahibi bulunamadı' is also line 5.
  - The position effect ('bu emir SHORT açıyor / büyütüyor'), the most actionable measured fact, is on line 6, at the end of a long position line (88 chars).
  - Lines 3-4 spend the top on second-order metrics: OI ratio and opposite-depth ratio share the line with the threshold metric, and the price range sits on the duration line. Lines 3-4 are 88 and 82 visible chars, so on a phone they wrap to about 2-3 rows each. The first 4 message lines then fill about 7-8 phone rows, and the owner still isn't shown.
  - Line 2 mixes core data ($ size, price) with detail (coin qty 23.25M SAND).
  - Owner line 5 is 102 chars because it packs owner + tif + reduce-only + verification note; the unknown-owner variant is 89 chars (explanation inline).
  - 'Duvardan şimdiye yenen' (current state) is line 7, below the position line. It's acceptable as detail, but it is the only measured 'state so far' fact.

**Önerilen ilk 4 satır:**
```
🧲 <b>YAPIŞKAN DUVAR</b> — <b>SAND</b> · SATIŞ · tek emir <b>$1.5M</b> @ 0.06265
📊 24s hacme oranı <b>%10.5</b> · en iyi satışın 5 seviye gerisinde
🔁 ilk görüşten beri 1 dk · bakışlarda en az <b>1 kez</b> yer değiştirdi
👤 Sahibi <a>0xc179..e0c8</a> (vault "drkmttr") → <b>bu emir SHORT açıyor / büyütüyor</b>
```

**Değişiklik planı:**

Only app/telegram/format.py changes; the helpers' sticky_end path (_sticky_pos(end=True), _sticky_head) must keep working unchanged.

Rendered from the test context (tests/test_stickywall.py lifecycle, at t0+60), the first 4 lines should read:
1. 🧲 YAPIŞKAN DUVAR — SAND · SATIŞ · tek emir $1.5M @ 0.06265 (58 chars)
2. 📊 24s hacme oranı %10.5 · en iyi satışın 5 seviye gerisinde (about 62)
3. 🔁 ilk görüşten beri 1 dk · bakışlarda en az 1 kez yer değiştirdi (64)
4. 👤 Sahibi 0xc179..e0c8 (vault "drkmttr") → bu emir SHORT açıyor / büyütüyor (74)
For a candidate owner, line 4 starts '👤 Sahip adayı …'. When the owner is unknown, it reads '👤 Sahibi bulunamadı — defter anonim' (35).

Line-by-line in sticky_alert:

L1 (header): merge the $ notional and price from the old line 2 into the header: `🧲 <b>YAPIŞKAN DUVAR</b> — <b>{sym}</b> · {side} · tek emir <b>{usd(ntl)}</b> @ {px5(px_last)}`.

L2: `📊 24s hacme oranı <b>%x.x</b> · {where}`. The 24h-volume ratio comes out of the facts list and `where` comes from the old line 2. day_vol is always present (no volume means no alert). Defensive fallback: if day_vol is missing, show only `📊 {where}`.

L3: the existing 🔁 line without the `({px5(lo)}–{px5(hi)})` range suffix. Wording stays the same: 'ilk görüşten beri', 'bakışlarda en az <b>N kez</b> yer değiştirdi' / 'bakışlarda aynı fiyattaydı'.

L4: owner head + position effect.
- Split _sticky_owner into a head and a detail part. Either add `_sticky_owner(w, part='head'|'detail')` or return a (head, detail) tuple; it is only called from sticky_alert.
- Head:
  - verified owner: `👤 Sahibi {alink}{vault}`
  - flow-only owner: `👤 Sahip adayı {alink}{vault}`
  - unknown owner: `👤 Sahibi bulunamadı — defter anonim`
- Effect: if the owner is set, pos_side is not None and the effect is known, append ` → <b>{eff}</b>`.
- Move the eff dict out of _sticky_pos (a module-level STICKY_EFF, or a small _sticky_eff(w) helper) so L4 can use it. _sticky_pos(end=False) then stops appending the effect. The end=True path already ignores it, so sticky_end is unaffected.

Below line 4 (detail, in this order):
- L5: `🍽 Duvardan şimdiye yenen: …` (unchanged, if eaten_usd).
- L6: `{📉|📈} Pozisyon: <b>SIDE qty SYM</b> ($ntl, giriş px)`, or `📭 SYM'da açık pozisyonu yok`. This is _sticky_pos(w) without the effect.
- L7: owner detail, with the 👤 prefix:
  - verified: `post-only (Alo), reduce-only DEĞİL — açık emriyle doğrulandı`
  - candidate: `duvar fiyatındaki dolumların en büyük maker'ı (akış payı %87); açık emirle doğrulanamadı`
  - unknown: `duvardan dolum görülmedi ya da açık emirle eşleşmedi`
- L8: `Tek emir {qty} {sym}` + (if mv) ` · bakışlardaki aralık {px5(lo)}–{px5(hi)}`. This is the qty from the old line 2 and the range from the old line 4.
- L9: `📊 OI'ye oranı %x · karşı tarafın görünen derinliğinin N katı` (the remaining facts, only if any).
- Then, unchanged: the italic 'Likidasyon değil …' note (both variants), `👁 … /takip_N`, DISCLAIMER.

No new data and no new I/O. The flip effect text is long; '(pozisyondan büyük)' may stay or move to L6 so L4 stays at most about 80 chars.

Update the sticky_alert docstring to state the order: 1) ne/coin/yön/$/fiyat, 2) 24s hacme oran + nerede, 3) ne kadar süre / kaç kez yer değiştirdi, 4) kim → emrin pozisyona etkisi; ayrıntı altta. Optionally also mention it in the README sticky section.

**Etkilenen testler:**
  - tests/test_stickywall.py:214-218: all substrings stay present unchanged ('🧲 <b>YAPIŞKAN DUVAR</b>', 'SAND', 'SATIŞ', 'post-only (Alo)', 'reduce-only DEĞİL', 'drkmttr', 'SHORT açıyor / büyütüyor', '/takip_', '24s hacme oranı <b>%10.', 'en az <b>1 kez</b>', 'en iyi satışın 5 seviye gerisinde', 'Likidasyon değil', 'fiyatla birlikte kendini yeniden koyan', 'Duvardan şimdiye yenen'), so no edit is needed. The plan keeps 'Sahibi' / 'Sahip adayı' / 'Sahibi bulunamadı' exactly. The test was re-run and passes on current code.
  - tests/test_stickywall.py:219 ('en iyi satışta' not in text): still holds.
  - tests/test_stickywall.py:333 and :429 ('Sahibi bulunamadı' in the alert): still hold, because the head keeps that exact phrase.
  - Recommended new assertion after tests/test_stickywall.py:219: `top = '\n'.join(text.split('\n')[:4])`, then assert 'SATIŞ', 'tek emir', '24s hacme oranı', 'en az <b>1 kez</b>', 'drkmttr' and 'SHORT açıyor / büyütüyor' are all in top, and 'post-only' and 'Likidasyon değil' are not.
  - Optional, near tests/test_stickywall.py:333: assert 'Sahibi bulunamadı' appears in the first 4 lines.
  - No other tests reference sticky_alert wording (test_stickywall.py:577 'Likidasyon değildir' is the config/page text, not this formatter).


---

## 12. track

**Biçimlendirici / çağrı yeri:** sticky_note (app/telegram/format.py:681), sent by follow_tick at app/radar/stickywall.py:1115/1123 as kind='track' to the follower's chat_id with stop_kb('wall', fid)

**Şu anki ilk 4 satır:**
```
👁 takip ettiğin duvar
🧲 Yapışkan duvar yarılandı — SAND · SATIŞ
şimdi $654K @ 0.06264 · bu dilimin tepesi $1.5M · defterde 42 dk
🍽 şimdiye yenen $899K · 1 kez yer değiştirdi
(line 5, outside the first 4) 👤 0xc179..e0c8 (vault "drkmttr")
```

**Olması gerekenler (essentials):**
  - What happened: the stage (yarılandı / yeni dilim / yeniden geldi), shown in the head
  - Which coin and side (SAND · SATIŞ)
  - Why the reader got it: this is the wall they follow (👁 takip ettiğin duvar)
  - Size now vs. reference: şimdi $X @ price vs. bu dilimin tepesi $Y (the half rule compares against the tranche peak)
  - How long it has been on the book: defterde N dk
  - Current state, eaten or pulled: şimdiye yenen $Z (owner's maker fills) and how many times it moved
  - Who: owner address and vault name

**Sorunlar:**
  - When the owner is known and eaten_usd is set (the usual case, because eaten_usd comes from the owner's own maker fills), the 👤 owner line is line 5. 'Who' falls outside the first 4 lines.
  - Line 1 is used only for the tag '👁 takip ettiğin duvar'. It carries no data, and it is hidden inside the first list element as an embedded '\n', so the lines list does not match the rendered lines. The event itself (yarılandı + coin + side) only starts on line 2, which is also what the push preview shows second.
  - Measured-wording slip: the line says '{n_moves} kez yer değiştirdi', but n_moves is counted from 30-second snapshots. sticky_alert correctly says 'bakışlarda en az N kez'. The note should also say 'en az'. When n_moves is 0 it currently prints '0 kez yer değiştirdi', which is noise.
  - Minor, data-limited, not in the plan: for tranche/reopen, 'bu dilimin tepesi' usually equals 'şimdi' (seg_peak resets per tranche), so the comparison says nothing there. The `where` value from _sticky_head is computed and never used. Both are left alone because using them would add content or new logic beyond reorder/merge.
  - Consistency note for the orchestrator: twapfollow notes (app/radar/twapfollow.py:159,176) and sticky_end (format.py:710) also put the '👁 takip ettiğin …' tag on its own first line. Merging the tag into the head here makes this note differ from those unless sticky_end gets the same change (sticky_end is a separate audit). The TWAP follow notes are only 1-2 lines, so the tag line costs them nothing.

**Önerilen ilk 4 satır:**
```
🧲 <b>Yapışkan duvar yarılandı</b> — <b>SAND</b> · SATIŞ · 👁 <b>takip ettiğin duvar</b>
şimdi <b>$654K</b> @ 0.06264 · bu dilimin tepesi $1.5M · defterde 42 dk
🍽 şimdiye yenen $899K · en az 1 kez yer değiştirdi
👤 0xc179..e0c8 (vault "drkmttr")
```

**Değişiklik planı:**

Only app/telegram/format.py sticky_note (lines 681-697) changes, and only by reordering, merging and one wording fix. No new data and no I/O.
1) Merge the tag into the head line. Replace lines[0] `f"👁 <b>takip ettiğin duvar</b>\n{head} — <b>{sym}</b> · {side}"` with `f"{head} — <b>{sym}</b> · {side} · 👁 <b>takip ettiğin duvar</b>"`. This removes the embedded '\n', so the event, coin and side become line 1 (and the push preview). It also frees one line.
2) Keep line 2 as it is: 'şimdi $X @ px · bu dilimin tepesi $Y · defterde N dk'.
3) Keep the 🍽 line third and make the wording measured. Change `· {n} kez yer değiştirdi` to `· en az {n} kez yer değiştirdi`, and append the clause only when n_moves > 0 (same idea as sticky_alert's 'bakışlarda en az N kez' and 'aynı fiyattaydı').
4) The 👤 owner line keeps its code position but now renders as line 4 at most, because the tag no longer takes a line. Every case stays at 4 lines or fewer: owner + eaten gives 4 lines, owner without eaten gives 3, and no owner gives 2.
Phone width: the rendered lines are 65-69, 63-64, 50 and 32 characters, which is about the length of the current line 3. The buttons (stop_kb) stay below, attached by the caller.
Optional, separate formatter: apply the same tag-into-head merge and the 'en az N kez' wording to sticky_end (format.py:710 and 714) so the two wall follow notes match.
I verified this by rendering the current and proposed versions for the half/tranche/reopen/no-owner cases with the scratch scripts in /tmp/claude-0/-home-user-hlearninginsiders/49de93d8-1c8e-5cfb-b772-3a1b67b2789e/scratchpad/audit/ (sticky_note_render.py and sticky_note_proposed.py).

**Etkilenen testler:**
  - None need updating. All existing assertions are substring checks, and the plan keeps every substring they rely on.
  - tests/test_stickywall.py:239 checks 'yarılandı' and 'takip ettiğin duvar' (still present: the tag stays verbatim, now as a suffix of line 1)
  - tests/test_stickywall.py:246 checks 'yeni dilim' and chat '-300' (unchanged)
  - tests/test_stickywall.py:282 checks 'yeniden geldi' and chat '-300' (unchanged)
  - tests/test_trackbtn.py:270 checks 'yarılandı' and reply_markup == stop_kb('wall', fid) (unchanged)
  - No test asserts on line order, '\n' position, 'kez yer değiştirdi' or line[0] of sticky_note. tests/test_trackbtn.py:243 splits the first line of position-tracker messages, not wall notes.
  - Suggested new assertion: in test_lifecycle_alert_half_tranche_eaten_reopen after line 239, check that nt.sent[-1][2].split('\n')[0] contains 'yarılandı' and 'SAND', and that the 👤 line is within the first 4 lines.


---

## 13. track

**Biçimlendirici / çağrı yeri:** sticky_end (app/telegram/format.py:705, STICKY_END table :699; caller app/radar/stickywall.py:1115, sent with kind='track' at :1123 to the follower chat, with the stop_kb button)

**Şu anki ilk 4 satır:**
```
👁 takip ettiğin duvar
🧲 Yapışkan duvar bitti — SAND · SATIŞ
🍽 YENİLDİ — kalan son kısım da sahibin dolumu oldu
tepe $1.5M · son görülen $1.5M · 4 dk · 3 kez yer değiştirdi (0.0621–0.06265)
```

**Olması gerekenler (essentials):**
  - What happened: the wall you follow has ended (it is your follow)
  - Coin and side: SAND · SATIŞ
  - Outcome, as measured: YENİLDİ / ÇEKİLDİ / KISMEN / KAYBOLDU, with its one-clause meaning
  - How much was actually filled: duvardan toplam yenen $ (only when an owner is known)
  - How big it was: tepe $ and son görülen $ (the remainder the outcome refers to)
  - How long it lasted: dur_txt(life)
  - Current state: the owner's position at the end (SHORT 61.90M SAND ($3.8M)) vs. its size when the wall started (duvar başında 38.09M)
  - Who: the owner. This field is not rendered anywhere in sticky_end today; sticky_note and twap_end do show 👤 alink(owner)
  - Detail, which belongs below the first 4 lines: how many times it moved and the price range, the yeni dilim/dönüş count, the 15-minute return hint, and the kayboldu explanation

**Sorunlar:**
  - format.py:710: lines[0] holds an embedded '\n', so the tag '👁 takip ettiğin duvar' takes up a whole visual line of its own. The real headline is visual line 2, so only 3 of the first 4 lines carry information.
  - The amount filled ('🍽 duvardan toplam yenen $900K', format.py:715) is on visual line 5. For an end note this is the main 'how much' number, and it sits outside the first 4 lines.
  - The current state, i.e. the owner's position at the end ('📉 Pozisyon: SHORT 61.90M SAND ($3.8M) · duvar başında 38.09M', format.py:717-719), is on visual line 6, outside the first 4.
  - format.py:712-713: visual line 4 is about 80 characters, which wraps to 2 or more lines on a phone. Half of it is low-priority detail ('3 kez yer değiştirdi (0.0621–0.06265)') sitting next to the important size and duration numbers.
  - 'Who' (the owner) is missing from the message entirely, although the row has w['owner'] and w['owner_name'] and sticky_note renders them. A pure reorder cannot fix this; see the optional step in the change plan.
  - Data consistency, outside formatting scope: in the YENİLDİ test case the note reads 'son görülen $1.5M' + 'YENİLDİ — kalan son kısım da sahibin dolumu oldu', but also 'toplam yenen $900K', which is less than the remainder it says was filled. The cause is stickywall.py:1011-1018: eaten_usd adds only _eaten_since (stream fills), not the `after` history fills that decided 'yenildi'. Readers may see this as a contradiction. Worth a separate look; not part of this reorder.

**Önerilen ilk 4 satır:**
```
👁 <b>takip ettiğin yapışkan duvar bitti</b> — <b>SAND</b> · SATIŞ
<b>🍽 YENİLDİ — kalan son kısım da sahibin dolumu oldu</b>
🍽 toplam yenen <b>$900K</b> · tepe $1.5M · son görülen $1.5M · 4 dk
📉 Pozisyon: <b>SHORT 61.90M SAND</b> ($3.8M) · duvar başında 38.09M
```

**Değişiklik planı:**

All changes are in sticky_end, app/telegram/format.py:705-725. They only reorder and merge existing content; there is no new data and no new I/O.
1) :710: replace the two-line lines[0] ('👁 <b>takip ettiğin duvar</b>\n🧲 <b>Yapışkan duvar bitti</b> — ...') with one line: f"👁 <b>takip ettiğin yapışkan duvar bitti</b> — <b>{sym}</b> · {side}". This removes the embedded \n.
2) :711: keep the STICKY_END status line unchanged as line 2.
3) :712-713: split the line in two. The size part becomes line 3: size = f"tepe {usd(peak_ntl)} · son görülen {usd(ntl_last)} · {dur_txt(life)}". When w['owner'] is set, prefix it with the eaten total moved up from :715: f"🍽 toplam yenen <b>{usd(eaten_usd)}</b> · " + size. The old word 'duvardan' is dropped to keep the line about 60 characters.
4) :717-719: move the _sticky_pos(w, end=True) position line up to line 4, still only when owner is set and p is set.
5) After that, add a detail line built from the moves part split off :713 plus the tranche suffix moved from :716: f"🔁 {n_moves} kez yer değiştirdi ({px5(px_min)}–{px5(px_max)})" + (f" · {tranches} yeni dilim/dönüş" if owner and tranches else ""). The 🔁 emoji matches the moves line in sticky_alert.
6) :720-724: leave the italic 15-minute return hint and the kayboldu explanation where they are, below everything else. The stop_kb button is unchanged.
Results: with no owner (kayboldu), the first 4 lines are headline / ❔ KAYBOLDU / 'tepe $1.5M · son görülen $1.3M · 30 dk' / 🔁 moves, then the note. With an owner, every line in the first 4 is 50 to 61 characters.
Verified in scratch (/tmp/claude-0/-home-user-hlearninginsiders/49de93d8-1c8e-5cfb-b772-3a1b67b2789e/scratchpad/audit/sticky_end_proposed.py): I monkeypatched fmt.sticky_end with this layout, and test_lifecycle_alert_half_tranche_eaten_reopen and test_flapping_wall_recheck_and_cloid both pass unchanged.
Optional step that is not a pure reorder (the user listed 'who' as essential): append f" · 👤 {alink(w['owner'])}" to line 4, or to line 1 if there is no position line. It uses the already-loaded row field, rendered the same way sticky_note does. This would make the follower note match twap_end, whose first line already carries 👤.
Separately, and out of scope for formatting: consider whether eaten_usd at the end should also include the `after` history fills (stickywall.py:1011-1018). Today YENİLDİ can sit next to 'toplam yenen' smaller than 'son görülen'.

**Etkilenen testler:**
  - tests/test_stickywall.py:265: asserts 'YENİLDİ' and 'SHORT 61.90M SAND' in the end note. Both stay in the first 4 lines; no change needed (verified passing with the proposed layout).
  - tests/test_stickywall.py:420: asserts 'ÇEKİLDİ' in the end note. Still on line 2; no change needed (verified passing).
  - tests/test_stickywall.py:239: asserts 'takip ettiğin duvar', but on sticky_note (the yarılandı note), not sticky_end. It is affected only if sticky_note's header is changed the same way. Under this plan sticky_end no longer contains the exact substring 'takip ettiğin duvar', and no test asserts it for sticky_end.
  - No other tests reference the sticky_end strings ('Yapışkan duvar bitti', 'duvardan toplam yenen', 'yeni dilim/dönüş', 'kez yer değiştirdi'). The 'son görülen' and 'ÇEKİLDİ' matches in tests/test_acctwatch.py are acctwatch formatters, not this one.


---

## 14. acct

**Biçimlendirici / çağrı yeri:** acct_started (app/telegram/format.py:872; callers app/radar/acctwatch.py:846 -> account group, public=False; app/telegram/bot.py:1443 /hesaplar with title 'anlık durum (… önce)', tail=False)

**Şu anki ilk 4 satır:**
```
👤 drkmttr · 0xc179..e0c8 — takip başladı
📊 Pozisyonlar
• LONG 4.1K ETH ($11.1M) · giriş 2700
• LONG 12.9K XMR ($7.0M) · giriş 544
```

**Olması gerekenler (essentials):**
  - who: account name + address link (line 1 today)
  - what/when: 'takip başladı' / 'takip yeniden başladı (…)' / 'anlık durum (N önce)' (line 1 today)
  - how big: account value and total open position ($13.1M / $33.6M) - today only in the italic footer, last line (15 of 15, 18 of 18 with wall)
  - positions at a glance: side + coin + $ for the largest, incl. the minority side (here the only SHORT, SAND $3.5M, is bullet 5 = line 7)
  - what is on the book that the UI does not show: walls at the price (🧲) and pending close/stop order groups (🎯) - today from line 9/10 down
  - current activity: realized fills in the last hour (🔁 SAND open short $2.9M / close short $2.6M) - today line 13/16
  - (below, details) qty + entry per position, each wall/ladder line, per-coin flow rows, 'Bundan sonra bu gruba…' rule note, truncation warning

**Sorunlar:**
  - Lines 2-4 are a section header plus the two largest positions; account size and total exposure (hesap $13.1M · açık pozisyon $33.6M) are at the very bottom in _acct_foot.
  - '📊 Pozisyonlar' takes a whole line just as a header.
  - Positions are sorted only by size, so the only SHORT (SAND $3.5M, the coin that also has a wall, a close ladder and all of the last hour's fills) shows up at line 7. A reader of the first 4 lines sees '2 LONGs' and does not see the side mix.
  - The first 4 lines do not show that walls (🧲) or pending close/stop groups (🎯, 'arayüzde görünmüyor', which is the alert's main value) exist, or that he traded in the last hour (🔁). With a wall and quotes present those sections start at lines 9-12 and 16.
  - On the stale-restart path, line 1 gets very long: the acctwatch.py:837-838 title adds '— aradaki değişiklikler zamanı bilinmediği için yazılmadı' (an explanation/rule note) to the head line, about 120 characters.
  - _acct_foot puts the truncation warning (a data-quality note) together with the size numbers, so the size cannot move up unless the two are split.

**Önerilen ilk 4 satır:**
```
👤 <b>drkmttr</b> · 0xc179..e0c8 — <b>takip başladı</b>   (unchanged: who + what/when; /hesaplar keeps 'anlık durum (N önce)')
💼 hesap <b>$13.1M</b> · açık pozisyon <b>$33.6M</b>   (size moved up from the footer)
📊 LONG ETH $11.1M · XMR $7.0M · HYPE $6.3M · +2 · SHORT SAND $3.5M   (position bullets merged: side/coin/$ only, up to 3 per side, larger side first; or '📭 Açık pozisyon yok')
🧲 1 duvar fiyatın dibinde · 🎯 3 bekleyen emir grubu · 🔁 son 1 saat dolum: SAND   (section headlines; each part only if non-empty; fixture without wall gives '🎯 2 bekleyen emir grubu · 🔁 son 1 saat dolum: SAND')
```

**Değişiklik planı:**

Scope: acct_started only. acct_events also uses _acct_head/_acct_foot; leave it unchanged. No new fields and no new I/O: everything comes from st['live'], st['walls'], st['ladders'], flow, st['acct'] and st['ntl'], which are already in hand.

1) Split _acct_foot (format.py:734-741):
   - New helper _acct_size(st) returns f"💼 hesap <b>{usd(st['acct'])}</b> · açık pozisyon <b>{usd(st['ntl'])}</b>".
   - New helper _acct_trunc(st) returns the existing italic '⚠️ ayrıntılı emir listesi N emirde kesik — …' text, or ''.
   - Keep _acct_foot as is for acct_events, or rebuild it from the two helpers with the same output.

2) acct_started (format.py:872-906), build the top first:
   L1 = the current head line (unchanged).
   L2 = _acct_size(st).
   L3 = the merged positions summary. Group live by side. Within each side sort by ntl desc and take 3, adding '+N' for the rest of that side. Order sides by their summed ntl, larger first. Each item is f"{sym} {usd(ntl)}". Join as "📊 LONG a · b · c · +N · SHORT d". If there is no live position, L3 = the existing '📭 Açık pozisyon yok' (moved here, not repeated below).
   L4 = ' · '.join of the parts that are non-empty:
   - walls (same filter: miss==0) gives f"🧲 {len(walls)} duvar fiyatın dibinde";
   - lads gives f"🎯 {len(lads)} bekleyen emir grubu" (the wording of the existing '… ve N emir grubu daha' line);
   - flow is not None gives f"🔁 {span.lower()} dolum: " + up to 3 coin labels in _flow_lines' order (by total usd desc), or 'yok' when coins is empty.
   Omit the 🔁 part when flow is None, which test :438 requires. Omit L4 entirely when it has no parts. Pull the span computation out of _flow_lines into _flow_span(flow) so '/hesaplar' cached windows still say 'Son N dk' correctly.

3) Below L4, keep the current detail order: the '📊 Pozisyonlar' header plus bullets with qty and entry (keep the header so the details stay readable), the 🧲 section, the 🎯 section with its 'henüz dolmamış (arayüzde görünmüyor)' header, _flow_lines, then the tail rule 'Bundan sonra bu gruba…' when tail is set. The last line becomes _acct_trunc(st) when it is non-empty. The size line is no longer repeated at the bottom.

4) Optional, same principle, at the caller (acctwatch.py:836-838): shorten the stale title to f"takip yeniden başladı (son yoklama {dur_txt(gap)} önce)". Pass the explanation 'aradaki değişiklikler zamanı bilinmediği için yazılmadı' through a new acct_started(note=…) kwarg and render it as an italic line just before the tail rule. The 'version' title stays as is, because test :363 pins it.

Measured length on the fixture: L1 40, L2 37, L3 about 65, L4 50-78 characters. Wording stays measured: counts and existing labels only, no predictions.

**Etkilenen testler:**
  - None of the existing assertions break under this plan. All are substring checks and every asserted string stays in the message. Below are the assertions that constrain the change, so re-verify them:
  - tests/test_acctwatch.py:170-176: 'drkmttr', 'takip başladı', 'ETH', 'SHORT 56.88M SAND' (stays in the detail bullets, so do not drop qty from the bullets), 'Bekleyen kapatma / stop emirleri', 'henüz dolmamış', 'arayüzde görünmüyor' (keep the 🎯 section header text), ladder lines, '🔁 <b>Son 1 saat — gerçekleşen dolumlar</b>' (keep the _flow_lines header unchanged; the L4 part uses a different lowercase 'son 1 saat dolum:' form), SAND flow row.
  - tests/test_acctwatch.py:179: 'kâr al' not in text (L4 wording 'bekleyen emir grubu' is safe).
  - tests/test_acctwatch.py:437-438: /hesaplar with client=None: 'anlık durum', 'SHORT 56.88M SAND', and '🔁' NOT in text. L4 must omit the 🔁 part when flow is None.
  - tests/test_acctwatch.py:441: 'kapatma (Close Short) $50K' (the flow row stays below).
  - tests/test_acctwatch.py:541: 'kesik' in the started text (the truncation warning must stay, now as the last line via _acct_trunc).
  - tests/test_acctwatch.py:363: 'takip yeniden başladı (emir etiketleri düzeltildi)' (do not change the version title).
  - tests/test_acctwatch.py:556, 560, 567: 'takip yeniden başladı' and '— <b>takip başladı</b>' (L1 format unchanged; if the optional stale-title split is done, :556 still passes).
  - Suggested new assertion near tests/test_acctwatch.py:176: the first 4 lines of text (text.split('\n')[:4]) contain 'hesap <b>$13.1M</b>', 'SHORT SAND $3.5M', 'bekleyen emir grubu' and '🔁 son 1 saat dolum: SAND'.


---

## 15. acct

**Biçimlendirici / çağrı yeri:** acct_events

**Şu anki ilk 4 satır:**
```
👤 drkmttr · 0xc179..e0c8
🧲✅ VVV alış duvarı DOLDU — son görülen kalan 14.8K VVV, dolan %97 (maker) · en az 2 dk sürdü · tepe $433K · ömrü boyunca duvardan dolan 15.0K VVV ($439K) · bu sürede pozisyon LONG 163.6K VVV → LONG 179.0K VVV
hesap $13.1M · açık pozisyon $37.8M
(no line 4: single-event messages are 3 Telegram lines (who / one long event line / account footer). The event line is 208 chars here, about 5-6 lines on a phone. Other renders from the tests: new wall 171-183 chars, wall_back 201, wall_end 172-222, ladder_change 193. In the multi-event case (flip+close+shrink) lines 2-4 are '🔄 ETH yön değiştirdi…' / '🚪 LIT LONG kapattı…' / '📉 VVV LONG küçülttü…', in alphabetical coin order, and the footer is line 5)
```

**Olması gerekenler (essentials):**
  - who: account name + address link (_acct_head)
  - what happened: açtı / kapattı / yön değiştirdi / büyüttü / küçülttü / Yeni duvar / Duvar geri geldi / duvar DOLDU-ÇEKİLDİ-KISMEN-bilinmiyor / Kurdu / Değiştirdi / emir grubu DOLDU-İPTAL
  - coin + side: LONG/SHORT for positions, ALIŞ/SATIŞ (+reduce-only) for walls and order groups
  - how big: $ notional (and qty); for grow/shrink the +/-% and Δ$ against the last reported size; for walls @price and distance to price (fiyata %); for ladders n emir, range and $
  - measured effect on the position: wall effect (SHORT açıyor / büyütüyor, SHORT'u kapatıyor, LONG'u boşaltıyor…) and ladder entry tag (girişin altında → kârda/zararda kapatır)
  - outcome of an ended wall/order group: dolan %, maker
  - how long: wall life (en az X sürdü) + tepe $
  - current state: coin position now (şimdi $, pozisyon şimdi…, X pozisyonu yok, bu sürede pozisyon A → B); account totals (footer) are context only
  - when = the poll time; the message is real time and needs no timestamp

**Sorunlar:**
  - wall_end (format.py:991-999): one 172-222 char line. The essential current-state segment ' · bu sürede pozisyon A → B' is the LAST piece, after the secondary _wall_life figure ' · ömrü boyunca duvardan dolan 15.0K VVV ($439K)'. On a phone it lands around visual line 5-6, outside the first 3-4 lines.
  - wall / wall_back (format.py:987-990 via _wall_txt at :850): 171-201 char line. The measured effect ('— SHORT açıyor / büyütüyor', '— SHORT'u kapatıyor') only arrives around char 110-140, after qty '(22.45M SAND)' and tif 'post-only, reduce-only'. The trailing ' · yoklamalarda 2 farklı emir numarası (kendini yeniden koyuyor)' is a detail mixed into the headline.
  - Multi-event messages: position events come out in alphabetical coin order (acctwatch.position_events iterates sorted(coins), acctwatch.py:238; combined as pev+wev+lev), not by importance or size. In the test, the ETH flip ($1.4M) comes before the LIT close ($4.6M). With 3+ events the account footer and later events fall below line 4.
  - Line 1 holds only who ('👤 drkmttr · 0xc179..e0c8', 24 chars); what happened starts on line 2. Acceptable because it is one short phone line and names the subject in the account group. No change proposed.
  - Ladder lines are long (ladder_change 193, ladder_new 134-143, ladder_gone 101-106 chars) but already put essentials first (kind/side/n/range/$ → distance → entry effect → önceki / pozisyon şimdi). Splitting them would not shorten the visible text, so they stay as they are.
  - Minor redundancy: open and close repeat the side ('HYPE LONG açtı: LONG 70.4K HYPE…', 'LIT LONG kapattı (önceki LONG 1.21M LIT…)'). Optional trim, not required.

**Önerilen ilk 4 satır:**
```
👤 drkmttr · 0xc179..e0c8
🧲✅ VVV alış duvarı DOLDU — son görülen kalan 14.8K VVV, dolan %97 (maker)
en az 2 dk sürdü · tepe $433K · bu sürede pozisyon LONG 163.6K VVV → LONG 179.0K VVV
↳ VVV duvarı: ömrü boyunca duvardan dolan 15.0K VVV ($439K)   [line 5 = footer 'hesap $13.1M · açık pozisyon $37.8M', unchanged and still last. New wall under the same layout: L2 '🧲 Yeni duvar: SAND SATIŞ duvarı $1.4M @ 0.0618 (fiyata %+0.00) — SHORT açıyor / büyütüyor' (89-90 chars), L3 '↳ SAND duvarı: 22.45M SAND · post-only · yoklamalarda 2 farklı emir numarası (kendini yeniden koyuyor)', L4 footer. Multi-event: '🚪 LIT LONG kapattı…' / '🔄 ETH yön değiştirdi…' / '📉 VVV LONG küçülttü…']
```

**Değişiklik planı:**

Only reordering and splitting of existing content. No new data, no new I/O, no wording changes to the measured phrases. All changes are in app/telegram/format.py.

1) Walls, extract a shared helper. Add `_wall_eff(w, live) -> str`: the effect_of + eff_txt mapping, currently inline at :854-861. Add `_wall_parts(w, live) -> (head, detail)`:
- head = f"<b>{sym}</b> {side} duvarı <b>{usd(ntl)}</b> @ {px5(px)} (fiyata %{dist:+.2f})" + (f" — {eff}" if eff)
- detail = f"{qty_txt(sz)} {sym} · {tif}{ro}" + (f" · yoklamalarda {n_o} farklı emir numarası (kendini yeniden koyuyor)" if n_o > 1)
- Keep `_wall_txt` output byte-identical: it is still used by acct_started at :886-888. It only switches to calling _wall_eff.

2) acct_events (:960-1015): replace the single `lines` list with `heads` and `more`. Return "\n".join([_acct_head(addr, st)] + heads + more + [_acct_foot(st)]). The footer stays last.
- open / close / flip / grow / shrink / ladder_new / ladder_change / ladder_gone: the same text as today, appended to heads (they are short or already essentials-first).
- wall (:987-988) and wall_back (:989-990): heads.append("🧲 Yeni duvar: " + head) and heads.append("🧲↩️ Duvar geri geldi (15 dk içinde): " + head) respectively. Then more.append(f"↳ <b>{sym}</b> duvarı: {detail}").
- wall_end (:991-999):
  - heads.append(_wall_end_head(sym, side, w, fill)), unchanged.
  - heads.append(f"en az {dur_txt(life)} sürdü · tepe {usd(ntl_max)}{chg}"). This is the old tail with `chg` (bu sürede pozisyon A → B) moved before _wall_life and onto its own line.
  - If _wall_life(sym, fill) is non-empty: more.append(f"↳ <b>{sym}</b> duvarı: " + wl.removeprefix(" · ")).

3) Optional but recommended: before the loop, stable-sort only the position events. Key: (0 if t in open/close/flip else 1, -usd), where usd = prev.ntl for close, |dusd| for grow/shrink, and cur.ntl for open/flip. Wall and ladder events keep their source order after them, already pev+wev+lev at acctwatch.py:503.

4) Do not touch _acct_head, _acct_foot, _wall_end_head, _wall_life text, _ladder_txt (tests call it directly at :340-341), _ladder_end_txt, or acct_started. Optional: README.md:888 lists the wall-end contents with 'ömrü boyunca … ve o sürede pozisyon değişimi'; it could be reworded to put the position change first.

Verified: I wrote a scratch prototype of exactly this layout, /tmp/claude-0/-home-user-hlearninginsiders/49de93d8-1c8e-5cfb-b772-3a1b67b2789e/scratchpad/audit/acct_events_proposed.py, and monkeypatched it in. All 9 tests in tests/test_acctwatch.py pass unchanged. The current-output render script is acct_events_render.py in the same dir.

**Etkilenen testler:**
  - No existing assertion breaks: all 9 tests in tests/test_acctwatch.py pass with the prototype layout.
  - tests/test_acctwatch.py:204-205 (new SAND wall): '🧲 Yeni duvar: <b>SAND</b> SATIŞ duvarı', 'post-only', 'SHORT açıyor / büyütüyor', '2 farklı emir numarası'. Still pass: head and ↳ detail keep these substrings intact.
  - tests/test_acctwatch.py:333-334 (reduce-only wall): 'post-only, reduce-only' must stay adjacent in the ↳ detail ('{tif}{ro}'); 'SHORT'u kapatıyor' is in the head. Still pass.
  - tests/test_acctwatch.py:608 (VVV wall): '🧲 Yeni duvar: <b>VVV</b> ALIŞ duvarı'. Still passes.
  - tests/test_acctwatch.py:218 and :220 (wall_end ÇEKİLDİ head; 'pozisyon SHORT 79.00M SAND → SHORT 100.00M SAND' now on line 2). Still pass.
  - tests/test_acctwatch.py:631-634 (DOLDU head; 'ömrü boyunca duvardan dolan 15.0K VVV ($439K)' now on the ↳ line; 'bu sürede pozisyon LONG 163.6K VVV → LONG 179.0K VVV'). Still pass.
  - tests/test_acctwatch.py:639-640 (KISMEN head) and :648-649 (bilinmiyor head). Unchanged text, still pass.
  - tests/test_acctwatch.py:244 ('🚪 <b>LIT</b> LONG kapattı' and '🔄 <b>ETH</b> yön değiştirdi') checks presence only, so it is order-independent and still passes with the optional sort. Worth adding an order assertion there (LIT close line before ETH flip line) if the sort is adopted.
  - Suggested new assertions: at :204, the wall head line (split('\n')[1]) contains '— SHORT açıyor / büyütüyor' and does not contain 'yoklamalarda'. At :634, the index of the 'bu sürede pozisyon' line is less than the index of the 'ömrü boyunca' line, and both are above the 'hesap $' footer line.


---

## 16. slice

**Biçimlendirici / çağrı yeri:** slice_summary (app/telegram/format.py:1186; caller app/radar/slicewatch.py:538 _summary_text, sent at :622 to ACCOUNT_CHAT_ID, public=False)

**Şu anki ilk 4 satır:**
```
🔂 Dilimli alım-satım — izleme başladı · 👤 0x30af..393e
CBRS: şu an ALIM SÜRÜYOR — 03:08 TSİ'den beri 213 emir · 9.5K CBRS ≈ $1.7M · dilimler arası ~32 sn · dilim ~$8K
🛠 HL TWAP emri değil (dolumlarda twapId yok) — her dilim tek Limit IOC emri: limit 179.53 = dolum ortalamasının %0.28 üstünde · 5 dolum / 3 fiyat seviyesi · tam doldu
🔄 Aynı dakikalarda: INTC kapatma (Close Long) $721K (212 emir) · DRAM kapatma (Close Long) $721K (212 emir) · MU kapatma (Close Long) $572K (212 emir)
```

**Olması gerekenler (essentials):**
  - What happened: watching started or restarted, and for a restart why and that the series in between were not written (title line)
  - Who: account name and address link
  - For each watched coin, the current state: buy/sell SÜRÜYOR with start time, order count and size ≈ $ total; or the last series (side, start → end, duration, orders, $); or none in the last N hours
  - Gap caveats on that state: the 'Kesintiden önce: … sürüyordu … seyri yazılmadı' line and the '(daha öncesi bakılan pencerenin dışında)' note
  - Current position for each coin: signed size, notional, entry, leverage, liq
  - Below the top lines (details): gap between slices and slice size, the 🛠 method line, 🔄 same-minute other coins, the 'Bu gruba' rule line with thresholds, and the lookback window

**Sorunlar:**
  - When a series is live (the main case), the 📊 Pozisyon line comes after the 🛠 method line (166 chars) and the 🔄 others line (150 chars), so it lands on line 5 and is not in the first 4. In this message the top lines are taken up by details while the current position is pushed out.
  - The live-run line is 111 chars. Its tail '· dilimler arası ~32 sn · dilim ~$8K' is method detail, but it makes the key state line wrap to about 3 lines on a phone. With the pre-window note the line reaches about 165 chars.
  - With more than one watched coin per address, each coin's block is 4 lines (state, 🛠, 🔄, position), so the second coin's state lands on line 6 and its position on line 9. Measured with CBRS+MU: MU state is line 6, 'MU pozisyonu yok' is line 9.
  - 'Kesintiden önce: <coin> … sürüyordu' lines are added only after every coin's block, not next to the coin they qualify. In the single-coin stale restart they reach line 4 only by luck, after the position line. With a live run or a second coin they drop below line 4, so the reader first sees 'son 12 saatte dilimli alım/satım yok' with no correction nearby.
  - Minor, and not fixable by reordering alone: the stale-restart title line is 117 chars because the reason is in parentheses. It is left unchanged because test line 446 pins that exact text.

**Önerilen ilk 4 satır:**
```
🔂 Dilimli alım-satım — izleme başladı · 👤 0x30af..393e
CBRS: şu an ALIM SÜRÜYOR — 03:08 TSİ'den beri 213 emir · 9.5K CBRS ≈ $1.7M
📊 Pozisyon: LONG 146.3K CBRS · şimdi $26.2M · giriş 180.67 · 4x izole · liq 142.73
dilimler arası ~32 sn · dilim ~$8K   (then 🛠 method, 🔄 others, Bu gruba…, bakılan pencere). Stale restart variant: 1 title(reason) · 2 'CBRS: son 12 saatte dilimli alım/satım yok' · 3 'Kesintiden önce: CBRS ALIM sürüyordu (03:08 TSİ'den beri, o ana kadar 4 emir · $32K) — kesintideki seyri yazılmadı' · 4 📊 Pozisyon. Two coins: 1 title · 2 CBRS state · 3 CBRS position · 4 MU state · 5 MU position · details below.
```

**Değişiklik planı:**

Only app/telegram/format.py changes. All changes reorder, merge or split existing content: no new data and no new I/O.

1) Split slice_summary into a top block and a detail block (lines 1205-1236).
   - Make a `detail = []` list.
   - In the per-coin loop, keep adding to `lines` only these, in this order:
     (a) the state line: _slice_run_line, or _slice_hist_line, or 'son N saatte … yok';
     (b) the 'Kesintiden önce' lines for this coin (see step 2);
     (c) the 📊 Pozisyon or '📊 SYM pozisyonu yok' line.
   - For each live run, add these to `detail` instead of `lines`: the gap and slice-size tail (step 3), _slice_method(...) (now line 1213), and _slice_others(...) (now lines 1214-1216).
   - After the loop, add any leftover prev_open lines (coins no longer in syms), then `lines += detail`. The 'Bu gruba' rule line (1231) and the <i>bakılan pencere</i> line (1235) stay last, unchanged.

2) prev_open (now lines 1224-1230): run the existing skip filter before the per-coin loop and collect the kept items. Inside the loop, add the item whose symbol_of(v['coin']) == sym right after that coin's state line. The text of the line does not change.

3) _slice_run_line (line 1170; only slice_summary uses it): drop the `gap` and '· dilim ~$…' tail, so the line ends at '≈ {usd(s['usd'])}'. In slice_summary, put it back as the first detail line for that run, using the same text: f"dilimler arası ~{gap_med:.0f} sn · dilim ~{usd(sl_med)}", with the 'dilimler arası' part left out when gap_med is None. This mirrors the '• son dilim … · dilimler arası … · dilim ~$' line in slice_status. The '(daha öncesi bakılan pencerenin dışında)' note stays next to the time it qualifies.

4) Optional, only when len(syms) > 1: start each detail line with f"<b>{esc(sym)}</b> · " so the 🛠 and 🔄 lines still name their coin once they move below the top block. This is the existing symbol, not new data.

Leave the title and who line, _slice_hist_line, _slice_method, _slice_others and the rule wording as they are. slice_events and slice_status are outside this formatter and need no change.

Check: I monkeypatched this layout in a scratch copy (scratchpad/audit/slice_summary_proposed.py) and ran test_buy_campaign_start_end, test_first_run_ongoing_pre_and_warm and test_replay_late_end_collapse_and_restart. All three pass unchanged. With one coin, the essentials are in lines 1-3 when a series is live and in lines 1-4 for a stale restart.

**Etkilenen testler:**
  - No existing assertion breaks: they all check substrings, and every substring stays contiguous. tests/test_slicewatch.py:200-202 (summary substrings), :369 ('şu an <b>ALIM SÜRÜYOR</b> — 03:08 TSİ'den beri'), :370 ('Limit IOC', DRAM), :389 ('pencerenin dışında'), :410-411 ('izleme başladı', 'son dizi: ALIM 03:08 →'), :446-447 ('izleme yeniden başladı (son yoklama 12 s 58 dk önce', 'Kesintiden önce: <b>CBRS</b> ALIM sürüyordu') were all checked against the monkeypatched layout.
  - Order assertion to add, tests/test_slicewatch.py after :369: L = text.split('\n'); L[1] starts with '<b>CBRS</b>: şu an <b>ALIM SÜRÜYOR</b>' and has no 'dilimler arası'; L[2] starts with '📊 Pozisyon:'; text.index('📊 Pozisyon') < text.index('🛠').
  - Order assertion to add, tests/test_slicewatch.py after :447: 'Kesintiden önce' and '📊 Pozisyon' both fall in text.split('\n')[:4], and text.index('Kesintiden önce') < text.index('📊 Pozisyon').
  - Order assertion to add, tests/test_slicewatch.py after :202: '📊 Pozisyon: <b>LONG 146.3K CBRS</b>' is in '\n'.join(text.split('\n')[:4]), and the 'Bu gruba' line comes after it.
  - Optional new test for 2 coins (cfg.slice_watch_list = f'{A}:CBRS, {A}:MU' plus a MU ticker row): the second coin's state line is at index 3 (4th line), and the 🛠 lines carry the '<b>CBRS</b> · ' / '<b>MU</b> · ' prefix.


---

## 17. slice

**Biçimlendirici / çağrı yeri:** slice_events (app/telegram/format.py:1161; blocks _slice_start_block :1103, _slice_end_block :1124; caller app/radar/slicewatch.py:556 _events_text, send :622)

**Şu anki ilk 4 satır:**
```
START L1: 🔂🟢 CBRS · DİLİMLİ ALIM BAŞLADI · 👤 0x30af..393e
START L2: İlk dilim 03:08 TSİ · şimdiye 3 emir · 134.1 CBRS ≈ $24K · ort. 179.03
START L3: ⏱ dilimler arası ~27 sn (22–32 sn) · dilim ~$8K · %100 taker
START L4: 🛠 HL TWAP emri değil (dolumlarda twapId yok) — her dilim tek Limit IOC emri: limit 179.53 = dolum ortalamasının %0.28 üstünde · 5 dolum / 3 fiyat seviyesi · tam doldu   [166 chars; 📊 Pozisyon is L5, 🔄 others L6, italic rule L7]
END L1: 🔂🏁 CBRS · DİLİMLİ ALIM BİTTİ · 👤 0x30af..393e
END L2: 03:08 → 03:18 TSİ (10 dk) · son dilimden sonra 5 dk aynı yönde dilim gelmedi
END L3: Toplam 20 emir · 893.9 CBRS ≈ $160K · ort. 179.03 · %100 taker
END L4: Fiyat: ilk dilim 179.03 → son dilim 179.03 (+0.00%)   [L5 ⏱ gap medians, L6 📈 hacim payı %16.9, L7 📊 Pozisyon, L8 🔄 others; ⚠️ pre / ⏱ late caveats at the very bottom]
```

**Olması gerekenler (essentials):**
  - What + coin + side: dilimli ALIM/SATIŞ BAŞLADI / BİTTİ (and on a flip, that the opposite run began)
  - Who: address/name
  - When / how long: first slice TSİ (start); first → last TSİ + duration (end)
  - How big: orders · qty · $ so far (start) / total (end)
  - How big vs normal: END = share of coin volume in the same minutes (%16.9); START = slice size + cadence (no volume/threshold measure exists in the start ctx)
  - How fast: gap between slices + slice size (start)
  - Current state: position before → after + şimdi $ · giriş · kaldıraç · liq
  - Caveats that change how the top numbers read: pre-window start (totals are from window start), late detection

**Sorunlar:**
  - START: the current-state line (📊 Pozisyon before → after, şimdi $26.2M, 4x izole, liq) is L5, behind a 166-char 🛠 method line that wraps to about 4-5 lines on a phone. Position ends up around visual line 8.
  - END: the only 'vs normal' measure (📈 CBRS hacmindeki payı %16.9) is L6 and the position line is L7. L4 (price first → last slice) and L5 (gap/slice medians, p10–p90) are detail but sit in the top 4.
  - END L3 also carries 'ort. 179.03 · %100 taker', which is detail and leaves no room to merge the volume share.
  - END: the '⚠️ Başlangıç ölçülmedi' caveat (pre-window run: L2/L3 totals count only from the window start) and the '⏱ Bitiş … geç fark edildi' caveat come last, below the 🔄 others line, so the caveat sits far from the numbers it qualifies. START's late note likewise comes after 🔄 others.
  - Flip message (END block + START block in one message): the SATIŞ BAŞLADI block starts at line 10. The first 4 lines still say it through L2 'ardından SATIŞ dizisi başladı', so that clause must stay on L2 (do not move 'why' below).
  - Not fixable by reorder (note only): START has no volume-share or threshold comparison in its ctx (share is computed only for end events; start_n/start_usd are not passed to slice_events). Also, when one round yields events for several watched coins, every block after the first is necessarily below line 4.

**Önerilen ilk 4 satır:**
```
START L1: 🔂🟢 <b>CBRS · DİLİMLİ ALIM BAŞLADI</b> · 👤 0x30af..393e   (unchanged)
START L2: İlk dilim <b>03:08 TSİ</b> · şimdiye 3 emir · 134.1 CBRS ≈ <b>$24K</b> · ort. 179.03   (unchanged)
START L3: ⏱ dilimler arası ~27 sn (22–32 sn) · dilim ~$8K · %100 taker   (unchanged)
START L4: 📊 Pozisyon LONG 119.4K CBRS → <b>LONG 119.5K CBRS</b> · şimdi $26.2M · giriş 180.67 · 4x izole · liq 142.73   (moved up from L5); below: [⏱ geç fark edildi if any] → 🛠 method → 🔄 Aynı dakikalarda → italic 'Bitti mesajı' rule
END L1: 🔂🏁 <b>CBRS · DİLİMLİ ALIM BİTTİ</b> · 👤 0x30af..393e   (unchanged)
END L2: <b>03:08 → 03:18 TSİ</b> (10 dk) · son dilimden sonra 5 dk aynı yönde dilim gelmedi   (unchanged; flip: '· ardından SATIŞ dizisi başladı')
END L3: Toplam <b>20 emir · 893.9 CBRS ≈ $160K</b> · 📈 CBRS hacmindeki payı: <b>%16.9</b>   (share merged up from L6; ort./taker moved down)
END L4: 📊 Pozisyon LONG 119.4K CBRS → <b>LONG 120.3K CBRS</b> · şimdi $26.2M · giriş 180.67 · 4x izole · liq 142.73   (moved up from L7); below: [⚠️ Başlangıç ölçülmedi] → [⏱ Bitiş … geç fark edildi] → 'Hacim payı: 893.9 / 5.3K CBRS, aynı dakikaların 1 dk mumları' → 'Fiyat: ort. 179.03 · ilk dilim 179.03 → son dilim 179.03 (+0.00%) · %100 taker' → ⏱ dilimler arası medyan … → 🔄 Aynı dakikalarda
```

**Değişiklik planı:**

I tested this plan by monkeypatching the two block builders in a scratch copy (repo files were not edited): /tmp/claude-0/-home-user-hlearninginsiders/49de93d8-1c8e-5cfb-b772-3a1b67b2789e/scratchpad/audit/slice_proto.py. The render script is slice_render.py in the same folder. All tests in tests/test_slicewatch.py pass with the change (0 failures).

_slice_start_block (format.py:1103-1121):
(1) Move the 📊 Pozisyon append (:1115-1116) to directly after the optional ⏱ gap line (:1110-1113), before the _slice_method append (:1114). Position becomes L4 (L3 if gap_med is None).
(2) Pull _slice_late(e.get('late'), ctx, '') out of the loop at :1117 and append it right after the position line, so the timing caveat is the first detail line.
(3) Then append _slice_method (the 🛠 line), then _slice_others if present, then the italic 'Bitti mesajı' rule (:1120, unchanged).
No wording changes.

_slice_end_block (format.py:1124-1158):
(1) Keep L1 (:1132) and L2 (:1133-1134, time + duration + why) unchanged. The flip clause must stay on L2.
(2) L3 (:1135-1136): keep 'Toplam <b>{n} emir · {qty} {sym} ≈ {usd}</b>'. Drop ' · ort. {vwap}' and the taker suffix from it. If e.get('share') is set, append f" · 📈 {sym} hacmindeki payı: <b>%{share:.1f}</b>"; this keeps the exact substring the test pins. L3 is about 67 chars.
(3) L4: the 📊 Pozisyon line (:1147-1148), moved up unchanged.
(4) Detail block below, in this order:
   a. the ⚠️ pre-window caveat (:1152-1154) when present;
   b. the late note (:1155-1157);
   c. the share basis moved out of the old :1144-1146 parenthetical, as f"Hacim payı: {qty(his)} / {qty(vol)} {sym}, aynı dakikaların {1 dk|15 dk} mumları" (only if share);
   d. a 'Fiyat:' line that absorbs ort./taker: "Fiyat: ort. {vwap}" + (" · ilk dilim {px0} → son dilim {px1} ({chg:+.2f}%)" if chg is not None) + (" · %{taker} taker" if taker is not None). This line is always emitted, so vwap and taker are never dropped.
   e. the ⏱ gap/slice-median line (:1139-1141), unchanged;
   f. _slice_others (:1149-1151).
slice_events (:1161) is unchanged; flip keeps the order 'END then START'.

Optional doc follow-up: README.md:981-989 lists BAŞLADI/BİTTİ contents in the old order (method before pozisyon; ortalama/fiyat before hacim payı). Reorder that wording to match.

Not in scope (needs new data, not a reorder): a start-side 'vs threshold' note would need rl[1]/rl[2] (start_n/start_usd) passed into the events ctx at slicewatch.py:553-554.

**Etkilenen testler:**
  - None of the existing assertions break. Every test in tests/test_slicewatch.py passed with the prototype layout (0 failures): :211-216 start substrings, :229-234 end substrings (incl. '📈 CBRS hacmindeki payı: <b>%16.9</b>', kept verbatim on L3, and '%100 taker', now on the Fiyat line), :267-271 flip, :329, :380-381, :394, :429 ('Toplam <b>N emir' is kept on L3).
  - Suggested new order pins (optional), tests/test_slicewatch.py after :216: L = text.split('\n'); assert L[3].startswith('📊 Pozisyon') and L[4].startswith('🛠')
  - tests/test_slicewatch.py after :233: L = text.split('\n'); assert L[2].startswith('Toplam <b>20 emir') and '%16.9' in L[2] and L[3].startswith('📊 Pozisyon')
  - tests/test_slicewatch.py after :267: assert 'ardından SATIŞ dizisi başladı' in flip.split('\n')[1]
  - tests/test_slicewatch.py :394 (pre-window case): optionally assert the line after '📊 Pozisyon' starts with '⚠️ Başlangıç ölçülmedi'


---

## 18. liqattack

**Biçimlendirici / çağrı yeri:** liq_attack_alert (app/telegram/format.py:1421)

**Şu anki ilk 4 satır:**
```
🎯 LIQ ATTACK ADAYI — SNDK ↓   (big symbols add: · endeks/emtia kapısı)
Fiyatı %1.50 aşağı itmek ≈ $50K defter yer; karşılığında $1.2M long patlar (oran 24.1×)   [87 chars]
🔔 kapı: ≤%2 içinde $1.2M liq · oran 24.1× ≥ 2
daha geniş bakınca: %3.25 mesafe içinde $3.7M liq, oran 37.6×   (italic)   --- below the first 4: L5 'hedef: 0xaaaa..aaaa $1.2M @ 98.50', L6 'şimdi 100.00 → hedef 98.50 · hafta sonu bitişine 30s 0dk' (italic), L7 the disclaimer. When the book is thin, L3 becomes '🕳 Görünen defter hedefe varmadan bitiyor — maliyet yalnız görünen derinlikten; ötesi ölçülmedi', which pushes kapı to L4, the wider view to L5, the targets to L6 and price/time to L7. Rendered with the test_alert_gate fixture through the real la.scan() (scratch: /tmp/claude-0/-home-user-hlearninginsiders/49de93d8-1c8e-5cfb-b772-3a1b67b2789e/scratchpad/audit/liqattack_render.py, liqattack_worst.py)
```

**Olması gerekenler (essentials):**
  - What it is: a weekend LIQ ATTACK candidate, the coin (SNDK) and the push direction (↓/↑), plus the class tag 'endeks/emtia kapısı' for big symbols
  - Which side gets liquidated and how much: $1.2M long
  - How far the price must move and what it costs: %1.50 push, about $50K of visible book
  - How big compared with the cost: oran 24.1×. When the book is thin, the 🕳 'defter ince' flag must sit next to the ratio, because the cost is then only a lower bound and the ratio an upper bound
  - Who: the target positions (address, notional, liq price), up to 3
  - Current state: mark now → target price, plus the move since Friday close when known
  - When/how long: time left until the weekend window ends ('hafta sonu bitişine Xs Ydk'). This is also the only place the words 'hafta sonu' appear

**Sorunlar:**
  - L3 is the 🔔 kapı line, a rule note. It repeats the $1.2M and the 24.1× already on L2. Its only new content is the thresholds (≤%2, ≥2), yet it takes a prime slot.
  - L4 is 'daha geniş bakınca …', context about a wider, secondary candidate, and it takes a prime slot.
  - WHO is missing from the first 4: the 'hedef:' target addresses are on L5 (L6 when the book is thin).
  - CURRENT STATE and WHEN are missing from the first 4: 'şimdi 100.00 → hedef 98.50 · Cuma kapanışından … · hafta sonu bitişine 30s 0dk' is on L6 (L7 when thin) and in italics. Nothing in the first 4 lines says it is the weekend window or how long it lasts.
  - Thin-book case: a 94-char explanation sentence becomes L3 and pushes everything down a line, so the first 4 are header, cost, caveat, kapı. The qualifier is essential; its explanation is detail.
  - L2 is 87 chars, and the phrase 'defter yer; karşılığında' is long. It wraps to about 3 lines on a phone.
  - Minor, outside a pure reorder: the $ threshold s['alert_min'] (≥$1M, or ≥$50M for endeks/emtia) appears nowhere. The kapı line shows only ≤%2 and the ratio ≥2.
  - Minor: after the reorder, 'hedef' would mean two things on adjacent lines (the target price on L3, the target positions on L4). A 👤 prefix on the positions line separates them.
  - Note: scan() only sends when alert_ok, which requires a zone, so production messages always take the zone branch. The non-zone 'near_usd' branch is reached only by direct calls (tests/test_wording.py:150).

**Önerilen ilk 4 satır:**
```
🎯 LIQ ATTACK ADAYI — SNDK ↓   [+ ' · endeks/emtia kapısı' when big; unchanged]
Fiyatı %1.50 aşağı itmek ≈ $50K defter yer → $1.2M long patlar (oran 24.1×)   [+ ' · 🕳 defter ince' when thin]
şimdi 100.00 → hedef 98.50 [· Cuma kapanışından %-1.10] · hafta sonu bitişine 30s 0dk   (ctx line moved up, no italics)
👤 hedef: 0xaaaa..aaaa $1.2M @ 98.50 [· up to 2 more targets]   --- then below: 🔔 kapı line (unchanged), <i>daha geniş bakınca …</i> (unchanged), the full 🕳 explanation sentence (when thin), the disclaimer. If there are no targets (direct call / test_wording), L4 is simply the next detail line. Prototyped in scratch liqattack_proposed.py: zone 71/57/35 chars, worst case (thin + 3 targets) 87/84/93 chars
```

**Değişiklik planı:**

Edit only app/telegram/format.py, liq_attack_alert (1421-1470). No change is needed in app/radar/liqattack.py:314-317 or in the fan-out, which appends FOOT after the disclaimer. The plan adds no new data and no new I/O.

1. lines[0], the header (1442-1443): unchanged.

2. lines[1], the cost line (1444-1447): replace '; karşılığında ' with ' → ', keeping 'defter yer'. When `thin` is true, append ' · 🕳 defter ince'. The substrings '<b>%X aşağı</b>', 'long patlar' and 'oran <b>N×</b>' are preserved.

3. Move the ctx block (1463-1469: build `ctx`, add dev_close, add the wk countdown) to right after `lines` is created, and append it as lines[2] as plain text instead of wrapping it in '<i>…</i>'.

4. Move the targets block (1460-1462) right after the ctx line and change its prefix from 'hedef: ' to '👤 hedef: '.

5. Next comes the existing `if zone:` / `elif near_usd` block (1452-1459), text unchanged: the 🔔 kapı line, then the italic 'daha geniş bakınca' line.

6. Move the `if thin:` block (1449-1451) after that zone block, text unchanged. The full 🕳 explanation becomes detail; the short marker from step 2 stays on top.

7. The disclaimer stays last, unchanged.

Optional, outside a pure reorder: show the $ gate in the kapı line, e.g. '≤%2 içinde ≥ {usd(s['alert_min'])} kapısı'. The field is already in `s`.

A prototype of exactly this layout lives in /tmp/claude-0/-home-user-hlearninginsiders/49de93d8-1c8e-5cfb-b772-3a1b67b2789e/scratchpad/audit/liqattack_proposed.py. With it, every existing assertion's substrings still match.

**Etkilenen testler:**
  - No existing assertion breaks. All the substrings below were checked against the prototype output; the kapı line is kept below line 4, so its substrings survive.
  - tests/test_liqattack.py:155-156 ('LIQ ATTACK ADAYI', 'SNDK', '↓', 'long patlar'): still pass
  - tests/test_liqattack.py:364 ('≤%2', '$1.2M</b> liq' from the kapı line): still pass
  - tests/test_liqattack.py:366 ('%1.50 aşağı' present, '%3.25 aşağı' absent): still pass
  - tests/test_liqattack.py:367 ('daha geniş bakınca: %3.25' and 'oran 24' from the kapı line): still pass
  - tests/test_liqattack.py:437 ('SP500', 'endeks/emtia kapısı', '≤%1 içinde', '$60.0M'): still pass
  - tests/test_wording.py:150-152 ('Pazartesi açılış fiyatı hakkında ölçüm içermez'): still pass
  - Recommended new assertion after tests/test_liqattack.py:367: f4 = bot.sent[0].split('\n')[:4]; assert 'long patlar' in f4[1] and 'hafta sonu bitişine' in f4[2] and 'hedef:' in f4[3] and 'kapı:' not in '\n'.join(f4)
  - Recommended new assertion at tests/test_wording.py:150 (thin case): atk.split('\n')[1] contains '🕳' and 'Görünen defter hedefe varmadan bitiyor' not in '\n'.join(atk.split('\n')[:4])


---

## 19. cryptoliq

**Biçimlendirici / çağrı yeri:** crypto_liq_alert (app/telegram/format.py:1493), plus app/radar/cascade.py describe(). Called from app/radar/cryptoliq.py:869 and sent with send_rich :884 (the full text becomes the photo caption when it is 1000 visible characters or less). The same text goes to fan-out. The fan-out digest uses line 1 only.

**Şu anki ilk 4 satır:**
```
🔥 HYPE — likidasyona ≤%1 · 2. uyarı · 3 pozisyon · $3.5M
🔥 🔴 SHORT $2.0M · liq 40.36 (%0.90 üstte) · 10x · 👤 0xaaaa..aaaa → /takip_101
🔴 SHORT $800K · liq 40.64 (%1.60 üstte) · 5x · 👤 0xbbbb..bbbb → /takip_102
🟢 LONG $650K · liq 39.12 (%2.20 altta) · 20x · 👤 0xcccc..cccc 🤖MM → /takip_103
```

**Olması gerekenler (essentials):**
  - What happened, and which coin and stage: positions are near liquidation, stage threshold ≤%2.5/≤%1/≤%0.5, '2. uyarı'/'SON UYARI' (header)
  - How many positions and the total size (header: 'N pozisyon · $X')
  - Which side is at risk and the forced flow it would cause: 'long'lar patlarsa zorunlu SATIŞ ~$' / 'short'lar patlarsa zorunlu ALIŞ ~$' (impact line)
  - Nearest position: side, size, liq price, % distance above or below, leverage, owner address with MM/VAULT tag, and /takip_N
  - Current state: 'şimdi <price>'
  - Data freshness / when: 'defterler az önce doğrulandı (canlı)' or 'ölçüm Xdk önce (son süpürme; sonda alınamadı)'
  - Detail that can go below: other positions, '… +N', 💣 Zincir (describe), 'ayrıca N pozisyon daha eşikte', the caveat 'havuzdaki adresler — HL'nin tamamı değil · kapsama…', and DISCLAIMER

**Sorunlar:**
  - When the alert lists 2 or more fresh positions (normal for HYPE/BTC-type coins), the first 4 lines are the header plus 3 position rows. The impact line (which side, forced SATIŞ/ALIŞ amounts), the current price and the freshness come only after the whole list and the '… +N' line. That is line 5 and line 8 with 3 rows, and up to lines 9 and 13 with 6 rows plus a cascade and an 'ayrıca' line. Rendered HYPE stage-2 example: impact on line 5, 'şimdi 40.00 · ölçüm 1h önce' on line 8.
  - The header gives no side. A reader only learns from the impact line whether longs or shorts are at risk and whether the forced flow is a sale or a purchase, and that line sits below the list.
  - Freshness decides how much to trust the alert (live check vs. a sweep 47 min or 1 h old). It is merged into the last italic context line together with the pool caveat and coverage. In the realistic example that line is about 150 characters, so essential data and the caveat share one long line at the bottom.
  - A single-position alert (the test cases) already has everything in 4 lines: header, row, impact, context. Its line 4 is still long, though: price, freshness and the 'havuzdaki adresler — HL'nin tamamı değil' caveat on one line.
  - The 💣 Zincir line from describe() is about 200 characters. That is right for the detail section and it must not move into the first 4. No change is needed in cascade.py.
  - Minor, out of scope: size relative to the notification threshold ($500K) is not shown. The formatter does not receive min_usd, and adding it would be new data, so it is left out.

**Önerilen ilk 4 satır:**
```
🔥 HYPE — likidasyona ≤%1 · 2. uyarı · 3 pozisyon · $3.5M
şimdi 40.00 · ölçüm 1h önce (son süpürme; sonda alınamadı)
📉 long'lar patlarsa zorunlu SATIŞ ~$650K · 📈 short'lar patlarsa zorunlu ALIŞ ~$2.8M
🔥 🔴 SHORT $2.0M · liq 40.36 (%0.90 üstte) · 10x · 👤 0xaaaa..aaaa → /takip_101
```

**Değişiklik planı:**

Only app/telegram/format.py crypto_liq_alert changes. The work is reordering plus splitting one line; no wording, data or I/O changes. Nothing changes in cascade.py, the cryptoliq.py caller, the short_caption or the keys.
(1) Line 1 is the header (format.py:1510-1511), unchanged. The fan-out digest and tests that check startswith('🚨'/'🔥 <b>PUMP</b>') still work.
(2) Line 2: move the freshness block (format.py:1540-1546: ctx=['şimdi px'] + 'defterler az önce doğrulandı (canlı)' or 'ölçüm {age} önce (son süpürme; sonda alınamadı)') up to just after the header. Emit it as '<i>' + ' · '.join(ctx) + '</i>', holding price and freshness only.
(3) Line 3: move the impact block (format.py:1525-1533, the SATIŞ/ALIŞ sums) to just after line 2. It always appears, because every fresh row is long or short.
(4) Line 4 onward: the position rows loop (1512-1521), nearest first, so line 4 is the nearest or trigger position: who, size, liq, distance, leverage, /takip. The '… +N pozisyon daha' line (1522-1524) follows.
(5) Then the existing order: cascade describe() (1534-1536), then 'ayrıca N pozisyon daha eşikte' (1537-1539).
(6) The caveat stays at the bottom as its own line: '<i>havuzdaki adresler — HL'nin tamamı değil' + (' · ' + coverage_ctx) + '</i>' (from 1547-1549), then DISCLAIMER.
(7) Update the docstring to describe the order: header → şimdi/freshness → forced flow → positions → chain/old → caveat.
The prototype renders: single position 303 vs. 305 visible characters; HYPE example 856 vs. 858. So the ≤1000 caption-fit decision is unchanged or slightly more lenient. Every line in the first 4 is 83 characters or less; the longest is the two-sided impact line.
Scratch scripts: /tmp/claude-0/-home-user-hlearninginsiders/49de93d8-1c8e-5cfb-b772-3a1b67b2789e/scratchpad/audit/cryptoliq.py (renders the current text) and /tmp/claude-0/-home-user-hlearninginsiders/49de93d8-1c8e-5cfb-b772-3a1b67b2789e/scratchpad/audit/cryptoliq_proposed.py (prototype; it re-checks the existing substring assertions).

**Etkilenen testler:**
  - tests/test_tracker_liq.py:119-120 — needs updating: it asserts /takip_{offers[0]} in al[1] and /takip_{offers[1]} in al[2]. Position rows move to al[3] and al[4] (header, şimdi/freshness, impact, then rows), so the indices change to al[3]/al[4] and the message's al[:3] becomes al[:5].
  - tests/test_cryptoliq.py:290, 299-300, 501-506 — still pass: header text and position, '≤%x', '2. uyarı', 'SON UYARI', '%0.90 altta', '🚨 🟢 LONG', 'doğrulandı', 'ölçüm 47dk önce', '1 pozisyon', disclaimer, no PROPR.
  - tests/test_coverage.py:118-119 — still pass, as long as the caveat line keeps 'havuzdaki adresler — HL'nin tamamı değil · kapsama …' together (the plan does).
  - tests/test_fanout.py:285 — not affected; the digest reads the first line, and the header stays first.


---

## 20. cryptoliq (split-mode photo caption)

**Biçimlendirici / çağrı yeri:** inline short-caption f-string at app/radar/cryptoliq.py:881-882 (`cap = f"📈 <b>{coin}</b> · liq {px(fresh[0]['liq_px'])} · %{fresh[0]['dist']:.2f} kaldı"`), passed as short_caption to notifier.send_rich at cryptoliq.py:884. It is used only when the full crypto_liq_alert text is over CAPTION_MAX=1000 visible characters. In that case send_rich (app/notify.py:197-202) sends the full text FIRST and the chart photo with this caption SECOND, so the caption is the newest message in the channel: it is what the chat-list preview and that message's push notification show.

**Şu anki ilk 4 satır:**
```
📈 PUMP · liq 0.0031 · %1.80 kaldı
(no line 2; the caption is one line of 33 visible / 40 raw chars. It is identical for stage 1 💥, stage 2 🔥 and stage 3 🚨 SON UYARI, and for a long or a short: the short stage-3 render gives '📈 PUMP · liq 0.0032 · %0.40 kaldı')
```

**Olması gerekenler (essentials):**
  - stage / severity and threshold: 💥 ≤%2.5, 🔥 2. uyarı ≤%1, 🚨 SON UYARI ≤%0.5
  - coin
  - side: LONG (liq below, forced SELL) or SHORT (liq above, forced BUY)
  - size: notional of the nearest position, plus position count and total in this alert
  - liq price and distance to it (% left, below or above)
  - that the line is the nearest of several when more than one position is in the alert
  - who (address, /takip) and current price stay in the full text sent just before; not needed in the caption

**Sorunlar:**
  - Side is missing. '%1.80 kaldı' does not say whether price has to fall (long) or rise (short), so a long and a short caption look the same.
  - The 📈 icon reads as 'up / pump'. For a long liquidation (price falls, forced SELL) it contradicts the text's own '📉 long'lar patlarsa zorunlu SATIŞ' line. The photo already makes it obvious that this is a chart.
  - Stage is missing. The header icon and tail (💥 / 🔥 · 2. uyarı / 🚨 · SON UYARI) are dropped, so a final warning has the same caption as a first warning.
  - Size is missing: no notional, no position count and no total (e.g. '2 pozisyon · $1.9M'). The threshold (≤%2.5) is missing too, so 'how big vs. the threshold' is absent.
  - With several positions the caption shows only fresh[0] and does not say it is the nearest one.
  - Split mode fires exactly when the text is longest (many positions, cascade, coverage lines). So the biggest alerts get the thinnest caption, and that caption is the last message and the one the channel preview shows.

**Önerilen ilk 4 satır:**
```
💥 PUMP — likidasyona ≤%2.5 · 1 pozisyon · $1.2M
🟢 LONG $1.2M · liq 0.0031 · %1.80 kaldı (altta)
(with several positions line 2 gets a prefix: 'en yakın: 🟢 LONG $1.2M · liq 0.0031 · %1.80 kaldı (altta)'; stage 3 example: '🚨 PUMP — likidasyona ≤%0.5 · SON UYARI · 12 pozisyon · $147.6M' / 'en yakın: 🔴 SHORT $12.3M · liq 0.0032 · %0.40 kaldı (üstte)')
(nothing else; the caption stays 2 lines: 109 raw / 95 visible chars for the test fixture, 119/105 for long+short, 136/122 worst case checked; each line is at most 62 visible chars)
```

**Değişiklik planı:**

Only app/radar/cryptoliq.py:881-882 changes. No new data or I/O: everything comes from `text` (already built at :866) and `fresh[0]` (already used by the old caption).
1) Line 1 = the full text's own header, taken verbatim with `text.split("\n", 1)[0]`. This is the header built at format.py:1513-1514 ('{icon} <b>COIN</b> — likidasyona ≤%{thr}{tail} · N pozisyon · <b>$total</b>'), so the caption now carries stage, threshold, SON UYARI, count and total, and always matches the message above it.
2) Line 2 merges the old caption's 'liq PX · %D kaldı' with the side/notional/altta-üstte parts of the first position row (format.py:1522-1524). Leverage, the address link and /takip are left out because they are in the text above.
3) Drop the 📈 prefix.
Sketch:
  p0 = fresh[0]; lg = p0.get("side") == "long"
  cap = (text.split("\n", 1)[0] + "\n" + ("en yakın: " if len(fresh) > 1 else "")
         + f"{'🟢 LONG' if lg else '🔴 SHORT'} {fmt.usd(p0['notional'])}"
         f" · liq {fmt.px(p0['liq_px'])} · %{p0['dist']:.2f} kaldı ({'altta' if lg else 'üstte'})")
Optional: move this into app/telegram/format.py as `crypto_liq_caption(text, fresh)`, next to crypto_liq_alert, so all formatters live in one place. The wording stays measured (distance and size only, no prediction). Rendered with tests/test_cryptoliq.py fixtures via scratchpad/audit/cryptoliq_cap.py and cryptoliq_cap_proposed.py.

**Etkilenen testler:**
  - tests/test_cryptoliq.py:417: `'kaldı' in cap and 'PUMP' in cap` still passes. `len(cap) < 120` also still passes for this fixture (109 raw), but with little margin: multi-position or stage-3 captions reach 119-136 raw. Change it to `fmt.visible_len(cap) < 160` and add `cap.startswith('💥') and 'LONG' in cap and '$1.2M' in cap and 'altta' in cap and '📈' not in cap` to pin the new layout.
  - tests/test_cryptoliq.py:236 is not affected (combined mode: the photo caption there is the full text).


---

## 21. cryptoliq

**Biçimlendirici / çağrı yeri:** crypto_liq_closed (app/telegram/format.py:1782-1817; caller app/radar/cryptoliq.py:932 _closure_notes -> notifier.send('cryptoliq', ...) at :934, text sent unchanged)

**Şu anki ilk 4 satır:**
```
1| 💀 PUMP — LİKİDE OLDU
2| 🔴 SHORT $700K · liq 0.0033 · son mesafe %0.30 · 💀 likide oldu · gerçekleşen 0.0033 · 👤 0x1111..1111   (99 chars; the 'close' variant is 98, the 'unknown' variant 114)
3| 📈 ~$700K zorunlu ALIŞ piyasaya çarptı
4| (blank) -> 5| ℹ️ Gözlem aracıdır, yatırım tavsiyesi değildir.   || Multi-row example (xyz:HYPE, 2 liq + 1 close): 1| 💀 HYPE — LİKİDE OLDU · 3 pozisyon  2| 🟢 LONG $2.4M · liq 41.20 · son mesafe %0.35 · 💀 likide oldu · gerçekleşen 41.18 · 👤 0x1111..1111  3| 🟢 LONG $800K · liq 40.90 · son mesafe %1.10 · 🏁 kapatıldı (likidasyon kaydı yok) · 👤 0x2222..2222  4| 🔴 SHORT $600K · ... 💀 likide oldu ...  5| 📉 ~$2.4M zorunlu SATIŞ piyasaya çarptı · 📈 ~$600K zorunlu ALIŞ piyasaya çarptı
```

**Olması gerekenler (essentials):**
  - What happened: liquidated (💀) vs closed without a liquidation record (🏁) vs could not be verified
  - Coin
  - Side (LONG/SHORT) and size (notional), which is how big the forced flow was
  - Market impact: ~$X forced SELL (long liq) / forced BUY (short liq) hit the market
  - Where: realized fill price (gerçekleşen) vs liq price, and the last measured distance before it vanished
  - Who: wallet address (hypurrscan link)
  - For multi-position notes: how many of N were actually liquidated, plus the aggregate impact
  - (Not rendered today, so out of scope for a reorder: when it closed (closed_ts = scan-detection time; a note can go out up to CLOSE_NOTE_MAX_AGE=24h later), and which stage alert came before it)

**Sorunlar:**
  - Single-row case (the common one): the facts are technically in lines 1-3, but the header has no side or size. Line 2 is one 98-114 char run-on that packs side, size, liq px, last distance, outcome, realized px and address, and on a phone it wraps to about 3 visual lines.
  - The single-row text repeats itself: the header says '💀 LİKİDE OLDU' and line 2 says '💀 likide oldu' again.
  - The market-impact line ('~$X zorunlu SATIŞ/ALIŞ piyasaya çarptı') is the main point for readers, but it is appended after ALL position rows. With 3 rows it is line 5, so it falls outside the first 4 lines (rendered multi example). With more rows it sinks further.
  - The multi-row header is misleading. The head turns 💀 'LİKİDE OLDU' as soon as one row is a liquidation, and it shows the total count ('LİKİDE OLDU · 3 pozisyon') even when only 2 of the 3 were liquidated and 1 was a normal close.
  - Multi-row order follows closed_ts (SQL ORDER BY coin, closed_ts), not importance. The biggest liquidation can end up below a smaller plain close.
  - In the close and unknown variants, the qualifier that tells the reader what kind of exit it was ('likidasyon kaydı yok' / 'likidasyon mu kapatma mı doğrulanamadı') sits mid-line, after size, liq and distance.
  - Minor gap, out of scope (would be new data): no timing. closed_ts is set at scan detection and is not rendered, and a note may go out up to 24h after detection (e.g. after a failed send).

**Önerilen ilk 4 satır:**
```
1| 💀 <b>PUMP</b> — 🔴 SHORT <b>$700K</b> <b>LİKİDE OLDU</b>   (close: '🏁 <b>PUMP</b> — izlenen 🟢 LONG <b>$400K</b> kapatıldı'; unknown: '... kapandı'; multi: '💀 <b>HYPE</b> — <b>LİKİDE OLDU</b> · 2/3 pozisyon')
2| 📈 ~$700K zorunlu <b>ALIŞ</b> piyasaya çarptı   (close: 'likidasyon kaydı yok'; unknown: 'likidasyon mu kapatma mı doğrulanamadı'; multi with both sides: '📉 ~$2.4M zorunlu <b>SATIŞ</b> · 📈 ~$600K zorunlu <b>ALIŞ</b> piyasaya çarptı')
3| gerçekleşen 0.0033 · liq 0.0033 · son mesafe %0.30   (close/unknown: 'liq 0.0031 · son mesafe %0.40'; multi: first row '🟢 LONG <b>$2.4M</b> · 💀 likide · 👤 0x1111..1111')
4| 👤 0x1111..1111   (multi: '   gerçekleşen 41.18 · liq 41.20 · son mesafe %0.35', then the other rows as 2-line pairs, liquidations first by size desc; DISCLAIMER stays last)
```

**Değişiklik planı:**

This only touches app/telegram/format.py crypto_liq_closed (1786-1817). The caller and the data stay as they are, and nothing new is shown except the liquidated/total count in the mixed multi header.
(a) Move the impact block (1807-1815) above the position rows: compute sell, buy and imp first, then insert the line right after the header (or right after the qualifier line for close/unknown, where there is no impact line). Single-sided text stays exactly the same ('📉 ~$X zorunlu <b>SATIŞ</b> piyasaya çarptı'). When both sides are present, merge them into one line: '📉 ~$X zorunlu <b>SATIŞ</b> · 📈 ~$Y zorunlu <b>ALIŞ</b> piyasaya çarptı'.
(b) len(rows)==1:
  - Fold the row's tag and usd(notional) (now in line 1804) into the header. liq: f"💀 <b>{sym}</b> — {tag} <b>{usd}</b> <b>LİKİDE OLDU</b>". close: f"🏁 <b>{sym}</b> — izlenen {tag} <b>{usd}</b> kapatıldı". unknown: same with 'kapandı'.
  - close/unknown: the qualifier becomes line 2 on its own ('likidasyon kaydı yok' / 'likidasyon mu kapatma mı doğrulanamadı').
  - Then the impact line (liq only).
  - Then one 'where' line: '[gerçekleşen {px(closed_px)} · ]liq {px(liq_px)} · son mesafe %{last_dist:.2f}'.
  - Then '👤 {alink}'.
  - Drop the duplicate '💀 likide oldu' text.
(c) len(rows)>1:
  - Keep the header. When rows are mixed, the count becomes ' · {len(liq)}/{len(rows)} pozisyon' (all liq or none liq: ' · {n} pozisyon' as now; the all-close head can read 'izlenen pozisyonlar kapandı').
  - The impact line goes on line 2.
  - Sort rows: liq first, then notional desc.
  - Each row becomes 2 short lines. Line A: '{tag} <b>{usd}</b> · {💀 likide | 🏁 kapatıldı (likidasyon kaydı yok) | 🏁 kapandı · doğrulanamadı} · 👤 {alink}'. Line B: '   [gerçekleşen X · ]liq Y · son mesafe %Z'.
(d) DISCLAIMER stays last, unchanged.
Lines stay at 50 chars or less in the single-row case (62 for the two-sided impact line).
I prototyped this as a scratch function. With it monkeypatched in, tests/test_cryptoliq.py passes in full; the prototype is at /tmp/claude-0/-home-user-hlearninginsiders/49de93d8-1c8e-5cfb-b772-3a1b67b2789e/scratchpad/audit/proposed_fmt.py, renders in cryptoliq_closed.py and cryptoliq_closed_proposed.py, test run in run_patched.py.

**Etkilenen testler:**
  - No existing assertion has to change. Verified by running tests/test_cryptoliq.py with the proposed formatter monkeypatched in: all tests pass.
  - tests/test_cryptoliq.py:509 — 'LİKİDE OLDU', 'ALIŞ</b> piyasaya çarptı' and 'gerçekleşen 0.0033' are still present. Suggested addition: assert tc.split('\n')[0] contains 'SHORT' and '$700K', and line[1] contains 'zorunlu <b>ALIŞ</b>'.
  - tests/test_cryptoliq.py:310-311 — startswith('💀 <b>PUMP</b>'), 'LİKİDE OLDU', 'gerçekleşen', 'SATIŞ</b> piyasaya çarptı' and 'son mesafe %0.40' are all kept (single-sided impact wording unchanged).
  - tests/test_cryptoliq.py:353 — startswith('🏁') and 'likidasyon kaydı yok' are kept.
  - tests/test_cryptoliq.py:370 — 'doğrulanamadı' is kept.
  - Optional new test: a multi-row mixed case (2 liq + 1 close, both sides). Assert the header has '2/3 pozisyon', line 2 is the combined impact line, and the first row is the largest liquidation. No such test exists today.


---

## 22. cryptovol

**Biçimlendirici / çağrı yeri:** crypto_vol_alert (+ what_happened) — app/telegram/format.py:1820 (what_happened at :1360). Callers: app/radar/cryptovol.py:324 (send_rich :329 to crypto_chat_id; the full text is the photo caption when it is 1024 visible chars or less) and app/radar/equityvol.py:186 when route_for picks a crypto dex. Rendered with scratch scripts in /tmp/claude-0/-home-user-hlearninginsiders/49de93d8-1c8e-5cfb-b772-3a1b67b2789e/scratchpad/audit/ (cryptovol_render.py, cryptovol_proposed.py, cryptovol_run_proposed.py). Input was the test_volchart rec (PUMP, $1.2M, 7.6x, day_vol 3.3e8) plus a realistic window_brief. alert_forensics defaults to True (config.py:1033), so the brief block is the production case. The test fixture turns it off (brief=None), which gives a 4-line message: header / size+price / 24s hacim / disclaimer.

**Şu anki ilk 4 satır:**
```
🚀 PUMP — 24 saatin en yüksek 5 dakikalık hacmi
5dk hacim $1.2M · önceki rekorun 7.6×'i · fiyat 0.0033 🟢 +0.06%
📊 24s hacim $330.0M
📈 Yeni LONG açılmış — açık pozisyon arttı, fiyat yükseldi (OI %+1.84, fiyat %+0.42) · OI ölçümü 10dk penceresinde
```

**Olması gerekenler (essentials):**
  - What: this 5-min bucket set the 24-hour record for volume (header, line 1)
  - Which coin (line 1)
  - How big: 5-min volume in $ (line 2)
  - Compared with normal: multiple of the previous 24-hour max ('önceki rekorun N×'i'), plus the scale given by 24h volume (lines 2-3)
  - Price change in that same 5-min bucket, 🟢/🔴 (line 2)
  - Side or position reading from OI: new LONG/SHORT, closed, changed hands (line 4, but buried in a ~115-char line)
  - Who traded: number of addresses, buy vs sell, net, taker % (line 5, OUTSIDE the first 4)
  - Largest addresses with their position and action (lines 6-8, detail, OK below)
  - When / how long: the window is the last closed 5-min bucket. It is stated as '5dk' and the message timestamp gives the time. bucket_ts is not printed, which is acceptable.

**Sorunlar:**
  - With forensics on (the default), the 'who' line ('👥 son 5dk · 14 adres · alım $820K / satım $310K · net +$510K · taker %71') is line 5. A reader of the first 4 lines does not see whether the record was net buying or net selling, or how many addresses took part.
  - Line 3 spends a whole line on '📊 24s hacim $330.0M'. That is one context number and belongs on the size line.
  - Line 4 is the full what_happened head: verdict, then why (with OI%/fiyat%), then the widened-window note. It is about 115 visible chars and wraps to 3 or more lines on a phone. The explanation is detail, and only the short verdict ('📈 Yeni LONG açılmış') belongs at the top.
  - When OI is not measured (verdict 'belirsiz'), line 4 is '❔ Belirsiz — OI ölçümü yok — kripto OI kaydı yeni açıldı ya da …' (about 90 chars). That is a non-finding in the top block, and the useful '👥' line drops to line 5.
  - Line 2 mixes two topics, size and price. Price fits better next to the OI verdict ('what price and positions did'), which also frees room on line 2 for the 24h volume.
  - Minor, no change proposed: the bucket time (bucket_ts) is never printed. The alert goes out right after the bucket closes, so the message timestamp is enough, and adding it would be new content.
  - Minor: sym on line 1 is not esc()'d (same in equity_vol_alert). Harmless for HL symbols.

**Önerilen ilk 4 satır:**
```
🚀 <b>PUMP</b> — <b>24 saatin en yüksek 5 dakikalık hacmi</b>
5dk hacim <b>$1.2M</b> · önceki rekorun <b>7.6×</b>'i · 24s hacim $330.0M
fiyat 0.0033 🟢 <b>+0.06%</b> · 📈 <b>Yeni LONG açılmış</b>
👥 son 5dk · 14 adres · alım <b>$820K</b> / satım <b>$310K</b> · net +$510K · taker %71
```

**Değişiklik planı:**

Only crypto_vol_alert changes (format.py:1820-1837). what_happened stays byte-identical for its other callers (format.py:377 whale, :1865 equity_vol_alert, :1942 closed-session), which are audited separately. Measured visible widths after the change: L1 46, L2 59, L3 43, L4 72 chars. Total length stays about 576 visible chars, so the combined photo+caption path is unchanged.
1) Line 1 stays as is. Do not touch the substring '24 saatin en yüksek 5 dakikalık hacmi' (test_volchart.py:119).
2) Line 2: keep '5dk hacim <b>{usd}</b>' and the optional ' · önceki rekorun <b>{ratio:.1f}×</b>'i'. Remove the price part from this line and append ' · 24s hacim {usd(day_vol)}' when e['day_vol'] is set. Delete the standalone '📊 24s hacim' line (current :1831-1832).
3) Line 3 is new and built from existing content: f"fiyat {px(e['px'])} {arrow} <b>{chg:+.2f}%</b>" (moved from line 2). If brief is set and verdict is not 'belirsiz', append f" · {_verdict_icon(v)} <b>{v[:1].upper()+v[1:]}</b>" (esc'd). This is the verdict half of what_happened's head, built the same way as format.py:1372.
4) Below that: wh = what_happened(brief). Append wh[1:], which is the 👥 flow line (or the empty-breakdown reason), then the top-address bullets, then the probe_err line. The 👥 line or the empty reason becomes line 4.
5) Below the flow and bullets, append the OI explanation that was removed from the top. If verdict is not 'belirsiz': f"<i>↳ {esc(why)}</i>", plus the existing widened note ' · <i>OI ölçümü {span//60}dk penceresinde</i>' when b.widened and b.span. If verdict is 'belirsiz': append the unchanged wh[0] ('❔ Belirsiz — …') here instead of putting anything on line 3. If brief is None, skip steps 4-5 entirely. That gives a 4-line message: header / size+24s / price / disclaimer.
6) The disclaimer stays as the last line, unchanged.
Optional helper so the head is not rebuilt by hand: add a keyword to what_happened(b, *, head=True). With head=False it skips out.append(head), and a sibling _oi_head(b) returns (short_verdict_html, why_html). Default behavior stays identical. Consistency note for the equityvol audit: equity_vol_alert (:1840) has the same structure and should get the same reorder, with its '🌙 ABD kapalı' line kept right after line 3. The current output and the proposed output were both rendered with the scratch scripts listed above to check line widths.

**Etkilenen testler:**
  - tests/test_volchart.py:119 — asserts '24 saatin en yüksek 5 dakikalık hacmi' in the caption. Still passes because line 1 is unchanged. No edit needed.
  - tests/test_volchart.py:120 — asserts 'PUMP' and '$1.2M' in the caption. Still passes. No edit needed.
  - No test asserts '📊 24s hacim', the price-on-line-2 placement, or what_happened strings for this formatter (checked with grep over tests/). The '24s hacim' assertions in tests/test_twaplive.py:350/693/698/854 belong to TWAP formatters and are unaffected.
  - Suggested new assertion in tests/test_volchart.py, near :119: the first 4 caption lines contain '24 saatin en yüksek', '$1.2M', '24s hacim', and the fiyat/🟢 line. Optionally add a pure-formatter case with a brief containing n_addr, asserting that line 4 starts with '👥' and that the OI 'why' text appears only after line 4.


---

## 23. cryptovol / equityvol (split-mode photo caption)

**Biçimlendirici / çağrı yeri:** short_caption at app/radar/cryptovol.py:91-95. Callers: app/radar/cryptovol.py:330 (kind cryptovol) and app/radar/equityvol.py:188-192 (kind cryptovol or equityvol, via route_for). It only goes out when send_rich takes the split path (app/notify.py:183-203): the full text is longer than CAPTION_MAX=1000 visible chars, which in practice means a long forensics brief. The full crypto_vol_alert / equity_vol_alert text goes first as its own message. Then the PNG is posted as a separate channel message with this one-line caption. Fan-out (_publish) carries the full text, not this caption. Rendered with tests/test_volchart.py raw_candles(300), giving rec notional=$1.2M, ratio=7.55, chg=+0.06%, px=0.00327. Scratch scripts are in /tmp/claude-0/-home-user-hlearninginsiders/49de93d8-1c8e-5cfb-b772-3a1b67b2789e/scratchpad/audit/ (cryptovol_short_caption.py, cryptovol_short_caption_proposed.py).

**Şu anki ilk 4 satır:**
```
📊 PUMP · 5dk hacim $1.2M · 24 saatin rekoru
(no line 2: the caption is a single line, 44 visible chars; the same for xyz:NVDA → '📊 NVDA · …' and for a -1.84% down bucket)
(no line 3)
(no line 4)
```

**Olması gerekenler (essentials):**
  - Coin / symbol (PUMP)
  - What happened: highest 5-minute volume of the last 24h (24 saatin rekoru)
  - Size: 5dk hacim $1.2M
  - Size vs normal: önceki rekorun 7.6×'i (rec['ratio'], already shown on line 2 of the companion text)
  - Direction and size of the price move in that 5-minute bucket: fiyat 0.0033 🟢 +0.06% (rec['px'] and rec['chg_pct'], already shown on line 2 of the companion text)
  - Timing is implicit: the last closed 5-minute bucket, sent right away. The full text has no explicit time either, so nothing more is needed
  - Who: not applicable to the caption. The forensics brief lives in the full text above, and the caption need not repeat it

**Sorunlar:**
  - The caption is a stand-alone channel post (the photo is sent after the text). It leaves out 'how big vs normal': the ratio to the previous 24h maximum (önceki rekorun N×'i) appears nowhere in it. A $1.2M record at 1.1× and one at 7.6× look identical.
  - It leaves out the price move of the bucket (🟢/🔴 chg%). A -1.84% drop and a +0.06% rise produce byte-identical captions ('📊 ETH · 5dk hacim $1.2M · 24 saatin rekoru'), so the caption hides the only direction information the alert has.
  - Both missing values are already in the rec dict passed to short_caption and already printed on line 2 of crypto_vol_alert / equity_vol_alert (app/telegram/format.py:1826-1830, 1846-1850). Adding them mirrors existing content: no new data and no new I/O.
  - Minor, optional: the emoji is 📊 for both markets, while the full text uses 🚀 (crypto) and 📈 (equity). short_caption does not know the market, and fixing this would need new routing logic, so leave it as is.
  - No test pins the caption's content: test_volchart.py:155-157 passes its own literal 'kısa altyazı', and test_wording does not scan it. A regression would go unnoticed.

**Önerilen ilk 4 satır:**
```
📊 <b>PUMP</b> · 5dk hacim <b>$1.2M</b> · 24 saatin rekoru
önceki rekorun <b>7.6×</b>'i · fiyat 0.0033 🟢 <b>+0.06%</b>
(no line 3: 91 visible chars in total; a phone shows two short lines)
(no line 4)
```

**Değişiklik planı:**

In app/radar/cryptovol.py:91-95 (short_caption), keep line 1 exactly as it is now and append a second line, using the same wording and format as line 2 of crypto_vol_alert (format.py:1827-1830):
  1) Import px alongside esc and usd from ..telegram.format.
  2) Compute chg = rec.get('chg_pct') or 0; arrow = '🟢' if chg > 0 else ('🔴' if chg < 0 else '⚪'); ratio = rec.get('ratio').
  3) l2 = (f"önceki rekorun <b>{ratio:.1f}×</b>'i · " if ratio else "") + f"fiyat {px(rec.get('px'))} {arrow} <b>{chg:+.2f}%</b>". The ratio guard is needed because find_record returns ratio=None when prev_max is 0.
  4) Return line1 + '\n' + l2.
This change only merges content: both values come from the same rec and are already shown in the companion message, with no new data and no new I/O. Wording stays measured; the up/down arrow describes only the bucket's past price change. Rendered output: 'önceki rekorun 7.6×'i · fiyat 0.0033 🟢 +0.06%' and 'önceki rekorun 3.2×'i · fiyat 181.42 🔴 -1.84%'; when ratio is None the line is 'fiyat 4012 ⚪ +0.00%'. The total stays at or under about 91 visible chars, far below the 1024-char caption limit. The fix covers equityvol automatically, because equityvol.py:188 imports this same function. No caller changes are needed. Optionally add a pin test in tests/test_volchart.py (for example in test_parse or a new test): cap = cv.short_caption('PUMP', rec); assert '$1.2M' in cap and '×' in cap and '+0.06%' in cap and cap.count('\n') == 1.

**Etkilenen testler:**
  - None break. No existing assertion checks short_caption's output.
  - tests/test_volchart.py:155-157 (send_rich split path) passes its own literal short_caption='kısa altyazı' and asserts bot.photos[-1][1] == 'kısa altyazı', so it is unaffected.
  - tests/test_volchart.py:161-162 passes short_caption='x', so it is unaffected.
  - tests/test_fanout.py:109 passes short_caption='c', so it is unaffected.
  - tests/test_volchart.py:118-119 (scan combined path) asserts on the full text caption ('24 saatin en yüksek 5 dakikalık hacmi', 'PUMP', '$1.2M'), not on short_caption, so it is unaffected.
  - Recommended new pin, optional: tests/test_volchart.py test_parse (around line 74), asserting that short_caption contains the ratio '×' and the signed chg '%' and has exactly 2 lines.


---

## 24. equityvol

**Biçimlendirici / çağrı yeri:** equity_vol_alert (+ what_happened) — app/telegram/format.py:1840 (what_happened at :1360); caller app/radar/equityvol.py:186, send_rich at :191 (photo plus full caption; fan-out text is the same with FOOT added at the end)

**Şu anki ilk 4 satır:**
```
📈 SHEIN — 24 saatin en yüksek 5 dakikalık hacmi
5dk hacim $2.8M · önceki rekorun 3.5×'i · fiyat 15.40 🔴 -12.50%
📊 24s hacim $41.3M
🌙 ABD kapalı — dayanak hisseyle arbitraj yapılamıyor, bu hacim saf perp akışı
```

**Olması gerekenler (essentials):**
  - What happened: the highest 5-minute volume of the last 24h on a stock perp, coin plus stock badge (📈 SHEIN)
  - Size: the 5-minute notional ($2.8M)
  - Compared with normal: × the previous 24h record bucket (3.5×), with 24h volume as context ($41.3M)
  - Price in that bucket: price and % change with 🟢/🔴 (15.40, -12.50%)
  - What kind of flow, i.e. the side: the OI reading verdict with its measurement (🩸 LONG kapanmış / likide olmuş, OI -4.10%)
  - Who: 👥 address count, buy/sell, net, taker %, or why there is no breakdown
  - When / how long: the last closed 5-minute bucket, sent within one scan cycle (5dk; 'son 5dk' on the 👥 line)
  - Current state: US market closed, so the volume is pure perp flow with no arbitrage (when it applies)

**Sorunlar:**
  - US closed (the most common state, roughly 80% of hours; equity perps trade 24/7): the full 🌙 explanation takes line 4. The OI verdict (what kind of flow) drops to line 5 and the 👥 who/flow line to line 6, so neither side nor who is in the first 4 lines.
  - US open: line 4 is the verdict line. what_happened builds it as icon + verdict + ' — ' + why + ' · OI ölçümü 10dk penceresinde', about 125 characters, roughly 3 wrapped lines on a phone. The 👥 who line is line 5, outside the first 4.
  - Line 3 '📊 24s hacim $41.3M' uses a whole top line for one context number. It can be merged into the size line.
  - The verdict's 'why' repeats the price change already on line 2 ('fiyat %-12.50'), so the top area spends space on duplicate numbers while who is pushed out.
  - 'ABD kapalı' is the essential state, but it sits at the top as a full explanatory sentence. A short badge on the headline would do, with the explanation lower down.
  - Minor, no fix needed: there is no clock time for the bucket. 'When' reads as 'just now' from 5dk / 'son 5dk', which is fine because the alert goes out within one 300-second scan. Adding bucket_ts would be new content, so it is out of scope.
  - Without a brief (forensics off or failed) the message is already only 3-4 lines and fine. The problem only appears when a brief exists, which is the normal case.

**Önerilen ilk 4 satır:**
```
📈 SHEIN — 24 saatin en yüksek 5 dakikalık hacmi · 🌙 ABD kapalı
5dk hacim $2.8M · önceki rekorun 3.5×'i · 24s hacim $41.3M
fiyat 15.40 🔴 -12.50% · 🩸 LONG kapanmış / likide olmuş (OI %-4.10, 10dk)
👥 son 5dk · 7 adres · alım $410K / satım $1.9M · net −$1.5M · taker %78
```

**Değişiklik planı:**

Only the order and merging of existing content change. No new data, no new I/O, the caller is unchanged and the wording stays measured. I mocked the layout in /tmp/claude-0/-home-user-hlearninginsiders/49de93d8-1c8e-5cfb-b772-3a1b67b2789e/scratchpad/audit/equityvol_proposal.py; the current output was rendered by equityvol_render.py in the same folder. Neither file is in the repo.

A) what_happened (app/telegram/format.py:1360): add a keyword-only flag `compact: bool = False`. With compact=False the output is byte-identical to today, so the other callers stay as they are: the whale/large-trade alert (:377), crypto_vol_alert (:1834) and offhours_move (:1942). With compact=True:
  (1) out[0] is just `{icon} <b>{Verdict}</b>`. When b['oi_chg'] is not None it gets the suffix ` (OI %{oi_chg:+.2f}` + (`, {span//60 or 1}dk` if widened and span) + `)`. That number is already printed inside why; the window tag carries the existing 'OI ölçümü Ndk penceresinde' caveat in short form, so the top line is not sharper than the data.
  (2) The ' — why' and the widened note are not appended to the head.
  (3) After the 👥 flow line, the top-address bullets or the empty-reason line, and before probe_err, append one detail line: `{icon} {esc(why)}` + (` · <i>OI ölçümü Ndk penceresinde</i>` if widened and span).

B) equity_vol_alert (format.py:1840):
  - Compute `closed` first with the same try/except around hourstats.us_closed().
  - Call `wh = what_happened(e.get('brief'), compact=True)`.
  - L1 = the existing headline + (` · 🌙 <b>ABD kapalı</b>` if closed).
  - L2 = `5dk hacim <b>$X</b>` + the existing ratio part + (` · 24s hacim {usd(day_vol)}` if day_vol). This merges old line 3 and drops its 📊 line.
  - L3 = `fiyat {px} {arrow} <b>{chg:+.2f}%</b>` + (` · {wh[0]}` if wh). The price tail moves here from old L2 and is merged with the verdict head.
  - Then `lines += wh[1:]`: L4 becomes the 👥 flow line (or the empty-reason line), followed by the address bullets, the verdict why/window detail line and probe_err.
  - Then, if closed, append the unchanged '🌙 <b>ABD kapalı</b> — dayanak hisseyle arbitraj yapılamıyor, bu hacim saf perp akışı' line, moved from old L4 to below the brief.
  - The disclaimer stays last. Update the docstring with the 14.10 rule.
  - Without a brief: L1-L3, then 🌙 (if closed), then the disclaimer, which still fits in 4 lines.

C) Visible length grows by about 15 characters (585 to about 600; 663 to about 680 when closed), still well under CAPTION_MAX 1000, so the combined photo mode stays the same. Fan-out (fanout.deliver adds FOOT at the end) shows the same first 4 lines in DMs. No changes are needed in app/radar/equityvol.py, short_caption or send_rich.

D) Consistency: crypto_vol_alert (format.py:1820) is the twin of this formatter. The equityvol scan also sends para: crypto-dex coins through it (route_for), so it should get the same layout (compact=True, 24s merged into L2, price + verdict on L3) in its own audit; otherwise the two volume channels look different.

E) Optional: add a test that renders equity_vol_alert with a brief and us_closed patched both ways and checks the first 4 lines: L1 has the sym (and 'ABD kapalı' when closed), L2 has '×' and '24s hacim', L3 has '%' and the verdict, L4 starts with '👥'. It should also check that what_happened(b) without compact is unchanged.

**Etkilenen testler:**
  - No existing test checks equity_vol_alert or what_happened text, so nothing has to change.
  - tests/test_wording.py: source scan only; none of the new or moved strings match a FORBIDDEN pattern, so no update.
  - tests/test_volchart.py:119-120: crypto caption checks for '24 saatin en yüksek 5 dakikalık hacmi', 'PUMP' and '$1.2M'; the headline wording is kept, so it is unaffected (it only matters once crypto_vol_alert gets the twin change).
  - tests/test_fanout.py:162: passes the literal text '📈 TSLA' and does not render, so it is unaffected.
  - New test suggested (not existing): check the first 4 lines of equity_vol_alert with us_closed true and false, and that what_happened without compact is unchanged.


---

## 25. offhours

**Biçimlendirici / çağrı yeri:** offhours_move (app/telegram/format.py:1901, wrapped by app/radar/offhours.py:398 fmt_move; callers offhours.py:266 dev band -> send :267, offhours.py:296 spike -> send :298; same text goes to owner chat and fan-out via Notifier._publish)

**Şu anki ilk 4 satır:**
```
🟢📈 SNDK — kapanıştan beri %2.10 yukarıda
kapanış 100.00 → 102.10 │ 40hdır kapalı
🕰 ABD açılışına 26s 15dk (Thu 21:02 TSİ)
🗓 Hafta sonu penceresi: 9s 20dk kaldı
[render: weekend dev band with a brief. L5 = '📊 Açık pozisyon (OI) kapanıştan beri %+1.0', L6 = '🔥 SHORT kapanmış (squeeze) — açık pozisyon azaldı, fiyat yükseldi (OI %-1.20, fiyat %+0.90)', L7 = '👥 son 60dk · 7 adres · alım $1.8M / satım $620K · net +$1.2M · taker %71', L8-9 = address bullets, L10 = italic disclaimer, L11 = PROPR]
[weekday spike render: L1 '🔴📉 MU — 10 dakikada %1.60 düştü' · L2 '100.00 → 98.40 │ 5hdır kapalı' · L3 '🕰 ABD açılışına 11s 20dk (Thu 06:07 TSİ)' · L4 '📊 Açık pozisyon (OI) kapanıştan beri %+0.4'. Then L5 is the italic threshold note 'Hafta içi kapalı pencere — eşik %1.5 (hafta sonu daha düşük).' and L6 is the verdict '🩸 LONG kapanmış / likide olmuş ...']
[test_wording.py:154 context (no brief, no countdown): L1 head · L2 'kapanış 470.00 → 480.00 │ 2hdır kapalı' · L3 '📊 Açık pozisyon (OI) kapanıştan beri %-3.0' · L4 disclaimer · L5 PROPR]
```

**Olması gerekenler (essentials):**
  - What happened and its type: a cumulative move since the US close (dev band) or a short-window spike (N minutes)
  - Coin and direction: symbol, up or down
  - Size: % move, plus the threshold it crossed (spike: spike_thr is in m; dev band: band_pct is NOT passed in m, so it cannot be shown without new data)
  - Current state: reference price (close or N minutes ago) → current price
  - When / how long: how long the US session has been closed, and the countdown to the US open (code comment requires it in every message)
  - Positioning over the move window: OI change since close
  - Who / what kind of flow: the what_happened OI verdict (new LONG, SHORT squeeze, ...) plus the 👥 address flow (window, address count, buy/sell, net)

**Sorunlar:**
  - The 'who' block (what_happened: OI verdict line and 👥 flow line) shows up at lines 6-7 in both realistic renders (weekend dev and weekday spike), so it is outside the first 4. Three things push it down: the 🗓 weekend-window line, the separate 📊 OI line, and the weekday threshold note. All of them come before what_happened (format.py:1930-1943).
  - '🗓 Hafta sonu penceresi: Xs Ydk kaldı' takes line 4 on weekends. It says when the bot's alert window ends, not anything about the market, so it should go below.
  - OI-since-close sits on its own line ('📊 Açık pozisyon (OI) kapanıştan beri ...', format.py:1932-1933). It uses the same close anchor as line 2, so it can be merged into line 2.
  - Weekday spike: the threshold (how big compared with the trigger) appears only inside an italic rule note on line 5 (format.py:1936-1939). The number belongs in line 1 and the explanation below. Weekend spikes never show spike_thr even though it is in m. Dev band alerts show no band threshold at all, and band_pct is not in m (offhours.py:240-244 base dict), so this cannot be fixed without new data.
  - Two OI windows can look like a contradiction. One line says OI since close (+1.0). The verdict line says '(OI %-1.20, fiyat %+0.90)' for the brief window (60 min for dev, DEV_BRIEF_SEC=3600; window_min for spike). That window is named only on the next 👥 line, and not at all when there is no address breakdown (n_addr=0 → 'Adres kırılımı yok'). The reorder puts these lines next to each other, so the gap is more visible. This is an existing issue in shared what_happened.
  - The countdown uses strftime %a, which gives an English weekday ('Thu 21:02 TSİ') because the server locale is en_US, inside a Turkish message (format.py:1928). A Turkish table already exists at app/radar/seans.py:73 GUN_TR.
  - Minor: age_str gives an English 'h', so line 2 reads '40hdır kapalı', while the next line uses 's' for saat ('26s 15dk'). Units are inconsistent on adjacent lines.

**Önerilen ilk 4 satır:**
```
🟢📈 SNDK — kapanıştan beri %2.10 yukarıda   (spike, weekday: 🔴📉 MU — 10 dakikada %1.60 düştü (eşik %1.5))
kapanış 100.00 → 102.10 │ 40hdır kapalı · OI kapanıştan beri %+1.0   (spike: 100.00 → 98.40 │ 5hdır kapalı · OI kapanıştan beri %+0.4)
🕰 ABD açılışına 26s 15dk (Thu 21:04 TSİ)
🔥 SHORT kapanmış (squeeze) — açık pozisyon azaldı, fiyat yükseldi (OI %-1.20, fiyat %+0.90)
[then L5 👥 son 60dk · 7 adres · alım/satım · net · taker, L6-7 address bullets, L8 🗓 Hafta sonu penceresi, (weekday spike: italic threshold note), italic disclaimer, ✅ PROPR'da listeli. With no brief: L4 is the 🗓 line, or for a weekday spike the threshold note. Rendered and checked with scratchpad/audit/offhours_proposed.py; longest top line is the verdict at 91-92 chars, which comes from shared what_happened]
```

**Değişiklik planı:**

Edit only app/telegram/format.py offhours_move (lines 1901-1950). No new data and no new I/O.
1) Before building head, compute once: wk_thr = m["kind"] == "spike" and not m.get("weekend", True) and m.get("spike_thr"). In the spike head (1912-1913), when wk_thr, append f" (eşik %{m['spike_thr']:g})". This moves the threshold number from the italic note to line 1.
2) Line 1919: line 2 becomes detail + f" │ {age_str(...)}dır kapalı" + (f" · OI kapanıştan beri %{m['oi_chg']:+.1f}" if m.get("oi_chg") is not None else ""). Delete the separate '📊 Açık pozisyon (OI) ...' append (1932-1933).
3) Keep the 🕰 countdown block (1925-1929) right after line 2, so it is line 3.
4) Move `lines += what_happened(m.get("brief"))` (1943) up to directly after the countdown block. The verdict becomes line 4, and the 👥 flow and bullets follow it.
5) Move the 🗓 weekend_left block (1930-1931) to below what_happened.
6) Move the weekday threshold italic note (1936-1939, gated by wk_thr) to below the 🗓 line. Its text can stay unchanged.
7) The disclaimer (1944-1946) and PROPR_NOTE (1947-1948) stay last. Keep the existing comments with their blocks; the 1940-1942 comment about the window of what_happened still applies.
Optional, beyond a pure reorder:
(a) Countdown weekday in Turkish: replace %a with a GUN_TR-style tuple indexed by when.weekday().
(b) Make the brief window explicit on the verdict line, since it now sits under 'OI kapanıştan beri'. For example, in offhours_move: wh = what_happened(brief); if wh and brief.get('minutes'): wh[0] = f"son {brief['minutes']}dk: " + wh[0]. This uses existing data and only touches the local copy.
(c) Use a consistent hour unit in '40hdır kapalı'.
(d) Show '(eşik %X)' on weekend spikes too; spike_thr is already in m.

**Etkilenen testler:**
  - tests/test_wording.py:156 — `"OI) kapanıştan beri %-3.0" in off` will FAIL. The text becomes 'OI kapanıştan beri %-3.0' on line 2. Change it to `"OI kapanıştan beri %-3.0" in off.split("\n")[1]`. Keep the `"kuruluyor" not in off` part.
  - tests/test_offhours_alert.py:144 — `"2.10" in bot.sent[0] or "2.1" in bot.sent[0]` is unaffected; the head is unchanged.
  - tests/test_offhours_alert.py:235 — `"MU" in bot.sent[0]` is unaffected.
  - tests/test_offhours_alert.py:219-232 — the fmt_move stub/boom wrapper is unaffected; the fmt_move(m) signature does not change.
  - Suggested new pins (tests/test_wording.py near line 156): a weekday spike with spike_thr=1.5, weekend=False gives a first line ending in '(eşik %1.5)'. With a brief, the verdict line is among the first 4 lines, and '🗓 Hafta sonu penceresi' comes after it.


---

## 26. lowvol

**Biçimlendirici / çağrı yeri:** lowvol_alert @ app/telegram/format.py:1951 (caller app/radar/lowvol.py:108, send :110 → owner chat + fan-out; fan-out appends FOOT "🔕 türleri/coinleri değiştir: /bildirimler" in app/telegram/fanout.py:85)

**Şu anki ilk 4 satır:**
```
🐘 SESSİZ SUDA DEV POZİSYON — AAPL
Düşük hacimli hissede absürt boyutlu YENİ pozisyon:
🟢LONG $3.4M @ 41.27 · açılış 47dk önce
👤 0x8f3a..7f80
```

**Olması gerekenler (essentials):**
  - What happened: a new, very large position on a quiet (low-volume or OI-dominated) stock — the 'sessiz su devi' alert
  - Which coin, and the side (LONG/SHORT) with its size ($ notional)
  - Size compared with normal, the reason the alert fired: OI share %, daily volume, position ≈ N× daily volume
  - When: how long ago it was opened ('YENİ', açılış X önce), plus the entry price
  - Who: the address, plus its suspicion score and track record (sicil tuttu/tutmadı) when present
  - Below the top lines: the rule note (low volume, very large size), then the PROPR listed line

**Sorunlar:**
  - The 📊 line (OI payı %46 · günlük hacim $1.9M · poz ≈ hacmin 1.8 katı) is on line 5. It is the only measurement that says why the alert fired, and it is not in the first 4 lines.
  - Line 2 ('Düşük hacimli hissede absürt boyutlu YENİ pozisyon:') is a generic rule sentence with no data, but it takes one of the top 4 slots.
  - Line 2 claims 'Düşük hacimli hissede' in every case. That is not always true: dominants() (app/radar/lowvol.py:62-66) admits a row when low_volume OR oi_share >= lowvol_min_oi_share. A position that holds ≥20% of OI on a stock trading ≥$5M/day passes the $2.5M alert floor and still gets the low-volume label. p['low_volume'] is already in the dict.
  - Line 1 (the notification banner/preview) has the coin but no side or size. LONG/SHORT $3.4M only appears on line 3, and 'YENİ' is cut off from it on line 2.
  - The who-context is spread out: the address is on line 4, but 'Şüphe skoru' and 'Sicil' are on lines 6-7, outside the top 4. The 🎯 emoji is used twice.
  - Minor: the formatter has no DISCLAIMER line (owner chat gets none; fan-out only gets FOOT). It is out of scope for a reorder-only change, but if one is ever added it goes at the bottom.

**Önerilen ilk 4 satır:**
```
🐘 SESSİZ SU DEVİ · AAPL · 🟢LONG $3.4M
📊 OI payı %46 · günlük hacim $1.9M · poz ≈ hacmin 1.8 katı
🆕 YENİ · açılış 47dk önce · @ 41.27
👤 0x8f3a..7f80 · şüphe skoru 🚨72 · sicil 3 tuttu / 1 tutmadı
```

**Değişiklik planı:**

Only lowvol_alert in app/telegram/format.py:1951-1976 changes. No new data and no new I/O: every value already comes from p, and the title 'Sessiz su devi' is the existing label in app/notify.py:28/74. Order follows the TWAP redo (e49e472): what, then size vs volume, then when, then who.
1) Line 1 (merge of old L1 and the side/size part of old L3): f"🐘 <b>SESSİZ SU DEVİ</b> · <b>{sym}</b> · {_side_badge(p['side'])} <b>{usd(p['notional'])}</b>". This replaces old L1 'SESSİZ SUDA DEV POZİSYON — {sym}'.
2) Line 2 (old L5 moved up, unchanged): build the facts list (OI payı / günlük hacim / poz ≈ hacmin N katı) and append "📊 " + " · ".join(facts) right after line 1, only if facts is non-empty. If it is empty, the next lines move up and the message still fits in 4 lines.
3) Line 3 (merge of old L2's 'YENİ' with the time and price from old L3): f"🆕 YENİ · açılış {age_str(p.get('opened_ts'))} önce · @ {px(p.get('entry_px'))}".
4) Line 4 (merge of old L4, L6 and L7): who = [f"👤 {alink(p['address'])}"]. If p.get('score'), append f"şüphe skoru {score_badge(p['score'])}". If hits or misses, append f"sicil {hits} tuttu / {misses} tutmadı". Then lines.append(" · ".join(who)). This drops the duplicate 🎯 lines 6-7.
5) Below line 4: the old L2 rule sentence becomes an italic note. Make it conditional on the existing field: "<i>Düşük hacimli hissede absürt boyutlu yeni pozisyon.</i>" if p.get('low_volume', True), else "<i>Hissenin açık pozisyonunda (OI) absürt pay tutan yeni pozisyon.</i>". This fixes the false low-volume claim for OI-dominant entries. If strict reorder-only is required, keep the single original sentence and just move it here.
6) PROPR_NOTE stays last, unchanged.
Checked by rendering in scratch/audit/lowvol_proposed.py. Line lengths are 37/58/35/60 visible characters. test_wording.hits() finds nothing on either variant. Update the docstring to cite the 14.10 '3-4 satır' rule, as was done for twap_alert.

**Etkilenen testler:**
  - No test checks lowvol_alert text: no test calls lowvol_alert or check_alerts for lowvol, and no test asserts 'SESSİZ SUDA', 'absürt boyutlu' or 'Şüphe skoru'.
  - tests/test_wording.py:110 test_source_strings_measured: this scans the new string literals in app/telegram/format.py. I checked the proposed strings ('SESSİZ SU DEVİ', 'şüphe skoru', 'sicil … tuttu / … tutmadı', the OI note) against FORBIDDEN: 0 hits. No change needed.
  - tests/test_fanout.py:308-310: this only checks that 'coin=' appears in app/radar/lowvol.py. lowvol.py does not change, so it is unaffected.
  - Optional: add a new assertion for the first 4 lines in a new or existing test, e.g. lines[0] has the side and size, lines[1] starts with '📊', lines[3] starts with '👤'. Also check that the note does not say 'Düşük hacimli' when low_volume is False.


---

## 27. track

**Biçimlendirici / çağrı yeri:** track_offer

**Şu anki ilk 4 satır:**
```
👣 SNDK earnings geçti — balina çıkışını takip edelim mi?
Komuta bas, takip başlasın. Balina toplam pozunun %10 kadarını her kapattığında haber veririm (her işlemi değil — spam yok). Tam kapanış + yön değişimi ANINDA gelir.

1. 🔴SHORT 0x1234..5678 — şu an $3.2M (rapordaki: $2.4M) · skor 87
```

**Olması gerekenler (essentials):**
  - What: the SNDK earnings report is out, and the bot offers to track the whale's exit (header)
  - Who and which side: one line per open whale with address, LONG/SHORT badge and rank (skor)
  - How big: the current live size, plus the pre-earnings snapshot size ('rapordaki') when the two differ by more than 10%
  - Action: the tappable /takip_N for each whale, on the same line as that whale
  - Current state: whales that have already closed (🚪 ... zaten kapatmış, with side and size before)
  - Details (below the top lines): the rule (notice at each 10% step, full close or side flip notified at once) and the footer (duration, /takipler)

**Sorunlar:**
  - Line 2 is a 165-character rule paragraph that wraps to about 4-5 lines on a phone, and line 3 is blank. The first whale only shows up on line 4, so the top 3-4 lines (and the phone preview) carry no side, address, size or command.
  - Each offer takes 2 lines, because '   → /takip_N' sits on its own line (format.py:2129). With OFFER_LIMIT=4 (tracker.py:29) the list runs over lines 4-11, and even the first whale's command is on line 5. Other formatters put the command inline at the end of the row (format.py:1518, 1570, 1593).
  - The whales that already closed are part of the current state, but they come after an extra blank line near the bottom (line 9 in the example), below every offer.
  - Closed-only case (offers empty): the header still asks 'takip edelim mi?' though there is nothing to tap. Lines 3-4 are two blank lines in a row (the header block ends with "", and format.py:2131 appends another), so the first 4 lines are just the header and the rule paragraph. The '⏳ Takip N gün sürer' footer also appears even though no tracking can start.
  - Accuracy, outside the reorder scope: the footer says '⏳ Takip 14 gün sürer', but with the default track_auto_stop=0 (config.py:759) tracking does not end when that time runs out. It only extends and sends a check-in message (tracker.py:390-411). The config text for track_expire_days says the same thing: 'Takip bu süreyle BİTMEZ'.
  - Minor and outside scope: the 'rapordaki' size is really the pre-earnings T-1h/pre snapshot (tracker.py:168-176), not a number from the report. The label is ambiguous.

**Önerilen ilk 4 satır:**
```
👣 SNDK earnings geçti — balina çıkışını takip edelim mi?
1. 🔴SHORT 0x1234..5678 — şu an $3.2M (rapordaki: $2.4M) · skor 87 → /takip_41
2. 🟢LONG 0xabcd..9999 — şu an $880K · skor 62 → /takip_42
🚪 0x5555..0000 zaten kapatmış (🟢LONG $1.1M idi)
```

**Değişiklik planı:**

All changes are in app/telegram/format.py, in track_offer (lines 2113-2136). Only order and merges change; the wording stays the same.
(a) Line 2115: the initial `lines` list keeps only the header f"👣 <b>{symbol} earnings geçti — balina çıkışını takip edelim mi?</b>". Move the 'Komuta bas, takip başlasın. ... ANINDA gelir.' sentence (2116-2118) and its trailing "" out of the header block.
(b) Lines 2128-2129: merge the command into the whale line, so it reads lines.append(f"{i}. {_side_badge(o['side'])} {alink(o['address'])} — {size_txt}{score} → /takip_{o['offer_id']}"). Delete the separate '   → /takip_N' line. This matches the inline ' → /takip_N' used at format.py:1518, 1570 and 1593.
(c) Lines 2130-2134: remove the `lines.append("")` separator inside `if already_closed:` so the 🚪 lines follow the offers directly.
(d) After the offers and closed lines: `if offers:` append "" and then the unchanged rule sentence from (a). It can optionally be split into two lines before 'Tam kapanış + yön değişimi ANINDA gelir.' for phone width.
(e) Keep the '⏳ Takip ... · /takipler ile yönet' footer last. Optionally emit it only when offers is non-empty.
Result: the header plus up to 3 whales (or 2 whales plus the closed note) fill lines 1-4, each with side, address, current and pre-earnings size, skor and its /takip_N.
Optional wording changes beyond reordering, flagged for the owner:
(f) When offers is empty, end the header with the existing phrase 'balina zaten kapatmış' instead of the question.
(g) Make the footer match track_auto_stop. When it is off, show something like '⏳ {N} günde bir yoklarım · /takipler ile yönet' instead of 'Takip N gün sürer'.
The caller (app/radar/tracker.py:221-222) does not change. The renders are in /tmp/claude-0/-home-user-hlearninginsiders/49de93d8-1c8e-5cfb-b772-3a1b67b2789e/scratchpad/audit/track_offer.py (current) and track_offer_proposed.py (proposed).

**Etkilenen testler:**
  - None: no test calls fmt.track_offer or checks its text. A grep of tests/ for track_offer( finds nothing. tests/test_tracker_liq.py:95-125 covers offer_positions plus crypto_liq_snapshot and crypto_liq_alert, not this formatter.
  - tests/test_wording.py:63 (the SOURCES scan of app/telegram/format.py): no change is expected, because the moved text adds no forbidden pattern. Optional change (f) only reuses 'zaten kapatmış'.
  - Suggested new assertion, in tests/test_tracker_liq.py or a new test: after the change, for fmt.track_offer('SNDK', offers, closed, cfg).split('\n'), check that '/takip_{id}' and the address prefix are both in lines[1], and that 'Komuta bas' is not in the first 4 lines.


---

## 28. track

**Biçimlendirici / çağrı yeri:** track_step (app/telegram/format.py:2174; caller app/radar/tracker.py:365). Rendered with the context from test d at tests/test_tracker_liq.py:169-175: a HYPE short going from 50k to 40k szi at mark 40, liq 40.8 to 42.0. Scratch scripts: /tmp/claude-0/-home-user-hlearninginsiders/49de93d8-1c8e-5cfb-b772-3a1b67b2789e/scratchpad/audit/track_step_render.py (current output) and track_step_proposed.py (proposed layout, run with --tests).

**Şu anki ilk 4 satır:**
```
✂️ BALİNA KAPATIYOR — HYPE
👤 0xaaaa..aaaa 🔴SHORT
Bu adımda toplamın %20 kadarını kapattı · başlangıçtan beri %20 kapandı   [71 chars; add path: '… %20 kadarını EKLEDİ · poz başlangıca göre %120 seviyesinde' = 77 chars]
Güncel: $1.6M (başlangıç $2.0M)
(line 5: liq 40.80 → 42.00 · line 6: ✅ PROPR'da listeli)
```

**Olması gerekenler (essentials):**
  - What happened: the tracked whale reduced (✂️ KAPATIYOR) or added (➕ EKLİYOR) at least one step (track_step_pct) of its base size
  - Which coin and side: symbol plus 🔴SHORT/🟢LONG
  - Size vs threshold: this step as % of the base position (e.g. %20 against the %10 step rule)
  - Cumulative state: % closed since tracking began, or % of base if it grew, with current notional vs starting notional ($1.6M vs $2.0M)
  - Who: the address link
  - Current risk state: liq price and how it moved (prev → cur). Test d pins that the step message carries the liq line ('adım mesajı liq'li')
  - When: the message is sent in real time, so 'now' is implicit. No elapsed-time or #id field exists in this message (siblings track_liq_move, track_flip and track_closed show #id)

**Sorunlar:**
  - The liq line (current risk state, and one of the fields the test pins) is on line 5, outside the first 4.
  - Line 2 uses a whole line for just the address and side. The headline 'BALİNA KAPATIYOR — HYPE' has no direction, so someone scanning sees the coin but not whether the whale is short or long.
  - Line 3 is 71 chars (77 on the add path), so it wraps to two lines on a phone. On a phone, 'Güncel' already falls to visual line 5 and liq to line 6 or 7.
  - The cumulative change is said twice: '%20 kapandı' on line 3 and '$1.6M (başlangıç $2.0M)' on line 4. They can be one line.
  - The add-after-partial-close case reads awkwardly: '… kadarını EKLEDİ · başlangıçtan beri %20 kapandı' sits on one line, with the dollar state on the next.
  - Minor and optional (not reorder-only): unlike its sibling track_* messages, the message has no tracker (#id). Elapsed time since start (t['created_ts']) is not shown either; that would be new content, so it is out of scope.
  - Already fine: the wording is measured, PROPR_NOTE is last, there is no disclaimer or rule text in the body, and the 🛑/⏰ buttons go through reply_markup and are not part of the text.

**Önerilen ilk 4 satır:**
```
✂️ <b>BALİNA KAPATIYOR</b> — HYPE 🔴SHORT   (33 chars; add path: ➕ <b>BALİNA EKLİYOR</b> — HYPE 🔴SHORT)
Bu adımda toplamın <b>%20</b> kadarını kapattı   (39 chars; add path: … kadarını EKLEDİ)
Güncel: <b>$1.6M</b> (başlangıç $2.0M · <b>%20</b> kapandı)   (45 chars; above-base add: (başlangıç $2.0M · <b>%120</b> seviyesinde))
👤 0xaaaa..aaaa · liq 40.80 → <b>42.00</b>   (34 chars; unchanged liq: '· liq <b>40.80</b>'; no live liq: just '👤 0xaaaa..aaaa') — then line 5: ✅ PROPR'da listeli (unchanged, still last)
```

**Değişiklik planı:**

This only reorders and merges what is already there. No new data and no new I/O. Same order as the TWAP redo in e49e472: what → how much → state → who/position.
1) format.py:2179 and 2182: add ` {_side_badge(t['side'])}` to the end of `head`, so the side moves from line 2 into the headline.
2) format.py:2184-2187: shorten total_txt, because the 'başlangıç $X' in the same parenthesis now gives the context. 'başlangıçtan beri <b>%{closed_total:.0f}</b> kapandı' becomes '<b>%{closed_total:.0f}</b> kapandı'. 'poz başlangıca göre <b>%{100-closed_total:.0f}</b> seviyesinde' becomes '<b>%{100-closed_total:.0f}</b> seviyesinde'.
3) format.py:2188-2192: build lines as [head, act, f"Güncel: <b>{usd(live['notional'])}</b> (başlangıç {usd(t.get('base_notional'))} · {total_txt})", who]. `act` now stands alone on line 2, without the '· {total_txt}' suffix.
4) format.py:2189 and 2193-2196: `who = f"👤 {alink(t['address'])}"`. When live['liq_px'] is set, add ' · ' plus the existing liq text, exactly as it is today: 'liq {px(prev)} → <b>{px(cur)}</b>', or 'liq <b>{px(cur)}</b>' when prev is missing or unchanged. The old separate address line (2189) and the appended liq line (2195) both go away. `who` becomes line 4.
5) format.py:2197-2198: PROPR_NOTE is unchanged and stays as line 5, the last line.
Optional, not reorder-only: add ' (#{t['id']})' to the head so it matches track_liq_move and track_flip.
Checked in scratch: I monkeypatched fmt.track_step with this layout and ran every test in tests/test_tracker_liq.py plus tests/test_trackbtn.py::test_tracker_messages_carry_button. All pass. Every line is 50 chars or less (the longest is the above-base add case, line 3 at 50).

**Etkilenen testler:**
  - tests/test_tracker_liq.py:175: no change needed. Its substrings 'BALİNA KAPATIYOR', '%20' and 'liq 40.80 → <b>42.00</b>' are all kept, and the test passes against the proposed layout. Recommended: add a layout assertion here so the order can't regress, e.g. `head4 = step.split('\n')[:4]; assert '🔴SHORT' in head4[0] and any('liq ' in l for l in head4)`
  - tests/test_trackbtn.py:245: no change needed. heads[1] (the first line) still contains 'KAPATIYOR' (passes with the proposed layout).


---

## 29. track

**Biçimlendirici / çağrı yeri:** track_closed (app/telegram/format.py:2202; caller app/radar/tracker.py:331)

**Şu anki ilk 4 satır:**
```
💀🚪 BALİNA LİKİDE OLDU — HYPE
👤 0xaaaa..aaaa 🔴SHORT $2.0M pozisyonu likide oldu · gerçekleşen 42.01.
🩸 Yaklaşık sonuç: $81K zarar (-4.0%)
giriş 40.00 → çıkış ~41.62 (son fiyattan hesap)   [line 5 = 'Takip #1 sona erdi.', line 6 = '✅ PROPR'da listeli'; close variant is the same shape: '🚨🚪 BALİNA TAMAMEN KAPATTI — HYPE' / '👤 0xbbbb..bbbb 🟢LONG $400K pozisyonunu kapattı.' / '💰 Yaklaşık sonuç: $5K kâr (+1.2%)' / 'giriş 40.00 → çıkış ~40.50 (son fiyattan hesap)']
```

**Olması gerekenler (essentials):**
  - What happened: fully closed vs liquidated (or cause could not be verified), in the header on line 1
  - Coin/symbol, line 1
  - Who: whale address link, line 2
  - Side (LONG/SHORT), line 2
  - Size: tracking-start notional (base_notional), line 2
  - Liquidation fill price (liq case, 'gerçekleşen X'), line 2
  - Unverified-cause caveat ('likidasyon mu kapatma mı doğrulanamadı', unknown case), line 2
  - Result: approximate P&L in $ and % (when computable), line 3
  - Current state: tracking #N has ended, so no more alerts come for this position. Today this is on line 5 whenever P&L exists

**Sorunlar:**
  - When P&L exists, the state line 'Takip #N sona erdi.' (tracker id and 'tracking is over') lands on line 5, outside the first 4. In a channel with several trackers, the #N is what tells readers which tracker just ended.
  - Line 4 is taken by the calculation footnote '<i>giriş X → çıkış ~Y (son fiyattan hesap)</i>'. That is an explanation of how the number was computed, and the rule says it belongs below. The cause is in format.py:2219-2222: the footnote is glued to the P&L result inside one list element with an embedded '\n', so it cannot be moved without splitting that element.
  - Minor (phone width): in the unknown case, line 2 is 88 visible characters because the 'doğrulanamadı' caveat is appended to it, so it wraps to about 3 lines on a phone. Liq line 2 is 70 characters. The caveat is essential for measured wording, so it should stay in line 2. It is only flagged here.
  - Data mismatch, outside reorder scope: in the liq case, line 2 shows the real liquidation fill ('gerçekleşen 42.01') while the P&L and calc line use the latest mark ('çıkış ~41.62'), so two different exit prices appear in one message. Cause: tracker.py:323 calls estimate_pnl(t) BEFORE _closure_kind (tracker.py:329) returns cpx. A possible separate fix that adds no I/O (it removes one mark read) is to compute kind/cpx first and call estimate_pnl(t, cpx). The '(son fiyattan hesap)' wording would then need to depend on which price was used.
  - Data accuracy, outside reorder scope: size and P&L always use the tracking-start base (base_notional, base_szi), even if the whale had already trimmed. Example: test_tracker_liq step d cuts 50k to 40k, then a liquidation follows, yet the message says '$2.0M pozisyonu likide oldu' and computes P&L on 50k. This overstates both. The formatter receives `base` and `last` but never uses them.
  - No time or duration information: nothing says how long the position was tracked or held. t['created_ts'] is available but not rendered. This cannot be fixed by reordering alone, so it is only flagged here.

**Önerilen ilk 4 satır:**
```
💀🚪 BALİNA LİKİDE OLDU — HYPE
👤 0xaaaa..aaaa 🔴SHORT $2.0M pozisyonu likide oldu · gerçekleşen 42.01.
🩸 Yaklaşık sonuç: $81K zarar (-4.0%)
Takip #1 sona erdi.   [below: '<i>giriş 40.00 → çıkış ~41.62 (son fiyattan hesap)</i>', then '✅ PROPR'da listeli'. Close variant: header / '👤 … 🟢LONG $400K pozisyonunu kapattı.' / '💰 Yaklaşık sonuç: $5K kâr (+1.2%)' / 'Takip #2 sona erdi.'. With no P&L: header / who line / 'Takip #N sona erdi.' / PROPR]
```

**Değişiklik planı:**

This only reorders lines. No text, data or I/O changes. All edits are in app/telegram/format.py, function track_closed, lines 2216-2223. Lines 1-2 (the header and the who/side/size line, including the liq 'gerçekleşen' and unknown 'doğrulanamadı' tails) stay exactly as they are.

1) Split the P&L list element in two:
- Keep `lines.append(f"{emoji} Yaklaşık sonuç: <b>{usd(abs(pnl['usd']))}</b> {'kâr' if pnl['usd'] >= 0 else 'zarar'}{pct}")` as line 3.
- Assign the footnote to a local variable instead of appending it: `calc = f"<i>giriş {px(pnl['entry'])} → çıkış ~{px(pnl['exit'])} (son fiyattan hesap)</i>"`. Initialise `calc = None` before the `if pnl:` block.

2) Keep `lines.append(f"Takip #{t['id']} sona erdi.")` right after the P&L block. It becomes line 4 when P&L exists and line 3 when it does not.

3) Then add `if calc: lines.append(calc)` so the calculation footnote moves below the essentials.

4) The PROPR_NOTE append stays last, unchanged.

Resulting order: header, then who/side/size/how, then P&L result, then 'Takip #N sona erdi.', then the italic calc footnote, then PROPR. This matches the shape of track_flip, whose line 3 is 'Takip yeni yönle devam ediyor (#N).'.

Not in this plan (flag to the user separately, since each changes data, not layout):
- (a) In tracker.py:323-330, compute _closure_kind before estimate_pnl and pass cpx, so the liq P&L uses the real fill price.
- (b) Use the unused `last` argument so a partially trimmed position is not reported at full start size.
- (c) Optionally show tracking duration from created_ts.

Optional new test: in test_tracker_liq step e, assert `closed.split('\n')[3].startswith(f'Takip #{tid}')` when P&L is present. The existing fixture has entry_px=40 and a MAIN_CTX mark, so check first that P&L is actually rendered there.

**Etkilenen testler:**



---

## 30. digest

**Biçimlendirici / çağrı yeri:** digest @ app/telegram/format.py:2388 (caller app/main.py:258, sent at :259 via notifier.send("digest", priority="normal") to the owner chat only; pending items come from app/notify.py:206, events from upcoming_events(3), and top is the 5 highest-score positions with score >= 50)

**Şu anki ilk 4 satır:**
```
🌅 GÜNAYDIN — gece raporu

😴 Sessiz saatte biriken bildirimler:
  🐋 büyük işlem × 3
```

**Olması gerekenler (essentials):**
  - That this is the overnight (quiet-hour) report
  - How many alerts were held overnight in total, and of which types
  - Which coins the held alerts were about: the first line of the latest items (each alert's own line 1)
  - Current state: the most suspicious open position right now (coin, side, size, score, age)
  - The next upcoming earnings: symbol, time in TSİ, and the exact/uncertain flag ('~', '⚠️sabah olabilir')
  - If nothing was held: say plainly that the night was quiet

**Sorunlar:**
  - The first 4 visible lines are a header, a blank line, a section heading and one count row. The reader learns only 'büyük işlem × 3'. The total, the other alert types, every coin name, the current suspicious positions (line 12 and below in the render) and today's earnings (line 17 and below) all sit below the fold. A phone push preview shows only 'GÜNAYDIN — gece raporu / Sessiz saatte biriken bildirimler:'.
  - The counts take one line per alert type (format.py:2396-2398) and there is no total, so the top lines carry little information.
  - The heading 'En önemli 3'ü' (format.py:2399) is wrong. pending_digest_items sorts by ts ASC (notify.py:209-210) and the code takes pending[-3:], so these are the 3 MOST RECENT items, printed oldest first. No importance ranking exists, so the heading claims something that was not measured.
  - Earnings rows follow the DB order, which is date_et only (earnings/calendar.py:675), so the first row is not always the soonest event. The heading 'Bugün/yarın bilanço' (format.py:2413) is also inaccurate: upcoming_events(3) (main.py:249) covers up to 3 days ahead.
  - Dependency on other formatters: the item list prints each alert's line 1, and today those lines are bare headers. whale_fill_alert gives '🐋 BÜYÜK İŞLEM — NVDA' with no side or size (format.py:362). anomaly_alert gives '📡 ANOMALİ — COIN' with no trigger (format.py:296). The record-holder case gives '🎯 SİCİLLİ BALİNA AMD'E DÖNDÜ — AMD', with the symbol twice. The digest list only becomes informative once those formatters make line 1 self-contained.
  - Minor: the DIGEST_LABELS.get(k, k) fallback (format.py:2398) prints raw keys such as 'offhours', 'pattern', 'liqattack', 'twap', 'cryptovol' and 'listing'. These kinds get queued when quiet_allow_high=0. Human labels for them already exist in notify.KINDS[k][1].
  - The blank line right after the header ('\n' prefix at format.py:2395/2403) wastes one of the 4 top lines.

**Önerilen ilk 4 satır:**
```
🌅 <b>GÜNAYDIN — gece raporu</b>
😴 5 bildirim birikti: 🐋 büyük işlem ×3 · 📡 anomali ×2   (if nothing was held: '😴 Gece sessizdi — bekleyen bildirim yok.')
🚨 En şüpheli: [82] <b>NVDA</b> SHORT $4.2M · 9h
📅 Bilanço: ☀️ <b>JPM</b> 14.10 13:45 TSİ~  ⚠️sabah olabilir
```

**Değişiklik planı:**

All changes go in app/telegram/format.py digest() (lines 2388-2421). main.py, notify.py and the data stay as they are; no new I/O.
(a) Add two small helpers so rows are formatted once. _digest_top_row(p) is the existing row at :2409-2410 without the leading spaces. _digest_ev_row(e) is the existing row at :2415-2417, keeping '~' and '⚠️sabah olabilir'.
(b) Compute live = [e for e in events if not e.get('passed')] at the top of the function. Sort it by e.get('report_ts') or 0. report_ts is already set by annotate(), so this is a reorder only and the soonest event comes first.
(c) Line 2: remove the leading '\n'. Replace the heading at :2395 plus the one-line-per-type loop at :2396-2398 with ONE line: f"😴 {len(pending)} bildirim birikti: " + " · ".join(f"{DIGEST_LABELS.get(k,k)} ×{n}" for the 3 most frequent types). If more types exist, append f" · +{rest} diğer", where rest is the sum of the remaining counts. The no-pending text at :2403 stays the same, minus its '\n'.
(d) Line 3: if top is not empty, '🚨 En şüpheli: ' + _digest_top_row(top[0]).
(e) Line 4: if live is not empty, '📅 Bilanço: ' + _digest_ev_row(live[0]).
(f) Below a blank line, move the item list here and change its heading from "En önemli 3'ü" to "Son 3 bildirim (yeniden eskiye):". Iterate reversed(pending[-3:]) and keep printing payload line 1 as today.
(g) Then '\n🚨 <b>Diğer şüpheli pozisyonlar:</b>' with top[1:], only if len(top) > 1. Then '\n📅 <b>Yaklaşan diğer bilançolar:</b>' with live[1:6], only if len(live) > 1. This replaces 'Bugün/yarın bilanço', which does not match the 3-day window.
(h) DISCLAIMER stays last.
Optional (h2): make the label fallback DIGEST_LABELS.get(k) or KINDS.get(k, ('', k))[1] from app.notify, so held high-priority types show human labels.
Not in this formatter: the line-1 fixes for whale_fill_alert (:349), anomaly_alert (:295) and the other quiet-hour kinds belong to their own audits. The digest depends on them.
I checked the new strings against the FORBIDDEN/ALLOW patterns in tests/test_wording.py; none match. In the proposed render the lines are 24, 53, 40 and about 48 visible characters. Scratch renders: /tmp/claude-0/-home-user-hlearninginsiders/49de93d8-1c8e-5cfb-b772-3a1b67b2789e/scratchpad/audit/digest_render.py (current) and digest_proposed.py (proposed).

**Etkilenen testler:**
  - None. No test calls fmt.digest or checks DIGEST_LABELS, 'GÜNAYDIN', "En önemli 3'ü" or 'Bugün/yarın bilanço' (grep over tests/).
  - tests/test_wording.py scans format.py string literals with its AST check. The proposed new strings pass its FORBIDDEN/ALLOW patterns, so no assertion changes are needed.
  - Suggested new test (does not exist yet): render digest() with 5 pending items, 3 top positions and 2 events. Assert that the first 4 lines hold the total and type counts, top[0]'s symbol and the soonest event's symbol, and that 'Son 3 bildirim' lists the newest item first.


---

## 31. strat

**Biçimlendirici / çağrı yeri:** strat_stop (app/telegram/format.py:2641; caller app/lab/deliver.py:85 on_stop; rec built at app/lab/looks.py:208 = {rule_id, ver, n_c, mean})

**Şu anki ilk 4 satır:**
```
🧪 Canlı kural durduruldu (koruma kuralı) · L v1
Onaydan sonraki 22 kümede ortalama net -0.98%. Koruma: her 20 yeni kümede tek yönlü t (p ≤ 0.05, tekrara göre düzeltilmemiş — bilerek erken durdurur, gerçek etkili kuralı da durdurabilir). Yeni olay mesajı gelmeyecek.   [one 217-char line, about 5-6 lines on a phone]
Betimleme — karar yalnız önceden yazılı bakış noktasında; geçmiş ölçüm, tahmin değil · mesaj yalnız sahibin onayladığı kuraldan, strateji asla aramaz   [STRAT_FOOT, italic]
(none - the message is 3 logical lines)
```

**Olması gerekenler (essentials):**
  - What happened: a live (approved) rule was stopped by the guard (koruma kuralı)
  - Which rule: rule_id + version (no coin or side; this alert is about the rule, not a market event)
  - How big compared with the threshold: how many post-approval clusters (n_c) and the mean net result (mean), against the guard test (one-sided t, p ≤ 0.05, every 20 new clusters)
  - Current state / consequence: no more event messages will come from this rule
  - When: implicit (sent when the check runs); the rec has no timestamp field, so nothing to add

**Sorunlar:**
  - Line 2 is one 217-character block that holds the measurement, the guard rule, the caveat in brackets and the consequence. On a phone it wraps to about 5-6 visual lines, so the first 3-4 visible lines do not cover everything.
  - The current state ('Yeni olay mesajı gelmeyecek.') comes last, after the long caveat in brackets, so it is pushed below the first 3-4 visible lines.
  - The guard threshold (tek yönlü t, p ≤ 0.05) is mixed into the same bracket as the explanatory caveat ('tekrara göre düzeltilmemiş — bilerek erken durdurur, gerçek etkili kuralı da durdurabilir'). The threshold is essential; the caveat is detail and belongs below.
  - The mean net value is not bold. strat_pass and _lab_line bold 'ortalama net', so this is a small inconsistency (optional fix, cosmetic only).

**Önerilen ilk 4 satır:**
```
🧪 <b>Canlı kural durduruldu (koruma kuralı)</b> · L v1
Onaydan sonraki 22 kümede ortalama net <b>-0.98%</b>
Koruma: her 20 yeni kümede tek yönlü t, p ≤ 0.05
Yeni olay mesajı gelmeyecek.
```

**Değişiklik planı:**

In app/telegram/format.py:2641-2645, rewrite strat_stop as "\n".join([...]) of 6 lines. No new data is used (only rec rule_id/ver/n_c/mean, as now), no new I/O, and all wording is existing text that has been split or moved.
L1 (unchanged): f"🧪 <b>Canlı kural durduruldu (koruma kuralı)</b> · {esc(rec['rule_id'])} v{rec['ver']}"
L2: first sentence of the current line 2, without the trailing period. Optionally bold the value to match strat_pass: f"Onaydan sonraki {rec['n_c']} kümede ortalama net <b>{_lab_pct(rec.get('mean'))}</b>"
L3: the threshold part of the current 'Koruma:' clause, taken out of the brackets: "Koruma: her 20 yeni kümede tek yönlü t, p ≤ 0.05"
L4: the closing sentence moved up: "Yeni olay mesajı gelmeyecek."
L5 (detail, below the fold, italic): the rest of the bracketed caveat: "<i>Tekrara göre düzeltilmemiş — bilerek erken durdurur, gerçek etkili kuralı da durdurabilir.</i>"
L6: STRAT_FOOT (unchanged, last).
Rendered check: lines 1-4 are 47/45/48/28 characters for rule 'L' and at most 55 for 'HAC-KRP-F', so they fit a phone line without wrapping. The detail line is 90 characters. The dedup key in deliver.py:85 (lab:durdu:{rule_id}:{ver}) does not change, and neither does the caller signature. There are no predictions, and the measured wording is kept word for word.

**Etkilenen testler:**
  - tests/test_lab_rules.py:318 - asserts only that 'durduruldu' in the sent text. It still passes after the reorder (I checked with a scratch render); no update needed.
  - No other test refers to strat_stop's text (grep for 'Yeni olay mesajı', 'koruma kuralı', 'lab:durdu' in tests/ returns nothing). Optional: add an assertion that the first 4 lines contain 'durduruldu', 'ortalama net', 'p ≤ 0.05' and 'gelmeyecek'.


---

## 32. openmove

**Biçimlendirici / çağrı yeri:** openmove_report (app/telegram/format.py:2795; helpers _gap_line :2778, _mover_lines :2783, both shared with movewin_report)

**Şu anki ilk 4 satır:**
```
🔔 Açılışın en hareketlileri — şu ana kadar, ilk 12 dk (16:30–16:42 TSİ) · Çar 07.10
⚠️ canlı akışın görülmediği aralık: 16:29 (40 sn), 16:32–16:33, 16:33–16:34, 16:34–16:35 (+2) — o aralığın işlemleri eksik
1. SNDK 🟢 +4.82% · aralık %6.10 · $3.2M (24s ort. 14×)
2. CBRS 🔴 -3.90% · aralık %5.02 · $1.1M (24s ort. 9.0×)
```

**Olması gerekenler (essentials):**
  - What: a ranking of the stocks that moved most at the open, by |% price change| (header)
  - Window and state: first 5 min / first 30 min (finished) or 'şu ana kadar, ilk N dk' (/acilis, still running) (header)
  - When: TSİ start and end times plus day label (header)
  - Who: top movers by symbol, with † when there was no pre-open trade (rows)
  - Side and size of each move: arrow and signed % from the reference price (rows)
  - How big vs normal: range %, USD volume and the multiple of the 24h average volume for the same span (rows)
  - 30-min report only: the 5-min change in the row tail (secondary, fine at the end of the row)
  - Coverage caveat when the live feed had gaps (secondary: must stay visible, but not ahead of the numbers)
  - Below the essentials: universe and traded counts, volume floor and how many fell under it, the reference rule, the † note, and the measurement-not-prediction disclaimer (footer)

**Sorunlar:**
  - With no feed gaps (the normal 5/30 reports, and the REPORT5/REPORT30 tests), the first 4 lines are already right: header (what, window, times, day) plus the top 3 movers (symbol, arrow, %, range, volume, ×24h). Nothing else needs to change in that case.
  - When coverage has gaps, _gap_line is appended right after the header (format.py:2803-2805), so the caveat comes before the data. This happens on a restart during the window, a WS drop, or the bot starting less than 1 min before the open, and it is common for /acilis live views. Only the top 2 movers then fit in lines 2-4, and the 3rd mover is pushed out.
  - The gap line is long: 122 chars in the test example, and up to about 4 ranges plus '(+N)'. On a phone it wraps to about 3 rows, so what actually sits on screen under the headline is a coverage caveat (a detail or explanation) rather than the ranking. This is the same kind of issue the TWAP rework fixed by moving notes below the essentials.
  - Empty window with gaps: the order is header, gap line, then 'Bu pencerede $25K üstü işlem gören hisse yok.' The actual result (no qualifying movers) ends up on line 3 behind the caveat.
  - Minor, below the first 4 lines so not blocking: the footer is a single 200-270-char italic line. It is acceptable as detail, and splitting it is optional, not required by this audit.
  - Header length is 68-69 chars (83 for live), so it wraps once on a phone. It still carries only essentials (what, window, time, day) and nothing in it can be moved out. Leave it as is.
  - Note for consistency: movewin_report (format.py ~2858) has the identical header, gap line and rows order. Its separate audit should make the same move. Do not change the shared _gap_line or _mover_lines helpers themselves.

**Önerilen ilk 4 satır:**
```
🔔 Açılışın en hareketlileri — şu ana kadar, ilk 12 dk (16:30–16:42 TSİ) · Çar 07.10
1. SNDK 🟢 +4.82% · aralık %6.10 · $3.2M (24s ort. 14×)
2. CBRS 🔴 -3.90% · aralık %5.02 · $1.1M (24s ort. 9.0×)
3. MU ⚪ +0.00% · aralık %0.40 · $30K
```

**Değişiklik planı:**

This is a pure reorder inside openmove_report (app/telegram/format.py:2795-2815). Wording, data and helpers do not change.
1) Take the 3 lines at 2803-2805 out of their place right after the header:
   gl = _gap_line(...)
   if gl: lines.append(gl)
2) Re-insert them after `lines += _mover_lines(rows)` (currently :2809), before the dagger and footer append (:2810-2815). The new order is:
   header → (empty-state line OR mover rows) → ⚠️ gap line (if any) → italic footer.
   The caveat therefore stays visible as the line directly under the list, which is still on the first screen because the message is at most about 12 lines, and it stays outside the italic footer.
3) Leave _gap_line and _mover_lines untouched, because movewin_report shares them. Apply the same one-line move to movewin_report only if its own audit agrees.
4) Optionally update the README line at README.md:2096-2098 ('rapora saatleriyle yazılır') to say 'listenin altında'. It does not currently describe the position, so this is not required.
I checked the result by rendering the proposed order in a scratch script (scratchpad/audit/openmove_render.py). Live with gaps becomes: header, 3 movers, ⚠️ line, footer. Empty with gaps becomes: header, 'Bu pencerede … yok.', ⚠️ line, footer. With no gaps, the output is byte-identical to today, so REPORT5 and REPORT30 still match.

**Etkilenen testler:**
  - tests/test_openmove.py:339-342: in test_late_restart_holiday (D2 restart case) the expected split('\n')[:3] is [header, ⚠️ gap line, '1. SNDK† …']. Swap the expected entries at :341 and :342 to get [header, '1. <b>SNDK</b>† 🟢 <b>+2.00%</b> · …', '⚠️ <i>canlı akışın görülmediği aralık: 16:29–16:35 — o aralığın işlemleri eksik</i>'].
  - tests/test_openmove.py:434-436: in test_coverage_gaps there are no rows, so the message is header, 'Bu pencerede … yok.', ⚠️, footer. lines[1] would now be the empty-state line. Change the assertion to lines[2] (or lines[-2]).
  - tests/test_openmove.py:553-557: in test_message_format the live rep has rows=[], so `head, warn = live.split('\n')[:2]` would pick up the empty-state line as warn. Change it to take [0] and [2] (or [0] and [-2]). The expected strings stay the same.
  - Unaffected, since there are no gaps or only the header/prefix is checked: tests/test_openmove.py:65-74 (REPORT5/REPORT30 used at :286, :291, :301), :550-551 (empty, no gaps), :600-603 (/acilis startswith header and SNDK substring), and tests/test_movewin.py (movewin_report is not touched).


---

## 33. movewin

**Biçimlendirici / çağrı yeri:** movewin_report (app/telegram/format.py:2871; caller app/radar/movewin.py:296, sent with _send kind 'movewin' to the requesting chat, public=False; tests tests/test_movewin.py)

**Şu anki ilk 4 satır:**
```
⏱ En hareketliler — 5 dk ölçüm (15:30:00–15:35:00 TSİ) · Çar 07.10
1. MU† 🟢 +5.00% · aralık %5.00 · $22K
2. CBRS° 🟢 +4.00% · aralık %0.00 · $21K
3. SNDK 🟢 +2.79% · aralık %2.99 · $87K (24s ort. 2.9×)
```

**Olması gerekenler (essentials):**
  - What: an N-minute ⏱ measurement (/5dk) has finished and this is its list of top movers (header: '⏱ En hareketliler — N dk ölçüm')
  - When / how long: the window start and end in TSİ with seconds, plus the day (header)
  - Which stocks and which way: the top movers ranked by |%|, each with symbol, 🟢/🔴 and the signed % change against the last trade before the window started
  - How big vs. normal/threshold: the dollar volume traded in the window and, when available, '24s ort. N×' (multiple of normal 24h-pace volume); the dollar threshold appears in the empty-case line
  - Current state / data quality: whether the live feed missed part of the window (⚠️ gap line), and, if the window had no qualifying trades, the line saying no stock traded above $X
  - Who: not applicable (stocks, not addresses)
  - Below the fold (secondary): how many stocks were watched and traded, how many fell under the threshold, the reference rule, what † and ° mean, the data source, the 'ölçüm, tahmin değil' disclaimer and the late-delivery note

**Sorunlar:**
  - Normal path (no feed gap, which includes the test context REPORT at tests/test_movewin.py:56) already passes: line 1 says what, how long, when and the day, and lines 2-4 are the top 3 movers with direction, %, volume and ×-of-normal. Every essential is in the first 4 lines.
  - Conditional problem: when rep['cover']['gaps'] is non-empty, the 76-char '⚠️ canlı akışın görülmediği aralık: … — o aralığın işlemleri eksik' line is inserted as line 2 (format.py:2877-2879). That pushes the #3 mover to line 5. On a phone the header (66-67 chars) and the gap line each wrap to about 2 lines, so the #1 mover only appears around visual line 5. This is a data-quality caveat, so under the rule set in e49e472 (detail and notes go below) it belongs under the ranking, not above it. Rendered gap case: 1 header, 2 ⚠️ gap, 3 '1. SNDK 🔴 -4.81% …', 4 '2. MU 🟢 +3.92% …', 5 '3. CRWV …'.
  - Minor, not blocking: the late-delivery note ('rapor 1 dk 25 sn gecikmeli gönderildi') sits at the very end of a 321-364 char italic footer. It is secondary because the header already gives the window times, so leaving it there is fine. Splitting the long footer is optional and outside the first-4 scope.
  - Minor, not proposed: in each row 'aralık %x' comes before volume and '24s ort. N×'. Moving it would put size and ×-normal earlier on narrow phones, but _mover_lines (format.py:2783) is shared with openmove_report, so that change belongs to a joint openmove/movewin decision, not this pass. openmove_report (format.py:2803-2805) also puts the gap line at line 2; whatever is decided for movewin should be applied the same way there in the openmove audit.

**Önerilen ilk 4 satır:**
```
⏱ En hareketliler — 15 dk ölçüm (15:30:00–15:45:00 TSİ) · Çar 07.10
1. SNDK 🔴 -4.81% · aralık %6.25 · $2.4M (24s ort. 14×)
2. MU 🟢 +3.92% · aralık %5.10 · $1.2M (24s ort. 7.1×)
3. CRWV 🟢 +3.10% · aralık %4.03 · $800K (24s ort. 4.7×)
```

**Değişiklik planı:**

This is a reorder only, inside movewin_report (app/telegram/format.py:2871-2893). No new data, no new wording, no I/O.
(1) Move the gap block, `gl = _gap_line((rep.get("cover") or {}).get("gaps") or [])` / `if gl: lines.append(gl)` (lines 2877-2879), from right after the header to right after `lines += _mover_lines(rows)` (line 2883), before the `notes`/footer computation. The resulting order is: header → (empty-case 'Bu pencerede $X üstü işlem gören hisse yok.' line, or mover rows 1..N) → ⚠️ gap line (only if gaps) → italic footer (unchanged, still ending with the late note).
(2) Optionally update the docstring to say the ⚠️ coverage note sits under the list.
Effects: the no-gap output is byte-identical to today (verified in scratch: proposed(repA) == movewin_report(repA)). In the gap case the first 4 become header plus top 3 movers, and the gap warning moves to just above the footer. The empty case with a gap becomes header, 'yok' line, ⚠️ gap, footer.
Leave _mover_lines, _gap_line, the header text, the footer text and the late suffix unchanged. For consistency, the separate openmove_report audit should make the same move at format.py:2803-2805.
Scratch renders are in /tmp/claude-0/-home-user-hlearninginsiders/49de93d8-1c8e-5cfb-b772-3a1b67b2789e/scratchpad/audit/movewin_render.py and movewin_proposed.py.

**Etkilenen testler:**
  - None of the existing assertions need to change: tests/test_movewin.py:56-63 (REPORT constant, no gaps) stays identical
  - tests/test_movewin.py:201 (movewin_report(rep) == REPORT): unchanged, no gaps in that rep
  - tests/test_movewin.py:281 (tick sends text == REPORT): unchanged
  - tests/test_movewin.py:300 (endswith ' · rapor 1 dk 10 sn gecikmeli gönderildi</i>'): unchanged, the footer is still the last line
  - tests/test_movewin.py:313-316 are for movewin_short, not affected
  - Recommended new assertion, e.g. in tests/test_movewin.py test_tick_report_retry_and_short near line 300 or in test_retro_window_measure_and_report near line 201: with rep['cover']['gaps'] non-empty and rows present, lines[1] starts with '1. ', the ⚠️ gap line comes right after the last mover row, and lines[-1] is the <i> footer


---

## 34. sim

**Biçimlendirici / çağrı yeri:** sim_opened (app/telegram/format.py:3120-3152; caller app/radar/sim.py:561 -> _notify -> sim_chat_id)

**Şu anki ilk 4 satır:**
```
🧪 SİM AÇILDI · HYPE · 🟢LONG 2x · ön bacak
giriş 40.00 · 500 adet · $20K (marjin $10K)
🎯 hedef 40.96 (+2.40% · zincir sonu · limit) · 🛑 stop 36.00 (-10.0%)
balina: 🔴SHORT $1.3M · liq 40.16 (%0.40 kaldı) · 👤 0xaaaa..aaaa · 3 poz zincirde
```

**Olması gerekenler (essentials):**
  - what: a paper (simulated) trade was opened, which leg (🧪 SİM AÇILDI · ön bacak)
  - coin, side and leverage (HYPE · LONG 2x)
  - size: notional (and margin)
  - entry price
  - target price and % from entry, stop price and % from entry
  - why/trigger: opposing whale side, notional, liq price and % distance to liq
  - who: whale address link
  - what happens next / how long: does a reverse leg open at the target (SHORT/LONG), or is there none; the 30-minute post-liq exit rule
  - simulation, no real order (the 🧪 SİM title carries it, so the footer can stay at the bottom)

**Sorunlar:**
  - The plan line (whether a reverse leg opens at the target, plus the '30 dk' exit rule: what happens next and how long) is line 5, outside the first 4. In the cascade case it is also the longest line in the message (178 chars rendered), so the forward-looking info is both late and hard to read on a phone.
  - Line 2 uses space on qty ('500 adet') and margin. Both are derivable details (margin = notional / leverage). Meanwhile size and leverage sit on two different lines (2x on L1, $20K on L2).
  - Line 3 is 68-82 chars because the target source label and 'limit' sit inside the target parenthetical ('+0.40% · liq fiyatı (defter yok) · limit'). Detail is wedged between target and stop, and on a phone the stop wraps onto a second visual line.
  - Line 4 appends casc_note ('3 poz zincirde', a cascade detail) after the whale address, pushing it to 80 chars.
  - The cascade plan sentence carries rule detail inline: '(hedef %0.75 geri çekilme, stop %10)' are reverse-leg parameters, not headline info.
  - Redundancy in the liq/no-book case: 'liq fiyatı (defter yok)' (L3), 'ters bacak YOK' (L5) and the italic note 'defter yok → hedef liq fiyatı, ters bacak yok' (L6) say the same thing three times. The target also equals the whale liq price, so e.g. 170.68 appears on both L3 and L4. The note text is built in app/radar/sim.py:150 (plan_leg1), outside the formatter, so it is left alone here.
  - Data gap (flag only, not fixable by reordering): leg 1's maximum life when liq never comes (sim_pre_max_min, 360 dk default, enforced in sim.expire app/radar/sim.py:241-243) is never mentioned, so 'how long' is only partly told.

**Önerilen ilk 4 satır:**
```
🧪 SİM AÇILDI · HYPE · 🟢LONG 2x · ön bacak · $20K
giriş 40.00 · 🎯 hedef 40.96 (+2.40%) · 🛑 stop 36.00 (-10.0%)
balina: 🔴SHORT $1.3M · liq 40.16 (%0.40 kaldı) · 👤 0xaaaa..aaaa
plan: hedefte long kapanır, aynı fiyattan SHORT açılır; iğne 30 dk içinde gelmezse piyasadan kapanır   [non-cascade variant: 'plan: hedefte kapanır, ters bacak YOK; liq'ten 30 dk sonra hedef yoksa piyasadan kapanır']
```

**Değişiklik planı:**

All edits are in app/telegram/format.py sim_opened (3120-3152). Nothing new is fetched or computed; every value used is already there. I prototyped this as a scratch copy (/tmp/claude-0/-home-user-hlearninginsiders/49de93d8-1c8e-5cfb-b772-3a1b67b2789e/scratchpad/audit/sim_opened_proposed.py) and all current test substrings still match. The current render is in .../scratchpad/audit/sim_opened.py.
1) L1 (3127): append notional to the head: f"🧪 <b>SİM AÇILDI</b> · {_sim_head(t)} · <b>{usd(t.get('notional'))}</b>". Do not change _sim_head itself, because sim_closed and sim_summary share it.
2) L2: merge the old lines 2 and 3 (3128-3130) into one price line, dropping qty, margin, src and 'limit' from it: f"giriş <b>{px(entry)}</b> · 🎯 hedef <b>{px(tp)}</b> ({tp_pct:+.2f}%) · 🛑 stop {px(stop)} ({stop_pct:+.1f}%)". This keeps the substring 'hedef <b>40.96</b>'.
3) L3: the whale line (3134-3137) stays as the trigger line, but drop the trailing casc_note segment (3137) from it.
4) L4: shorten the plan line (3141-3148) to its gist and keep it right after the whale line.
   - Cascade: f"plan: hedefte {'long' if up else 'short'} kapanır, aynı fiyattan <b>{'SHORT' if up else 'LONG'}</b> açılır; iğne {after} dk içinde gelmezse piyasadan kapanır". The price is dropped because it is already on L2; the rule parenthetical moves to step 5.
   - Non-cascade: 'plan: hedefte kapanır, ters bacak YOK; liq'ten {after} dk sonra hedef yoksa piyasadan kapanır'.
   - Keep '<b>SHORT</b> açılır' and 'ters bacak YOK' verbatim.
5) Below line 4 (new detail line): f"hedef: {src} · limit" + (f" · {esc(casc_note)}" if casc_note) + (cascade only: f" · ters bacak: hedef %{post_tp} geri çekilme, stop %{post_stop}"). This keeps 'liq fiyatı (defter yok)'. Rendered example: 'hedef: zincir sonu · limit · 3 poz zincirde · ters bacak: hedef %0.75 geri çekilme, stop %10'.
6) Next detail line: f"{qty:.4g} adet · marjin {usd(margin)}".
7) Unchanged at the bottom: the italic note (3149-3150), then 'bakiye … · tur #… · SIM_FOOT' (3151).
8) Optionally update the docstring (3121) to state the order: ne/ölçek → fiyatlar → balina → plan; ayrıntı altta.
Resulting line lengths: 47-48, 60-63, 63-64, 88-100 chars, then the details. The plan line is still the longest, but it dropped from 178 to 100 and now sits in position 4. Not proposed, because it would be new content: showing sim_pre_max_min (360 dk) on L4. Leave that for the user to decide. The duplicate 'defter yok → … ters bacak yok' note is produced by sim.py:150, so trimming it would be a separate change outside the formatter.

**Etkilenen testler:**
  - No existing assertion should break if the strings in the plan are kept verbatim. Assertions checked against the prototype:
  - tests/test_sim.py:258 'SİM AÇILDI' / 'HYPE': still pass
  - tests/test_sim.py:259 'hedef <b>40.96</b>' and 'SHORT</b> açılır': still pass because L2 keeps 'hedef <b>{px(tp)}</b>' and L4 keeps '<b>SHORT</b> açılır'
  - tests/test_sim.py:588 'ters bacak YOK', 'defter yok', 'liq fiyatı (defter yok)': still pass (the src label moves to the detail line)
  - tests/test_sim.py:626 'SİM AÇILDI', '5x', fmt.usd(16_500): still pass (the notional moves to L1)
  - tests/test_fanout.py:277 only seeds the literal payload '🧪 SİM AÇILDI', so it does not depend on the formatter
  - Suggested new assertion near tests/test_sim.py:588: split m into lines and check that lines[0] contains 'SİM AÇILDI' and the notional, lines[1] contains 'giriş' + 'hedef' + 'stop', lines[2] starts with 'balina:', lines[3] starts with 'plan:', and 'adet' / 'liq fiyatı (defter yok)' appear only after the first 4 lines


---

## 35. sim

**Biçimlendirici / çağrı yeri:** sim_closed (app/telegram/format.py:3155; caller app/radar/sim.py:646)

**Şu anki ilk 4 satır:**
```
🧪 SİM KAPANDI · HYPE · 🟢LONG 2x · ön bacak · ✅ hedef
40.00 → 40.96 · +$468 (+4.7% marjin) · ücret $12 · 3 dk · uç 41.00 / 39.90
bakiye $10K (+4.7% başlangıçtan) · 1 işlem · isabet %100
🔁 SHORT açıldı @ 40.96 · $21K · hedef 40.65 (-0.75%) · stop 45.06 (+10.0%) · en çok 120 dk
```

**Olması gerekenler (essentials):**
  - What happened: a paper (🧪 SİM) trade closed, and why: ✅ target / 🛑 stop / ⏱ time ran out / 🚫 thesis broken (header)
  - Which trade: coin, side, leverage, leg (ön/ters bacak) (header)
  - Result: entry → exit price, PnL in $ and as % of margin (the 'how big' measure for a sim trade)
  - How long it was open (dur_txt)
  - For stop/void/timeout: the one-line close reason ('aynı mumda hedef de vardı, stop sayıldı', 'balina liq olmadan kapattı', 'iğne 30 dk içinde hedefe uzanmadı', ...)
  - Current state: balance and % change from start, trade count, hit rate
  - Current state: whether a reverse leg opened now (🔁 side @ price, size, target/stop, time limit) or why it did not ('ters bacak açılmadı: ...')
  - Below the top lines: fee, high/low extremes (uç), plan notes from opening, footer 'simülasyon — gerçek emir yok · sayfa /sim'

**Sorunlar:**
  - Measured by rendering the real flows in tests/test_sim.py through the formatter (scratch script: /tmp/claude-0/-home-user-hlearninginsiders/49de93d8-1c8e-5cfb-b772-3a1b67b2789e/scratchpad/audit/sim_closed_render.py and sim_closed_extra.py). The base test case (no note) is fine: all essentials are in lines 1-4.
  - The italic note line is printed right after the result line (format.py:3165-3166), so whenever t['note'] is set, balance drops to line 4 and the current-state line (🔁 reverse leg opened / 'ters bacak açılmadı') drops to line 5. Concrete case: a cascade-target trade whose opening plan added a note ('son dilim kalan bakiyeden ($3K), tam dilim $10K' or 'görünen defter zincir sonunda bitti (hedef alt sınır)') hits its target and a SHORT leg opens. Line 3 is then a 103-char plan note and '🔁 SHORT açıldı @ 40.96 …' is line 5. The new open position is outside the first 4 lines.
  - Every target hit at the liq price (tp_src='liq') carries the opening note 'defter yok → hedef liq fiyatı, ters bacak yok'. That note always takes line 3, so 'ters bacak açılmadı: hedef zincir sonu değildi' is always line 5 (see test_sim.py:589 case).
  - For tp exits the note holds only opening-plan detail (sim.py _close:437 joins the plan note with `why`, and tp has no `why`), but it still takes a top slot ahead of balance and state.
  - For stop/void/timeout, any plan note is joined in front of the close reason with ' · '. The essential reason then sits at the end of a long line, e.g. 'defter yok → hedef liq fiyatı, ters bacak yok · aynı mumda hedef de vardı, stop sayıldı' (87 chars).
  - Line 2 mixes essentials (price move, PnL, duration) with detail (ücret $12, uç 41.00 / 39.90), making it 74-77 chars. The detail could move below and bring the line down to about 44 chars.
  - Wording is measured, with no predictions. The header's 🧪 SİM already marks it as a simulation, so the footer can stay at the bottom.

**Önerilen ilk 4 satır:**
```
🧪 SİM KAPANDI · HYPE · 🟢LONG 2x · ön bacak · ✅ hedef
40.00 → 40.96 · +$468 (+4.7% marjin) · 3 dk
bakiye $10K (+4.7% başlangıçtan) · 1 işlem · isabet %100
🔁 SHORT açıldı @ 40.96 · $21K · hedef 40.65 (-0.75%) · stop 45.06 (+10.0%) · en çok 120 dk   [or 'ters bacak açılmadı: …' on a leg-1 target hit without a reverse leg; or the close reason in italics, e.g. 'aynı mumda hedef de vardı, stop sayıldı', on stop/void/timeout; on a leg-2 target hit, the 'ücret $X · uç H / L' detail line]
```

**Değişiklik planı:**

Only sim_closed in app/telegram/format.py:3155-3187 changes. Nothing new is fetched or computed; existing content only moves.
1) Line 2 (format.py:3160-3163): remove ' · ücret {usd(fee_usd)}' and the ' · uç {hi} / {lo}' suffix. It keeps: '{entry} → <b>{exit}</b> · <b>{±$pnl}</b> ({pct:+.1f}% marjin) · {dur_txt}'.
2) Move the note block (format.py:3165-3166, `if t.get("note"): lines.append(<i>note</i>)`) from right after line 2 to after the child/elif block (format.py:3175-3184). Balance is then always line 3, and the state line (🔁 '… açıldı' or 'ters bacak açılmadı: …') is always line 4.
3) Split the note by exit reason. For exit_reason != 'tp', split it with note.rpartition(' · '). The last segment is the close reason that _close appends last (sim.py:437; reasons come from sim.py:231-262 and 623-624, and none contains ' · '). The opening-plan notes (plan_leg1 notes at sim.py:120-162) also never contain ' · '. If there is no ' · ', the whole note is the reason. Print the reason in italics right after the state block. For non-tp exits there is no child or 'açılmadı' line, so the reason lands on line 4. Print any remaining plan-note part in italics below it. For tp exits the whole note is plan detail and goes below the state line. This split is optional: moving the whole note still keeps the reason within line 4, but at the end of a long line.
4) Before SIM_FOOT, add one detail line: f"ücret {usd(fee_usd)}" + (f" · uç {px(hi)} / {px(lo)}" if hi_px and lo_px else "").
5) Final order: header / result+duration / balance+stats / state (🔁 child, 'ters bacak açılmadı', or close reason) / plan notes / ücret·uç / footer. Keep the 🔁 line as one line: size, target, stop and the 'en çok N dk' limit describe the position that is open now. Optionally update the docstring to cite the 14.10 'ilk 3-4 satır' rule.
I checked this order by rendering a scratch copy (no repo edits). Every case keeps its essentials in lines 1-4: the liq-target case, cascade target with plan notes and reverse leg, stop with plan note, and timeout.

**Etkilenen testler:**
  - tests/test_sim.py:288 ('SİM KAPANDI', '✅ hedef', 'SHORT açıldı</b> @ 40.96', '+$468'): substring checks, still pass, no change needed. Optional order check to add: msg.split('\n')[3].startswith('🔁') and 'ücret' not in '\n'.join(msg.split('\n')[:3])
  - tests/test_sim.py:289 (f'bakiye <b>{fmt.usd(10_467.928)}</b>', 'isabet %100'): still passes
  - tests/test_sim.py:304 ('ters bacak', '✅ hedef' in leg-2 close): still passes, since 'ters bacak' is in the header
  - tests/test_sim.py:331 ('🛑 stop', 'stop sayıldı'): still passes. Optional check to add: 'stop sayıldı' in bot.sent[-1][1].split('\n')[3]
  - tests/test_sim.py:346 ('🚫 tez bozuldu') and :353 ('⏱ süre doldu'): still pass
  - tests/test_sim.py:590 ('ters bacak açılmadı: hedef zincir sonu değildi', '15 dk'): still passes. Optional check to add: m2.split('\n')[3] == 'ters bacak açılmadı: hedef zincir sonu değildi' (it is line 5 today)
