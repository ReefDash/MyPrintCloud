// 3D view for the prepare/review screens: CC2 bed, the model (with cut planes) or sliced toolpaths.
// Printer coordinates throughout: millimetres, Z up, bed corner at the origin.
import * as THREE from "three";
import { OrbitControls } from "./vendor/addons/OrbitControls.js";
import { STLLoader } from "./vendor/addons/STLLoader.js";

export const BED = 256;
export const USABLE = [240, 240, 245];  // what the PC's splitter treats as printable
const PART_COLORS = [0x4fa3ff, 0xffa53d, 0x63d48a, 0xe86fd0, 0xf2dd4a, 0x8f7cff, 0x45d9d0, 0xff6f6f];
const TYPE_COLORS = {
  "Outer wall": 0xffa53d, "Inner wall": 0xf2dd4a, "Overhang wall": 0x3d7bff, "Sparse infill": 0xb2344b,
  "Internal solid infill": 0xb04ad6, "Solid infill": 0xb04ad6, "Top surface": 0xff5a5a, "Bottom surface": 0x6f7cff,
  "Bridge": 0x6fb0ff, "Internal Bridge": 0x6fb0ff, "Gap infill": 0xffffff, "Support": 0x63d48a,
  "Support interface": 0x2f9e5b, "Support transition": 0x2f9e5b, "Brim": 0x45d9d0, "Skirt": 0x45d9d0,
  "Prime tower": 0x999999, "Ironing": 0xff9ad5, "Other": 0x888888,
};
export const LEGEND = [["Outer wall", "Outer wall"], ["Inner wall", "Inner wall"], ["Sparse infill", "Infill"],
  ["Internal solid infill", "Solid infill"], ["Top surface", "Top"], ["Support", "Support"], ["Brim", "Brim"]]
  .map(([k, label]) => [label, "#" + TYPE_COLORS[k].toString(16).padStart(6, "0")]);

export class Viewer {
  constructor(host) {
    this.host = host;
    this.renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    host.appendChild(this.renderer.domElement);
    this.scene = new THREE.Scene();
    this.camera = new THREE.PerspectiveCamera(40, 1, 1, 5000);
    this.camera.up.set(0, 0, 1);
    this.controls = new OrbitControls(this.camera, this.renderer.domElement);
    this.controls.enableDamping = true;
    this.controls.target.set(BED / 2, BED / 2, 30);
    this.scene.add(new THREE.HemisphereLight(0xffffff, 0x445066, 1.6));
    const sun = new THREE.DirectionalLight(0xffffff, 1.6);
    sun.position.set(-150, -250, 400);
    this.scene.add(sun);
    this._bed();
    this.group = new THREE.Group();  // whatever is being shown (model or toolpaths)
    this.scene.add(this.group);
    this.home(120);
    this._ro = new ResizeObserver(() => this._resize());
    this._ro.observe(host);
    this._resize();
    const loop = () => { this._raf = requestAnimationFrame(loop); this.controls.update(); this.renderer.render(this.scene, this.camera); };
    loop();
  }

  _bed() {
    const plate = new THREE.Mesh(new THREE.PlaneGeometry(BED, BED), new THREE.MeshStandardMaterial({ color: 0x23272f, roughness: 0.9 }));
    plate.position.set(BED / 2, BED / 2, -0.05);
    this.scene.add(plate);
    const grid = new THREE.GridHelper(BED, 16, 0x3b4250, 0x3b4250);
    grid.rotation.x = Math.PI / 2;
    grid.position.set(BED / 2, BED / 2, 0);
    this.scene.add(grid);
    const pts = [[0, 0], [BED, 0], [BED, BED], [0, BED], [0, 0]].map(([x, y]) => new THREE.Vector3(x, y, 0.1));
    this.scene.add(new THREE.Line(new THREE.BufferGeometry().setFromPoints(pts), new THREE.LineBasicMaterial({ color: 0x6b7385 })));
    // the usable volume as a faint box
    const box = new THREE.Box3Helper(new THREE.Box3(new THREE.Vector3((BED - USABLE[0]) / 2, (BED - USABLE[1]) / 2, 0),
      new THREE.Vector3((BED + USABLE[0]) / 2, (BED + USABLE[1]) / 2, USABLE[2])), 0x39404d);
    this.scene.add(box);
  }

  home(height = 100) {
    const d = Math.max(260, height * 2.4);
    this.controls.target.set(BED / 2, BED / 2, Math.min(height / 2, 120));
    this.camera.position.set(BED / 2 - d * 0.35, BED / 2 - d * 0.95, d * 0.75);
  }

  _resize() {
    const w = this.host.clientWidth || 300, h = this.host.clientHeight || 300;
    this.renderer.setSize(w, h, false);
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
  }

