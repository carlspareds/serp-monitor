"""Captures the README screenshots and demo GIF from a freshly seeded demo.

Run from the repository root:

    .venv/bin/python scripts/capture_demo_screenshots.py

What it does, so the images match exactly what `make run` shows on a fresh
clone:
  1. Creates a throwaway directory with an empty .env and a new SQLite DB.
  2. Seeds that DB with scripts/seed_demo_data.py (the same script
     `make run` calls).
  3. Starts uvicorn on 127.0.0.1:8765 against that DB with the scheduler
     off and every credential forced blank (so a local .env with real keys
     can't leak into the screenshots or switch off demo mode).
  4. Captures each page at 1280x800 with headless Chromium (Playwright),
     then stops the server and deletes the temp directory.

Outputs:
  - docs/screenshots/{onboarding,dashboard_home,diff_view,review_queue,
    evidence_vault}.png
  - docs/demo.gif (< 5 MB)

Requires Playwright with Chromium installed (`playwright install chromium`).
"""
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from PIL import Image
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from database.models import url_hash

PORT = 8765
BASE_URL = f"http://127.0.0.1:{PORT}"
DOCS_DIR = ROOT / "docs"
SCREENSHOTS_DIR = DOCS_DIR / "screenshots"
# Seeded negative with an evidence snapshot (see scripts/seed_demo_data.py).
EVIDENCE_URL = "https://scam.example/jordan-ashworth-fraud"

# Forced blank for the server process: load_dotenv() never overrides a
# variable that is already set, even to "".
_BLANK_ENV = (
    "APIFY_API_KEY", "AUTH_USER", "AUTH_PASS", "LLM_PROVIDER", "GEMINI_API_KEY",
    "OPENAI_BASE_URL", "OPENAI_API_KEY", "OPENAI_MODEL", "SMTP_HOST",
    "SMTP_USER", "SMTP_PASS", "SMTP_FROM", "ALERT_EMAIL", "TELEGRAM_BOT_TOKEN",
    "SLACK_WEBHOOK_URL", "NTFY_URL", "NTFY_TOPIC",
)


def wait_for_server(url: str, timeout: float = 45.0) -> bool:
    start = time.time()
    while time.time() - start < timeout:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=2.0) as resp:
                if resp.status in (200, 302, 303):
                    return True
        except (urllib.error.URLError, TimeoutError, OSError):
            time.sleep(0.5)
    return False


def optimize_png(file_path: Path, max_kb: int = 300) -> None:
    img = Image.open(file_path)
    img.save(file_path, optimize=True)
    size_kb = file_path.stat().st_size / 1024
    if size_kb > max_kb:
        quantized = img.quantize(colors=256, method=Image.Quantize.MEDIANCUT)
        quantized.save(file_path, optimize=True)
        size_kb = file_path.stat().st_size / 1024
    print(f"  {file_path.name}: {size_kb:.1f} KB")


def create_demo_gif(frames: list[Path], output_path: Path, max_mb: float = 5.0) -> None:
    images = []
    for p in frames:
        img = Image.open(p).convert("RGB")
        img_resized = img.resize((1024, 640), Image.Resampling.LANCZOS)
        images.append(img_resized.quantize(colors=128, method=Image.Quantize.MEDIANCUT))
    images[0].save(output_path, save_all=True, append_images=images[1:],
                   duration=2200, loop=0, optimize=True)
    size_mb = output_path.stat().st_size / (1024 * 1024)
    print(f"  {output_path.name}: {size_mb:.2f} MB (target < {max_mb} MB)")


def main():
    SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="serp-demo-") as tmp:
        tmp_dir = Path(tmp)
        env_file = tmp_dir / ".env"
        env_file.write_text("")
        env = os.environ.copy()
        env.update({name: "" for name in _BLANK_ENV})
        env.update({
            "DB_PATH": str(tmp_dir / "seo_dashboard.db"),
            "ENV_FILE": str(env_file),
            "DISABLE_SCHEDULER": "1",
            # Throwaway local server on 127.0.0.1 with no account: the
            # local-demo opt-out (SECURITY.md, item 6) skips sign-in.
            "ALLOW_NO_AUTH": "1",
            "HOST": "127.0.0.1",
            "PYTHONPATH": "src",
        })

        print("Seeding a fresh demo database...")
        subprocess.run([sys.executable, "scripts/seed_demo_data.py"],
                       env=env, cwd=ROOT, check=True)

        server = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "dashboard.main:app", "--app-dir", "src",
             "--host", "127.0.0.1", "--port", str(PORT), "--log-level", "warning"],
            env=env, cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            print(f"Starting server on port {PORT}...")
            if not wait_for_server(f"{BASE_URL}/onboarding"):
                raise RuntimeError("Server failed to start in time.")

            targets = [
                ("onboarding.png", f"{BASE_URL}/onboarding"),
                ("dashboard_home.png", f"{BASE_URL}/"),
                # Dialogs and panels open over Results through deep links.
                ("diff_view.png", f"{BASE_URL}/results?modal=/ui/compare%3Fcountry%3Dus"),
                ("review_queue.png", f"{BASE_URL}/results?modal=/ui/review"),
                ("evidence_vault.png",
                 f"{BASE_URL}/results?panel=/ui/evidence%3Furl_hash%3D{url_hash(EVIDENCE_URL)}"),
            ]

            with sync_playwright() as p:
                browser = p.chromium.launch(
                    headless=True,
                    args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"],
                )
                context = browser.new_context(viewport={"width": 1280, "height": 800},
                                              device_scale_factor=1)
                page = context.new_page()
                for filename, url in targets:
                    print(f"Capturing {filename} from {url}...")
                    page.goto(url, wait_until="networkidle")
                    page.wait_for_timeout(300)
                    out_path = SCREENSHOTS_DIR / filename
                    page.screenshot(path=str(out_path))
                    optimize_png(out_path)
                browser.close()
        finally:
            server.terminate()
            server.wait()

    print("\nCreating demo animated GIF...")
    gif_frames = [SCREENSHOTS_DIR / name for name in (
        "onboarding.png", "dashboard_home.png", "diff_view.png",
        "review_queue.png", "evidence_vault.png",
    )]
    create_demo_gif(gif_frames, DOCS_DIR / "demo.gif")
    print("\nDone.")


if __name__ == "__main__":
    main()
