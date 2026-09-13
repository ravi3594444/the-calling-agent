// A small Canvas 2D field. No WebGL, textures, animation framework or DOM work
// per frame. Live deformation uses the same audio graph as audible playback.
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
    // The asymmetric contours read as a soft, living ring, not a loading icon.
    for (let ring = 0; ring < 17; ring++) {
      const radius = 57 + ring * 3.05;
      ctx.beginPath();
      for (let point = 0; point <= 150; point++) {
        const angle = (point / 150) * Math.PI * 2;
        const wave =
          Math.sin(angle * 3 + time + ring * 0.19) *
            Math.cos(angle * 2 - time * 0.6) *
            (6 + energy) +
          Math.sin(angle * 7 - time * 1.7 + ring * 0.14) * (1.7 + energy * 0.25);
        const r = radius + wave * (0.4 + ring / 24);
        const x = Math.cos(angle) * r;
        const y = Math.sin(angle) * r * 0.95;
        if (!point) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
      }
      ctx.closePath();
      ctx.strokeStyle =
        'hsla(' +
        hue +
        ', ' +
        (speaking ? 51 : 32) +
        '%, ' +
        (65 + ring) +
        '%, ' +
        (0.1 + Math.sin((ring / 17) * Math.PI) * 0.36 + this.amplitude * 0.14) +
        ')';
      ctx.lineWidth = ring % 4 === 0 ? 1.15 : 0.65;
      ctx.stroke();
    }
    // Sparse dust catches the light without a particle allocation loop.
    for (let dot = 0; dot < 64; dot++) {
      const angle = dot * 2.39996 + time * 0.015;
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
