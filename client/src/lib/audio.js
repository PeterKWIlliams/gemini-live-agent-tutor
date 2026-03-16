const TARGET_INPUT_RATE = 16000;
const TARGET_OUTPUT_RATE = 24000;

export function encodeAudioToPCM16(float32Array) {
  const buffer = new ArrayBuffer(float32Array.length * 2);
  const view = new DataView(buffer);

  for (let index = 0; index < float32Array.length; index += 1) {
    const sample = Math.max(-1, Math.min(1, float32Array[index]));
    view.setInt16(index * 2, sample < 0 ? sample * 0x8000 : sample * 0x7fff, true);
  }

  return arrayBufferToBase64(buffer);
}

export function decodePCM16ToAudio(base64String) {
  const buffer = base64ToArrayBuffer(base64String);
  const view = new DataView(buffer);
  const samples = new Float32Array(buffer.byteLength / 2);

  for (let index = 0; index < samples.length; index += 1) {
    samples[index] = view.getInt16(index * 2, true) / 0x8000;
  }

  return samples;
}

export function resampleAudio(float32Array, fromRate, toRate = TARGET_INPUT_RATE) {
  if (fromRate === toRate) {
    return float32Array;
  }

  const ratio = fromRate / toRate;
  const length = Math.round(float32Array.length / ratio);
  const output = new Float32Array(length);

  for (let index = 0; index < length; index += 1) {
    const position = index * ratio;
    const left = Math.floor(position);
    const right = Math.min(left + 1, float32Array.length - 1);
    const fraction = position - left;
    output[index] = float32Array[left] * (1 - fraction) + float32Array[right] * fraction;
  }

  return output;
}

export function getInputSampleRate() {
  return TARGET_INPUT_RATE;
}

export function getOutputSampleRate() {
  return TARGET_OUTPUT_RATE;
}

function arrayBufferToBase64(buffer) {
  let binary = '';
  const bytes = new Uint8Array(buffer);

  for (let index = 0; index < bytes.byteLength; index += 1) {
    binary += String.fromCharCode(bytes[index]);
  }

  return window.btoa(binary);
}

function base64ToArrayBuffer(base64String) {
  const binary = window.atob(base64String);
  const buffer = new ArrayBuffer(binary.length);
  const bytes = new Uint8Array(buffer);

  for (let index = 0; index < binary.length; index += 1) {
    bytes[index] = binary.charCodeAt(index);
  }

  return buffer;
}
