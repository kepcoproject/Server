/**
 * 스마트 에너지 절약 시스템 — 3D 가상 건물 시연
 *
 * 건물 속 방마다 가상 센서 노드가 하나씩 붙어 있다. 실제 ESP32 펌웨어와 같은
 * 경로로 서버와 이야기한다.
 *
 *   측정값 보내기   POST /api/sensors/data                 5초마다, 상태가 바뀌면 곧바로
 *   명령 가져가기   GET  /api/spaces/{id}/actuator/latest  2초마다
 *
 * 그래서 대시보드에서 조명을 끄면 여기서도 꺼지고, 여기서 사람이 나가면
 * 대시보드에 낭비가 뜬다. 시연하는 동안 서버와 3D 화면이 같은 이야기를 한다.
 *
 * 서버 없이도 돌아간다(오프라인 시연). 대회장 네트워크가 불안할 때 쓴다.
 */
import * as THREE from "three";
import { OrbitControls } from "three/addons/OrbitControls.js";
import { CSS2DRenderer, CSS2DObject } from "three/addons/CSS2DRenderer.js";

// ---------------------------------------------------------------------------
// 치수 (단위: m 쯤으로 생각하면 된다)
// ---------------------------------------------------------------------------
const ROOM_W = 4.4;
const ROOM_D = 3.8;
const FLOOR_H = 2.9;
const SLAB = 0.22;
const WALL_T = 0.1;
const WALL_H = FLOOR_H - SLAB;
const BUILDING_GAP = 6;
const MAX_FLOORS = 12;
const MAX_ROOMS = 60;

// ---------------------------------------------------------------------------
// 전력·조도 모델
//
// 서버의 판정 기준에 맞춰 두었다. 여기 숫자를 바꾸면 3D 화면과 대시보드가
// 서로 다른 판정을 내릴 수 있다.
//   - 대시보드 낭비 표시: 재실이 아니고 전력 > 50W
//   - 서버 알림(자연광 낭비): 재실이어도 조도 >= 400lux 이고 전력 >= 10W
// ---------------------------------------------------------------------------
const LIGHT_W = 110; // 조명 (LED 패널 여덟 장 남짓)
const STANDBY_W = 3; // 아무것도 안 켜도 흐르는 대기전력
const DEVICE_BASE_W = 60; // 사람이 있으면 켜지는 기기 (PC, 프로젝터)
const PER_PERSON_W = 25;
const MAX_PEOPLE = 3;
const WASTE_POWER_W = 50;

// 밤에 조명만 켠 방이 400lux 를 넘으면 멀쩡한 방이 '자연광 낭비'로 잡힌다.
// 그래서 조명 조도는 그 선 아래로 둔다. 낮에는 창으로 들어오는 빛만으로 넘는다.
const LAMP_LUX = 300;
const NIGHT_LUX = 12;
const DAY_LUX = 560;
const DAYLIGHT_LUX = 400;

// ---------------------------------------------------------------------------
// 박자
// ---------------------------------------------------------------------------
const INGEST_MS = 5000; // 서버의 오프라인 판정(180초)보다 넉넉히 짧게
const POLL_MS = 2000; // 대시보드는 15초 만에 "응답 없음"으로 포기하므로 짧게
const OFFLINE_REMOTE_DELAY_MS = 900; // 오프라인 시연에서 원격 명령이 오가는 시간 흉내

// 서버 없이 시연할 때 쓰는 건물
const OFFLINE_SPACES = [
  { spaceId: "demo-1", code: "교무실", building: "본관", floor: 1 },
  { spaceId: "demo-2", code: "행정실", building: "본관", floor: 1 },
  { spaceId: "demo-3", code: "2-1", name: "2학년 1반", building: "본관", floor: 2 },
  { spaceId: "demo-4", code: "2-2", name: "2학년 2반", building: "본관", floor: 2 },
  { spaceId: "demo-5", code: "2-3", name: "2학년 3반", building: "본관", floor: 2 },
  { spaceId: "demo-6", code: "3-1", name: "3학년 1반", building: "본관", floor: 3 },
  { spaceId: "demo-7", code: "3-2", name: "3학년 2반", building: "본관", floor: 3 },
  { spaceId: "demo-8", code: "자습실", building: "본관", floor: 3 },
  { spaceId: "demo-9", code: "과학실", building: "별관", floor: 1 },
  { spaceId: "demo-10", code: "컴퓨터실", building: "별관", floor: 1 },
  { spaceId: "demo-11", code: "음악실", building: "별관", floor: 2 },
];

// 첫 화면이 볼만하도록 방마다 처음 상태를 정해 둔다: [사람 수, 조명]
const OFFLINE_START = [
  [2, true], [0, true], [1, true], [0, false], [3, true], [0, false],
  [0, true], [1, true], [0, false], [2, true], [0, false],
];

const PERSON_COLORS = [0x5b8def, 0xe0795b, 0x6cc497, 0xc792ea, 0xf2c14e, 0x58c4d6];

// ---------------------------------------------------------------------------
// 상태
// ---------------------------------------------------------------------------
const state = {
  mode: "idle", // idle | online | offline
  night: true,
  dayMix: 0, // 0 = 밤, 1 = 낮. 목표값을 향해 천천히 움직인다
  auto: false,
  rooms: [],
  selected: null,
  hovered: null,
  api: { prefix: "", token: null, refresh: null, role: null, volts: 220, voltsCalibrated: false },
  link: { ok: 0, fail: 0, lastError: "" },
};

const $ = (s) => document.querySelector(s);

// ---------------------------------------------------------------------------
// 박자 — 워커에서 받는다
//
// 브라우저는 보이지 않는 탭의 타이머를 늦춘다(크롬은 5분 뒤 1분에 한 번까지).
// 대시보드 탭을 보는 동안 이 탭의 노드가 명령을 못 가져가면 대시보드에는
// "응답 없음"이 뜬다. 워커의 타이머는 늦춰지지 않으므로 박자를 거기서 받는다.
// ---------------------------------------------------------------------------
const ticker = new Worker(
  URL.createObjectURL(
    new Blob(
      [
        "const ids={};onmessage=e=>{const{name,ms}=e.data;clearInterval(ids[name]);" +
          "if(ms)ids[name]=setInterval(()=>postMessage(name),ms)};",
      ],
      { type: "text/javascript" }
    )
  )
);
const tickHandlers = {};
ticker.onmessage = (e) => tickHandlers[e.data]?.();
function every(name, ms, fn) {
  tickHandlers[name] = fn;
  ticker.postMessage({ name, ms });
}
function stopEvery(name) {
  delete tickHandlers[name];
  ticker.postMessage({ name, ms: 0 });
}

// ---------------------------------------------------------------------------
// 장면
// ---------------------------------------------------------------------------
const container = $("#scene");

const renderer = new THREE.WebGLRenderer({ antialias: true, powerPreference: "high-performance" });
renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
renderer.setSize(window.innerWidth, window.innerHeight);
renderer.outputColorSpace = THREE.SRGBColorSpace;
renderer.toneMapping = THREE.ACESFilmicToneMapping;
renderer.shadowMap.enabled = true;
renderer.shadowMap.type = THREE.PCFSoftShadowMap;
container.appendChild(renderer.domElement);

// 방 이름표는 DOM 으로 띄운다. 글자가 선명하고 한글이 깨지지 않는다.
const labelRenderer = new CSS2DRenderer();
labelRenderer.setSize(window.innerWidth, window.innerHeight);
Object.assign(labelRenderer.domElement.style, {
  position: "absolute",
  inset: "0",
  pointerEvents: "none",
});
container.appendChild(labelRenderer.domElement);

