"""Start de app zoals op de server en wacht tot hij antwoordt (zowel met als zonder wachtwoord)."""
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

app = Path(__file__).resolve().parent.parent / "app.py"
for poort, wachtwoord in ((8501, ""), (8502, "test")):
    env = {**os.environ, "JAARBOEK_WACHTWOORD": wachtwoord}
    p = subprocess.Popen([sys.executable, "-m", "streamlit", "run", str(app), f"--server.port={poort}",
                          "--server.headless=true"], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        for _ in range(60):
            try:
                if urllib.request.urlopen(f"http://localhost:{poort}/_stcore/health", timeout=2).read() == b"ok":
                    break
            except OSError:
                time.sleep(1)
        else:
            raise SystemExit("app start niet:\n" + p.stdout.read1().decode(errors="replace"))
        print(f"app antwoordt op poort {poort} (wachtwoord {'aan' if wachtwoord else 'uit'})")
    finally:
        p.terminate()
        p.wait(timeout=20)
