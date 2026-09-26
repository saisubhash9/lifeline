// Grok Voice client: realtime WebSocket, 24 kHz PCM in and out, client-side tools.
// The browser gets a short-lived client secret from our server; the API key never leaves it.

const SAMPLE_RATE = 24000;

const WORKLET = `
class Capture extends AudioWorkletProcessor {
  process(inputs) {
    const channel = inputs[0] && inputs[0][0];
    if (channel) this.port.postMessage(channel.slice(0));
    return true;
  }
}
registerProcessor("lifeline-capture", Capture);
`;

function toBase64(int16) {
  const bytes = new Uint8Array(int16.buffer);
  let binary = "";
  for (let i = 0; i < bytes.length; i += 0x8000) {
    binary += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  }
  return btoa(binary);
}

function fromBase64(text) {
  const binary = atob(text);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
  return new Int16Array(bytes.buffer);
}

class GrokVoice {
  constructor({ instructions, tools, onTranscript, onStatus, onTool }) {
    this.instructions = instructions;
    this.tools = tools; // {name: {description, parameters, run(args) -> object}}
    this.onTranscript = onTranscript;
    this.onStatus = onStatus;
    this.onTool = onTool;
    this.ws = null;
    this.ctx = null;
    this.micOn = false;
    this.pending = [];
    this.playAt = 0;
    this.sources = new Set();
    this.needsResponse = false;
    this.assistantText = "";
  }

  get connected() {
    return this.ws && this.ws.readyState === WebSocket.OPEN;
  }

  async connect() {
    this.onStatus("Requesting a voice session…");
    const response = await fetch("/api/voice/session", { method: "POST" });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      throw new Error(body.detail || "Voice session unavailable");
    }
    const session = await response.json();
    this.ctx = new AudioContext({ sampleRate: SAMPLE_RATE });
    await this.ctx.resume();
    await new Promise((resolve, reject) => {
      const ws = new WebSocket(session.url, [`xai-client-secret.${session.token}`]);
      this.ws = ws;
      ws.onopen = () => {
        this.send({
          type: "session.update",
          session: {
            voice: "eve",
            instructions: this.instructions,
            turn_detection: { type: "server_vad" },
            audio: {
              input: { format: { type: "audio/pcm", rate: SAMPLE_RATE } },
              output: { format: { type: "audio/pcm", rate: SAMPLE_RATE } },
            },
            tools: Object.entries(this.tools).map(([name, tool]) => ({
              type: "function",
              name,
              description: tool.description,
              parameters: tool.parameters || { type: "object", properties: {} },
            })),
          },
        });
        this.onStatus("Grok Voice connected. Turn the mic on to talk, or type below.");
        resolve();
      };
      ws.onerror = () => reject(new Error("Voice connection failed"));
      ws.onclose = () => {
        this.onStatus("Voice disconnected.");
        this.stopMic();
      };
      ws.onmessage = (event) => this.handle(JSON.parse(event.data));
    });
  }

  disconnect() {
    this.stopMic();
    this.stopPlayback();
    if (this.ws) this.ws.close();
    this.ws = null;
  }

  send(message) {
    if (this.connected) this.ws.send(JSON.stringify(message));
  }

  say(text, instructions) {
    this.send({ type: "conversation.item.create", item: { type: "message", role: "user", content: [{ type: "input_text", text }] } });
    this.send(instructions ? { type: "response.create", response: { instructions } } : { type: "response.create" });
  }

  async startMic() {
    if (this.micOn || !this.connected) return;
    this.stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, channelCount: 1 },
    });
    const url = URL.createObjectURL(new Blob([WORKLET], { type: "application/javascript" }));
    await this.ctx.audioWorklet.addModule(url);
    this.source = this.ctx.createMediaStreamSource(this.stream);
    this.node = new AudioWorkletNode(this.ctx, "lifeline-capture");
    let buffer = [];
    let length = 0;
    this.node.port.onmessage = (event) => {
      buffer.push(event.data);
      length += event.data.length;
      if (length < SAMPLE_RATE / 10) return;
      const pcm = new Int16Array(length);
      let offset = 0;
      for (const chunk of buffer) {
        for (let i = 0; i < chunk.length; i += 1) {
          const sample = Math.max(-1, Math.min(1, chunk[i]));
          pcm[offset + i] = sample < 0 ? sample * 0x8000 : sample * 0x7fff;
        }
        offset += chunk.length;
      }
      buffer = [];
      length = 0;
      this.send({ type: "input_audio_buffer.append", audio: toBase64(pcm) });
    };
    this.source.connect(this.node);
    this.micOn = true;
  }

  stopMic() {
    if (!this.micOn) return;
    this.micOn = false;
    if (this.source) this.source.disconnect();
    if (this.node) this.node.disconnect();
    if (this.stream) this.stream.getTracks().forEach((track) => track.stop());
  }

  play(base64) {
    const pcm = fromBase64(base64);
    const audio = this.ctx.createBuffer(1, pcm.length, SAMPLE_RATE);
    const channel = audio.getChannelData(0);
    for (let i = 0; i < pcm.length; i += 1) channel[i] = pcm[i] / 0x8000;
    const source = this.ctx.createBufferSource();
    source.buffer = audio;
    source.connect(this.ctx.destination);
    const start = Math.max(this.ctx.currentTime + 0.02, this.playAt);
    source.start(start);
    this.playAt = start + audio.duration;
    this.sources.add(source);
    source.onended = () => this.sources.delete(source);
  }

  stopPlayback() {
    this.sources.forEach((source) => {
      try { source.stop(); } catch (error) { /* already stopped */ }
    });
    this.sources.clear();
    this.playAt = 0;
  }

  async handle(message) {
    const type = message.type;
    if (type === "response.output_audio.delta" && message.audio) {
      this.play(message.audio);
    } else if (type === "response.output_audio.delta" && message.delta) {
      this.play(message.delta);
    } else if (type === "input_audio_buffer.speech_started") {
      this.stopPlayback();
    } else if (type === "response.output_audio_transcript.delta") {
      this.assistantText += message.delta || "";
      this.onTranscript("grok", this.assistantText, false);
    } else if (type === "response.output_audio_transcript.done") {
      this.onTranscript("grok", message.transcript || this.assistantText, true);
      this.assistantText = "";
    } else if (type.includes("input_audio_transcription") && message.transcript) {
      if (type.endsWith("completed") || type.endsWith("done")) this.onTranscript("you", message.transcript, true);
    } else if (type === "response.function_call_arguments.done") {
      const tool = this.tools[message.name];
      let args = {};
      try { args = JSON.parse(message.arguments || "{}"); } catch (error) { args = {}; }
      let output;
      try {
        output = tool ? await tool.run(args) : { error: `unknown tool ${message.name}` };
      } catch (error) {
        output = { error: String(error) };
      }
      this.onTool(message.name, args);
      this.send({ type: "conversation.item.create", item: { type: "function_call_output", call_id: message.call_id, output: JSON.stringify(output) } });
      this.needsResponse = true;
    } else if (type === "response.done") {
      if (this.needsResponse) {
        this.needsResponse = false;
        this.send({ type: "response.create" });
      }
    } else if (type === "error") {
      this.onStatus(`Voice error: ${(message.error && message.error.message) || "unknown"}`);
    }
  }
}

window.GrokVoice = GrokVoice;