const scene = new THREE.Scene();
scene.fog = new THREE.Fog(0x060a14, 70, 200);

const camera = new THREE.PerspectiveCamera(42, window.innerWidth / window.innerHeight, 0.1, 600);
camera.position.set(22, 18, 30);

const controls = new OrbitControls(camera, renderer.domElement);
controls.enableDamping = true;
controls.dampingFactor = 0.08;
controls.maxPolarAngle = Math.PI * 0.47; // 땅 밑으로 들어가지 않게
controls.minDistance = 5;
controls.maxDistance = 110;
controls.autoRotate = true;
controls.autoRotateSpeed = 0.35;

// 하늘빛 + 해(달)
const hemi = new THREE.HemisphereLight(0x3b4a7a, 0x0b0f1a, 0.3);
scene.add(hemi);

const sun = new THREE.DirectionalLight(0x9fb3ff, 0.4);
sun.position.set(-24, 34, 20);
sun.castShadow = true;
sun.shadow.mapSize.set(2048, 2048);
sun.shadow.bias = -0.0004;
sun.shadow.normalBias = 0.02;
scene.add(sun, sun.target);

// 땅
const groundMat = new THREE.MeshStandardMaterial({ color: 0x0c1220, roughness: 1 });
const ground = new THREE.Mesh(new THREE.PlaneGeometry(600, 600), groundMat);
ground.rotation.x = -Math.PI / 2;
ground.receiveShadow = true;
scene.add(ground);

const grid = new THREE.GridHelper(240, 120, 0x1d2a44, 0x131b2d);
grid.material.transparent = true;
grid.material.opacity = 0.55;
grid.position.y = 0.005;
scene.add(grid);

// 별 — 밤에만 보인다
const stars = (() => {
  const count = 700;
  const pos = new Float32Array(count * 3);
  for (let i = 0; i < count; i++) {
    const theta = Math.random() * Math.PI * 2;
    const phi = Math.random() * Math.PI * 0.42; // 하늘 위쪽에만
    const r = 260;
    pos[i * 3] = r * Math.sin(phi) * Math.cos(theta);
    pos[i * 3 + 1] = r * Math.cos(phi) + 10;
    pos[i * 3 + 2] = r * Math.sin(phi) * Math.sin(theta);
  }
  const geo = new THREE.BufferGeometry();
  geo.setAttribute("position", new THREE.BufferAttribute(pos, 3));
  const mat = new THREE.PointsMaterial({
    color: 0xdfe8ff,
    size: 1.1,
    sizeAttenuation: true,
    transparent: true,
    opacity: 0.85,
    fog: false,
    depthWrite: false,
  });
  return new THREE.Points(geo, mat);
})();
scene.add(stars);

// 건물들은 모두 여기 아래 붙는다. 다시 지을 때 이것만 비우면 된다.
const world = new THREE.Group();
scene.add(world);

// ---------------------------------------------------------------------------
// 재질 (여러 방이 함께 쓴다)
// ---------------------------------------------------------------------------
const MAT = {
  slab: new THREE.MeshStandardMaterial({ color: 0x3b465c, roughness: 0.92 }),
  backWall: new THREE.MeshStandardMaterial({ color: 0x2a354c, roughness: 0.95 }),
  // 밤에 콘크리트 모서리가 어둠에 묻히지 않도록 가는 선을 두른다 (건축 모형처럼)
  edge: new THREE.LineBasicMaterial({ color: 0x5b6b8f, transparent: true, opacity: 0.55 }),
  glass: new THREE.MeshStandardMaterial({
    color: 0x9fb4d8,
    transparent: true,
    opacity: 0.13,
    roughness: 0.08,
    metalness: 0.1,
    depthWrite: false,
  }),
  desk: new THREE.MeshStandardMaterial({ color: 0x5a4632, roughness: 0.8 }),
  board: new THREE.MeshStandardMaterial({ color: 0xdfe6ee, roughness: 0.4 }),
  skin: new THREE.MeshStandardMaterial({
    color: 0xf1d3b3,
    roughness: 0.7,
    emissive: 0xf1d3b3,
    emissiveIntensity: 0.07,
  }),
};

const GEO = {
  floor: new THREE.BoxGeometry(ROOM_W - WALL_T, 0.04, ROOM_D - WALL_T),
  backWall: new THREE.BoxGeometry(ROOM_W, WALL_H, WALL_T),
  sideWall: new THREE.BoxGeometry(WALL_T, WALL_H, ROOM_D),
  lamp: new THREE.BoxGeometry(ROOM_W * 0.46, 0.05, ROOM_D * 0.3),
  desk: new THREE.BoxGeometry(1.1, 0.72, 0.5),
  board: new THREE.BoxGeometry(1.8, 0.8, 0.03),
  outline: new THREE.EdgesGeometry(
    new THREE.BoxGeometry(ROOM_W - 0.08, WALL_H - 0.06, ROOM_D - 0.08)
  ),
  body: new THREE.CapsuleGeometry(0.17, 0.5, 4, 10),
  head: new THREE.SphereGeometry(0.14, 16, 12),
};

// 사람이 설 자리: 책상 앞 두 곳과 칠판 앞 한 곳 (방 안 좌표)
const SLOTS = [
  new THREE.Vector3(-1.05, 0, 0.62),
  new THREE.Vector3(1.05, 0, 0.62),
  new THREE.Vector3(0, 0, -0.75),
];
const DOOR = new THREE.Vector3(0, 0, ROOM_D / 2 + 0.7);

// 낮·밤 두 장면의 값. 사이를 dayMix 로 섞는다.
const SKY = {
  night: {
    bg: new THREE.Color(0x060a14),
    hemiSky: new THREE.Color(0x3b4a7a),
    hemiGround: new THREE.Color(0x0b0f1a),
    hemiI: 0.8,
    sun: new THREE.Color(0x9fb3ff),
    sunI: 0.9,
    ground: new THREE.Color(0x0c1220),
    slab: new THREE.Color(0x3b465c),
    backWall: new THREE.Color(0x2a354c),
    roomFloor: new THREE.Color(0x323b4f),
    exposure: 1.2,
    bulb: 9,
  },
  day: {
    bg: new THREE.Color(0xa9cdf0),
    hemiSky: new THREE.Color(0xd7ebff),
    hemiGround: new THREE.Color(0x6b7560),
    hemiI: 1.25,
    sun: new THREE.Color(0xfff1d6),
    sunI: 2.6,
    ground: new THREE.Color(0x7d8b72),
    slab: new THREE.Color(0xc4c9d2),
    backWall: new THREE.Color(0xe3e6ec),
    roomFloor: new THREE.Color(0xb8b0a2),
    exposure: 0.92,
    bulb: 3.5,
  },
};

// ---------------------------------------------------------------------------
// 방
// ---------------------------------------------------------------------------
function makeRoom(space, index) {
  const floor = Math.min(MAX_FLOORS, Math.max(1, Number(space.floor) || 1));
  return {
    spaceId: String(space.spaceId),
    code: space.code || String(space.spaceId),
    name: space.name || space.code || String(space.spaceId),
    building: space.building || "미지정",
    floor,
    index,
    // 가상 노드 이름. 디바이스 화면에 그대로 나오므로 가상인 것이 드러나게 짓는다.
    nodeKey: `SIM3D-${space.spaceId}`,
    occupants: 0,
    lightOn: false,
    powerW: STANDBY_W,
    lux: NIGHT_LUX,
    status: "off",
    // 서버와 주고받은 흔적
    lastCommandId: null,
    baselineSet: false,
    remotePending: null,
    lastSentAt: 0,
    sending: false,
    polling: false,
    sendSoon: null,
    // 화면
    glow: 0,
    flickerUntil: 0,
    people: [],
    obj: null,
  };
}

