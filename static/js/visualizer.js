// A small Canvas 2D field. No WebGL, textures, animation framework or DOM work
// per frame. Live deformation uses the same audio graph as audible playback.

const RINGS = 17;
const POINTS = 100;
const DOTS = 64;

// The angle and its harmonics depend only on the point index, never on the
// ring or the clock, so they are computed once instead of RINGS times a frame.
const COS = new Float32Array(POINTS + 1);
const SIN = new Float32Array(POINTS + 1);
const H3 = new Float32Array(POINTS + 1);
const H2 = new Float32Array(POINTS + 1);
const H7 = new Float32Array(POINTS + 1);
for (let point = 0; point <= POINTS; point++) {
  const angle = (point / POINTS) * Math.PI * 2;
  COS[point] = Math.cos(angle);
  SIN[point] = Math.sin(angle);
  H3[point] = angle * 3;
  H2[point] = angle * 2;
  H7[point] = angle * 7;
}

export class VoiceField {
  constructor(canvas, level = () => 0) {
    this.canvas = canvas;
    this.ctx = canvas.getContext('2d');
    this.level = level;
    this.phase = 'idle';
    this.amplitude = 0;
    this.visible = true;
    this.motion = matchMedia('(prefers-reduced-motion: reduce)');
    this.draw = this.draw.bind(this);
    this.resume = this.resume.bind(this);
    this.resize = new ResizeObserver(() => {
      const width = canvas.clientWidth;
      const ratio = Math.min(devicePixelRatio || 1, 2);
      canvas.width = canvas.height = Math.round(width * ratio);
      this.ctx?.setTransform(ratio, 0, 0, ratio, 0, 0);
      this.size = width;
      this.resume();
    });
    this.resize.observe(canvas);
    this.intersection = new IntersectionObserver((entries) => {
      this.visible = entries[0].isIntersecting;
      this.resume();
    });
    this.intersection.observe(canvas);
    document.addEventListener('visibilitychange', this.resume);
    this.motion.addEventListener('change', this.resume);
  }

  setPhase(phase) {
    this.phase = phase;
    this.resume();
  }

  resume() {
    cancelAnimationFrame(this.frame);
    if (!this.visible || document.hidden || !this.ctx) return;
    this.last = 0;
    this.frame = requestAnimationFrame(this.draw);
  }

  draw(now) {
    if (!this.visible || document.hidden || !this.ctx) return;
    const reduced = this.motion.matches;
    if (!reduced && now - this.last < 32) {
      this.frame = requestAnimationFrame(this.draw);
      return;
    }
    this.last = now;
    const time = reduced ? 0 : now / 1700;
    const target = reduced ? 0 : this.level();
    this.amplitude += (target - this.amplitude) * 0.23;
    const ctx = this.ctx;
    const size = this.size || 266;
    const center = size / 2;
    const scale = size / 266;
    const hearing = this.phase === 'hearing';
    const speaking = this.phase === 'speaking';
    const thinking = ['thinking', 'working', 'connecting', 'reconnecting'].includes(this.phase);
    const hue = hearing ? 166 : this.phase === 'error' ? 20 : 83;
    ctx.clearRect(0, 0, size, size);
    ctx.save();
    ctx.translate(center, center);
    ctx.scale(scale, scale);
    ctx.rotate(time * (thinking ? 0.12 : 0.025));
    const energy = this.amplitude * 19;
    const saturation = speaking ? 51 : 32;
    // Colour changes only with hue, saturation and amplitude, none of which
    // vary across a ring. Quantising amplitude lets the strings be reused
    // between frames instead of rebuilt 17 times each one.
    const band = Math.round(this.amplitude * 20);
    if (
      this.strokeHue !== hue ||
      this.strokeSaturation !== saturation ||
      this.strokeBand !== band
    ) {
      this.strokeHue = hue;
      this.strokeSaturation = saturation;
      this.strokeBand = band;
      this.strokes ||= new Array(RINGS);
      for (let ring = 0; ring < RINGS; ring++) {
        const alpha = 0.1 + Math.sin((ring / RINGS) * Math.PI) * 0.36 + (band / 20) * 0.14;
        this.strokes[ring] = `hsla(${hue}, ${saturation}%, ${65 + ring}%, ${alpha})`;
      }
    }
    // The asymmetric contours read as a soft, living ring, not a loading icon.
    for (let ring = 0; ring < RINGS; ring++) {
      const radius = 57 + ring * 3.05;
      const spread = 0.4 + ring / 24;
      const phase3 = time + ring * 0.19;
      const phase7 = ring * 0.14 - time * 1.7;
      const swing = 6 + energy;
      const ripple = 1.7 + energy * 0.25;
      const drift = time * 0.6;
      ctx.beginPath();
      for (let point = 0; point <= POINTS; point++) {
        const wave =
          Math.sin(H3[point] + phase3) * Math.cos(H2[point] - drift) * swing +
          Math.sin(H7[point] + phase7) * ripple;
        const r = radius + wave * spread;
        const x = COS[point] * r;
        const y = SIN[point] * r * 0.95;
        if (!point) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
      }
      ctx.closePath();
      ctx.strokeStyle = this.strokes[ring];
      ctx.lineWidth = ring % 4 === 0 ? 1.15 : 0.65;
      ctx.stroke();
    }
    // Sparse dust catches the light without a particle allocation loop.
    // Kept as direct arc fills: blitting a rotated, alpha-blended offscreen
    // canvas measured slower than redrawing these each frame.
    ctx.rotate(time * 0.015);
    for (let dot = 0; dot < DOTS; dot++) {
      const angle = dot * 2.39996;
      const r = 105 + Math.sin(dot * 4.9) * 14 + Math.sin(time + dot) * 2;
      const alpha = 0.12 + (Math.sin(dot + time) + 1) * 0.13;
      ctx.fillStyle = 'hsla(' + hue + ',40%,75%,' + alpha + ')';
      ctx.beginPath();
      ctx.arc(Math.cos(angle) * r, Math.sin(angle) * r, dot % 5 === 0 ? 1 : 0.55, 0, Math.PI * 2);
      ctx.fill();
    }
    ctx.restore();
    if (!reduced) this.frame = requestAnimationFrame(this.draw);
  }

  destroy() {
    cancelAnimationFrame(this.frame);
    this.resize.disconnect();
    this.intersection.disconnect();
    document.removeEventListener('visibilitychange', this.resume);
    this.motion.removeEventListener('change', this.resume);
  }
}