  clear() {
    for (const c of [...this.group.children]) {
      this.group.remove(c);
      c.traverse((o) => { if (o.geometry && !o.geometry.userData.keep) o.geometry.dispose(); if (o.material) o.material.dispose(); });
    }
    this.mesh = null; this.paths = null; this.layout = false;
  }

  parseSTL(buf) {  // geometry kept across view changes (prepare <-> layout)
    const geo = new STLLoader().parse(buf);
    geo.computeVertexNormals();
    geo.userData.keep = true;
    return geo;
  }

  // ---- prepare: the model, scaled and centred on the bed, coloured by the part each triangle ends up in
  setModel(stl, size) {
    this.clear();
    const geo = stl.isBufferGeometry ? stl : this.parseSTL(stl);  // min corner already at the origin (the PC did that)
    this.baseSize = size;
    this.baseGeo = geo;
    const mat = new THREE.MeshStandardMaterial({ vertexColors: true, roughness: 0.55, metalness: 0.05 });
    this.mesh = new THREE.Mesh(geo, mat);
    this.group.add(this.mesh);
    this.planes = new THREE.Group();
    this.group.add(this.planes);
    this.update(1, []);
    this.home(size[2]);
    this._laidOut = false;
  }

  update(scale, cuts) {
    if (!this.mesh) return;
    const s = this.baseSize.map((v) => v * scale);
    this.mesh.scale.setScalar(scale);
    this.mesh.position.set(BED / 2 - s[0] / 2, BED / 2 - s[1] / 2, 0);
    // colour triangles by region (cut positions are mm from the scaled model's min corner)
    const pos = this.baseGeo.attributes.position, n = pos.count;
    let col = this.baseGeo.attributes.color;
    if (!col) { col = new THREE.BufferAttribute(new Float32Array(n * 3), 3); this.baseGeo.setAttribute("color", col); }
    const ax = { x: 0, y: 1, z: 2 };
    const sorted = { x: [], y: [], z: [] };
    cuts.forEach((c) => sorted[c.axis].push(c.at));
    Object.values(sorted).forEach((a) => a.sort((p, q) => p - q));
    const tmp = new THREE.Color();
    for (let t = 0; t < n; t += 3) {
      let key = 0;
      for (const a of ["x", "y", "z"]) {
        const list = sorted[a];
        if (!list.length) continue;
        const i = ax[a];
        const c = (pos.array[t * 3 + i] + pos.array[(t + 1) * 3 + i] + pos.array[(t + 2) * 3 + i]) / 3 * scale;
        let k = 0; while (k < list.length && c > list[k]) k++;
        key = key * 7 + k + 1;
      }
      tmp.setHex(PART_COLORS[key % PART_COLORS.length]);
      for (let v = 0; v < 3; v++) col.setXYZ(t + v, tmp.r, tmp.g, tmp.b);
    }
    col.needsUpdate = true;
    // cut planes
    this.planes.clear();
    for (const c of cuts) {
      const i = ax[c.axis], dims = [s[0] + 16, s[1] + 16, s[2] + 16];
      // PlaneGeometry lies in local XY; after the rotations below: X cut spans (Z, Y), Y cut spans (X, Z)
      const [w, h] = i === 0 ? [dims[2], dims[1]] : i === 1 ? [dims[0], dims[2]] : [dims[0], dims[1]];
      const plane = new THREE.Mesh(new THREE.PlaneGeometry(w, h),
        new THREE.MeshBasicMaterial({ color: 0xff3b5c, transparent: true, opacity: 0.28, side: THREE.DoubleSide, depthWrite: false }));
      const o = [BED / 2 - s[0] / 2, BED / 2 - s[1] / 2, 0];
      plane.position.set(BED / 2, BED / 2, s[2] / 2);
      if (i === 2) plane.position.z = c.at;
      if (i === 0) { plane.rotation.set(0, Math.PI / 2, 0); plane.position.x = o[0] + c.at; }
      if (i === 1) { plane.rotation.set(Math.PI / 2, 0, 0); plane.position.y = o[1] + c.at; }
      this.planes.add(plane);
    }
  }

  // ---- several models / copies: a rough plate layout (the slicer arranges them exactly)
  // units: [{geo (or null for a cut part), scale, size: [x, y, z], x, y, color}], x/y = min corner in usable-area mm
  setLayout(units) {
    this.clear();
    this.layout = true;
    const ox = (BED - USABLE[0]) / 2, oy = (BED - USABLE[1]) / 2;
    let w = 0, h = 0, top = 0;
    units.forEach((u) => { w = Math.max(w, u.x + u.size[0]); h = Math.max(h, u.y + u.size[1]); top = Math.max(top, u.size[2]); });
    const cx = ox + (USABLE[0] - w) / 2, cy = oy + (USABLE[1] - h) / 2;  // centre the group like the slicer does
    for (const u of units) {
      let obj;
      if (u.geo) {
        obj = new THREE.Mesh(u.geo, new THREE.MeshStandardMaterial({ color: u.color, roughness: 0.55, metalness: 0.05 }));
        obj.scale.setScalar(u.scale);
      } else {
        obj = new THREE.Mesh(new THREE.BoxGeometry(...u.size), new THREE.MeshStandardMaterial({ color: u.color, transparent: true, opacity: 0.55 }));
        obj.geometry.translate(u.size[0] / 2, u.size[1] / 2, u.size[2] / 2);
      }
      obj.position.set(cx + u.x, cy + u.y, 0);
      this.group.add(obj);
    }
    if (!this._laidOut) { this.home(top); this._laidOut = true; }
  }

