// A decoded frame replaces its predecessor gradually on the same map surface.
const motions = new WeakMap();
const initialCorners = [[158.60, 53.04], [158.64, 53.04], [158.64, 53.02], [158.60, 53.02]];
export const motionDuration = 620;

export function addMotionSource(map, id) {
  const canvas = document.createElement('canvas');
  canvas.width = canvas.height = 1;
  map.addSource(id, { type: 'canvas', canvas, coordinates: initialCorners, animate: false });
}

export function raster(map, id, bounds, url) {
  const source = map.getSource(id);
  const canvas = source.getCanvas();
  let motion = motions.get(source);
  if (!motion) {
    motion = {};
    motions.set(source, motion);
  }
  const key = bounds.join(',');
  if (motion.url === url && motion.key === key) return;
  motion.url = url;
  motion.key = key;
  const image = new Image();
  image.onload = () => {
    if (motion.url !== url || motion.key !== key) return;
    cancelAnimationFrame(motion.animation);
    const previous = document.createElement('canvas');
    const sameCoordinates = motion.drawnKey === key;
    if (sameCoordinates) {
      previous.width = canvas.width;
      previous.height = canvas.height;
      previous.getContext('2d').drawImage(canvas, 0, 0);
    }
    canvas.width = image.naturalWidth;
    canvas.height = image.naturalHeight;
    const { width, height } = canvas;
    const context = canvas.getContext('2d');
    if (!sameCoordinates) {
      const [w, s, e, n] = bounds;
      source.setCoordinates([[w, n], [e, n], [e, s], [w, s]]);
    }
    motion.drawnKey = key;
    const duration = sameCoordinates && !matchMedia('(prefers-reduced-motion: reduce)').matches ? motionDuration : 0;
    const start = performance.now();
    let lastDraw = -Infinity;
    source.play();
    function draw(now) {
      const mix = duration ? Math.min(1, (now-start)/duration) : 1;
      const eased = mix*mix*(3-2*mix);
      if (now-lastDraw >= 30 || mix === 1) {
        context.clearRect(0, 0, width, height);
        if (mix < 1) {
          context.globalAlpha = 1-eased;
          context.drawImage(previous, 0, 0, width, height);
        }
        context.globalCompositeOperation = 'lighter';
        context.globalAlpha = eased;
        context.drawImage(image, 0, 0, width, height);
        context.globalAlpha = 1;
        context.globalCompositeOperation = 'source-over';
        map.triggerRepaint();
        lastDraw = now;
      }
      if (mix < 1) motion.animation = requestAnimationFrame(draw);
      else motion.animation = requestAnimationFrame(() => source.pause());
    }
    draw(start);
  };
  image.src = url;
}