// 방의 측정값과 판정. 서버와 같은 기준으로 계산한다.
function measure(room) {
  let w = STANDBY_W + (room.lightOn ? LIGHT_W : 0);
  if (room.occupants > 0) w += DEVICE_BASE_W + PER_PERSON_W * room.occupants;
  w *= 1 + (Math.random() - 0.5) * 0.04; // 실제 계측값처럼 조금씩 흔들린다
  room.powerW = Math.round(w * 10) / 10;

  const ambient = state.night ? NIGHT_LUX : DAY_LUX;
  room.lux = Math.max(0, Math.round(ambient + (room.lightOn ? LAMP_LUX : 0) + (Math.random() - 0.5) * 16));

  if (room.occupants === 0 && room.powerW > WASTE_POWER_W) room.status = "waste";
  else if (room.occupants > 0 && room.lightOn && room.lux >= DAYLIGHT_LUX) room.status = "daywaste";
  else if (!room.lightOn) room.status = "off";
  else room.status = "ok";
}

// 낭비로 볼 전력. 공실이면 전부, 자연광 낭비면 조명 몫만.
function wastedWatts(room) {
  if (room.status === "waste") return room.powerW;
  if (room.status === "daywaste") return LIGHT_W;
  return 0;
}

const STATUS_TEXT = {
  ok: ["정상", "ok"],
  off: ["소등", ""],
  waste: ["공실 낭비", "bad"],
  daywaste: ["자연광 낭비", "warn"],
};

function buildRoom(room, origin) {
  const g = new THREE.Group();
  g.position.copy(origin);
  g.userData.room = room;

  const floorMat = new THREE.MeshStandardMaterial({ color: 0x323b4f, roughness: 0.75, emissive: 0x000000 });
  const floor = new THREE.Mesh(GEO.floor, floorMat);
  floor.position.y = 0.02;
  floor.receiveShadow = true;

  const back = new THREE.Mesh(GEO.backWall, MAT.backWall);
  back.position.set(0, WALL_H / 2, -ROOM_D / 2 + WALL_T / 2);
  back.receiveShadow = true;

  const left = new THREE.Mesh(GEO.sideWall, MAT.glass);
  left.position.set(-ROOM_W / 2 + WALL_T / 2, WALL_H / 2, 0);

  const board = new THREE.Mesh(GEO.board, MAT.board);
  board.position.set(0, 1.45, -ROOM_D / 2 + WALL_T + 0.02);

  const desks = SLOTS.slice(0, 2).map((slot) => {
    const d = new THREE.Mesh(GEO.desk, MAT.desk);
    d.position.set(slot.x, 0.36, slot.z - 0.55);
    d.castShadow = true;
    d.receiveShadow = true;
    return d;
  });

  const lampMat = new THREE.MeshStandardMaterial({
    color: 0x30343d,
    emissive: 0xffd9a0,
    emissiveIntensity: 0,
    roughness: 0.5,
  });
  const lamp = new THREE.Mesh(GEO.lamp, lampMat);
  lamp.position.y = WALL_H - 0.05;

  const bulb = new THREE.PointLight(0xffcf8a, 0, 8, 1.6);
  bulb.position.set(0, WALL_H - 0.4, 0.3);

  const wasteLine = new THREE.LineSegments(
    GEO.outline,
    new THREE.LineBasicMaterial({ color: 0xff5a5f, transparent: true, opacity: 0, depthTest: false })
  );
  wasteLine.position.y = WALL_H / 2;
  wasteLine.renderOrder = 2;

  const selectLine = new THREE.LineSegments(
    GEO.outline,
    new THREE.LineBasicMaterial({ color: 0x4c8dff, transparent: true, opacity: 0, depthTest: false })
  );
  selectLine.position.y = WALL_H / 2;
  selectLine.scale.setScalar(1.015);
  selectLine.renderOrder = 3;

  // 이름표
  const el = document.createElement("div");
  el.className = "label";
  el.innerHTML = `<span class="dot"></span><span class="c"></span><span class="w"></span>`;
  el.addEventListener("click", (e) => {
    e.stopPropagation();
    select(room);
  });
  const label = new CSS2DObject(el);
  // 천장 위에 띄우면 윗층 바닥 모서리에 붙어 보여 어느 방 것인지 헷갈린다.
  // 방 안쪽 윗부분, 앞쪽 가장자리에 둔다.
  label.position.set(0, WALL_H * 0.8, ROOM_D / 2 - 0.15);

  g.add(floor, back, left, board, ...desks, lamp, bulb, wasteLine, selectLine, label);

  // 눌렀을 때 이 방으로 알아볼 부분
  for (const m of [floor, back, left, board, lamp, ...desks]) m.userData.room = room;

  room.obj = { group: g, floorMat, lampMat, bulb, wasteLine, selectLine, label, labelEl: el, hit: [floor, back, left, board, lamp, ...desks] };
  world.add(g);
  return g;
}

function updateLabel(room) {
  const o = room.obj;
  if (!o) return;
  const el = o.labelEl;
  el.querySelector(".c").textContent = room.code;
  el.querySelector(".w").textContent = `${Math.round(room.powerW)}W`;
  const dot = el.querySelector(".dot");
  dot.className = "dot " + ({ ok: "ok", waste: "bad", daywaste: "warn" }[room.status] || "");
  el.classList.toggle("lit", room.lightOn);
  el.classList.toggle("waste", room.status === "waste");
  el.classList.toggle("daywaste", room.status === "daywaste");
  el.classList.toggle("selected", state.selected === room);
}

// ---------------------------------------------------------------------------
// 사람
// ---------------------------------------------------------------------------
function makePerson(color) {
  const g = new THREE.Group();
  const bodyMat = new THREE.MeshStandardMaterial({
    color,
    roughness: 0.6,
    emissive: color,
    emissiveIntensity: 0.1,
  });
  const body = new THREE.Mesh(GEO.body, bodyMat);
  body.position.y = 0.42;
  const head = new THREE.Mesh(GEO.head, MAT.skin);
  head.position.y = 0.93;
  body.castShadow = head.castShadow = true;
  g.add(body, head);
  g.userData.phase = Math.random() * Math.PI * 2;
  return g;
}

const tweens = [];
function tween(duration, onUpdate, onDone) {
  tweens.push({ t0: performance.now(), duration, onUpdate, onDone });
}
const ease = (t) => (t < 0.5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2);

