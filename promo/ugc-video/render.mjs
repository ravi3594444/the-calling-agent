#!/usr/bin/env node
// Seek-render index.html into an MP4, stills or a contact sheet, and self-check it.
//
//   node render.mjs tableline-ugc.mp4        full render: H.264 video + the page's synthesized score (AAC)
//   node render.mjs --stills 1.5,12.4,20.6   stills at those seconds -> out/still-01.50s.jpg ...
//   node render.mjs --sheet 0.5              contact sheet, one frame every 0.5 s -> out/sheet.jpg
//   node render.mjs --check                  text collisions, loaded assets, loudness jumps at cuts
//
// The page exposes window.draw(frame), FRAMES, FPS, CUES, SCORE and READY; it is opened with
// ?render so its live preview loop stays off. Needs Node 18+, Playwright with Chromium, ffmpeg.
import { spawn, execSync } from 'node:child_process';
import { createRequire } from 'node:module';
import { mkdirSync, writeFileSync, rmSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const args = process.argv.slice(2);
const opt = name => { const i = args.indexOf(name); return i < 0 ? undefined : args[i + 1]; };
const outDir = join(here, 'out');

async function chromium() {
  for (const name of ['playwright', 'playwright-core']) {
    try { return (await import(name)).chromium; } catch {}
    try { return createRequire(join(process.cwd(), 'x'))(name).chromium; } catch {}
    try { return createRequire(join(execSync('npm root -g').toString().trim(), 'x'))(name).chromium; } catch {}
  }
  throw new Error('Playwright not found: npm i -D playwright (or playwright-core with a local Chrome)');
}

const browser = await (await chromium()).launch();
const page = await browser.newPage({ viewport: { width: 540, height: 960 } });
const errors = [], requests = [];
page.on('pageerror', e => errors.push(e.message));
page.on('request', r => requests.push(r.url()));
await page.goto(pathToFileURL(join(here, 'index.html')).href + '?render');
await page.evaluate(() => window.READY);
const info = await page.evaluate(() => ({ FRAMES, FPS, CUES, W, H }));
if (errors.length) { console.error('page errors:', errors); process.exit(1); }
mkdirSync(outDir, { recursive: true });

const grab = (f, q = .95) => page.evaluate(([f, q]) => { window.draw(f); return document.getElementById('c').toDataURL('image/jpeg', q).split(',')[1]; }, [f, q]);

if (args.includes('--stills')) {
  for (const s of opt('--stills').split(',').map(Number)) {
    const f = Math.min(info.FRAMES - 1, Math.round(s * info.FPS)), file = join(outDir, `still-${s.toFixed(2).padStart(5, '0')}s.jpg`);
    writeFileSync(file, Buffer.from(await grab(f), 'base64')); console.log(file);
  }
} else if (args.includes('--sheet')) {
  const every = Number(opt('--sheet') || 1), from = Number(opt('--from') || 0), to = Number(opt('--to') || info.FRAMES / info.FPS);
  const times = []; for (let s = from + every / 2; s < to; s += every) times.push(s);
  const jpg = await page.evaluate(([times, fps]) => {
    const cols = 8, tw = 216, th = 384, rows = Math.ceil(times.length / cols), sheet = document.createElement('canvas');
    sheet.width = cols * tw; sheet.height = rows * th; const x = sheet.getContext('2d'); x.fillStyle = '#000'; x.fillRect(0, 0, sheet.width, sheet.height);
    times.forEach((s, i) => {
      window.draw(Math.round(s * fps)); const cx = (i % cols) * tw, cy = Math.floor(i / cols) * th;
      x.drawImage(document.getElementById('c'), cx, cy, tw, th);
      x.fillStyle = 'rgba(0,0,0,.7)'; x.fillRect(cx, cy, 70, 24); x.fillStyle = '#fff'; x.font = '700 15px monospace'; x.fillText(s.toFixed(2) + 's', cx + 5, cy + 17);
    });
    return sheet.toDataURL('image/jpeg', .88).split(',')[1];
  }, [times, info.FPS]);
  const file = join(outDir, opt('--out') || 'sheet.jpg'); writeFileSync(file, Buffer.from(jpg, 'base64')); console.log(file);
} else if (args.includes('--check')) {
  // 1. text colliding with text, or sitting on something claimed as 'keep'
  const hits = await page.evaluate(([frames]) => {
    const found = {};
    for (let f = 0; f < frames; f += 2) {
      window.draw(f); const L = window.LAYOUT;
      for (let i = 0; i < L.length; i++) for (let j = i + 1; j < L.length; j++) {
        const a = L[i], b = L[j]; if (a.kind !== 'text' && b.kind !== 'text') continue;
        const ox = Math.min(a.x + a.w, b.x + b.w) - Math.max(a.x, b.x), oy = Math.min(a.y + a.h, b.y + b.h) - Math.max(a.y, b.y);
        if (ox > 4 && oy > 4) { const k = a.id.replace(/-\d+$/, '') + ' × ' + b.id.replace(/-\d+$/, ''); (found[k] ||= []).push(f); }
      }
    }
    return found;
  }, [info.FRAMES]);
  const pairs = Object.entries(hits);
  console.log(pairs.length ? 'LAYOUT: collisions\n' + pairs.map(([k, fs]) => `  ${k}: ${(fs[0] / info.FPS).toFixed(2)}s-${(fs[fs.length - 1] / info.FPS).toFixed(2)}s (${fs.length} frames)`).join('\n') : 'LAYOUT: CLEAN');
  // 2. nothing loaded but the page itself and the app's own font files
  const loaded = requests.filter(u => !u.split('?')[0].endsWith('/promo/ugc-video/index.html'));
  const fonts = loaded.filter(u => /\/static\/fonts\/manrope-\d+\.woff2$/.test(u)), foreign = loaded.filter(u => !fonts.includes(u));
  console.log(foreign.length ? 'ASSETS: unexpected loads\n  ' + foreign.join('\n  ') : `ASSETS: CLEAN (nothing loaded but ${fonts.length} Manrope font files)`);
  // 3. loudness: a cut should not jump more than +6 dB over the music before it. Same rule as the
  //    soundtrack skill's sync-check: RMS of the 0.3 s after the cut against the last second, or the
  //    typical level of the last 4 s if louder, or the level before a break when coming out of one.
  const audio = await page.evaluate(async ([cues]) => {
    const sr = 48000, ac = new OfflineAudioContext(2, Math.ceil(sr * (window.FRAMES / window.FPS)), sr); window.SCORE(ac);
    const buf = await ac.startRendering(), a = buf.getChannelData(0), b = buf.getChannelData(1), win = sr / 100, pow = [];
    let peak = 0;
    for (let i = 0; i + win <= a.length; i += win) { let s = 0; for (let k = i; k < i + win; k++) { const v = (a[k] + b[k]) / 2; s += v * v; peak = Math.max(peak, Math.abs(a[k]), Math.abs(b[k])); } pow.push(s / win); }
    const rms = (t0, t1) => { const i0 = Math.max(0, Math.round(t0 * 100)), i1 = Math.min(pow.length, Math.round(t1 * 100)); let s = 0; for (let i = i0; i < i1; i++) s += pow[i]; return Math.sqrt(s / Math.max(1, i1 - i0)) + 1e-6; };
    const report = cues.map(c => {
      const before = rms(c - 1, c), wins = [1, 2, 3, 4].map(k => rms(c - k, c - k + 1)), loud = Math.max(...wins), srt = [...wins].sort((x, y) => x - y);
      const ref = 20 * Math.log10(before / loud) < -10 ? loud : Math.max(before, (srt[1] + srt[2]) / 2);
      return { c, jump: +(20 * Math.log10(rms(c, c + .3) / ref)).toFixed(1) };
    });
    return { peak: +(20 * Math.log10(peak)).toFixed(1), report };
  }, [info.CUES]);
  const startles = audio.report.filter(r => r.jump > 6);
  console.log(`AUDIO: peak ${audio.peak} dBFS; cuts ${audio.report.map(r => `${r.c}s ${r.jump > 0 ? '+' : ''}${r.jump}dB`).join(', ')}`);
  console.log(startles.length ? 'AUDIO: STARTLE at ' + startles.map(r => r.c + 's').join(', ') : 'AUDIO: no startling jumps at cuts');
} else {
  const out = resolve(args.find(a => !a.startsWith('--')) || join(here, 'tableline-ugc.mp4'));
  const wav = out.replace(/\.mp4$/, '') + '.wav';
  const b64 = await page.evaluate(async () => {
    const sr = 48000, ac = new OfflineAudioContext(2, Math.ceil(sr * (window.FRAMES / window.FPS)), sr); window.SCORE(ac);
    const buf = await ac.startRendering(), n = buf.length, L = buf.getChannelData(0), R = buf.getChannelData(1);
    const dv = new DataView(new ArrayBuffer(44 + n * 4)), str = (o, s) => [...s].forEach((ch, i) => dv.setUint8(o + i, ch.charCodeAt(0)));
    str(0, 'RIFF'); dv.setUint32(4, 36 + n * 4, true); str(8, 'WAVEfmt '); dv.setUint32(16, 16, true); dv.setUint16(20, 1, true); dv.setUint16(22, 2, true);
    dv.setUint32(24, sr, true); dv.setUint32(28, sr * 4, true); dv.setUint16(32, 4, true); dv.setUint16(34, 16, true); str(36, 'data'); dv.setUint32(40, n * 4, true);
    for (let i = 0; i < n; i++) for (const [c, d] of [[0, L], [1, R]]) { const v = Math.max(-1, Math.min(1, d[i])); dv.setInt16(44 + (i * 2 + c) * 2, v < 0 ? v * 0x8000 : v * 0x7fff, true); }
    const bytes = new Uint8Array(dv.buffer); let bin = '';
    for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
    return btoa(bin);
  });
  writeFileSync(wav, Buffer.from(b64, 'base64'));
  const ff = spawn('ffmpeg', ['-y', '-loglevel', 'error', '-framerate', String(info.FPS), '-f', 'image2pipe', '-c:v', 'mjpeg', '-i', '-', '-i', wav,
    '-c:v', 'libx264', '-preset', 'slow', '-crf', '19', '-pix_fmt', 'yuv420p', '-profile:v', 'high', '-movflags', '+faststart',
    '-c:a', 'aac', '-b:a', '192k', '-shortest', out], { stdio: ['pipe', 'inherit', 'inherit'] });
  const done = new Promise((ok, fail) => ff.on('close', code => code ? fail(new Error('ffmpeg exited ' + code)) : ok()));
  const t0 = Date.now();
  for (let f = 0; f < info.FRAMES; f++) {
    if (!ff.stdin.write(Buffer.from(await grab(f, .96), 'base64'))) await new Promise(r => ff.stdin.once('drain', r));
    if (f % 150 === 0) process.stdout.write(`frame ${f}/${info.FRAMES} (${((Date.now() - t0) / 1000).toFixed(0)}s)\n`);
  }
  ff.stdin.end(); await done; rmSync(wav);
  writeFileSync(out.replace(/\.mp4$/, '') + '.cues.json', JSON.stringify({ fps: info.FPS, cues: info.CUES }));
  console.log(out);
}
if (errors.length) console.error('page errors:', errors);
await browser.close();
