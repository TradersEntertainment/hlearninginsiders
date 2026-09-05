"""Test koşucusu — pytest yoksa da çalışır.

    python tests/run.py            # hepsi
    python tests/run.py liqmap     # adı 'liqmap' içeren dosyalar

tests/test_*.py içindeki test_* fonksiyonlarını sırayla çağırır; assert hatası
ya da istisna = FAIL. pytest kuruluysa `python -m pytest tests` de aynı dosyaları
koşar (dosyalar pytest uyumludur)."""
import importlib.util
import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))


def main(argv):
    flt = argv[1:]
    files = sorted(f for f in os.listdir(HERE)
                   if f.startswith("test_") and f.endswith(".py")
                   and (not flt or any(x in f for x in flt)))
    total = failed = 0
    for f in files:
        spec = importlib.util.spec_from_file_location(f[:-3], os.path.join(HERE, f))
        mod = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(mod)
        except Exception:
            print(f"✗ {f}: içe aktarılamadı"); traceback.print_exc(); failed += 1; total += 1
            continue
        for name in dir(mod):
            if not name.startswith("test_"):
                continue
            fn = getattr(mod, name)
            if not callable(fn):
                continue
            total += 1
            try:
                fn()
                print(f"✓ {f}::{name}")
            except Exception:
                failed += 1
                print(f"✗ {f}::{name}")
                traceback.print_exc()
    print(f"\n{total - failed}/{total} geçti" + (f" · {failed} HATA" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