function syncPeople(room) {
  const o = room.obj;
  if (!o) return;
  // 모자라면 문에서 걸어 들어온다
  while (room.people.length < room.occupants) {
    const slot = SLOTS[room.people.length];
    const p = makePerson(PERSON_COLORS[(room.index * 2 + room.people.length) % PERSON_COLORS.length]);
    p.position.copy(DOOR);
    p.lookAt(slot.x, 0, slot.z);
    for (const m of p.children) m.userData.room = room;
    o.group.add(p);
    room.people.push(p);
    const from = DOOR.clone();
    tween(1100, (k) => {
      p.position.lerpVectors(from, slot, ease(k));
    }, () => p.lookAt(p.position.x, 0, -ROOM_D));
  }
  // 넘치면 문으로 걸어 나간다
  while (room.people.length > room.occupants) {
    const p = room.people.pop();
    const from = p.position.clone();
    p.lookAt(DOOR);
    tween(1000, (k) => {
      p.position.lerpVectors(from, DOOR, ease(k));
      const s = k > 0.7 ? 1 - (k - 0.7) / 0.3 : 1;
      p.scale.setScalar(Math.max(0.001, s));
    }, () => o.group.remove(p));
  }
}

// ---------------------------------------------------------------------------
// 건물 짓기
// ---------------------------------------------------------------------------
function clearWorld() {
  for (const child of [...world.children]) world.remove(child);
  tweens.length = 0;
}

function buildWorld(spaces) {
  clearWorld();
  const rooms = spaces.slice(0, MAX_ROOMS).map((s, i) => makeRoom(s, i));

  // 건물별 → 층별로 묶는다. 방이 많은 건물을 왼쪽에 둔다.
  const byBuilding = new Map();
  for (const r of rooms) {
    if (!byBuilding.has(r.building)) byBuilding.set(r.building, []);
    byBuilding.get(r.building).push(r);
  }
  const buildings = [...byBuilding.entries()].sort(
    (a, b) => b[1].length - a[1].length || a[0].localeCompare(b[0], "ko")
  );

  let cursor = 0;
  for (const [name, list] of buildings) {
    const floors = new Map();
    for (const r of list) {
      if (!floors.has(r.floor)) floors.set(r.floor, []);
      floors.get(r.floor).push(r);
    }
    for (const arr of floors.values()) arr.sort((a, b) => a.code.localeCompare(b.code, "ko", { numeric: true }));
    const topFloor = Math.max(...floors.keys());
    const cols = Math.max(...[...floors.values()].map((a) => a.length));
    const width = cols * ROOM_W;

    const b = new THREE.Group();
    b.position.x = cursor;
    world.add(b);

    // 층 바닥과 지붕
    for (let f = 1; f <= topFloor + 1; f++) {
      const slabGeo = new THREE.BoxGeometry(width + 0.4, SLAB, ROOM_D + 0.4);
      const slab = new THREE.Mesh(slabGeo, MAT.slab);
      slab.position.set(width / 2, (f - 1) * FLOOR_H + SLAB / 2, 0);
      slab.castShadow = true;
      slab.receiveShadow = true;
      const slabEdge = new THREE.LineSegments(new THREE.EdgesGeometry(slabGeo), MAT.edge);
      slabEdge.position.copy(slab.position);
      b.add(slab, slabEdge);
    }
    // 기둥
    for (let f = 1; f <= topFloor; f++) {
      for (const x of [0.05, width - 0.05]) {
        for (const z of [-ROOM_D / 2 + 0.05, ROOM_D / 2 - 0.05]) {
          const col = new THREE.Mesh(new THREE.BoxGeometry(0.22, WALL_H, 0.22), MAT.slab);
          col.position.set(x, (f - 1) * FLOOR_H + SLAB + WALL_H / 2, z);
          col.castShadow = true;
          b.add(col);
        }
      }
    }
    // 방
    for (const [f, arr] of floors) {
      arr.forEach((r, i) => {
        const origin = new THREE.Vector3(i * ROOM_W + ROOM_W / 2, (f - 1) * FLOOR_H + SLAB, 0);
        const g = buildRoom(r, origin);
        world.remove(g);
        b.add(g);
      });
      // 맨 오른쪽 방의 오른쪽 벽
      const right = new THREE.Mesh(GEO.sideWall, MAT.glass);
      right.position.set(arr.length * ROOM_W - WALL_T / 2, (f - 1) * FLOOR_H + SLAB + WALL_H / 2, 0);
      b.add(right);
    }
    // 층 표시
    for (let f = 1; f <= topFloor; f++) {
      const el = document.createElement("div");
      el.className = "flabel";
      el.textContent = `${f}F`;
      const lab = new CSS2DObject(el);
      lab.position.set(-0.7, (f - 1) * FLOOR_H + FLOOR_H / 2, ROOM_D / 2);
      b.add(lab);
    }
    // 건물 이름
    const sign = document.createElement("div");
    sign.className = "bsign";
    sign.textContent = name;
    const signObj = new CSS2DObject(sign);
    signObj.position.set(width / 2, 0.05, ROOM_D / 2 + 1.6);
    b.add(signObj);

    cursor += width + BUILDING_GAP;
  }

  // 가운데로 모은다
  const total = cursor - BUILDING_GAP;
  world.position.x = -total / 2;

  // 그림자가 건물 전체를 덮게 맞춘다
  const box = new THREE.Box3().setFromObject(world);
  const size = box.getSize(new THREE.Vector3());
  const half = Math.max(size.x, size.y, size.z) * 0.75 + 6;
  Object.assign(sun.shadow.camera, { left: -half, right: half, top: half, bottom: -half, near: 1, far: 160 });
  sun.shadow.camera.updateProjectionMatrix();

  state.rooms = rooms;
  for (const r of rooms) {
    measure(r);
    updateLabel(r);
  }
  applySky(state.dayMix);
  frameAll(false);
  return rooms;
}

// 전체가 보이게 카메라를 맞춘다
function frameAll(animate = true) {
  const box = new THREE.Box3().setFromObject(world);
  if (box.isEmpty()) return;
  const center = box.getCenter(new THREE.Vector3());
  // 세로 화면에서는 정면에 가깝게 본다. 비스듬하면 건물이 한쪽으로 쏠린다.
  const dir = camera.aspect < 1
    ? new THREE.Vector3(0.22, 0.38, 0.9).normalize()
    : new THREE.Vector3(0.5, 0.42, 0.76).normalize();
  const toPos = center.clone().addScaledVector(dir, fitDistance(box, center, dir));
  moveCamera(toPos, center, animate ? 900 : 0);
}

// 건물 상자의 모서리 여덟 개가 모두 화면 안에 들어오는 가장 가까운 거리.
// 구(球)로 어림하면 비스듬한 시점이나 세로로 긴 화면에서 잘리거나 너무 멀어진다.
// 실제로 투영해 보며 이분 탐색한다. 상단 바만큼 내린 화면 중심(setViewOffset)도
// 투영에 들어 있으므로 따로 셈할 필요가 없다.
function fitDistance(box, center, dir) {
  const corners = [];
  for (const x of [box.min.x, box.max.x])
    for (const y of [box.min.y, box.max.y])
      for (const z of [box.min.z, box.max.z]) corners.push(new THREE.Vector3(x, y, z));

  const savedPos = camera.position.clone();
  const savedQuat = camera.quaternion.clone();
  const p = new THREE.Vector3();
  let lo = 2;
  let hi = 500;
  for (let i = 0; i < 24; i++) {
    const mid = (lo + hi) / 2;
    camera.position.copy(center).addScaledVector(dir, mid);
    camera.lookAt(center);
    camera.updateMatrixWorld();
    const fits = corners.every((c) => {
      p.copy(c).project(camera);
      return Math.abs(p.x) <= 0.88 && Math.abs(p.y) <= 0.86 && p.z < 1;
    });
    if (fits) hi = mid;
    else lo = mid;
  }
  camera.position.copy(savedPos);
  camera.quaternion.copy(savedQuat);
  camera.updateMatrixWorld();
  return hi;
}

