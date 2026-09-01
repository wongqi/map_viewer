import * as THREE from "/assets/vendor/three.module.js";
import { OrbitControls } from "/assets/vendor/OrbitControls.js";

const viewport = document.getElementById("viewport");
const details = document.getElementById("details");
const loading = document.getElementById("loading");
const scene = new THREE.Scene();
scene.background = new THREE.Color(0x0b1117);
scene.add(new THREE.HemisphereLight(0xdbeeff, 0x182330, 1.8));
const camera = new THREE.PerspectiveCamera(45, 1, 0.1, 5000);
let renderer = null;
let controls = null;
try {
  renderer = new THREE.WebGLRenderer({ antialias: true, logarithmicDepthBuffer: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  viewport.appendChild(renderer.domElement);
  controls = new OrbitControls(camera, renderer.domElement);
} catch (error) {
  console.error("WebGL initialization failed", error);
  loading.textContent = `三维渲染不可用：${error.message || error}`;
  loading.classList.remove("done");
  document.getElementById("project-path").textContent = "请启用浏览器硬件加速或更换支持 WebGL 的浏览器";
}
if (controls) {
  controls.enableDamping = true;
  controls.dampingFactor = 0.08;
  controls.screenSpacePanning = false;
}
const root = new THREE.Group();
scene.add(root);
const mapGroup = new THREE.Group(), relationGroup = new THREE.Group(), transitionGroup = new THREE.Group(), cloudGroup = new THREE.Group();
root.add(mapGroup, relationGroup, transitionGroup, cloudGroup);
const raycaster = new THREE.Raycaster();
const pointer = new THREE.Vector2();
const objects = [];
let initialCamera = null;
let sceneData = null;
let transitionRecords = [];
let selectedTransitionId = null;
let waitingForMapClick = false;
let waitingForMoveClick = false;

function formatNumber(value, digits = 3) {
  return Number.isFinite(Number(value)) ? Number(value).toFixed(digits) : String(value);
}
function showDetails(title, rows) {
  details.innerHTML = `<h2>对象详情</h2><h3>${title}</h3><dl>${rows.map(([key, value]) => `<dt>${key}</dt><dd>${value}</dd>`).join("")}</dl>`;
}
function addLabel(text, position, color = "#dceeff") {
  const canvas = document.createElement("canvas");
  canvas.width = 512; canvas.height = 96;
  const ctx = canvas.getContext("2d");
  ctx.font = "bold 34px sans-serif"; ctx.fillStyle = color; ctx.textAlign = "center"; ctx.fillText(text, 256, 58);
  const texture = new THREE.CanvasTexture(canvas);
  const sprite = new THREE.Sprite(new THREE.SpriteMaterial({ map: texture, transparent: true, depthTest: false }));
  sprite.position.copy(position); sprite.scale.set(12, 2.25, 1); sprite.userData.label = true;
  root.add(sprite); return sprite;
}
function worldPosition(x, y, z) { return new THREE.Vector3(x, z, -y); }
function addMap(record) {
  const texture = new THREE.TextureLoader().load(record.image_url);
  texture.colorSpace = THREE.SRGBColorSpace;
  const geometry = new THREE.PlaneGeometry(record.size[0], record.size[1]);
  const material = new THREE.MeshBasicMaterial({ map: texture, transparent: true, opacity: .78, side: THREE.DoubleSide, depthWrite: false });
  const mesh = new THREE.Mesh(geometry, material);
  mesh.rotation.x = -Math.PI / 2;
  mesh.position.copy(worldPosition(record.world_origin[0] + record.size[0] / 2, record.world_origin[1] + record.size[1] / 2, record.z));
  mesh.userData = { kind: "map", record };
  mapGroup.add(mesh); objects.push(mesh);
  addLabel(record.map_id, mesh.position.clone().add(new THREE.Vector3(0, .8, 0)), record.z_source === "context" ? "#dceeff" : "#ffc978");
}
function addRelations(relations, records) {
  const byId = new Map(records.map(record => [record.map_id, record]));
  for (const relation of relations) {
    if (relation.from_map === "ROOT" || !byId.has(relation.from_map) || !byId.has(relation.to_map)) continue;
    const from = byId.get(relation.from_map), to = byId.get(relation.to_map);
    const a = worldPosition(relation.from_root[0], relation.from_root[1], from.z);
    const b = worldPosition(relation.to_root[0], relation.to_root[1], to.z);
    const line = new THREE.Line(new THREE.BufferGeometry().setFromPoints([a, b]), new THREE.LineBasicMaterial({ color: 0x42d3c5, transparent: true, opacity: .82 }));
    relationGroup.add(line);
    const direction = b.clone().sub(a).normalize();
    const arrow = new THREE.ArrowHelper(direction, a, a.distanceTo(b), 0x42d3c5, Math.min(2.5, a.distanceTo(b) * .15), .8);
    relationGroup.add(arrow);
  }
}
function transitionColor(type) {
  return ({ stairs: 0xff8f66, door: 0x8fc7ff, elevator: 0xd7a6ff, corridor: 0x79dfb8, manual: 0xffb454 })[type] || 0xffb454;
}
function addTransitions(transitions) {
  for (const transition of transitions) {
    const position = worldPosition(transition.world_x, transition.world_y, transition.world_z);
    const color = transitionColor(transition.type);
    const sphere = new THREE.Mesh(new THREE.SphereGeometry(.65, 12, 8), new THREE.MeshBasicMaterial({ color }));
    sphere.position.copy(position); sphere.userData = { kind: "transition", record: transition };
    transitionGroup.add(sphere); objects.push(sphere);
    const direction = new THREE.Vector3(Math.cos(transition.world_yaw_rad), 0, -Math.sin(transition.world_yaw_rad));
    transitionGroup.add(new THREE.ArrowHelper(direction, position, 2.2, color, .7, .45));
    if (transition.bidirectional) transitionGroup.add(new THREE.ArrowHelper(direction.clone().negate(), position, 1.45, color, .55, .35));
  }
}
function mapOptions() {
  return sceneData.maps.map(record => `<option value="${record.map_id}">${record.map_id}</option>`).join("");
}
function updateMapSelects() {
  document.getElementById("edit-from").innerHTML = mapOptions();
  document.getElementById("edit-to").innerHTML = mapOptions();
}
function fillEditor(record) {
  selectedTransitionId = record?.transition_id || null;
  document.getElementById("edit-id").value = record?.transition_id || "";
  document.getElementById("edit-type").value = record?.type || "manual";
  document.getElementById("edit-from").value = record?.from_map || sceneData.maps[0]?.map_id || "";
  document.getElementById("edit-to").value = record?.to_map || sceneData.maps[1]?.map_id || sceneData.maps[0]?.map_id || "";
  document.getElementById("edit-x").value = record ? record.world_x : "";
  document.getElementById("edit-y").value = record ? record.world_y : "";
  document.getElementById("edit-z").value = record ? record.world_z : "";
  document.getElementById("edit-yaw").value = record ? record.world_yaw_rad : "0";
  document.getElementById("edit-bidirectional").checked = record ? record.bidirectional : true;
  document.getElementById("edit-status").textContent = record ? `已选中 ${record.transition_id}` : "已准备新传送点";
}
function setNavigationLocked(locked) {
  if (controls) controls.enabled = !locked;
}
function setEditorEnabled(enabled) {
  const checkbox = document.getElementById("edit-mode");
  checkbox.checked = enabled;
  document.getElementById("editor-body").hidden = !enabled;
  if (!enabled) {
    waitingForMapClick = false;
    waitingForMoveClick = false;
    setNavigationLocked(false);
  }
}
function editorValue() {
  const number = id => Number(document.getElementById(id).value);
  return { transition_id: document.getElementById("edit-id").value.trim(), from_map: document.getElementById("edit-from").value, to_map: document.getElementById("edit-to").value, world_x: number("edit-x"), world_y: number("edit-y"), world_z: number("edit-z"), world_yaw_rad: number("edit-yaw"), bidirectional: document.getElementById("edit-bidirectional").checked, type: document.getElementById("edit-type").value };
}
function rebuildTransitions() {
  while (transitionGroup.children.length) transitionGroup.remove(transitionGroup.children[0]);
  for (let index = objects.length - 1; index >= 0; index--) if (objects[index].userData.kind === "transition") objects.splice(index, 1);
  addTransitions(transitionRecords);
}
function applyEditor() {
  const value = editorValue();
  if (!value.transition_id || value.from_map === value.to_map || [value.world_x, value.world_y, value.world_z, value.world_yaw_rad].some(Number.isNaN)) { document.getElementById("edit-status").textContent = "请填写有效 ID、不同地图和数值坐标"; return; }
  const existing = transitionRecords.findIndex(record => record.transition_id === selectedTransitionId);
  if (existing >= 0) {
    if (value.transition_id !== selectedTransitionId && transitionRecords.some(record => record.transition_id === value.transition_id)) { document.getElementById("edit-status").textContent = "ID 已存在"; return; }
    transitionRecords[existing] = value;
  } else {
    if (transitionRecords.some(record => record.transition_id === value.transition_id)) { document.getElementById("edit-status").textContent = "ID 已存在"; return; }
    transitionRecords.push(value);
  }
  selectedTransitionId = value.transition_id; rebuildTransitions(); document.getElementById("edit-status").textContent = "修改已应用到当前场景，点击保存写入 CSV";
}
function deleteEditor() {
  if (!selectedTransitionId) { document.getElementById("edit-status").textContent = "请先选中传送点"; return; }
  transitionRecords = transitionRecords.filter(record => record.transition_id !== selectedTransitionId);
  selectedTransitionId = null; rebuildTransitions(); fillEditor(null); document.getElementById("edit-status").textContent = "传送点已删除，点击保存写入 CSV";
}
async function saveEditor() {
  try {
    const response = await fetch("/api/transitions", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ transitions: transitionRecords }) });
    const result = await response.json();
    if (!response.ok || !result.ok) throw new Error(result.error || `保存失败 (${response.status})`);
    sceneData = result.scene; transitionRecords = sceneData.transitions.map(({ transition_id, from_map, to_map, world_x, world_y, world_z, world_yaw_rad, bidirectional, type }) => ({ transition_id, from_map, to_map, world_x, world_y, world_z, world_yaw_rad, bidirectional, type }));
    rebuildTransitions(); document.getElementById("edit-status").textContent = "已保存 transition_points.csv（原文件已备份为 .bak）";
  } catch (error) { document.getElementById("edit-status").textContent = `保存失败：${error.message}`; }
}
async function addPointCloud() {
  const response = await fetch("/api/pointcloud.bin");
  if (!response.ok) throw new Error(`point cloud request failed: ${response.status}`);
  const values = new Float32Array(await response.arrayBuffer());
  const positions = new Float32Array(values.length);
  const colors = new Float32Array(values.length);
  for (let i = 0; i < values.length; i += 3) {
    positions[i] = values[i]; positions[i + 1] = values[i + 2]; positions[i + 2] = -values[i + 1];
    const height = Math.max(0, Math.min(1, (values[i + 2] + 8) / 35));
    const color = new THREE.Color().setHSL(.56 - height * .2, .7, .72);
    colors[i] = color.r; colors[i + 1] = color.g; colors[i + 2] = color.b;
  }
  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute("position", new THREE.BufferAttribute(positions, 3));
  geometry.setAttribute("color", new THREE.BufferAttribute(colors, 3));
  cloudGroup.add(new THREE.Points(geometry, new THREE.PointsMaterial({ size: .11, vertexColors: true, sizeAttenuation: true, transparent: true, opacity: .75 })));
}
function fitCamera(records) {
  const box = new THREE.Box3().setFromObject(root);
  if (box.isEmpty()) return;
  const center = box.getCenter(new THREE.Vector3());
  const size = box.getSize(new THREE.Vector3());
  const radius = Math.max(size.length() * .62, 20);
  camera.position.copy(center).add(new THREE.Vector3(radius * .72, radius * .7, radius * .72));
  camera.near = Math.max(.1, radius / 1000); camera.far = Math.max(5000, radius * 12); camera.updateProjectionMatrix();
  if (controls) { controls.target.copy(center); controls.update(); }
  initialCamera = { position: camera.position.clone(), target: center.clone() };
}
function bindControls() {
  for (const [id, group] of [["toggle-maps", mapGroup], ["toggle-cloud", cloudGroup], ["toggle-relations", relationGroup], ["toggle-transitions", transitionGroup]]) {
    document.getElementById(id).addEventListener("change", event => { group.visible = event.target.checked; });
  }
  document.getElementById("reset-camera").addEventListener("click", () => {
    if (!initialCamera || !controls) return; camera.position.copy(initialCamera.position); controls.target.copy(initialCamera.target); controls.update();
  });
  updateMapSelects();
  document.getElementById("edit-mode").addEventListener("change", event => setEditorEnabled(event.target.checked));
  document.getElementById("new-edit").addEventListener("click", () => { setEditorEnabled(true); waitingForMapClick = true; waitingForMoveClick = false; setNavigationLocked(true); fillEditor(null); document.getElementById("edit-status").textContent = "请点击地图平面放置新传送点（视角已锁定）"; });
  document.getElementById("move-edit").addEventListener("click", () => { if (!selectedTransitionId) { document.getElementById("edit-status").textContent = "请先点击选中要移动的传送点"; return; } setEditorEnabled(true); waitingForMoveClick = true; waitingForMapClick = false; setNavigationLocked(true); document.getElementById("edit-status").textContent = "请点击地图平面设置新的 X/Y 坐标（视角已锁定）"; });
  document.getElementById("apply-edit").addEventListener("click", applyEditor);
  document.getElementById("delete-edit").addEventListener("click", deleteEditor);
  document.getElementById("save-edit").addEventListener("click", saveEditor);
  if (!renderer) return;
  renderer.domElement.addEventListener("pointerup", event => {
    const rect = renderer.domElement.getBoundingClientRect();
    pointer.x = ((event.clientX - rect.left) / rect.width) * 2 - 1; pointer.y = -((event.clientY - rect.top) / rect.height) * 2 + 1;
    raycaster.setFromCamera(pointer, camera);
    const hit = raycaster.intersectObjects(objects, false)[0];
    if (!hit) return;
    if (document.getElementById("edit-mode").checked && hit.object.userData.kind === "map" && waitingForMapClick) {
      const point = hit.point; document.getElementById("edit-x").value = point.x.toFixed(3); document.getElementById("edit-y").value = (-point.z).toFixed(3); document.getElementById("edit-z").value = point.y.toFixed(3); waitingForMapClick = false; setNavigationLocked(false); document.getElementById("edit-status").textContent = "已放置坐标，请选择起点/终点地图后点击应用修改"; return;
    }
    if (document.getElementById("edit-mode").checked && hit.object.userData.kind === "map" && waitingForMoveClick) {
      const record = transitionRecords.find(value => value.transition_id === selectedTransitionId);
      if (record) { record.world_x = Number(hit.point.x.toFixed(3)); record.world_y = Number((-hit.point.z).toFixed(3)); fillEditor(record); rebuildTransitions(); document.getElementById("edit-status").textContent = "已移动选中点，点击保存写入 CSV"; }
      waitingForMoveClick = false; setNavigationLocked(false); return;
    }
    if (document.getElementById("edit-mode").checked && hit.object.userData.kind === "transition") { fillEditor(hit.object.userData.record); return; }
    if (hit.object.userData.kind === "map") {
      const r = hit.object.userData.record;
      showDetails(r.map_id, [["世界原点", `${formatNumber(r.world_origin[0])}, ${formatNumber(r.world_origin[1])}, ${formatNumber(r.world_origin[2])}`], ["平面尺寸", `${formatNumber(r.size[0], 1)} × ${formatNumber(r.size[1], 1)} m`], ["分辨率", `${formatNumber(r.resolution, 3)} m/cell`], ["局部 origin", r.local_origin.map(v => formatNumber(v, 3)).join(", ")], ["高度来源", r.z_source]]);
    } else {
      const r = hit.object.userData.record;
      showDetails(r.transition_id, [["地图关系", `${r.from_map} → ${r.to_map}`], ["世界坐标", `${formatNumber(r.world_x)}, ${formatNumber(r.world_y)}, ${formatNumber(r.world_z)}`], ["yaw", `${formatNumber(r.world_yaw_rad)} rad`], ["类型", r.type], ["双向", r.bidirectional ? "是" : "否"], ["范围校验", r.in_range ? "通过" : "超出地图范围"]]);
    }
  });
}
async function init() {
  const response = await fetch("/api/scene");
  if (!response.ok) throw new Error(`scene request failed: ${response.status}`);
  const data = await response.json();
  document.getElementById("project-path").textContent = data.project_path || "地图项目";
  document.getElementById("map-count").textContent = data.maps.length;
  document.getElementById("transition-count").textContent = data.transitions.length;
  document.getElementById("point-count").textContent = data.pointcloud.count.toLocaleString();
  sceneData = data;
  transitionRecords = data.transitions.map(({ transition_id, from_map, to_map, world_x, world_y, world_z, world_yaw_rad, bidirectional, type }) => ({ transition_id, from_map, to_map, world_x, world_y, world_z, world_yaw_rad, bidirectional, type }));
  for (const record of data.maps) addMap(record);
  addRelations(data.relations, data.maps); addTransitions(data.transitions);
  if (data.pointcloud.available) { try { await addPointCloud(); } catch (error) { data.warnings.push(error.message); } }
  if (data.warnings.length) {
    const section = document.getElementById("warnings"); section.hidden = false;
    document.getElementById("warning-list").innerHTML = data.warnings.map(value => `<li>${value}</li>`).join("");
  }
  fitCamera(data.maps); bindControls(); loading.classList.add("done");
}
function resize() { const width = viewport.clientWidth, height = viewport.clientHeight; camera.aspect = width / height; camera.updateProjectionMatrix(); if (renderer) renderer.setSize(width, height, false); }
window.addEventListener("resize", resize); if (renderer) resize();
init().catch(error => {
  console.error("map viewer initialization failed", error);
  loading.textContent = `加载失败：${error.message}`;
  loading.classList.remove("done");
  document.getElementById("project-path").textContent = "请查看浏览器控制台中的错误信息";
});
function animate() { requestAnimationFrame(animate); if (controls) controls.update(); if (renderer) renderer.render(scene, camera); }
animate();
