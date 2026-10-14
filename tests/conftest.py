"""pytest için: testlerin tempfile.mkdtemp() klasörleri tek koşu klasörüne düşer, koşu bitince silinir
(tests/run.py ile aynı)."""
import atexit
import shutil
import tempfile

_d = tempfile.mkdtemp(prefix="hlr-tests-")
tempfile.tempdir = _d
atexit.register(shutil.rmtree, _d, True)