function focusRoom(room) {
  const target = new THREE.Vector3();
  room.obj.group.getWorldPosition(target);
  target.y += WALL_H / 2;
  const offset = camera.position.clone().sub(controls.target);
  // 너무 멀면 가까이 당겨서 방이 잘 보이게 한다
  if (offset.length() > 26) offset.setLength(26);
  moveCamera(target.clone().add(offset), target, 700);
}

function moveCamera(toPos, toTarget, ms) {
  if (!ms) {
    camera.position.copy(toPos);
    controls.target.copy(toTarget);
    controls.update();
    return;
  }
  const fromPos = camera.position.clone();
  const fromTarget = controls.target.clone();
  tween(ms, (k) => {
    const e = ease(k);
    camera.position.lerpVectors(fromPos, toPos, e);
    controls.target.lerpVectors(fromTarget, toTarget, e);
  });
}

// ---------------------------------------------------------------------------
// 방에서 일어나는 일
// ---------------------------------------------------------------------------
function changed(room, { send = true } = {}) {
  const before = room.status;
  measure(room);
  if (room.status !== before) {
    if (room.status === "waste") log("bad", room, `공실 낭비 감지 · ${Math.round(room.powerW)}W`);
    if (room.status === "daywaste") log("warn", room, "자연광이 충분한데 조명이 켜져 있습니다");
    if (before === "waste" && room.status !== "waste") log("ok", room, "낭비 해소");
  }
  updateLabel(room);
  syncPeople(room);
  if (state.selected === room) renderPanel();
  renderHud();
  if (send) sendSoon(room);
}

function enter(room, n = 1, { byScenario = false } = {}) {
  const before = room.occupants;
  room.occupants = Math.min(MAX_PEOPLE, room.occupants + n);
  if (room.occupants === before) return;
  // 밤에 들어오면 사람이 불부터 켠다
  let lit = "";
  if (state.night && !room.lightOn) {
    room.lightOn = true;
    room.flickerUntil = performance.now() + 380;
    lit = " · 조명 켬";
  } else if (!state.night && !room.lightOn && byScenario && Math.random() < 0.5) {
    // 낮에도 습관처럼 불을 켜는 사람이 있다 — 자연광 낭비가 여기서 나온다
    room.lightOn = true;
    room.flickerUntil = performance.now() + 380;
    lit = " · 습관적으로 조명 켬";
  }
  log("ok", room, `입실 · 재실 ${room.occupants}명${lit}`);
  changed(room);
}

function leave(room, { forgetLight = null } = {}) {
  if (room.occupants === 0) return;
  room.occupants -= 1;
  if (room.occupants === 0) {
    // 마지막 사람이 나갈 때 불을 끄고 갈지. 시나리오에서는 가끔 깜빡한다.
    const forget = forgetLight ?? true;
    if (room.lightOn && !forget) {
      room.lightOn = false;
      log("", room, "마지막 사람이 조명을 끄고 퇴실");
    } else if (room.lightOn) {
      log("warn", room, "조명을 켜 둔 채 모두 퇴실");
    } else {
      log("", room, "퇴실 · 빈 방");
    }
  } else {
    log("", room, `퇴실 · 재실 ${room.occupants}명`);
  }
  changed(room);
}

function wallSwitch(room) {
  room.lightOn = !room.lightOn;
  if (room.lightOn) room.flickerUntil = performance.now() + 380;
  log("", room, `벽 스위치로 조명 ${room.lightOn ? "켬" : "끔"}`);
  changed(room);
}

// 서버(또는 오프라인 흉내)에서 내려온 명령을 실행한다
function applyRemote(room, on, source) {
  const who = source === "auto" ? "자동 절전" : "원격 제어";
  room.remotePending = null;
  if (room.lightOn === on) {
    log("accent", room, `${who} 명령 수신 — 이미 ${on ? "켜져" : "꺼져"} 있음`);
    if (state.selected === room) note(`노드가 명령을 받았습니다. 조명은 이미 ${on ? "켜져" : "꺼져"} 있었습니다.`, "ok");
    changed(room);
    return;
  }
  room.lightOn = on;
  if (on) room.flickerUntil = performance.now() + 380;
  log("accent", room, `${who} → 조명 ${on ? "켬" : "끔"}`);
  if (state.selected === room) note(`노드가 명령을 받아 조명을 ${on ? "켰" : "껐"}습니다.`, "ok");
  changed(room);
}

// ---------------------------------------------------------------------------
// 서버와 이야기하기
// ---------------------------------------------------------------------------
async function readEnvelope(res) {
  const text = await res.text();
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

// 로그인. 화면과 같은 주소에서 서빙할 때는 API 가 /client-api 아래에 있고,
// 로컬 개발에서는 접두사가 없다. 봉투({success, ...})가 돌아오는 쪽을 찾는다.
async function login(loginId, password) {
  let reached = false;
  for (const prefix of ["/client-api", ""]) {
    let res;
    try {
      res = await fetch(`${prefix}/auth/login`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ loginId, password }),
      });
    } catch {
      continue;
    }
    reached = true;
    const json = await readEnvelope(res);
    if (!json || typeof json.success !== "boolean") continue; // 이 접두사가 아니다
    if (!json.success) throw new Error(json.error?.message || "로그인에 실패했습니다");
    state.api.prefix = prefix;
    state.api.token = json.data.accessToken;
    state.api.refresh = json.data.refreshToken;
    state.api.role = json.data.user?.role || null;
    return json.data;
  }
  throw new Error(reached ? "시연 API 를 찾지 못했습니다" : "서버에 연결할 수 없습니다");
}

async function refreshToken() {
  if (!state.api.refresh) return false;
  const res = await fetch(`${state.api.prefix}/auth/refresh`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ refreshToken: state.api.refresh }),
  });
  const json = await readEnvelope(res);
  if (!json?.success) return false;
  state.api.token = json.data.accessToken;
  state.api.refresh = json.data.refreshToken || state.api.refresh;
  return true;
}

