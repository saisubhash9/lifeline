"""Narrate text with Grok Voice (force_message speaks the exact script) and save 24 kHz WAV."""
import asyncio, base64, json, os, ssl, sys, wave
import certifi, websockets

ENV = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
for line in open(ENV):
    if "=" in line and not line.strip().startswith("#"):
        key, value = line.strip().split("=", 1)
        os.environ.setdefault(key, value)

URL = "wss://api.x.ai/v1/realtime?model=grok-voice-latest"


async def speak(text: str, path: str, voice: str = "eve") -> float:
    ctx = ssl.create_default_context(cafile=certifi.where())
    async with websockets.connect(URL, additional_headers={"Authorization": f"Bearer {os.environ['XAI_API_KEY']}"}, ssl=ctx, max_size=None) as ws:
        await ws.send(json.dumps({"type": "session.update", "session": {
            "voice": voice,
            "instructions": "You are a documentary narrator.",
            "turn_detection": None,
            "audio": {"output": {"format": {"type": "audio/pcm", "rate": 24000}, "speed": 1.0}}}}))
        await ws.send(json.dumps({"type": "conversation.item.create", "item": {
            "type": "force_message", "role": "assistant", "interruptible": False,
            "content": [{"type": "output_text", "text": text}]}}))
        pcm = bytearray()
        got_audio = False
        while True:
            try:
                msg = json.loads(await asyncio.wait_for(ws.recv(), 12 if got_audio else 40))
            except asyncio.TimeoutError:
                break
            kind = msg["type"]
            if kind == "response.output_audio.delta":
                pcm += base64.b64decode(msg.get("audio") or msg.get("delta"))
                got_audio = True
            elif kind in ("response.done", "response.output_audio.done") and got_audio:
                break
            elif kind == "error":
                raise RuntimeError(msg)
    with wave.open(path, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(24000)
        out.writeframes(bytes(pcm))
    return len(pcm) / 2 / 24000


if __name__ == "__main__":
    print(asyncio.run(speak(sys.argv[1], sys.argv[2])))
