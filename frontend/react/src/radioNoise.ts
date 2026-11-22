export type NoiseProfile = "white" | "brown";

export function noiseAudio(profile: NoiseProfile) {
  const sampleRate = 22050;
  const frames = sampleRate * 5;
  const dataBytes = frames * 2;
  const buffer = new ArrayBuffer(44 + dataBytes);
  const data = new DataView(buffer);
  function word(offset: number, value: string) {
    for (let index = 0; index < value.length; index++) data.setUint8(offset + index, value.charCodeAt(index));
  }
  word(0, "RIFF");
  data.setUint32(4, 36 + dataBytes, true);
  word(8, "WAVEfmt ");
  data.setUint32(16, 16, true);
  data.setUint16(20, 1, true);
  data.setUint16(22, 1, true);
  data.setUint32(24, sampleRate, true);
  data.setUint32(28, sampleRate * 2, true);
  data.setUint16(32, 2, true);
  data.setUint16(34, 16, true);
  word(36, "data");
  data.setUint32(40, dataBytes, true);
  let previous = 0;
  for (let frame = 0; frame < frames; frame++) {
    const white = Math.random() * 2 - 1;
    previous = (previous + 0.02 * white) / 1.02;
    const level = profile === "brown" ? previous * 2.5 : white * 0.2;
    const fade = Math.min(1, frame / 128, (frames - 1 - frame) / 128);
    data.setInt16(44 + frame * 2, Math.max(-1, Math.min(1, level)) * fade * 32767, true);
  }
  return new Blob([buffer], { type: "audio/wav" });
}
