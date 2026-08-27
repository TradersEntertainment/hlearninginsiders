"""AI analist — LLM ÖNERİR, Python KARAR VERİR.

Bir dil modeline ham satır verip "örüntü bul" demek, kendinden emin uydurma
üretir; finansal veride bu sadece işe yaramaz değil, zararlıdır. O yüzden iş
dörde bölünmüştür:

1. `briefing.py`  — istatistiği PYTHON hesaplar (kesin, ucuz, doğrulanabilir)
2. `client.py`    — model brifingi okur, yapılandırılmış hipotez döner
3. `schema.py`    — hipotez ÖLÇÜLEBİLİR mi diye süzülür (kapalı metrik enum'u)
4. `analyst.py`   — vadesi gelen hipotezi PYTHON ölçer: tuttu / tutmadı /
                    ölçülemedi. Model böylece kendi siciliyle yargılanır.

Bu, projede zaten var olan balina sicilinin (`recompute.py`) aynısıdır:
iddia + ufuk + sonradan ölçülebilir bir sonuç. Uyduran model istatistikte
kendini ele verir; kullanıcının ona inanması gerekmez.

Çıktı YALNIZ sitede görünür — Telegram'a hiçbir şey düşmez.
"""
