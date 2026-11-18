"""Records docs/chat-demo.gif from a running app (real model, light theme).

Usage: streamlit run chat/app.py --server.port 8599  (in another shell)
       python scripts/record_demo.py [http://localhost:8599]
Needs: pip install playwright pillow && playwright install chromium
"""

from __future__ import annotations

import io
import sys
import time
from pathlib import Path

from PIL import Image
from playwright.sync_api import Page, sync_playwright

URL = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8599"
OUT = Path(__file__).resolve().parent.parent / "docs" / "chat-demo.gif"
frames: list[tuple[Image.Image, int]] = []


def grab(page: Page, ms: int) -> None:
    img = Image.open(io.BytesIO(page.screenshot())).convert("RGB")
    frames.append((img, ms))


def watch(page: Page, seconds: float, ms: int = 400) -> None:
    end = time.time() + seconds
    while time.time() < end:
        grab(page, ms)
        page.wait_for_timeout(ms)


def ask(page: Page, text: str) -> None:
    box = page.get_by_test_id("stChatInputTextArea")
    box.click()
    for chunk in range(0, len(text), 4):
        box.type(text[chunk : chunk + 4])
        grab(page, 120)
    grab(page, 300)
    box.press("Enter")


with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 860, "height": 600}, color_scheme="light")
    page.goto(URL)
    page.get_by_test_id("stChatInputTextArea").wait_for()
    watch(page, 1.0)
    ask(page, "I need a card saying Ship 0.1.0")
    page.get_by_text("A2UI JSON the agent sent").first.wait_for(timeout=60000)
    watch(page, 2.0)
    ask(page, "a form to book a table for two")
    page.get_by_text("A2UI JSON the agent sent").nth(1).wait_for(timeout=60000)
    watch(page, 2.0)
    frame = page  # Components v2 render in the page, not an iframe
    name = frame.locator("input[type=text]").first
    name.click()
    name.type("Ada Lovelace", delay=60)
    grab(page, 600)
    frame.locator("button[type=button]").last.click()
    watch(page, 9.0)
    grab(page, 3000)
    browser.close()

# Drop duplicates (merge time), then save.
merged: list[list] = []
for img, ms in frames:
    if merged and merged[-1][0].tobytes() == img.tobytes():
        merged[-1][1] += ms
    else:
        merged.append([img, ms])
pal = [im.convert("P", palette=Image.ADAPTIVE, colors=64) for im, _ in merged]
OUT.parent.mkdir(exist_ok=True)
pal[0].save(OUT, save_all=True, append_images=pal[1:], duration=[m[1] for m in merged], loop=0, optimize=True)
print(f"{len(merged)} frames, {OUT.stat().st_size / 1024:.0f} KiB")
