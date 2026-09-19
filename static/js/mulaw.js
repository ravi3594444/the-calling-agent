/**
 * G.711 mu-law, so the browser can hear what a phone hears.
 *
 * A browser call runs at 24 kHz linear PCM and sounds nothing like a phone
 * line. Telephony is 8 kHz mu-law: narrower band, coarser quantisation, and a
 * noticeably different voice. Testing turn-taking and barge-in at browser
 * fidelity tells you how it feels in a browser -- which is not the product.
 *
 * This is the ITU-T G.711 algorithm as published (the Sun reference
 * implementation). The exponent table is BUILT rather than transcribed,
 * because a mistyped entry in a 256-entry constant is silent: audio still
 * plays, it just sounds wrong in a way nobody can pin down.
 */

const BIAS = 0x84; // 132, added so the log curve starts at the right place
const CLIP = 32635; // 32767 - BIAS, the largest value that survives the bias
const SIGN_BIT = 0x80;
const QUANT_MASK = 0x0f;
const SEG_MASK = 0x70;
const SEG_SHIFT = 4;

/** Segment (exponent) for each of the 256 possible values of (sample >> 7). */
const EXPONENT = new Uint8Array(256);
for (let i = 0; i < 256; i++) {
  // Segment k covers the range where bit (k+7) is the highest bit set.
  let exponent = 0;
  for (let bit = 7; bit >= 1; bit--) {
    if (i & (1 << bit)) {
      exponent = bit;
      break;
    }
  }
  EXPONENT[i] = exponent;
}

/** One 16-bit sample to one mu-law byte. */
export function encodeSample(sample) {
  let sign = (sample >> 8) & SIGN_BIT;
  if (sign !== 0) sample = -sample;
  if (sample > CLIP) sample = CLIP;

  sample += BIAS;
  const exponent = EXPONENT[(sample >> 7) & 0xff];
  const mantissa = (sample >> (exponent + 3)) & QUANT_MASK;
  return ~(sign | (exponent << SEG_SHIFT) | mantissa) & 0xff;
}

/** One mu-law byte back to one 16-bit sample. */
export function decodeSample(byte) {
  const value = ~byte & 0xff;
  let sample = ((value & QUANT_MASK) << 3) + BIAS;
  sample <<= (value & SEG_MASK) >> SEG_SHIFT;
  return value & SIGN_BIT ? BIAS - sample : sample - BIAS;
}

export function encode(pcm) {
  const out = new Uint8Array(pcm.length);
  for (let i = 0; i < pcm.length; i++) out[i] = encodeSample(pcm[i]);
  return out;
}

export function decode(bytes) {
  const out = new Int16Array(bytes.length);
  for (let i = 0; i < bytes.length; i++) out[i] = decodeSample(bytes[i]);
  return out;
}

/** mu-law bytes -> base64, for the wire. */
export function toBase64(bytes) {
  let binary = '';
  for (let i = 0; i < bytes.length; i += 8192) {
    binary += String.fromCharCode(...bytes.subarray(i, i + 8192));
  }
  return btoa(binary);
}

/** base64 -> mu-law bytes. One byte per sample, so no length check is needed. */
export function fromBase64(value) {
  const binary = atob(value);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return bytes;
}
