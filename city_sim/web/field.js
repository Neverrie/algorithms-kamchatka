import * as THREE from './vendor/three.module.min.js';

const COLD = new THREE.Color('#56d2fe');
const WARM = new THREE.Color('#ffd576');

export class FieldLayer {
  id = 'physical-field';
  type = 'custom';
  renderingMode = '3d';

  onAdd(map, gl) {
    this.map = map;
    this.camera = new THREE.Camera();
    this.scene = new THREE.Scene();
    this.renderer = new THREE.WebGLRenderer({ canvas: map.getCanvas(), context: gl });
    this.renderer.autoClear = false;
  }

  setData(payload, mode) {
    this.payload = payload;
    this.mode = mode;
    return this.refresh();
  }

  refresh() {
    if (this.mesh) {
      this.scene.remove(this.mesh);
      this.mesh.geometry.dispose();
      this.mesh.material.dispose();
      this.mesh = null;
    }
    if (!this.payload?.positions.length) {
      this.map.triggerRepaint();
      return null;
    }
    const { positions, fields, reference_height_m: height } = this.payload;
    const origin = maplibregl.MercatorCoordinate.fromLngLat(positions[0], 0);
    this.origin = new THREE.Matrix4().makeTranslation(origin.x, origin.y, 0);
    const xyz = new Float32Array(positions.length * 6);
    const anchors = new Float32Array(positions.length * 3);
    const direction = new Float32Array(positions.length * 3);
    const colors = [];
    const active = [];
    const values = this.mode === 'pressure' ? fields.pressure_pa : positions.map((_, i) => {
      const u = fields.velocity_east_mps[i];
      const v = fields.velocity_north_mps[i];
      return u == null || v == null ? null : Math.hypot(u, v, fields.velocity_up_mps?.[i] ?? 0);
    });
    const numeric = values.filter(Number.isFinite);
    if (!numeric.length) {
      this.map.triggerRepaint();
      return null;
    }
    const min = Math.min(...numeric);
    const max = Math.max(...numeric);
    for (const [i, lonLat] of positions.entries()) {
      if (!Number.isFinite(values[i])) continue;
      const base = this.map.getTerrain() ? this.map.queryTerrainElevation(lonLat) : 0;
      if (base == null) continue;
      const point = maplibregl.MercatorCoordinate.fromLngLat(lonLat, base + height);
      const color = COLD.clone().lerp(WARM, max === min ? .5 : (values[i] - min) / (max - min));
      const idx = active.length;
      active.push(i);
      anchors.set([point.x - origin.x, point.y - origin.y, point.z], idx * 3);
      colors.push(color.r, color.g, color.b);
      if (this.mode === 'wind') {
        const scale = point.meterInMercatorCoordinateUnits();
        direction.set([scale * fields.velocity_east_mps[i], -scale * fields.velocity_north_mps[i],
          scale * (fields.velocity_up_mps?.[i] ?? 0)], idx * 3);
        colors.push(color.r, color.g, color.b);
      }
    }
    if (!active.length) {
      this.map.triggerRepaint();
      return null;
    }
    const geometry = new THREE.BufferGeometry();
    if (this.mode === 'wind') {
      geometry.setAttribute('position', new THREE.BufferAttribute(xyz.subarray(0, active.length * 6), 3));
      this.mesh = new THREE.LineSegments(geometry, new THREE.LineBasicMaterial({
        vertexColors: true, transparent: true, opacity: .9
      }));
    } else {
      geometry.setAttribute('position', new THREE.BufferAttribute(anchors.subarray(0, active.length * 3), 3));
      this.mesh = new THREE.Points(geometry, new THREE.PointsMaterial({
        vertexColors: true, size: 6, sizeAttenuation: false, transparent: true, opacity: .85
      }));
    }
    geometry.setAttribute('color', new THREE.Float32BufferAttribute(colors, 3));
    this.mesh.frustumCulled = false;
    this.scene.add(this.mesh);
    Object.assign(this, { anchors, direction, active });
    this.map.triggerRepaint();
    return { min, max };
  }

  render(_gl, frame) {
    if (!this.mesh) return;
    if (this.mode === 'wind') {
      const attribute = this.mesh.geometry.getAttribute('position');
      const t = performance.now() / 1000;
      for (const [index, sample] of this.active.entries()) {
        const phase = (t + (sample % 17) / 17) % 1;
        const i = index * 3;
        const a = this.anchors;
        const d = this.direction;
        attribute.setXYZ(index * 2, a[i] + d[i] * phase, a[i + 1] + d[i + 1] * phase, a[i + 2] + d[i + 2] * phase);
        attribute.setXYZ(index * 2 + 1, a[i] + d[i] * (phase + .65),
          a[i + 1] + d[i + 1] * (phase + .65), a[i + 2] + d[i + 2] * (phase + .65));
      }
      attribute.needsUpdate = true;
      this.map.triggerRepaint();
    }
    this.camera.projectionMatrix.fromArray(frame.defaultProjectionData.mainMatrix).multiply(this.origin);
    this.renderer.resetState();
    this.renderer.render(this.scene, this.camera);
  }

  onRemove() {
    this.mesh?.geometry.dispose();
    this.mesh?.material.dispose();
    this.renderer.dispose();
  }
}
