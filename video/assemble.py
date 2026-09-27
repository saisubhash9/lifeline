"""Assemble the Lifeline demo video from Imagine clips, slides, the walkthrough, and narration."""

import asyncio
import json
import subprocess
import sys
import wave
from pathlib import Path

import imageio_ffmpeg

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from tts import speak  # noqa: E402

FF = imageio_ffmpeg.get_ffmpeg_exe()
TEMPO = 1.1
FPS = 30
DUR = json.loads((HERE / "durations.json").read_text())
TIMELINE = json.loads((HERE / "timeline.json").read_text())
PARTS = HERE / "parts"
PARTS.mkdir(exist_ok=True)
V_OUT = ["-c:v", "libx264", "-preset", "medium", "-crf", "19", "-pix_fmt", "yuv420p", "-r", str(FPS)]
A_OUT = ["-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2"]


def run(args):
    subprocess.run([FF, "-loglevel", "error", "-y", *args], check=True)


def seconds(key):
    return DUR[key] / TEMPO


def wav_seconds(path):
    with wave.open(str(path)) as w:
        return w.getnframes() / w.getframerate()


def narr(key):
    return str(HERE / f"n_{key}.wav")


def clip(name, key, out, overlay, extra=0.7, fade_title=False):
    """Imagine clip + caption/title overlay + narration over the clip's own audio at low volume."""
    length = seconds(key) + extra
    overlay_filter = (
        "[1:v]format=rgba,fade=t=in:st=0.8:d=0.8:alpha=1[ov];[0:v][ov]overlay=0:0"
        if fade_title
        else "[1:v]format=rgba[ov];[0:v][ov]overlay=0:0"
    )
    run([
        "-i", str(HERE / f"imagine_{name}.mp4"), "-loop", "1", "-i", str(HERE / overlay), "-i", narr(key),
        "-filter_complex",
        f"[0:v]scale=1920:1080,setsar=1[bg];{overlay_filter.replace('[0:v]', '[bg]')},fade=t=in:st=0:d=0.4[v];"
        f"[0:a]volume=0.22[bed];[2:a]atempo={TEMPO},adelay=300|300,volume=1.6[n];[bed][n]amix=inputs=2:normalize=0,apad[a]",
        "-map", "[v]", "-map", "[a]", "-t", f"{length:.2f}", *V_OUT, *A_OUT, str(PARTS / out),
    ])


def slide(key, image, out, extra=0.6, fade_out=False):
    length = seconds(key) + extra
    frames = int(length * FPS)
    fades = f",fade=t=in:st=0:d=0.4" + (f",fade=t=out:st={length - 1.2:.2f}:d=1.2" if fade_out else "")
    audio_fade = f",afade=t=out:st={length - 1.0:.2f}:d=1.0" if fade_out else ""
    run([
        "-loop", "1", "-i", str(HERE / image), "-i", narr(key),
        "-filter_complex",
        f"[0:v]scale=2112:1188,zoompan=z='min(zoom+0.00035,1.05)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d={frames}:s=1920x1080:fps={FPS}{fades}[v];"
        f"[1:a]atempo={TEMPO},adelay=300|300,volume=1.6,apad{audio_fade}[a]",
        "-map", "[v]", "-map", "[a]", "-t", f"{length:.2f}", *V_OUT, *A_OUT, str(PARTS / out),
    ])


