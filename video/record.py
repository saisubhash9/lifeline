"""Record the Lifeline walkthrough in Chrome with a visible cursor and subtitles.

Writes video/walkthrough.webm and video/timeline.json (segment start times in seconds
from the start of the recording, plus Grok's live copilot answer text and time).
"""

import json
import shutil
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
SCRIPT = json.loads((HERE / "script.json").read_text())
DUR = json.loads((HERE / "durations.json").read_text())
TEMPO = 1.1  # narration is sped up slightly in the final mix
URL = "http://127.0.0.1:8010/#t=300"
QUESTION = "What happened to emergency comms during this storm, and why? Two short sentences."

HELPERS = """
(() => {
  if (window.__demo) return;
  window.__demo = true;
  const style = document.createElement('style');
  style.textContent = `
    #demo-cursor { position: fixed; left: 0; top: 0; width: 28px; height: 28px; border-radius: 50%;
      background: rgba(255,255,255,0.92); border: 2px solid #0b111b; box-shadow: 0 2px 10px rgba(0,0,0,0.6);
      z-index: 99999; pointer-events: none; transform: translate(-50%, -50%);
      transition: left 0.9s cubic-bezier(.4,.1,.2,1), top 0.9s cubic-bezier(.4,.1,.2,1); }
    #demo-ripple { position: fixed; width: 22px; height: 22px; border-radius: 50%; border: 3px solid #3dce86;
      z-index: 99998; pointer-events: none; transform: translate(-50%, -50%) scale(0.5); opacity: 0; }
    #demo-ripple.go { animation: ripple 0.6s ease-out; }
    @keyframes ripple { from { opacity: 1; transform: translate(-50%,-50%) scale(0.6); } to { opacity: 0; transform: translate(-50%,-50%) scale(3.2); } }
    #demo-caption { position: fixed; left: 50%; bottom: 34px; transform: translateX(-50%); max-width: 1440px; width: max-content;
      text-align: center; font: 600 27px/1.35 Inter, -apple-system, sans-serif; color: #fff; padding: 10px 22px; border-radius: 12px;
      background: rgba(5,8,14,0.82); border: 1px solid rgba(186,206,232,0.2); z-index: 99997; pointer-events: none; transition: opacity .3s; }
    #demo-caption:empty { opacity: 0; }
    .demo-glow { outline: 3px solid #f4c56d !important; outline-offset: 4px; border-radius: 14px; transition: outline .2s; }
  `;
  document.head.appendChild(style);
  const cursor = document.createElement('div'); cursor.id = 'demo-cursor'; cursor.style.left = '720px'; cursor.style.top = '420px';
  const ripple = document.createElement('div'); ripple.id = 'demo-ripple';
  const caption = document.createElement('div'); caption.id = 'demo-caption';
  document.body.style.zoom = '1.3333';
  document.documentElement.append(cursor, ripple, caption);
})();
"""