  // ---- review: toolpaths from the PC's compact format (see bridge/preview.py)
  async setToolpaths(gzBuffer) {
    this.clear();
    const raw = await new Response(new Blob([gzBuffer]).stream().pipeThrough(new DecompressionStream("gzip"))).arrayBuffer();
    const dv = new DataView(raw);
    let o = 0;
    const tag = String.fromCharCode(dv.getUint8(0), dv.getUint8(1), dv.getUint8(2), dv.getUint8(3)); o = 4;
    if (tag !== "MPTP") throw new Error("Unknown preview format");
    o++;  // version
    const nTypes = dv.getUint8(o++), types = [];
    for (let i = 0; i < nTypes; i++) { const L = dv.getUint8(o++); types.push(new TextDecoder().decode(new Uint8Array(raw, o, L))); o += L; }
    const tcol = types.map((t) => new THREE.Color(TYPE_COLORS[t] ?? 0x888888));
    const nLayers = dv.getUint32(o, true); o += 4;
    // first pass: count segments
    let segs = 0, p = o;
    for (let l = 0; l < nLayers; l++) {
      p += 4; const nl = dv.getUint32(p, true); p += 4;
      for (let k = 0; k < nl; k++) { p += 1; const np = dv.getUint16(p, true); p += 2; segs += Math.max(0, np - 1); p += np * 4; }
    }
    const P = new Float32Array(segs * 6), C = new Float32Array(segs * 6);
    this.layerEnd = new Uint32Array(nLayers); this.layerZ = new Float32Array(nLayers);
    let v = 0; p = o;
    let maxZ = 0;
    for (let l = 0; l < nLayers; l++) {
      const z = dv.getFloat32(p, true); p += 4; this.layerZ[l] = z; maxZ = Math.max(maxZ, z);
      const nl = dv.getUint32(p, true); p += 4;
      for (let k = 0; k < nl; k++) {
        const c = tcol[dv.getUint8(p)] || tcol[tcol.length - 1]; p += 1;
        const np = dv.getUint16(p, true); p += 2;
        let px = dv.getInt16(p, true) / 100, py = dv.getInt16(p + 2, true) / 100; p += 4;
        for (let i = 1; i < np; i++) {
          const x = dv.getInt16(p, true) / 100, y = dv.getInt16(p + 2, true) / 100; p += 4;
          P[v * 3] = px; P[v * 3 + 1] = py; P[v * 3 + 2] = z; C[v * 3] = c.r; C[v * 3 + 1] = c.g; C[v * 3 + 2] = c.b; v++;
          P[v * 3] = x; P[v * 3 + 1] = y; P[v * 3 + 2] = z; C[v * 3] = c.r; C[v * 3 + 1] = c.g; C[v * 3 + 2] = c.b; v++;
          px = x; py = y;
        }
      }
      this.layerEnd[l] = v;
    }
    const geo = new THREE.BufferGeometry();
    geo.setAttribute("position", new THREE.BufferAttribute(P, 3));
    geo.setAttribute("color", new THREE.BufferAttribute(C, 3));
    this.paths = new THREE.LineSegments(geo, new THREE.LineBasicMaterial({ vertexColors: true }));
    this.group.add(this.paths);
    this.nLayers = nLayers;
    this.home(maxZ);
    let hmin = 9, hmax = 0;  // real layer heights (skip the first layer, which has its own height)
    for (let l = 2; l < nLayers; l++) { const d = this.layerZ[l] - this.layerZ[l - 1]; if (d > 0.01) { hmin = Math.min(hmin, d); hmax = Math.max(hmax, d); } }
    if (hmax === 0) { hmin = hmax = nLayers > 1 ? this.layerZ[1] - this.layerZ[0] : 0.2; }
    return { layers: nLayers, height: maxZ, hmin, hmax };
  }

  setLayer(l) {  // show layers 1..l
    if (!this.paths || !this.nLayers) return;
    l = Math.max(1, Math.min(this.nLayers, l));
    this.paths.geometry.setDrawRange(0, this.layerEnd[l - 1]);
    return this.layerZ[l - 1];
  }

  dispose() {
    cancelAnimationFrame(this._raf);
    this._ro.disconnect();
    this.clear();
    this.controls.dispose();
    this.renderer.dispose();
    this.renderer.domElement.remove();
  }
}
