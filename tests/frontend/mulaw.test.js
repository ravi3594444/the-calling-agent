/**
 * G.711 mu-law, checked numerically rather than by eye.
 *
 * A mistyped entry in a codec table is silent: audio still plays, it just
 * sounds wrong in a way nobody can pin down. So these assert the properties
 * the standard defines -- reference values, and signal-to-noise on a tone.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { decode, decodeSample, encode, encodeSample } from '../../static/js/mulaw.js';

test('matches the reference values in the standard', () => {
  assert.equal(encodeSample(0), 255, 'silence');
  assert.equal(encodeSample(32635), 128, 'full scale positive');
  assert.equal(encodeSample(-32635), 0, 'full scale negative');
});

test('every byte except negative zero survives a decode/encode round trip', () => {
  const broken = [];
  for (let byte = 0; byte < 256; byte++) {
    if (encodeSample(decodeSample(byte)) !== byte) broken.push(byte);
  }
  // 127 is mu-law's negative zero; it decodes to 0, which canonically
  // re-encodes to 255. That is the standard, not a defect.
  assert.deepEqual(broken, [127]);
});

test('a 440 Hz tone survives with the SNR G.711 promises', () => {
  const samples = 8000;
  const pcm = new Int16Array(samples);
  for (let i = 0; i < samples; i++) {
    pcm[i] = Math.round(Math.sin((2 * Math.PI * 440 * i) / 8000) * 20000);
  }

  const back = decode(encode(pcm));
  let signal = 0;
  let noise = 0;
  for (let i = 0; i < samples; i++) {
    signal += pcm[i] ** 2;
    noise += (pcm[i] - back[i]) ** 2;
  }
  const snr = 10 * Math.log10(signal / noise);

  // G.711 gives ~38 dB. Anything under 30 means the tables are wrong.
  assert.ok(snr > 30, `SNR was ${snr.toFixed(1)} dB`);
});

test('clips rather than wrapping at full scale', () => {
  // Wrapping would turn the loudest part of a word into a burst of noise.
  assert.equal(encodeSample(32767), encodeSample(32635));
  assert.equal(encodeSample(-32768), encodeSample(-32635));
});

test('one byte per sample, so a frame is a third the size of PCM16', () => {
  const pcm = new Int16Array(160); // 20 ms at 8 kHz
  assert.equal(encode(pcm).length, 160);
  assert.equal(pcm.byteLength, 320);
});