// 로그인이 필요한 호출. 토큰이 만료됐으면 한 번 갱신하고 다시 시도한다.
async function authed(path, { method = "GET", body } = {}, retried = false) {
  const res = await fetch(`${state.api.prefix}${path}`, {
    method,
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${state.api.token}`,
    },
    body: body ? JSON.stringify(body) : undefined,
  });
  const json = await readEnvelope(res);
  if (res.status === 401 && !retried && (await refreshToken())) {
    return authed(path, { method, body }, true);
  }
  return { status: res.status, json };
}

// 측정값 보내기 — 펌웨어의 postSensorData() 와 같은 본문
async function ingest(room) {
  if (state.mode !== "online" || room.sending) return;
  room.sending = true;
  measure(room);
  const amp = room.powerW / state.api.volts;
  try {
    const res = await fetch("/api/sensors/data", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        node_key: room.nodeKey,
        space_id: room.spaceId,
        occupancy: room.occupants > 0,
        light_lux: room.lux,
        current_amp: Math.round(amp * 10000) / 10000,
        source: "sim3d",
      }),
    });
    if (!res.ok) throw new Error(`측정값 전송 실패 (${res.status})`);
    const json = await res.json();
    // 서버는 전류 × 전압으로 전력을 계산한다. 서버의 전압 설정을 모르므로
    // 첫 응답으로 거꾸로 구해 둔다. 그래야 3D 숫자와 대시보드 숫자가 같다.
    if (!state.api.voltsCalibrated && json.powerW && amp > 0.1) {
      const v = json.powerW / amp;
      if (v > 50 && v < 500) state.api.volts = v;
      state.api.voltsCalibrated = true;
    }
    room.lastSentAt = Date.now();
    state.link.ok += 1;
    state.link.lastError = "";
  } catch (err) {
    state.link.fail += 1;
    state.link.lastError = err.message || "전송 실패";
  } finally {
    room.sending = false;
    renderHud();
  }
}

// 상태가 바뀌면 다음 박자를 기다리지 않고 곧 보낸다 (대시보드가 빨리 따라오게)
function sendSoon(room) {
  if (state.mode !== "online") return;
  clearTimeout(room.sendSoon);
  room.sendSoon = setTimeout(() => ingest(room), 250);
}

// 명령 가져가기 — 펌웨어의 폴링과 같다
async function pollActuator(room) {
  if (state.mode !== "online" || room.polling) return;
  room.polling = true;
  try {
    const res = await fetch(
      `/api/spaces/${encodeURIComponent(room.spaceId)}/actuator/latest?actuator_type=light`
    );
    if (res.status === 204) {
      room.baselineSet = true;
      return;
    }
    if (!res.ok) return;
    const cmd = await res.json();
    if (!room.baselineSet) {
      // 이 화면을 열기 전에 있던 명령은 실행하지 않는다. 어제 내려간 "끄기"가
      // 지금 켜 둔 방의 불을 끄면 시연하는 사람이 당황한다.
      room.baselineSet = true;
      room.lastCommandId = cmd.commandId;
      return;
    }
    if (cmd.commandId && cmd.commandId !== room.lastCommandId) {
      room.lastCommandId = cmd.commandId;
      applyRemote(room, cmd.action === "on", cmd.source);
    }
  } catch {
    // 폴링 실패는 조용히 넘긴다. 다음 박자에 다시 한다.
  } finally {
    room.polling = false;
  }
}

// 이 화면에서 원격 명령 보내기 — 대시보드의 제어 패널과 같은 API
async function sendRemote(room, on) {
  if (room.remotePending) return;
  room.remotePending = on ? "on" : "off";
  renderPanel();

  if (state.mode === "offline") {
    note("명령을 보냈습니다. 노드가 가져가길 기다리는 중…");
    log("faint", room, `원격 명령 전송 · 조명 ${on ? "켜기" : "끄기"}`);
    setTimeout(() => applyRemote(room, on, "manual"), OFFLINE_REMOTE_DELAY_MS);
    return;
  }

  note("명령을 보내는 중…");
  try {
    const { status, json } = await authed("/control/commands", {
      method: "POST",
      body: { spaceId: room.spaceId, action: "LIGHT", value: on ? "ON" : "OFF", overrideMinutes: 30 },
    });
    if (status === 202 && json?.success) {
      note("서버가 접수했습니다. 노드가 가져가길 기다리는 중…");
      log("faint", room, `원격 명령 접수 · 조명 ${on ? "켜기" : "끄기"}`);
      // 2초 폴링이 가져간다. 15초 안에 안 오면 포기한다 (대시보드와 같은 기준).
      const pending = room.remotePending;
      setTimeout(() => {
        if (room.remotePending === pending) {
          room.remotePending = null;
          if (state.selected === room) {
            note("15초 동안 노드가 응답하지 않았습니다.", "bad");
            renderPanel();
          }
        }
      }, 15000);
      return;
    }
    room.remotePending = null;
    const code = json?.error?.code;
    const msg =
      code === "E4030"
        ? "관리자 계정으로 로그인해야 원격 제어를 할 수 있습니다"
        : json?.error?.message || `명령을 보내지 못했습니다 (${status})`;
    note(msg, "bad");
    renderPanel();
  } catch {
    room.remotePending = null;
    note("서버에 연결할 수 없습니다", "bad");
    renderPanel();
  }
}

function startNetwork() {
  // 방마다 시간을 조금씩 어긋나게 보내 한꺼번에 몰리지 않게 한다
  every("ingest", INGEST_MS, () => {
    state.rooms.forEach((r, i) => setTimeout(() => ingest(r), i * 90));
  });
  every("poll", POLL_MS, () => {
    for (const r of state.rooms) pollActuator(r);
  });
  state.rooms.forEach((r, i) => setTimeout(() => ingest(r), i * 90));
  for (const r of state.rooms) pollActuator(r);
}

function stopNetwork() {
  stopEvery("ingest");
  stopEvery("poll");
}

// ---------------------------------------------------------------------------
// 자동 시나리오 — 사람들이 알아서 드나든다
// ---------------------------------------------------------------------------
let nextScenarioAt = 0;

function scenarioStep() {
  const rooms = state.rooms;
  if (!rooms.length) return;
  const room = rooms[Math.floor(Math.random() * rooms.length)];
  const roll = Math.random();

  if (room.occupants === 0) {
    if (roll < 0.62) enter(room, Math.random() < 0.35 ? 2 : 1, { byScenario: true });
    else if (room.lightOn && roll < 0.8) {
      // 빈 방에 불이 켜져 있는데 지나가던 사람이 끈다
      wallSwitch(room);
    }
    return;
  }
  if (roll < 0.45) {
    // 마지막 사람이 나갈 때 절반쯤은 불 끄는 걸 깜빡한다
    leave(room, { forgetLight: Math.random() < 0.5 });
  } else if (roll < 0.72 && room.occupants < MAX_PEOPLE) {
    enter(room, 1, { byScenario: true });
  } else if (!state.night && room.lightOn && roll < 0.85) {
    wallSwitch(room); // 햇빛이 충분하다는 걸 알아챈 사람
  }
}

function setAuto(on) {
  state.auto = on;
  $("#t-auto").setAttribute("aria-pressed", String(on));
  if (on) {
    nextScenarioAt = 0;
    every("scenario", 500, () => {
      const now = performance.now();
      if (now < nextScenarioAt) return;
      scenarioStep();
      nextScenarioAt = now + 3200 + Math.random() * 3300;
    });
    log("accent", null, "자동 시나리오 시작 — 사람들이 알아서 드나듭니다");
  } else {
    stopEvery("scenario");
    log("", null, "자동 시나리오 멈춤");
  }
}

// ---------------------------------------------------------------------------
// 낮·밤
// ---------------------------------------------------------------------------
function setNight(night) {
  state.night = night;
  $("#t-daynight").setAttribute("aria-pressed", String(!night));
  $("#t-daynight-icon").textContent = night ? "🌙" : "☀️";
  $("#t-daynight-text").textContent = night ? "밤" : "낮";
  log("", null, night ? "밤이 되었습니다 — 창밖 조도 12lux" : "낮이 되었습니다 — 창밖 조도 560lux");
  // 창밖 빛이 바뀌면 조도가 바뀌고, 판정도 바뀐다
  for (const r of state.rooms) changed(r);
}

const _c = new THREE.Color();
function applySky(mix) {
  const n = SKY.night;
  const d = SKY.day;
  _c.copy(n.bg).lerp(d.bg, mix);
  renderer.setClearColor(_c);
  scene.fog.color.copy(_c);
  hemi.color.copy(n.hemiSky).lerp(d.hemiSky, mix);
  hemi.groundColor.copy(n.hemiGround).lerp(d.hemiGround, mix);
  hemi.intensity = THREE.MathUtils.lerp(n.hemiI, d.hemiI, mix);
  sun.color.copy(n.sun).lerp(d.sun, mix);
  sun.intensity = THREE.MathUtils.lerp(n.sunI, d.sunI, mix);
  groundMat.color.copy(n.ground).lerp(d.ground, mix);
  MAT.slab.color.copy(n.slab).lerp(d.slab, mix);
  MAT.backWall.color.copy(n.backWall).lerp(d.backWall, mix);
  renderer.toneMappingExposure = THREE.MathUtils.lerp(n.exposure, d.exposure, mix);
  stars.material.opacity = 0.85 * (1 - mix);
  MAT.edge.opacity = 0.55 * (1 - mix) + 0.18 * mix;
  // 방 바닥도 따라간다. 밤 색 그대로 두면 낮에 불 꺼진 방만 짙은 남색으로 떠 보인다.
  _c.copy(n.roomFloor).lerp(d.roomFloor, mix);
  for (const r of state.rooms) r.obj?.floorMat.color.copy(_c);
  grid.material.opacity = 0.55 * (1 - mix) + 0.12 * mix;
}

// ---------------------------------------------------------------------------
// 화면 — 상단 숫자, 방 패널, 기록
// ---------------------------------------------------------------------------
function renderHud() {
  const rooms = state.rooms;
  const total = rooms.reduce((s, r) => s + r.powerW, 0);
  const waste = rooms.reduce((s, r) => s + wastedWatts(r), 0);
  const occupied = rooms.filter((r) => r.occupants > 0).length;
  $("#s-total").innerHTML = `${Math.round(total).toLocaleString()}<small>W</small>`;
  $("#s-waste").innerHTML = `${Math.round(waste).toLocaleString()}<small>W</small>`;
  $("#s-occ").innerHTML = `${occupied}<small>/ ${rooms.length}</small>`;

  const link = $("#s-link");
  if (state.mode === "offline") {
    link.innerHTML = `<span class="dot accent"></span><span>오프라인 시연</span>`;
  } else if (state.mode === "online") {
    const bad = state.link.lastError;
    link.innerHTML = bad
      ? `<span class="dot bad"></span><span title="${escapeHtml(bad)}">전송 실패</span>`
      : `<span class="dot ok"></span><span>연결됨 · ${state.link.ok.toLocaleString()}회</span>`;
  } else {
    link.innerHTML = `<span class="dot"></span><span>대기</span>`;
  }
}

function select(room) {
  const prev = state.selected;
  state.selected = room;
  if (prev && prev.obj) updateLabel(prev);
  if (room) {
    updateLabel(room);
    controls.autoRotate = false;
    focusRoom(room);
    note("");
    renderPanel();
    $("#panel").hidden = false;
  } else {
    $("#panel").hidden = true;
  }
}

function renderPanel() {
  const r = state.selected;
  if (!r) return;
  $("#p-title").textContent = r.code;
  $("#p-sub").textContent = [r.building, `${r.floor}층`, r.name !== r.code ? r.name : null]
    .filter(Boolean)
    .join(" · ");
  const [text, tone] = STATUS_TEXT[r.status];
  const chip = $("#p-chip");
  chip.className = `chip ${tone}`;
  chip.innerHTML = `<span class="dot ${tone}"></span><span>${text}</span>`;

  $("#p-occ").innerHTML = `${r.occupants}<small>명</small>`;
  $("#p-power").innerHTML = `${Math.round(r.powerW)}<small>W</small>`;
  $("#p-lux").innerHTML = `${r.lux}<small>lux</small>`;
  $("#p-light").textContent = r.lightOn ? "켜짐" : "꺼짐";

  $("#p-enter").disabled = r.occupants >= MAX_PEOPLE;
  $("#p-leave").disabled = r.occupants === 0;
  $("#p-switch").textContent = `💡 벽 스위치 ${r.lightOn ? "끄기" : "켜기"}`;
  $("#p-remote-on").disabled = !!r.remotePending;
  $("#p-remote-off").disabled = !!r.remotePending;

  const node =
    state.mode === "online"
      ? `가상 노드 ${r.nodeKey} · ${r.lastSentAt ? ago(r.lastSentAt) + " 전송" : "전송 전"}`
      : `가상 노드 ${r.nodeKey} · 오프라인 시연`;
  $("#p-meta").textContent = node;
}

function note(text, tone = "") {
  const el = $("#p-note");
  el.textContent = text;
  el.className = `note ${tone}`;
}

const logEl = $("#log");
function log(tone, room, text) {
  const empty = logEl.querySelector(".empty");
  if (empty) empty.remove();
  const li = document.createElement("li");
  const now = new Date();
  const hh = String(now.getHours()).padStart(2, "0");
  const mm = String(now.getMinutes()).padStart(2, "0");
  const ss = String(now.getSeconds()).padStart(2, "0");
  li.innerHTML = `<span class="dot ${tone}"></span><time>${hh}:${mm}:${ss}</time><span>${
    room ? `<b>${escapeHtml(room.code)}</b> · ` : ""
  }${escapeHtml(text)}</span>`;
  logEl.prepend(li);
  // 많이 쌓이면 건물 아래쪽을 가린다. 방금 일어난 일이 중요하다.
  while (logEl.children.length > 6) logEl.lastElementChild.remove();
}

function ago(ts) {
  const s = Math.max(0, Math.round((Date.now() - ts) / 1000));
  return s < 2 ? "방금" : `${s}초 전`;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

// ---------------------------------------------------------------------------
// 누르기
// ---------------------------------------------------------------------------
const raycaster = new THREE.Raycaster();
const pointer = new THREE.Vector2();
let downAt = null;
let lastInteraction = performance.now();

function pick(e) {
  const rect = renderer.domElement.getBoundingClientRect();
  pointer.x = ((e.clientX - rect.left) / rect.width) * 2 - 1;
  pointer.y = -((e.clientY - rect.top) / rect.height) * 2 + 1;
  raycaster.setFromCamera(pointer, camera);
  const targets = [];
  for (const r of state.rooms) {
    if (!r.obj) continue;
    targets.push(...r.obj.hit);
    for (const p of r.people) targets.push(...p.children);
  }
  const hit = raycaster.intersectObjects(targets, false)[0];
  return hit?.object.userData.room || null;
}

renderer.domElement.addEventListener("pointerdown", (e) => {
  downAt = { x: e.clientX, y: e.clientY };
  controls.autoRotate = false;
  lastInteraction = performance.now();
});
renderer.domElement.addEventListener("pointerup", (e) => {
  if (!downAt) return;
  const moved = Math.hypot(e.clientX - downAt.x, e.clientY - downAt.y);
  downAt = null;
  if (moved > 6) return; // 끌어서 돌린 것
  select(pick(e));
});
renderer.domElement.addEventListener("pointermove", (e) => {
  if (downAt) return;
  const room = pick(e);
  if (room !== state.hovered) {
    state.hovered = room;
    renderer.domElement.style.cursor = room ? "pointer" : "";
  }
});
renderer.domElement.addEventListener("wheel", () => {
  controls.autoRotate = false;
  lastInteraction = performance.now();
}, { passive: true });

window.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && state.selected) select(null);
});

$("#p-close").addEventListener("click", () => select(null));
$("#p-enter").addEventListener("click", () => state.selected && enter(state.selected));
$("#p-leave").addEventListener("click", () => state.selected && leave(state.selected, { forgetLight: true }));
$("#p-switch").addEventListener("click", () => state.selected && wallSwitch(state.selected));
$("#p-remote-on").addEventListener("click", () => state.selected && sendRemote(state.selected, true));
$("#p-remote-off").addEventListener("click", () => state.selected && sendRemote(state.selected, false));

$("#t-daynight").addEventListener("click", () => setNight(!state.night));
$("#t-auto").addEventListener("click", () => setAuto(!state.auto));
$("#t-view").addEventListener("click", () => {
  select(null);
  frameAll(true);
  controls.autoRotate = true;
});

// 상단 바가 화면 위쪽을 가린다. 그만큼 장면의 중심을 아래로 내려서
// 건물이 가려지지 않은 영역 한가운데 오게 한다. (휴대폰에서는 바가 세 줄이라 크다)
function fitViewport() {
  const w = window.innerWidth;
  const h = window.innerHeight;
  camera.aspect = w / h;
  const top = document.querySelector(".topbar").getBoundingClientRect().bottom;
  camera.setViewOffset(w, h, 0, -Math.round(top / 2), w, h);
  camera.updateProjectionMatrix();
  renderer.setSize(w, h);
  labelRenderer.setSize(w, h);
}
window.addEventListener("resize", fitViewport);
fitViewport();

// ---------------------------------------------------------------------------
// 매 장면
// ---------------------------------------------------------------------------
let last = performance.now();
renderer.setAnimationLoop((now) => {
  const dt = Math.min(0.05, (now - last) / 1000);
  last = now;
  const t = now / 1000;

  // 낮·밤이 천천히 바뀐다
  const target = state.night ? 0 : 1;
  if (Math.abs(state.dayMix - target) > 0.001) {
    state.dayMix += (target - state.dayMix) * Math.min(1, dt * 2.2);
    applySky(state.dayMix);
  }
  const bulbMax = THREE.MathUtils.lerp(SKY.night.bulb, SKY.day.bulb, state.dayMix);

  // 걷기·카메라 이동
  for (let i = tweens.length - 1; i >= 0; i--) {
    const tw = tweens[i];
    const k = Math.min(1, (now - tw.t0) / tw.duration);
    tw.onUpdate(k);
    if (k >= 1) {
      tweens.splice(i, 1);
      tw.onDone?.();
    }
  }

  for (const r of state.rooms) {
    const o = r.obj;
    if (!o) continue;
    // 불이 부드럽게 켜지고 꺼진다. 켤 때는 형광등처럼 두어 번 깜빡인다.
    let goal = r.lightOn ? 1 : 0;
    if (now < r.flickerUntil) goal = Math.random() < 0.55 ? 1 : 0.15;
    r.glow += (goal - r.glow) * Math.min(1, dt * (now < r.flickerUntil ? 30 : 7));
    o.bulb.intensity = r.glow * bulbMax;
    o.lampMat.emissiveIntensity = r.glow * 2.4;
    o.floorMat.emissive.setRGB(0.16 * r.glow, 0.11 * r.glow, 0.04 * r.glow);

    // 낭비 테두리가 숨 쉬듯 깜빡인다
    const pulse = 0.5 + 0.5 * Math.sin(t * 4.2);
    if (r.status === "waste") {
      o.wasteLine.material.color.setHex(0xff5a5f);
      o.wasteLine.material.opacity = 0.35 + 0.55 * pulse;
    } else if (r.status === "daywaste") {
      o.wasteLine.material.color.setHex(0xff9f43);
      o.wasteLine.material.opacity = 0.25 + 0.45 * pulse;
    } else {
      o.wasteLine.material.opacity = 0;
    }
    o.selectLine.material.opacity = state.selected === r ? 0.95 : state.hovered === r ? 0.4 : 0;

    // 서 있는 사람이 살짝 움직인다
    for (const p of r.people) p.children[0].position.y = 0.42 + Math.sin(t * 2 + p.userData.phase) * 0.012;
  }

  // 한동안 손대지 않으면 다시 천천히 돈다
  if (!controls.autoRotate && !state.selected && now - lastInteraction > 40000) controls.autoRotate = true;

  controls.update(dt);
  renderer.render(scene, camera);
  labelRenderer.render(scene, camera);
});

// 패널의 "n초 전"과 상단 숫자를 살아 있게
every("clock", 1000, () => {
  if (state.selected) renderPanel();
});

// ---------------------------------------------------------------------------
// 시작
// ---------------------------------------------------------------------------
function applyStart(rooms, starts) {
  rooms.forEach((r, i) => {
    const [people, light] = starts[i % starts.length];
    r.occupants = people;
    r.lightOn = light;
    r.glow = light ? 1 : 0;
    measure(r);
    updateLabel(r);
    syncPeople(r);
  });
  // 처음 등장은 걸어 들어오는 대신 제자리에 서 있게 한다
  for (const tw of tweens) tw.t0 = -1e9;
  renderHud();
}

function startOffline() {
  state.mode = "offline";
  $("#mode-label").textContent = "3D 가상 건물 · 오프라인 시연";
  $("#start").hidden = true;
  log("accent", null, `오프라인 시연 시작 — 방 ${state.rooms.length}곳`);
  renderHud();
  setAuto(true);
}

async function startOnline(loginId, password) {
  const user = await login(loginId, password);

  const spaces = await authed("/spaces");
  const items = spaces.json?.data?.items || [];
  if (!items.length) throw new Error("서버에 등록된 공간이 없습니다. 공간 관리에서 먼저 만들어 주세요");

  // 대시보드가 지금 보여주는 상태에서 출발한다. 그래야 3D 를 켜는 순간
  // 대시보드 숫자가 튀지 않는다.
  const map = await authed("/monitoring/occupancy-map");
  const now = new Map((map.json?.data?.spaces || []).map((s) => [s.spaceId, s]));

  stopNetwork();
  buildWorld(items);
  const starts = state.rooms.map((r, i) => {
    const s = now.get(r.spaceId);
    if (!s) return [0, false];
    if (s.occupied) return [1 + (i % 2), (s.powerW || 0) > DEVICE_BASE_W + PER_PERSON_W + 10 || state.night];
    return [0, !!s.wasteFlag];
  });
  applyStart(state.rooms, starts);

  state.mode = "online";
  $("#mode-label").textContent = `3D 가상 건물 · ${user.user?.name || loginId} 님 연결`;
  $("#start").hidden = true;
  log("ok", null, `서버 연결 — 가상 노드 ${state.rooms.length}개가 측정값을 보내기 시작합니다`);
  if (state.api.role !== "ADMIN") {
    log("warn", null, "관리자 계정이 아니라 이 화면에서 원격 제어는 할 수 없습니다");
  }
  startNetwork();
  renderHud();
}

$("#login").addEventListener("submit", async (e) => {
  e.preventDefault();
  const btn = $("#l-submit");
  const err = $("#l-error");
  err.textContent = "";
  btn.disabled = true;
  btn.textContent = "연결하는 중…";
  try {
    await startOnline($("#l-id").value.trim(), $("#l-pw").value);
  } catch (ex) {
    err.textContent = ex.message || "연결하지 못했습니다";
  } finally {
    btn.disabled = false;
    btn.textContent = "서버에 연결";
  }
});
$("#l-offline").addEventListener("click", startOffline);

// 시작 화면 뒤로 보이는 건물. 오프라인 시연을 고르면 그대로 이어서 쓴다.
buildWorld(OFFLINE_SPACES);
applyStart(state.rooms, OFFLINE_START);
applySky(0);
renderHud();
$("#l-id").focus();
