// Jarvis's voice in Google Meet: the mic Meet asks for comes back with Jarvis's voice mixed in.
// Jarvis serves its speech on this socket while `jarvis meeting` runs (meet_voice.py, PORT).
(() => {
  const SOCKET = "ws://127.0.0.1:47123";
  const ctx = new AudioContext();
  const voice = ctx.createGain(); // Jarvis's speech; every mic Meet gets mixes it in
  let next = 0;
  // Chrome keeps the context silent until the page gets a click or a key; joining the call is one.
  for (const type of ["pointerdown", "keydown"]) addEventListener(type, () => ctx.resume(), { capture: true });

  function connect() {
    const ws = new WebSocket(SOCKET);
    ws.binaryType = "arraybuffer";
    let rate = 0;
    ws.onmessage = ({ data }) => {
      if (typeof data === "string") return void (rate = JSON.parse(data).rate);
      if (!rate) return;
      const pcm = new Int16Array(data);
      const buffer = ctx.createBuffer(1, pcm.length, rate);
      const samples = buffer.getChannelData(0);
      for (let i = 0; i < pcm.length; i++) samples[i] = pcm[i] / 32768;
      const source = ctx.createBufferSource();
      source.buffer = buffer;
      source.connect(voice);
      next = Math.max(next, ctx.currentTime + 0.05); // 50 ms of slack for network jitter
      source.start(next);
      next += buffer.duration;
    };
    ws.onclose = () => setTimeout(connect, 5000); // Jarvis not running yet, or restarted
  }
  connect();

  const devices = navigator.mediaDevices;
  const getUserMedia = devices.getUserMedia.bind(devices);
  devices.getUserMedia = async (constraints) => {
    const stream = await getUserMedia(constraints);
    const [mic] = stream.getAudioTracks();
    if (!mic) return stream;
    ctx.resume();
    const out = ctx.createMediaStreamDestination();
    const input = ctx.createMediaStreamSource(new MediaStream([mic]));
    input.connect(out);
    voice.connect(out);
    const [mixed] = out.stream.getAudioTracks();
    // Meet reads the device from the track it gets; answer for the real mic.
    Object.defineProperty(mixed, "label", { get: () => mic.label });
    for (const name of ["getSettings", "getCapabilities", "getConstraints", "applyConstraints"]) {
      mixed[name] = (...args) => mic[name](...args);
    }
    const stop = mixed.stop.bind(mixed);
    mixed.stop = () => {
      stop();
      mic.stop(); // or the mic light stays on after Meet lets go
      input.disconnect();
      voice.disconnect(out);
    };
    return new MediaStream([mixed, ...stream.getVideoTracks()]);
  };
})();