def walkthrough(out):
    start = next(item["t"] for item in TIMELINE if item["key"] == "_start")
    end = next(item["t"] for item in TIMELINE if item["key"] == "_end")
    cuts = sorted((item["from"], item["to"]) for item in TIMELINE if item["key"] == "_cut" and item["to"] - item["from"] > 0.3)

    def mapped(t):
        shift = sum(min(b, t) - a for a, b in cuts if t > a)
        return t - start - shift

    answer = next(item for item in TIMELINE if item["key"] == "_answer")
    answer_wav = HERE / "n_answer.wav"
    if not answer_wav.exists():
        asyncio.run(speak(answer["text"], str(answer_wav), voice="eve"))
    answer_len = wav_seconds(answer_wav)
    next_start = next(item["t"] for item in TIMELINE if not item["key"].startswith("_") and item["t"] > answer["t"])
    window = mapped(next_start) - mapped(answer["t"]) - 0.3
    answer_tempo = min(1.25, max(1.0, answer_len / window)) if window > 0 else 1.0
    print(f"answer {answer_len:.1f}s in window {window:.1f}s -> tempo {answer_tempo:.2f}")

    keep = []
    cursor = start
    for a, b in cuts:
        keep.append((cursor, a))
        cursor = b
    keep.append((cursor, end))
    select = "+".join(f"between(t,{a:.3f},{b:.3f})" for a, b in keep)

    inputs, chains, labels = [], [], []
    for item in TIMELINE:
        if item["key"].startswith("_"):
            continue
        delay = int(max(0.0, mapped(item["t"]) + 0.15) * 1000)
        inputs += ["-i", narr(item["key"])]
        index = len(inputs) // 2
        chains.append(f"[{index}:a]atempo={TEMPO},volume=1.6,adelay={delay}|{delay}[s{index}]")
        labels.append(f"[s{index}]")
    inputs += ["-i", str(answer_wav)]
    index = len(inputs) // 2
    delay = int((mapped(answer["t"]) + 0.2) * 1000)
    chains.append(f"[{index}:a]atempo={answer_tempo:.3f},volume=1.5,adelay={delay}|{delay}[s{index}]")
    labels.append(f"[s{index}]")
    total = mapped(end)
    audio = ";".join(chains) + f";{''.join(labels)}amix=inputs={len(labels)}:normalize=0,apad[a]"
    run([
        "-i", str(HERE / "walkthrough.webm"), *inputs,
        "-filter_complex",
        f"[0:v]fps={FPS},select='{select}',setpts=N/{FPS}/TB,scale=1920:1080,setsar=1,fade=t=in:st=0:d=0.4[v];{audio}",
        "-map", "[v]", "-map", "[a]", "-t", f"{total:.2f}", *V_OUT, *A_OUT, str(PARTS / out),
    ])


def concat(names, out):
    listing = PARTS / "list.txt"
    listing.write_text("".join(f"file '{PARTS / name}'\n" for name in names))
    run(["-f", "concat", "-safe", "0", "-i", str(listing), *V_OUT, *A_OUT, "-movflags", "+faststart", str(out)])


def main():
    clip("flare", "intro1", "01_flare.mp4", "overlay_cap_intro1.png")
    clip("impact", "intro2", "02_impact.mp4", "overlay_cap_intro2.png")
    clip("fleet", "intro3", "03_title.mp4", "overlay_title.png", extra=1.6, fade_title=True)
    slide("problem1", "slide_problem1.png", "04_problem1.mp4")
    slide("problem2", "slide_problem2.png", "05_problem2.mp4")
    slide("solution", "slide_solution.png", "06_solution.mp4")
    walkthrough("07_walkthrough.mp4")
    slide("outro", "slide_outro.png", "08_outro.mp4", extra=1.8, fade_out=True)
    names = ["01_flare.mp4", "02_impact.mp4", "03_title.mp4", "04_problem1.mp4", "05_problem2.mp4", "06_solution.mp4", "07_walkthrough.mp4", "08_outro.mp4"]
    for name in names:
        probe = subprocess.run([FF, "-i", str(PARTS / name)], capture_output=True, text=True).stderr
        print(name, next((line.strip() for line in probe.splitlines() if "Duration" in line), "?"))
    concat(names, HERE / "Lifeline_demo.mp4")
    probe = subprocess.run([FF, "-i", str(HERE / "Lifeline_demo.mp4")], capture_output=True, text=True).stderr
    print("FINAL", next(line.strip() for line in probe.splitlines() if "Duration" in line))


if __name__ == "__main__":
    main()