def main():
    out = HERE / "rec"
    shutil.rmtree(out, ignore_errors=True)
    events = []
    with sync_playwright() as p:
        browser = p.chromium.launch(
            channel="chrome",
            headless=True,
            args=["--autoplay-policy=no-user-gesture-required", "--use-fake-ui-for-media-stream"],
        )
        context = browser.new_context(
            viewport={"width": 1920, "height": 1080},
            record_video_dir=str(out),
            record_video_size={"width": 1920, "height": 1080},
        )
        page = context.new_page()
        t0 = time.monotonic()
        now = lambda: round(time.monotonic() - t0, 2)  # noqa: E731

        page.goto(URL)
        page.wait_for_selector("#fleets .fleet", timeout=60000)
        page.wait_for_selector("#headline .tile", timeout=60000)
        page.evaluate(HELPERS)
        page.wait_for_timeout(600)
        events.append({"key": "_start", "t": now()})

        def caption(text):
            page.evaluate("t => { document.getElementById('demo-caption').textContent = t; }", text)

        def move(selector, dx=0.5, dy=0.5):
            box = page.locator(selector).first.bounding_box()
            if not box:
                return
            x, y = box["x"] + box["width"] * dx, box["y"] + box["height"] * dy
            page.evaluate("([x, y]) => { const c = document.getElementById('demo-cursor'); c.style.left = x + 'px'; c.style.top = y + 'px'; }", [x, y])
            page.wait_for_timeout(950)

        def click(selector, dx=0.5, dy=0.5):
            move(selector, dx, dy)
            page.evaluate("""() => { const c = document.getElementById('demo-cursor'); const r = document.getElementById('demo-ripple');
                r.style.left = c.style.left; r.style.top = c.style.top; r.classList.remove('go'); void r.offsetWidth; r.classList.add('go'); }""")
            page.locator(selector).first.click()
            page.wait_for_timeout(250)

        def scroll(selector, block="start", offset=-70):
            page.evaluate(
                """([s, top, o]) => new Promise((done) => {
                    const from = window.scrollY;
                    const to = top ? 0 : Math.max(0, from + document.querySelector(s).getBoundingClientRect().top + o * 1.3333);
                    const start = performance.now();
                    const step = (now) => {
                        const k = Math.min(1, (now - start) / 750);
                        const ease = k < 0.5 ? 2 * k * k : 1 - Math.pow(-2 * k + 2, 2) / 2;
                        window.scrollTo(0, from + (to - from) * ease);
                        if (k < 1) requestAnimationFrame(step); else done();
                    };
                    requestAnimationFrame(step);
                })""",
                [selector, block == "top", offset],
            )
            page.wait_for_timeout(200)

        def glow(selector, on=True):
            page.evaluate("([s, on]) => document.querySelector(s).classList.toggle('demo-glow', on)", [selector, on])

        def segment(key, actions):
            start = time.monotonic()
            events.append({"key": key, "t": now()})
            caption(SCRIPT[key])
            actions()
            remaining = DUR[key] / TEMPO + 0.35 - (time.monotonic() - start)
            if remaining > 0:
                page.wait_for_timeout(int(remaining * 1000))
            caption("")

        # w1: people first
        def w1():
            scroll("body", "top")
            move(".intro", 0.25, 0.4)
            page.wait_for_timeout(2600)
            scroll("#people", offset=-240)
            glow("#people")
            move("#people", 0.18, 0.6)
            page.wait_for_timeout(2400)
            move("#people", 0.5, 0.6)
        segment("w1", w1)
        glow("#people", False)

        # w2: Grok Sun watch
        def w2():
            scroll("#sunwatch", offset=-60)
            move("#sunwatch", 0.12, 0.35)
            page.wait_for_timeout(1500)
            move("#sunwatch", 0.6, 0.3)
        segment("w2", w2)

        # w3: X-ray timeline
        def w3():
            scroll(".timeline-card", offset=-60)
            move("#chart", 0.35, 0.55)
            page.wait_for_timeout(1800)
            move("#chart", 0.2, 0.2)
        segment("w3", w3)

        # w4: ML flare warning
        def w4():
            click("#jump-decision")
            scroll(".timeline-card", offset=-40)
            glow("#decision")
            move("#decision", 0.5, 0.3)
        segment("w4", w4)
        glow("#decision", False)

        # w5: Grok verification with the coronagraph panel
        def w5():
            click("#jump-decision")
            scroll(".grid-main", offset=-60)
            glow("#decision")
            move("#decision", 0.5, 0.2)
            page.wait_for_timeout(3500)
            move(".verify-panel", 0.7, 0.75)
        segment("w5", w5)
        glow("#decision", False)

        # w6: play the storm
        def w6():
            scroll(".timeline-card", offset=-60)
            click('#speeds button[data-speed="30"]')
            click("#play")
            move("#map", 0.45, 0.45)
        segment("w6", w6)
        click("#play")

        # w7: five fleets
        def w7():
            scroll("#fleets", offset=-40)
            for i, dx in enumerate((0.1, 0.3, 0.5, 0.7, 0.9)):
                move("#fleets", dx, 0.8)
                page.wait_for_timeout(700)
            glow("#fleets .fleet.lifeline_ew")
        segment("w7", w7)
        glow("#fleets .fleet.lifeline_ew", False)

        # w8: Grok Voice
        def w8():
            scroll(".grid-main", offset=-50)
            click("#voice-connect")
            move(".copilot", 0.5, 0.55)
        segment("w8", w8)
        idle_from = now()
        page.wait_for_function("document.querySelectorAll('#transcript .bubble.grok').length > 0", timeout=45000)
        page.wait_for_function("state.voice && state.voice.assistantText === ''", timeout=45000)
        page.wait_for_timeout(600)
        events.append({"key": "_cut", "from": idle_from + 1.2, "to": now() - 0.2})
        before = page.evaluate("document.querySelectorAll('#transcript .bubble.grok').length")
        click("#ask-text")
        page.locator("#ask-text").type(QUESTION, delay=24)
        click("#ask button")
        wait_from = now()
        page.wait_for_function(f"document.querySelectorAll('#transcript .bubble.grok').length > {before}", timeout=60000)
        answer_t = now()
        events.append({"key": "_cut", "from": wait_from + 0.6, "to": answer_t - 0.2})
        last, stable = "", 0
        for _ in range(160):
            bubbles = page.evaluate("[...document.querySelectorAll('#transcript .bubble.grok')].slice(%d).map(b => b.textContent).join(' ')" % before)
            speaking = page.evaluate("state.voice && state.voice.assistantText !== ''")
            stable = stable + 1 if bubbles == last else 0
            last = bubbles
            if stable >= 10 and not speaking:
                break
            page.wait_for_timeout(250)
        hold = max(0.0, len(last.split()) / 2.6 + 1.2 - (now() - answer_t))
        events.append({"key": "_answer", "t": answer_t, "text": last})
        page.wait_for_timeout(int(hold * 1000))

        # w9: held-out ML results
        def w9():
            page.evaluate("state.voice && state.voice.disconnect()")
            scroll("#model-card", offset=-40)
            glow("#model-card")
            move("#model-card", 0.3, 0.55)
            page.wait_for_timeout(3000)
            move("#model-card", 0.8, 0.35)
        segment("w9", w9)
        glow("#model-card", False)

        # w10: evaluation
        def w10():
            scroll(".eval", offset=-40)
            glow("#headline")
            move("#headline", 0.1, 0.5)
        segment("w10", w10)
        glow("#headline", False)

        # w11: debrief
        def w11():
            scroll(".debrief", offset=-200)
            click("#debrief-button")
        segment("w11", w11)
        wait_from = now()
        page.wait_for_function("document.querySelectorAll('#debrief-text p').length > 1", timeout=120000)
        events.append({"key": "_cut", "from": wait_from + 0.3, "to": now() - 0.3})
        page.wait_for_timeout(300)
        scroll(".debrief", offset=-80)
        move("#debrief-text", 0.4, 0.4)
        page.wait_for_timeout(4200)
        events.append({"key": "_end", "t": now()})

        video_path = page.video.path()
        context.close()
        browser.close()
    shutil.move(video_path, HERE / "walkthrough.webm")
    (HERE / "timeline.json").write_text(json.dumps(events, indent=1))
    print(json.dumps(events, indent=1))


if __name__ == "__main__":
    main()
