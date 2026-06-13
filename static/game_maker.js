/* ===========================================================================
 * CatoCode Game Maker
 * A vanilla-JS 2D game editor: scene editor + drag-and-drop logic blocks +
 * image/audio assets + live preview. Compiles to a self-contained project
 * (index.html + game.js + game.json) saved through the normal /api/projects
 * flow so games list/publish/play/remix like any HTML project.
 * ========================================================================= */
(function () {
  'use strict';

  // ----------------------------------------------------------------------- //
  // boot / state
  // ----------------------------------------------------------------------- //
  const BOOT = window.BOOT || { mode: 'new', project: null, loggedIn: false };
  const EDITING = BOOT.mode === 'edit' && !!BOOT.project;

  const AUDIO_EXTS = ['mp3', 'wav', 'ogg', 'm4a'];
  const IMAGE_EXTS = ['png', 'jpg', 'jpeg', 'gif', 'webp', 'svg', 'ico', 'bmp'];
  const extOf = (n) => (n.split('.').pop() || '').toLowerCase();
  const isAudio = (n) => AUDIO_EXTS.includes(extOf(n));
  const isImage = (n) => IMAGE_EXTS.includes(extOf(n));

  let _idc = 1;
  const uid = (p) => (p || 'o') + (_idc++).toString(36) + Math.random().toString(36).slice(2, 5);

  // Normalise settings to the stage (camera viewport) / level (full world) model.
  // Legacy projects only had width/height → those become both stage and level size.
  function migrateSettings(s) {
    s = s || {};
    const w = +s.stageWidth || +s.width || 480;
    const h = +s.stageHeight || +s.height || 320;
    s.stageWidth = Math.max(120, Math.min(1280, Math.round(w)));
    s.stageHeight = Math.max(120, Math.min(960, Math.round(h)));
    s.levelWidth = Math.max(s.stageWidth, Math.min(8000, Math.round(+s.levelWidth || +s.width || s.stageWidth)));
    s.levelHeight = Math.max(s.stageHeight, Math.min(4000, Math.round(+s.levelHeight || +s.height || s.stageHeight)));
    // Mirror to width/height for any legacy reader (engine canvas == stage size).
    s.width = s.stageWidth; s.height = s.stageHeight;
    if (s.gravity == null) s.gravity = 900;
    if (!s.background) s.background = '#bfe3ff';
    return s;
  }

  function starterDef() {
    const W = 480, H = 320;
    return {
      settings: { stageWidth: W, stageHeight: H, levelWidth: W, levelHeight: H, width: W, height: H, gravity: 900, background: '#bfe3ff' },
      variables: ['score', 'lives'],
      startScene: 0,
      scenes: [{
        name: 'Scene 1',
        objects: [
          { id: uid('g'), name: 'ground', type: 'block', x: 0, y: H - 24, w: W, h: 24,
            rotation: 0, color: '#6b8f3a', physics: { solid: true, gravity: false, vx: 0, vy: 0 }, scripts: [] },
          { id: uid('p'), name: 'player', type: 'block', x: 40, y: H - 24 - 44, w: 32, h: 44,
            rotation: 0, color: '#2563eb',
            physics: { solid: false, gravity: true, vx: 0, vy: 0 },
            scripts: [
              { op: 'on_start', args: {}, children: [
                { op: 'forever', args: {}, children: [
                  { op: 'if', args: { cond: { kind: 'key', key: 'ArrowLeft' } }, children: [ { op: 'change_x', args: { n: -3 }, children: [] } ] },
                  { op: 'if', args: { cond: { kind: 'key', key: 'ArrowRight' } }, children: [ { op: 'change_x', args: { n: 3 }, children: [] } ] },
                ] },
              ] },
              { op: 'on_key', args: { key: 'ArrowUp' }, children: [ { op: 'jump', args: { power: 380 }, children: [] } ] },
            ] },
        ],
      }],
    };
  }

  let def, assets, projectId, published, title, description;
  if (EDITING) {
    assets = Object.assign({}, BOOT.project.images || {});
    try { def = JSON.parse((BOOT.project.files || {})['game.json']); } catch (e) { def = null; }
    if (!def || !Array.isArray(def.scenes) || !def.scenes.length) def = starterDef();
    projectId = BOOT.project.id;
    published = !!BOOT.project.published;
    title = BOOT.project.title || '';
    description = BOOT.project.description || '';
  } else {
    assets = {};
    def = starterDef();
    projectId = null; published = false; title = ''; description = '';
  }
  if (!def.variables) def.variables = ['score', 'lives'];
  migrateSettings(def.settings);

  const stageW = () => def.settings.stageWidth;
  const stageH = () => def.settings.stageHeight;
  const levelW = () => def.settings.levelWidth;
  const levelH = () => def.settings.levelHeight;

  let sceneIdx = 0;
  let selId = null;
  let activeTab = 'scene';
  let zoom = 1;
  let snap = true;

  const scene = () => def.scenes[sceneIdx];
  const objs = () => scene().objects;
  const selected = () => objs().find((o) => o.id === selId) || null;
  const objNames = () => objs().map((o) => o.name);
  const imageAssets = () => Object.keys(assets).filter(isImage);
  const audioAssets = () => Object.keys(assets).filter(isAudio);

  // ----------------------------------------------------------------------- //
  // block specifications
  // ----------------------------------------------------------------------- //
  const CAT = {
    events:  { color: '#C9700A', label: 'Events' },
    motion:  { color: '#2563EB', label: 'Motion' },
    control: { color: '#D97706', label: 'Control' },
    sensing: { color: '#0891B2', label: 'Sensing' },
    looks:   { color: '#7C3AED', label: 'Looks' },
    sound:   { color: '#DB2777', label: 'Sound' },
    vars:    { color: '#EA580C', label: 'Variables' },
    flow:    { color: '#16A34A', label: 'Game flow' },
  };

  // field: { k, t:'num'|'text'|'select'|'cond', src, def }
  // label: array of strings and { f:'<fieldKey>' }
  const SPECS = {
    on_start:     { cat: 'events', hat: true,  pal: 'when game starts', label: ['when game starts'] },
    on_key:       { cat: 'events', hat: true,  pal: 'when key pressed', label: ['when key', { f: 'key' }, 'pressed'], fields: [{ k: 'key', t: 'select', src: 'keys', def: 'ArrowUp' }] },
    on_click:     { cat: 'events', hat: true,  pal: 'when clicked', label: ['when this is clicked'] },
    on_collision: { cat: 'events', hat: true,  pal: 'when touching', label: ['when touching', { f: 'target' }], fields: [{ k: 'target', t: 'select', src: 'objects_any', def: 'any' }] },

    move_by:      { cat: 'motion', pal: 'move x/y', label: ['move x', { f: 'dx' }, 'y', { f: 'dy' }], fields: [{ k: 'dx', t: 'num', def: 0 }, { k: 'dy', t: 'num', def: 0 }] },
    change_x:     { cat: 'motion', pal: 'change x', label: ['change x by', { f: 'n' }], fields: [{ k: 'n', t: 'num', def: 5 }] },
    change_y:     { cat: 'motion', pal: 'change y', label: ['change y by', { f: 'n' }], fields: [{ k: 'n', t: 'num', def: 5 }] },
    set_velocity: { cat: 'motion', pal: 'set velocity', label: ['set velocity x', { f: 'vx' }, 'y', { f: 'vy' }], fields: [{ k: 'vx', t: 'num', def: 0 }, { k: 'vy', t: 'num', def: 0 }] },
    jump:         { cat: 'motion', pal: 'jump', label: ['jump with power', { f: 'power' }], fields: [{ k: 'power', t: 'num', def: 380 }] },
    follow:       { cat: 'motion', pal: 'follow', label: ['follow', { f: 'target' }, 'at speed', { f: 'speed' }], fields: [{ k: 'target', t: 'select', src: 'objects', def: '' }, { k: 'speed', t: 'num', def: 120 }] },

    forever:      { cat: 'control', c: true, pal: 'forever', label: ['forever'] },
    repeat:       { cat: 'control', c: true, pal: 'repeat', label: ['repeat', { f: 'count' }], fields: [{ k: 'count', t: 'num', def: 10 }] },
    if:           { cat: 'control', c: true, pal: 'if', label: ['if', { f: 'cond' }], fields: [{ k: 'cond', t: 'cond' }] },
    if_else:      { cat: 'control', c: true, c2: true, pal: 'if / else', label: ['if', { f: 'cond' }], fields: [{ k: 'cond', t: 'cond' }] },
    wait:         { cat: 'control', pal: 'wait', label: ['wait', { f: 'secs' }, 'seconds'], fields: [{ k: 'secs', t: 'num', def: 0.5 }] },

    show:         { cat: 'looks', pal: 'show', label: ['show'] },
    hide:         { cat: 'looks', pal: 'hide', label: ['hide'] },
    switch_sprite:{ cat: 'looks', pal: 'switch sprite', label: ['switch sprite to', { f: 'sprite' }], fields: [{ k: 'sprite', t: 'select', src: 'images', def: '' }] },
    set_text:     { cat: 'looks', pal: 'set text', label: ['set text to', { f: 'value' }], fields: [{ k: 'value', t: 'text', def: 'Hello' }] },

    play_sound:   { cat: 'sound', pal: 'play sound', label: ['play sound', { f: 'sound' }], fields: [{ k: 'sound', t: 'select', src: 'audio', def: '' }] },
    play_music:   { cat: 'sound', pal: 'play music (loop)', label: ['play music', { f: 'music' }, '(loop)'], fields: [{ k: 'music', t: 'select', src: 'audio', def: '' }] },
    stop_sounds:  { cat: 'sound', pal: 'stop all sounds', label: ['stop all sounds'] },
    set_volume:   { cat: 'sound', pal: 'set volume', label: ['set volume to', { f: 'value' }, '%'], fields: [{ k: 'value', t: 'num', def: 80 }] },

    set_var:      { cat: 'vars', pal: 'set variable', label: ['set', { f: 'name' }, 'to', { f: 'value' }], fields: [{ k: 'name', t: 'select', src: 'vars', def: 'score' }, { k: 'value', t: 'text', def: '0' }] },
    change_var:   { cat: 'vars', pal: 'change variable', label: ['change', { f: 'name' }, 'by', { f: 'delta' }], fields: [{ k: 'name', t: 'select', src: 'vars', def: 'score' }, { k: 'delta', t: 'num', def: 1 }] },

    spawn:        { cat: 'flow', pal: 'spawn', label: ['spawn', { f: 'template' }, 'at x', { f: 'x' }, 'y', { f: 'y' }], fields: [{ k: 'template', t: 'select', src: 'objects', def: '' }, { k: 'x', t: 'num', def: 0 }, { k: 'y', t: 'num', def: 0 }] },
    destroy:      { cat: 'flow', pal: 'destroy', label: ['destroy', { f: 'target' }], fields: [{ k: 'target', t: 'select', src: 'self_objects', def: 'self' }] },
    go_to_scene:  { cat: 'flow', pal: 'go to scene', label: ['go to scene', { f: 'sceneName' }], fields: [{ k: 'sceneName', t: 'select', src: 'scenes', def: '' }] },
    win:          { cat: 'flow', pal: 'win game', label: ['win game —', { f: 'message' }], fields: [{ k: 'message', t: 'text', def: 'You win!' }] },
    lose:         { cat: 'flow', pal: 'game over', label: ['game over —', { f: 'message' }], fields: [{ k: 'message', t: 'text', def: 'Game over' }] },
  };

  const KEY_OPTS = [
    ['ArrowUp', '↑ Up'], ['ArrowDown', '↓ Down'], ['ArrowLeft', '← Left'], ['ArrowRight', '→ Right'],
    [' ', 'Space'], ['Enter', 'Enter'], ['a', 'A'], ['w', 'W'], ['s', 'S'], ['d', 'D'],
    ['z', 'Z'], ['x', 'X'],
  ];
  const COND_KINDS = [
    ['always', 'always'], ['touching', 'touching…'], ['key', 'key pressed…'],
    ['mouse', 'mouse pressed'], ['edge', 'touching edge'], ['var', 'variable…'],
  ];
  const OPS = [['>', '>'], ['<', '<'], ['>=', '≥'], ['<=', '≤'], ['==', '=']];

  function selectSource(src) {
    switch (src) {
      case 'keys': return KEY_OPTS;
      case 'objects': return objNames().map((n) => [n, n]);
      case 'objects_any': return [['any', 'any object']].concat(objNames().map((n) => [n, n]));
      case 'self_objects': return [['self', 'self']].concat(objNames().map((n) => [n, n]));
      case 'images': return [['', '(none)']].concat(imageAssets().map((n) => [n, n]));
      case 'audio': return [['', '(none)']].concat(audioAssets().map((n) => [n, n]));
      case 'vars': return (def.variables || []).map((v) => [v, v]).concat([['__new__', '＋ new variable…']]);
      case 'scenes': return def.scenes.map((s) => [s.name, s.name]);
      default: return [];
    }
  }

  function newBlock(op) {
    const spec = SPECS[op];
    const b = { op, args: {}, children: spec.c ? [] : undefined, children2: spec.c2 ? [] : undefined };
    (spec.fields || []).forEach((f) => {
      if (f.t === 'cond') b.args[f.k] = { kind: 'always' };
      else b.args[f.k] = f.def;
    });
    return b;
  }

  // ----------------------------------------------------------------------- //
  // generic helpers
  // ----------------------------------------------------------------------- //
  const $ = (id) => document.getElementById(id);
  function el(tag, cls, txt) { const e = document.createElement(tag); if (cls) e.className = cls; if (txt != null) e.textContent = txt; return e; }
  function makeSelect(options, value, onChange) {
    const s = el('select');
    options.forEach(([v, l]) => { const o = el('option', null, l); o.value = v; if (v === value) o.selected = true; s.appendChild(o); });
    s.addEventListener('change', () => { onChange(s.value); markDirty(); });
    return s;
  }
  let toastTimer;
  function toast(msg, err) {
    const t = $('toast'); t.textContent = msg; t.classList.toggle('error', !!err); t.classList.add('show');
    clearTimeout(toastTimer); toastTimer = setTimeout(() => t.classList.remove('show'), 2200);
  }
  function escapeHtml(s) { return String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])); }

  // Inline SVG icon set (currentColor, consistent stroke) for JS-rendered chrome.
  const ICONS = {
    sprite: '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="16" rx="2"/><circle cx="8.5" cy="9" r="1.5"/><path d="M21 15l-5-4-7 6"/></svg>',
    block: '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="5" width="18" height="14" rx="1.5"/><path d="M3 12h18"/><path d="M10 5v7"/><path d="M15 12v7"/></svg>',
    text: '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M6 5h12"/><path d="M12 5v14"/><path d="M9 19h6"/></svg>',
    camera: '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="7" width="13" height="10" rx="2"/><path d="M16 10.5l5-2.5v8l-5-2.5z"/></svg>',
    music: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M9 17V5l11-2v12"/><circle cx="6" cy="17" r="3"/><circle cx="17" cy="15" r="3"/></svg>',
    drag: '<svg width="13" height="13" viewBox="0 0 24 24" fill="currentColor" stroke="none"><circle cx="9" cy="6" r="1.6"/><circle cx="15" cy="6" r="1.6"/><circle cx="9" cy="12" r="1.6"/><circle cx="15" cy="12" r="1.6"/><circle cx="9" cy="18" r="1.6"/><circle cx="15" cy="18" r="1.6"/></svg>',
    x: '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg>',
    logic: '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="4" width="16" height="7" rx="1.5"/><rect x="4" y="13" width="11" height="7" rx="1.5"/></svg>',
  };
  const typeIcon = (t) => ICONS[t] || ICONS.block;
  // create an element whose innerHTML is an inline SVG icon
  function iconEl(tag, cls, name) { const e = el(tag, cls); e.innerHTML = ICONS[name] || ''; return e; }

  // ======================================================================= //
  //  THE GAME ENGINE  — written as a self-contained function and serialised
  //  via .toString() so the *same* code powers the live preview and the
  //  compiled game.js. No references to the editor's scope are allowed here.
  // ======================================================================= //
  function GAME_ENGINE() {
    window.startGame = function (DEF) {
      const canvas = document.getElementById('gameCanvas');
      const ctx = canvas.getContext('2d');
      const W = DEF.settings.stageWidth || DEF.settings.width || 480;   // stage = visible canvas
      const H = DEF.settings.stageHeight || DEF.settings.height || 320;
      const LW = Math.max(W, DEF.settings.levelWidth || W);             // level = full scrollable world
      const LH = Math.max(H, DEF.settings.levelHeight || H);
      canvas.width = W; canvas.height = H;
      const GRAV = DEF.settings.gravity == null ? 800 : DEF.settings.gravity;
      const BG = DEF.settings.background || '#bfe3ff';
      const AEXT = ['mp3', 'wav', 'ogg', 'm4a'];
      const isAud = (n) => AEXT.indexOf((n.split('.').pop() || '').toLowerCase()) >= 0;
      const assetURL = (n) => (window.__ASSETS__ && window.__ASSETS__[n]) || n;

      // ---- assets ----
      const images = {};
      function loadImage(name) {
        return new Promise((res) => { const im = new Image(); im.onload = () => { images[name] = im; res(); }; im.onerror = () => res(); im.src = assetURL(name); });
      }
      let actx = null, master = null; const buffers = {}; let activeSrcs = [];
      function initAudio() { if (actx) return; try { actx = new (window.AudioContext || window.webkitAudioContext)(); master = actx.createGain(); master.gain.value = 0.8; master.connect(actx.destination); } catch (e) {} }
      function loadAudio(name) { if (!actx) return Promise.resolve(); return fetch(assetURL(name)).then((r) => r.arrayBuffer()).then((b) => actx.decodeAudioData(b)).then((buf) => { buffers[name] = buf; }).catch(() => {}); }
      function playBuf(name, loop) { if (!actx || !buffers[name]) return; const s = actx.createBufferSource(); s.buffer = buffers[name]; s.loop = !!loop; s.connect(master); s.start(); activeSrcs.push(s); s.onended = () => { activeSrcs = activeSrcs.filter((x) => x !== s); }; }
      function stopAll() { activeSrcs.forEach((s) => { try { s.stop(); } catch (e) {} }); activeSrcs = []; }

      function collect() {
        const imgs = {}, auds = {};
        DEF.scenes.forEach((sc) => sc.objects.forEach((o) => {
          if (o.sprite) imgs[o.sprite] = 1;
          const walk = (arr) => { (arr || []).forEach((b) => {
            const a = b.args || {};
            if (a.sprite) imgs[a.sprite] = 1;
            if (a.sound) auds[a.sound] = 1;
            if (a.music) auds[a.music] = 1;
            walk(b.children); walk(b.children2);
          }); };
          (o.scripts || []).forEach((st) => walk(st.children));
        }));
        return { images: Object.keys(imgs).filter((n) => n && !isAud(n)), audio: Object.keys(auds).filter((n) => n && isAud(n)) };
      }

      // ---- runtime ----
      let RT = null, raf = 0, last = 0;
      function makeInstance(o) {
        const p = o.physics || {};
        return {
          name: o.name, type: o.type, x: o.x, y: o.y, w: o.w, h: o.h, rot: o.rotation || 0,
          sprite: o.sprite || null, color: o.color || '#3b82f6', text: o.text || '', fontSize: o.fontSize || 20,
          vx: p.vx || 0, vy: p.vy || 0, solid: !!p.solid, grav: !!p.gravity,
          visible: o.visible !== false, onGround: false, scripts: o.scripts || [], dead: false, _coll: {},
        };
      }
      function buildScene(idx) {
        const sc = DEF.scenes[idx];
        RT = { sceneIdx: idx, objs: [], vars: {}, keys: {}, mouseDown: false, dt: 0, threads: [], ended: false, next: null };
        (DEF.variables || []).forEach((v) => { RT.vars[v] = 0; });
        sc.objects.forEach((o) => RT.objs.push(makeInstance(o)));
        RT.objs.slice().forEach((inst) => startHandlers(inst, 'on_start'));
      }
      function startHandlers(inst, evType, key) {
        (inst.scripts || []).forEach((st) => {
          if (st.op !== evType) return;
          if (evType === 'on_key' && st.args && st.args.key !== key) return;
          RT.threads.push({ inst, gen: runStack(st.children || [], inst) });
        });
      }

      // ---- interpreter (generators yield once per frame for loops/waits) ----
      function* runStack(blocks, self) { for (let i = 0; i < blocks.length; i++) { if (RT.ended || self.dead) return; yield* runBlock(blocks[i], self); } }
      function* runBlock(b, self) {
        const a = b.args || {};
        switch (b.op) {
          case 'move_by': self.x += +a.dx || 0; self.y += +a.dy || 0; break;
          case 'change_x': self.x += +a.n || 0; break;
          case 'change_y': self.y += +a.n || 0; break;
          case 'set_velocity': self.vx = +a.vx || 0; self.vy = +a.vy || 0; break;
          case 'jump': if (self.onGround) { self.vy = -(+a.power || 0); self.onGround = false; } break;
          case 'follow': { const t = findOne(a.target); if (t) { const dx = (t.x + t.w / 2) - (self.x + self.w / 2), dy = (t.y + t.h / 2) - (self.y + self.h / 2); const d = Math.hypot(dx, dy) || 1; const sp = (+a.speed || 0) * RT.dt; self.x += dx / d * sp; self.y += dy / d * sp; } break; }
          case 'wait': { let t = +a.secs || 0; while (t > 0) { t -= RT.dt; yield; } break; }
          case 'forever': while (true) { yield* runStack(b.children || [], self); yield; if (RT.ended || self.dead) return; }
          case 'repeat': { const n = Math.max(0, Math.floor(+a.count || 0)); for (let i = 0; i < n; i++) { yield* runStack(b.children || [], self); yield; if (RT.ended || self.dead) return; } break; }
          case 'if': if (evalCond(a.cond, self)) yield* runStack(b.children || [], self); break;
          case 'if_else': if (evalCond(a.cond, self)) yield* runStack(b.children || [], self); else yield* runStack(b.children2 || [], self); break;
          case 'show': self.visible = true; break;
          case 'hide': self.visible = false; break;
          case 'switch_sprite': self.sprite = a.sprite || null; break;
          case 'set_text': self.text = subVars(a.value, self); break;
          case 'play_sound': playBuf(a.sound, false); break;
          case 'play_music': playBuf(a.music, true); break;
          case 'stop_sounds': stopAll(); break;
          case 'set_volume': if (master) master.gain.value = Math.max(0, Math.min(1, (+a.value || 0) / 100)); break;
          case 'set_var': RT.vars[a.name] = numOr(a.value); break;
          case 'change_var': RT.vars[a.name] = (+RT.vars[a.name] || 0) + (+a.delta || 0); break;
          case 'spawn': doSpawn(a.template, +a.x || 0, +a.y || 0); break;
          case 'destroy': if (a.target === 'self') self.dead = true; else RT.objs.forEach((o) => { if (o.name === a.target) o.dead = true; }); break;
          case 'go_to_scene': { const i = DEF.scenes.findIndex((s) => s.name === a.sceneName); if (i >= 0) RT.next = i; break; }
          case 'win': endGame(a.message || 'You win!', true); break;
          case 'lose': endGame(a.message || 'Game over', false); break;
          default: break;
        }
      }
      function numOr(v) { const n = parseFloat(v); return isNaN(n) ? v : n; }
      function subVars(v, self) {
        let s = String(v == null ? '' : v);
        s = s.replace(/\$(\w+)/g, (m, k) => (k in RT.vars ? RT.vars[k] : m));
        return s;
      }
      function findOne(name) { return RT.objs.find((o) => !o.dead && o.name === name) || null; }
      function overlap(a, b) { return a.x < b.x + b.w && a.x + a.w > b.x && a.y < b.y + b.h && a.y + a.h > b.y; }
      function touchingAny(self, target) {
        for (const o of RT.objs) { if (o === self || o.dead) continue; if (target === 'any' || o.name === target) { if (overlap(self, o)) return true; } }
        return false;
      }
      function evalCond(c, self) {
        if (!c) return false;
        switch (c.kind) {
          case 'always': return true;
          case 'touching': return touchingAny(self, c.target);
          case 'key': return !!RT.keys[c.key];
          case 'mouse': return RT.mouseDown;
          case 'edge': return self.x < 0 || self.y < 0 || self.x + self.w > LW || self.y + self.h > LH;
          case 'var': { const v = +RT.vars[c.var] || 0, t = +c.value || 0; switch (c.op) { case '>': return v > t; case '<': return v < t; case '>=': return v >= t; case '<=': return v <= t; default: return v === t; } }
          default: return false;
        }
      }
      function doSpawn(name, x, y) {
        const tmpl = DEF.scenes[RT.sceneIdx].objects.find((o) => o.name === name);
        if (!tmpl) return;
        const inst = makeInstance(Object.assign({}, tmpl, { x, y }));
        RT.objs.push(inst); startHandlers(inst, 'on_start');
      }

      // ---- physics ----
      function physics() {
        const solids = RT.objs.filter((o) => o.solid && !o.dead);
        for (const o of RT.objs) {
          if (o.dead || o.solid) continue;
          if (o.grav) o.vy += GRAV * RT.dt;
          // X
          o.x += o.vx * RT.dt;
          for (const s of solids) { if (overlap(o, s)) { if (o.vx > 0) o.x = s.x - o.w; else if (o.vx < 0) o.x = s.x + s.w; o.vx = 0; } }
          // Y
          o.y += o.vy * RT.dt; o.onGround = false;
          for (const s of solids) { if (overlap(o, s)) { if (o.vy > 0) { o.y = s.y - o.h; o.onGround = true; } else if (o.vy < 0) { o.y = s.y + s.h; } o.vy = 0; } }
        }
      }
      function collisions() {
        RT.objs.forEach((inst) => {
          if (inst.dead) return;
          (inst.scripts || []).forEach((st, si) => {
            if (st.op !== 'on_collision') return;
            const now = touchingAny(inst, (st.args && st.args.target) || 'any');
            const was = inst._coll[si];
            if (now && !was) RT.threads.push({ inst, gen: runStack(st.children || [], inst) });
            inst._coll[si] = now;
          });
        });
      }

      // ---- main loop ----
      function tick(ts) {
        if (!RT || RT.ended) return;
        if (RT.next != null) { const n = RT.next; buildScene(n); }
        RT.dt = last ? Math.min(0.05, (ts - last) / 1000) : 0; last = ts;
        const queue = RT.threads; RT.threads = [];
        for (const t of queue) { if (t.inst.dead) continue; const r = t.gen.next(); if (!r.done) RT.threads.push(t); }
        physics();
        collisions();
        RT.objs = RT.objs.filter((o) => !o.dead);
        render();
        raf = requestAnimationFrame(tick);
      }
      function startLoop() { last = 0; cancelAnimationFrame(raf); raf = requestAnimationFrame(tick); }

      // ---- render ----
      function clampCam(x, y) {
        return { x: Math.max(0, Math.min(LW - W, x)), y: Math.max(0, Math.min(LH - H, y)) };
      }
      function camOffset() {
        const cam = RT.objs.find((o) => o.type === 'camera');
        if (!cam) return clampCam(0, 0);
        const tgt = RT.objs.find((o) => o.name === cam.text) || RT.objs.find((o) => o.grav);
        if (!tgt) return clampCam(0, 0);
        return clampCam((tgt.x + tgt.w / 2) - W / 2, (tgt.y + tgt.h / 2) - H / 2);
      }
      function drawObj(o) {
        if (o.type === 'camera') return;
        ctx.save();
        ctx.translate(o.x + o.w / 2, o.y + o.h / 2);
        if (o.rot) ctx.rotate(o.rot * Math.PI / 180);
        if (o.sprite && images[o.sprite]) ctx.drawImage(images[o.sprite], -o.w / 2, -o.h / 2, o.w, o.h);
        else if (o.type === 'text') { ctx.fillStyle = o.color || '#0f172a'; ctx.font = (o.fontSize || 20) + 'px Inter, system-ui, sans-serif'; ctx.textBaseline = 'middle'; ctx.fillText(o.text || '', -o.w / 2, 0); }
        else { ctx.fillStyle = o.color || '#3b82f6'; ctx.fillRect(-o.w / 2, -o.h / 2, o.w, o.h); }
        ctx.restore();
      }
      function render() {
        ctx.setTransform(1, 0, 0, 1, 0, 0);
        ctx.fillStyle = BG; ctx.fillRect(0, 0, W, H);
        const cam = camOffset();
        ctx.save(); ctx.translate(-cam.x, -cam.y);
        RT.objs.forEach((o) => { if (o.visible) drawObj(o); });
        ctx.restore();
        ctx.fillStyle = 'rgba(15,23,42,.85)'; ctx.font = '14px Inter, system-ui, sans-serif'; ctx.textBaseline = 'alphabetic';
        let yy = 18; Object.keys(RT.vars).forEach((k) => { ctx.fillText(k + ': ' + RT.vars[k], 8, yy); yy += 17; });
      }

      // ---- overlays ----
      function overlay(text, sub, onClick) {
        cancelAnimationFrame(raf);
        ctx.setTransform(1, 0, 0, 1, 0, 0);
        if (!RT) { ctx.fillStyle = BG; ctx.fillRect(0, 0, W, H); }
        ctx.fillStyle = 'rgba(15,23,42,.55)'; ctx.fillRect(0, 0, W, H);
        ctx.fillStyle = '#fff'; ctx.textAlign = 'center';
        ctx.font = 'bold 26px Inter, system-ui, sans-serif'; ctx.fillText(text, W / 2, H / 2 - 6);
        ctx.font = '14px Inter, system-ui, sans-serif'; ctx.fillText(sub || '', W / 2, H / 2 + 22);
        ctx.textAlign = 'left';
        const handler = () => { canvas.removeEventListener('pointerdown', handler); onClick(); };
        canvas.addEventListener('pointerdown', handler);
      }
      function endGame(msg, won) { if (!RT) return; RT.ended = true; cancelAnimationFrame(raf); overlay(msg, 'Click to play again', boot); }

      // ---- input ----
      const PREVENT = ['ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight', ' '];
      window.addEventListener('keydown', (e) => { if (!RT) return; if (!RT.keys[e.key]) RT.objs.slice().forEach((o) => startHandlers(o, 'on_key', e.key)); RT.keys[e.key] = true; if (PREVENT.indexOf(e.key) >= 0) e.preventDefault(); });
      window.addEventListener('keyup', (e) => { if (RT) RT.keys[e.key] = false; });
      // Canvas is object-fit:contain inside its box, so map through the letterbox.
      function ptr(e) {
        const r = canvas.getBoundingClientRect();
        const scale = Math.min(r.width / canvas.width, r.height / canvas.height) || 1;
        const dw = canvas.width * scale, dh = canvas.height * scale;
        const offX = r.left + (r.width - dw) / 2, offY = r.top + (r.height - dh) / 2;
        const cam = RT ? camOffset() : { x: 0, y: 0 };
        return { x: (e.clientX - offX) / scale + cam.x, y: (e.clientY - offY) / scale + cam.y };
      }
      canvas.addEventListener('pointerdown', (e) => { if (!RT || RT.ended) return; RT.mouseDown = true; const p = ptr(e); for (let i = RT.objs.length - 1; i >= 0; i--) { const o = RT.objs[i]; if (!o.dead && o.visible && p.x >= o.x && p.x <= o.x + o.w && p.y >= o.y && p.y <= o.y + o.h) { startHandlers(o, 'on_click'); break; } } });
      window.addEventListener('pointerup', () => { if (RT) RT.mouseDown = false; });

      // ---- boot ----
      function boot() {
        buildScene(DEF.startScene || 0);
        render();
        overlay('▶  Click to play', '', async () => {
          initAudio();
          if (actx && actx.state === 'suspended') { try { await actx.resume(); } catch (e) {} }
          const want2 = collect();
          await Promise.all(want2.audio.map(loadAudio));
          startLoop();
        });
      }
      const want = collect();
      Promise.all(want.images.map(loadImage)).then(boot);
    };
  }
  const ENGINE_SRC = '(' + GAME_ENGINE.toString() + ')();';

  // ----------------------------------------------------------------------- //
  // compile / preview
  // ----------------------------------------------------------------------- //
  // Shared page chrome: the canvas fills the area preserving aspect ratio, and any
  // unavoidable letterbox bars use the game's own background colour (never black).
  function frameCss(bg) {
    return 'html,body{margin:0;height:100%;background:' + bg + ';overflow:hidden}'
      + '#wrap{position:fixed;inset:0}'
      + 'canvas{display:block;width:100%;height:100%;object-fit:contain;background:' + bg + ';touch-action:none}';
  }
  function indexHtml(t) {
    const bg = def.settings.background || '#bfe3ff';
    return '<!doctype html><html lang="en"><head><meta charset="utf-8">'
      + '<meta name="viewport" content="width=device-width,initial-scale=1">'
      + '<title>' + escapeHtml(t || 'CatoCode Game') + '</title>'
      + '<style>' + frameCss(bg) + '</style></head>'
      + '<body><div id="wrap"><canvas id="gameCanvas"></canvas></div>'
      + '<script src="game.js"></scr' + 'ipt></body></html>';
  }
  function buildGameJS() { return ENGINE_SRC + '\nwindow.startGame(' + JSON.stringify(def) + ');\n'; }

  function previewHtml() {
    const bg = def.settings.background || '#bfe3ff';
    return '<!doctype html><html><head><meta charset="utf-8">'
      + '<style>' + frameCss(bg) + '</style></head>'
      + '<body><div id="wrap"><canvas id="gameCanvas"></canvas></div>'
      + '<script>window.__ASSETS__=' + JSON.stringify(assets) + ';</scr' + 'ipt>'
      + '<script>' + ENGINE_SRC + '</scr' + 'ipt>'
      + '<script>window.startGame(' + JSON.stringify(def) + ');</scr' + 'ipt>'
      + '</body></html>';
  }
  // One self-contained, offline-capable .html — assets inlined as data URLs, no /r/ deps.
  function standaloneHtml() {
    const bg = def.settings.background || '#bfe3ff';
    return '<!doctype html><html lang="en"><head><meta charset="utf-8">'
      + '<meta name="viewport" content="width=device-width,initial-scale=1">'
      + '<title>' + escapeHtml(title || 'CatoCode Game') + '</title>'
      + '<style>' + frameCss(bg) + '</style></head>'
      + '<body><div id="wrap"><canvas id="gameCanvas"></canvas></div>'
      + '<script>window.__ASSETS__=' + JSON.stringify(assets) + ';</scr' + 'ipt>'
      + '<script>' + ENGINE_SRC + '</scr' + 'ipt>'
      + '<script>window.startGame(' + JSON.stringify(def) + ');</scr' + 'ipt>'
      + '</body></html>';
  }
  function loadPreview() {
    // Rebuild a *fresh* iframe each call so stale runtime / audio / rAF state never
    // leaks across edits and the document always reflects the current def + assets.
    const old = $('previewFrame');
    const fresh = document.createElement('iframe');
    fresh.id = 'previewFrame';
    fresh.setAttribute('sandbox', 'allow-scripts allow-pointer-lock');
    old.replaceWith(fresh);
    fresh.srcdoc = previewHtml();
  }
  function slugify(s) {
    return String(s || '').toLowerCase().trim().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 60);
  }
  function exportHtmlFile() {
    const blob = new Blob([standaloneHtml()], { type: 'text/html' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = (slugify(title) || 'my-game') + '.html';
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 2000);
    toast('Exported ' + a.download);
  }

  // ----------------------------------------------------------------------- //
  // ASSET upload + list
  // ----------------------------------------------------------------------- //
  function uniqueAssetName(name) {
    let safe = name.replace(/[^A-Za-z0-9_\-.]/g, '_');
    if (!assets[safe]) return safe;
    const dot = safe.lastIndexOf('.'); const base = dot >= 0 ? safe.slice(0, dot) : safe; const ext = dot >= 0 ? safe.slice(dot) : '';
    let i = 2; while (assets[base + '_' + i + ext]) i++; return base + '_' + i + ext;
  }
  function readDataURL(file) { return new Promise((res, rej) => { const fr = new FileReader(); fr.onload = () => res(fr.result); fr.onerror = rej; fr.readAsDataURL(file); }); }
  $('uploadBtn').addEventListener('click', () => $('assetUpload').click());
  $('assetUpload').addEventListener('change', async (e) => {
    const files = Array.from(e.target.files || []); e.target.value = '';
    for (const file of files) {
      if (file.size > 4 * 1024 * 1024) { toast('"' + file.name + '" is over 4MB', true); continue; }
      if (!isImage(file.name) && !isAudio(file.name)) { toast('Unsupported file: ' + file.name, true); continue; }
      const nm = uniqueAssetName(file.name);
      assets[nm] = await readDataURL(file);
      markDirty();
    }
    renderAssets(); renderProps(); toast('Asset(s) added');
  });

  function renderAssets() {
    const ul = $('assetList'); ul.innerHTML = '';
    const names = Object.keys(assets).sort();
    if (!names.length) { const li = el('li', 'muted-note', 'No assets yet — upload images & sounds.'); ul.appendChild(li); return; }
    names.forEach((nm) => {
      const li = el('li', 'aitem');
      if (isImage(nm)) { const im = el('img'); im.src = assets[nm]; li.appendChild(im); }
      else { li.appendChild(iconEl('span', 'badge-a', 'music')); }
      const span = el('span', 'nm', nm); span.title = nm; li.appendChild(span);
      if (isImage(nm)) {
        li.draggable = true;
        li.addEventListener('dragstart', (ev) => { ev.dataTransfer.setData('text/asset', nm); DRAG = null; });
        li.addEventListener('click', () => addObject('sprite', null, null, nm));
        li.title = 'Click to add, or drag onto the scene';
      }
      const del = iconEl('button', null, 'x'); del.title = 'Delete asset';
      del.addEventListener('click', (ev) => { ev.stopPropagation(); delete assets[nm]; renderAssets(); renderProps(); markDirty(); });
      li.appendChild(del);
      ul.appendChild(li);
    });
  }

  // ----------------------------------------------------------------------- //
  // OBJECT list + add/select
  // ----------------------------------------------------------------------- //
  function renderObjList() {
    const ul = $('objList'); ul.innerHTML = '';
    if (!objs().length) { ul.appendChild(el('li', 'muted-note', 'No objects yet.')); return; }
    objs().forEach((o) => {
      const li = el('li', 'oitem' + (o.id === selId ? ' active' : ''));
      const ico = el('span'); ico.style.display = 'inline-flex'; ico.innerHTML = typeIcon(o.type);
      li.appendChild(ico);
      li.appendChild(el('span', 'nm', o.name));
      const del = iconEl('button', null, 'x'); del.title = 'Delete';
      del.addEventListener('click', (ev) => { ev.stopPropagation(); deleteObject(o.id); });
      li.appendChild(del);
      li.addEventListener('click', () => selectObject(o.id));
      ul.appendChild(li);
    });
  }
  function selectObject(id) { selId = id; renderObjList(); renderProps(); renderCanvas(); if (activeTab === 'logic') renderScript(); }
  function deleteObject(id) {
    const i = objs().findIndex((o) => o.id === id); if (i < 0) return;
    objs().splice(i, 1); if (selId === id) selId = null;
    renderObjList(); renderProps(); renderCanvas(); if (activeTab === 'logic') renderScript(); markDirty();
  }
  function addObject(type, x, y, sprite) {
    const W = stageW(), H = stageH();
    const isText = type === 'text';
    const o = {
      id: uid(type[0]), name: uniqueObjName(type), type,
      x: x == null ? Math.round(W / 2 - 24) : Math.round(x - 24),
      y: y == null ? Math.round(H / 2 - 24) : Math.round(y - 24),
      w: isText ? 140 : 48, h: isText ? 36 : 48, rotation: 0,
      sprite: sprite || null, color: type === 'block' ? '#22c55e' : (isText ? '#0f172a' : '#3b82f6'),
      text: isText ? 'Text' : '', fontSize: 22,
      physics: { solid: type === 'block', gravity: false, vx: 0, vy: 0 }, scripts: [],
    };
    if (type === 'camera') { o.w = 24; o.h = 24; o.color = '#f59e0b'; }
    objs().push(o); selectObject(o.id); toast(o.name + ' added'); markDirty();
  }
  function uniqueObjName(base) { let i = 1, n; do { n = base + (i === 1 ? '' : i); i++; } while (objNames().includes(n)); return n; }

  document.querySelectorAll('.add-btn').forEach((b) => b.addEventListener('click', () => addObject(b.dataset.add)));

  // ----------------------------------------------------------------------- //
  // PROPERTIES panel
  // ----------------------------------------------------------------------- //
  function field(labelText, input) { const f = el('div', 'field'); const l = el('label', null, labelText); f.appendChild(l); f.appendChild(input); return f; }
  function numInput(val, onChange) { const i = el('input'); i.type = 'number'; i.value = val; i.addEventListener('input', () => { onChange(Number(i.value)); markDirty(); }); return i; }
  function renderProps() {
    const box = $('propsBody'); box.innerHTML = '';
    const o = selected();
    if (!o) { box.appendChild(el('p', 'muted-note', 'Select an object to edit its properties.')); return; }

    const nameI = el('input'); nameI.type = 'text'; nameI.value = o.name;
    nameI.addEventListener('change', () => { const v = nameI.value.trim(); if (v) { o.name = v; renderObjList(); markDirty(); } });
    box.appendChild(field('Name', nameI));

    const grid = el('div', 'grid2');
    grid.appendChild(field('X', numInput(o.x, (v) => { o.x = v; renderCanvas(); })));
    grid.appendChild(field('Y', numInput(o.y, (v) => { o.y = v; renderCanvas(); })));
    grid.appendChild(field('Width', numInput(o.w, (v) => { o.w = Math.max(4, v); renderCanvas(); })));
    grid.appendChild(field('Height', numInput(o.h, (v) => { o.h = Math.max(4, v); renderCanvas(); })));
    box.appendChild(grid);
    box.appendChild(field('Rotation°', numInput(o.rotation || 0, (v) => { o.rotation = v; renderCanvas(); })));

    if (o.type === 'sprite' || o.type === 'block') {
      const sel = makeSelect([['', '(no image)']].concat(imageAssets().map((n) => [n, n])), o.sprite || '', (v) => { o.sprite = v || null; renderCanvas(); });
      box.appendChild(field('Sprite image', sel));
    }
    if (o.type === 'camera') {
      const sel = makeSelect([['', '(player)']].concat(objNames().filter((n) => n !== o.name).map((n) => [n, n])), o.text || '', (v) => { o.text = v; });
      box.appendChild(field('Follow target', sel));
    }
    if (o.type === 'text') {
      const ti = el('input'); ti.type = 'text'; ti.value = o.text; ti.addEventListener('input', () => { o.text = ti.value; renderCanvas(); markDirty(); });
      box.appendChild(field('Text', ti));
      box.appendChild(field('Font size', numInput(o.fontSize || 20, (v) => { o.fontSize = v; renderCanvas(); })));
    }
    if (o.type !== 'camera') {
      const ci = el('input'); ci.type = 'color'; ci.value = /^#/.test(o.color) ? o.color : '#3b82f6';
      ci.addEventListener('input', () => { o.color = ci.value; renderCanvas(); markDirty(); });
      box.appendChild(field('Colour', ci));
    }

    if (o.type !== 'camera' && o.type !== 'text') {
      const ph = el('div'); ph.style.marginTop = '6px';
      o.physics = o.physics || { solid: false, gravity: false, vx: 0, vy: 0 };
      ph.appendChild(checkbox('Solid (others collide)', o.physics.solid, (v) => { o.physics.solid = v; }));
      ph.appendChild(checkbox('Gravity (falls)', o.physics.gravity, (v) => { o.physics.gravity = v; }));
      const vg = el('div', 'grid2');
      vg.appendChild(field('Start vel X', numInput(o.physics.vx || 0, (v) => { o.physics.vx = v; })));
      vg.appendChild(field('Start vel Y', numInput(o.physics.vy || 0, (v) => { o.physics.vy = v; })));
      ph.appendChild(vg);
      box.appendChild(ph);
    }

    const actions = el('div'); actions.style.display = 'flex'; actions.style.gap = '6px'; actions.style.marginTop = '10px'; actions.style.flexWrap = 'wrap';
    const logicBtn = el('button', 'btn btn-sky'); logicBtn.innerHTML = ICONS.logic + '<span>Edit logic</span>'; logicBtn.style.flex = '1';
    logicBtn.addEventListener('click', () => switchTab('logic'));
    const dupBtn = el('button', 'btn btn-ghost', 'Duplicate');
    dupBtn.addEventListener('click', () => duplicateObject(o.id));
    const delBtn = el('button', 'btn btn-ghost danger', 'Delete');
    delBtn.addEventListener('click', () => deleteObject(o.id));
    actions.append(logicBtn, dupBtn, delBtn);
    box.appendChild(actions);
  }
  function checkbox(labelText, val, onChange) {
    const w = el('label', 'chk'); const i = el('input'); i.type = 'checkbox'; i.checked = !!val;
    i.addEventListener('change', () => { onChange(i.checked); markDirty(); }); w.appendChild(i); w.appendChild(el('span', null, labelText)); return w;
  }
  function duplicateObject(id) {
    const o = objs().find((x) => x.id === id); if (!o) return;
    const copy = JSON.parse(JSON.stringify(o)); copy.id = uid(o.type[0]); copy.name = uniqueObjName(o.type); copy.x += 16; copy.y += 16;
    objs().push(copy); selectObject(copy.id); markDirty();
  }

  // ----------------------------------------------------------------------- //
  // SCENE canvas editor
  // ----------------------------------------------------------------------- //
  const sc = $('sceneCanvas'); const sctx = sc.getContext('2d');
  let drag = null; // { mode:'move'|'resize', id, ox, oy }
  function applyZoom() { sc.style.width = (levelW() * zoom) + 'px'; sc.style.height = (levelH() * zoom) + 'px'; }
  function imgCache(name) { if (!name) return null; if (imgCache._c && imgCache._c[name]) return imgCache._c[name]; imgCache._c = imgCache._c || {}; const im = new Image(); im.onload = () => renderCanvas(); im.src = assets[name] || ''; imgCache._c[name] = im; return im; }
  // Where the stage-sized camera viewport sits at scene start (mirrors the engine).
  function editorCamRect() {
    const SW = stageW(), SH = stageH(), LW = levelW(), LH = levelH();
    const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
    let cx = 0, cy = 0;
    const cam = objs().find((o) => o.type === 'camera');
    if (cam) {
      const tgt = objs().find((o) => o.name === cam.text) || objs().find((o) => o.physics && o.physics.gravity);
      if (tgt) { cx = (tgt.x + tgt.w / 2) - SW / 2; cy = (tgt.y + tgt.h / 2) - SH / 2; }
    }
    return { x: clamp(cx, 0, Math.max(0, LW - SW)), y: clamp(cy, 0, Math.max(0, LH - SH)), w: SW, h: SH };
  }
  function renderCanvas() {
    sc.width = levelW(); sc.height = levelH(); applyZoom();
    sctx.setTransform(1, 0, 0, 1, 0, 0);
    sctx.fillStyle = def.settings.background || '#bfe3ff'; sctx.fillRect(0, 0, sc.width, sc.height);
    // grid
    sctx.strokeStyle = 'rgba(15,23,42,.06)'; sctx.lineWidth = 1;
    for (let x = 0; x <= sc.width; x += 16) { sctx.beginPath(); sctx.moveTo(x, 0); sctx.lineTo(x, sc.height); sctx.stroke(); }
    for (let y = 0; y <= sc.height; y += 16) { sctx.beginPath(); sctx.moveTo(0, y); sctx.lineTo(sc.width, y); sctx.stroke(); }
    objs().forEach((o) => {
      sctx.save(); sctx.translate(o.x + o.w / 2, o.y + o.h / 2); if (o.rotation) sctx.rotate(o.rotation * Math.PI / 180);
      const im = o.sprite ? imgCache(o.sprite) : null;
      if (im && im.complete && im.naturalWidth) sctx.drawImage(im, -o.w / 2, -o.h / 2, o.w, o.h);
      else if (o.type === 'text') { sctx.fillStyle = o.color || '#0f172a'; sctx.font = (o.fontSize || 20) + 'px Inter, sans-serif'; sctx.textBaseline = 'middle'; sctx.fillText(o.text || 'Text', -o.w / 2, 0); }
      else if (o.type === 'camera') {
        sctx.strokeStyle = '#f59e0b'; sctx.lineWidth = 2; sctx.strokeRect(-o.w / 2, -o.h / 2, o.w, o.h);
        // small vector camcorder glyph (no emoji)
        sctx.fillStyle = '#f59e0b';
        const bw = Math.min(o.w, o.h) * 0.42, bh = bw * 0.7, lx = bw / 2;
        sctx.fillRect(-bw / 2 - 1, -bh / 2, bw, bh);
        sctx.beginPath(); sctx.moveTo(lx - 1, -bh / 2 + 1); sctx.lineTo(lx - 1 + bh * 0.6, -bh / 2 - 1);
        sctx.lineTo(lx - 1 + bh * 0.6, bh / 2 + 1); sctx.lineTo(lx - 1, bh / 2 - 1); sctx.closePath(); sctx.fill();
      }
      else { sctx.fillStyle = o.color || '#3b82f6'; sctx.fillRect(-o.w / 2, -o.h / 2, o.w, o.h); }
      sctx.restore();
      if (o.id === selId) {
        sctx.strokeStyle = '#0ea5e9'; sctx.lineWidth = 2; sctx.setLineDash([5, 4]); sctx.strokeRect(o.x, o.y, o.w, o.h); sctx.setLineDash([]);
        sctx.fillStyle = '#0ea5e9'; sctx.fillRect(o.x + o.w - 7, o.y + o.h - 7, 10, 10);
      }
    });
    // camera viewport overlay (only when the level is bigger than the stage)
    if (levelW() > stageW() || levelH() > stageH()) {
      const cr = editorCamRect();
      sctx.save();
      sctx.strokeStyle = 'rgba(99,102,241,.95)'; sctx.lineWidth = 2; sctx.setLineDash([9, 6]);
      sctx.strokeRect(cr.x + 1, cr.y + 1, cr.w - 2, cr.h - 2); sctx.setLineDash([]);
      sctx.fillStyle = 'rgba(99,102,241,.95)'; sctx.fillRect(cr.x, cr.y, 96, 18);
      sctx.fillStyle = '#fff'; sctx.font = '11px Inter, sans-serif'; sctx.textBaseline = 'middle';
      sctx.fillText('camera view', cr.x + 8, cr.y + 9);
      sctx.restore();
    }
  }
  function ptOf(e) { const r = sc.getBoundingClientRect(); return { x: (e.clientX - r.left) / zoom, y: (e.clientY - r.top) / zoom }; }
  function snapV(v) { return snap ? Math.round(v / 8) * 8 : Math.round(v); }
  sc.addEventListener('pointerdown', (e) => {
    if (spaceDown || e.button === 1) return; // space / middle-button → let the scroller pan
    const p = ptOf(e); const o = selected();
    if (o && p.x >= o.x + o.w - 9 && p.x <= o.x + o.w + 4 && p.y >= o.y + o.h - 9 && p.y <= o.y + o.h + 4) {
      drag = { mode: 'resize', id: o.id }; sc.setPointerCapture(e.pointerId); return;
    }
    let hit = null; const list = objs();
    for (let i = list.length - 1; i >= 0; i--) { const t = list[i]; if (p.x >= t.x && p.x <= t.x + t.w && p.y >= t.y && p.y <= t.y + t.h) { hit = t; break; } }
    if (hit) { selectObject(hit.id); drag = { mode: 'move', id: hit.id, ox: p.x - hit.x, oy: p.y - hit.y }; sc.setPointerCapture(e.pointerId); }
    else { selId = null; renderObjList(); renderProps(); renderCanvas(); }
  });
  sc.addEventListener('pointermove', (e) => {
    if (!drag) return; const p = ptOf(e); const o = objs().find((x) => x.id === drag.id); if (!o) return;
    if (drag.mode === 'move') { o.x = snapV(p.x - drag.ox); o.y = snapV(p.y - drag.oy); }
    else { o.w = Math.max(8, snapV(p.x - o.x)); o.h = Math.max(8, snapV(p.y - o.y)); }
    renderCanvas();
  });
  sc.addEventListener('pointerup', () => { if (drag) { drag = null; renderProps(); markDirty(); } });
  sc.addEventListener('dragover', (e) => { if (e.dataTransfer.types.includes('text/asset')) e.preventDefault(); });
  sc.addEventListener('drop', (e) => {
    const nm = e.dataTransfer.getData('text/asset'); if (!nm) return; e.preventDefault(); const p = ptOf(e); addObject('sprite', p.x, p.y, nm);
  });
  $('snapChk').addEventListener('change', (e) => { snap = e.target.checked; });
  $('zoomIn').addEventListener('click', () => { zoom = Math.min(2.5, zoom + 0.25); renderCanvas(); });
  $('zoomOut').addEventListener('click', () => { zoom = Math.max(0.2, zoom - 0.25); renderCanvas(); });

  // ---- pan: hold Space (or middle mouse) and drag to scroll large levels ----
  let spaceDown = false, pan = null;
  const sceneScroll = $('sceneScroll');
  function setPannable(on) { sceneScroll.classList.toggle('pannable', !!on && !pan); }
  window.addEventListener('keydown', (e) => {
    if (e.code !== 'Space' || activeTab !== 'scene') return;
    const t = e.target; if (t && /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName)) return;
    spaceDown = true; setPannable(true); e.preventDefault();
  });
  window.addEventListener('keyup', (e) => { if (e.code === 'Space') { spaceDown = false; setPannable(false); } });
  sceneScroll.addEventListener('pointerdown', (e) => {
    if (!spaceDown && e.button !== 1) return;
    pan = { x: e.clientX, y: e.clientY, sl: sceneScroll.scrollLeft, st: sceneScroll.scrollTop };
    sceneScroll.classList.add('panning'); sceneScroll.classList.remove('pannable');
    sceneScroll.setPointerCapture(e.pointerId); e.preventDefault();
  });
  sceneScroll.addEventListener('pointermove', (e) => {
    if (!pan) return;
    sceneScroll.scrollLeft = pan.sl - (e.clientX - pan.x);
    sceneScroll.scrollTop = pan.st - (e.clientY - pan.y);
  });
  function endPan() { if (!pan) return; pan = null; sceneScroll.classList.remove('panning'); setPannable(spaceDown); }
  sceneScroll.addEventListener('pointerup', endPan);
  sceneScroll.addEventListener('pointercancel', endPan);

  // ----------------------------------------------------------------------- //
  // LOGIC block editor (drag & drop)
  // ----------------------------------------------------------------------- //
  let DRAG = null; // { kind:'new', op } | { kind:'move', block, fromArr, fromIdx }

  function renderPalette() {
    const wrap = $('blockPalette'); wrap.innerHTML = '';
    const byCat = {};
    Object.keys(SPECS).forEach((op) => { const c = SPECS[op].cat; (byCat[c] = byCat[c] || []).push(op); });
    Object.keys(CAT).forEach((cat) => {
      const ops = byCat[cat]; if (!ops) return;
      const g = el('div', 'palette-cat'); g.appendChild(el('div', 'cat-label', CAT[cat].label));
      ops.forEach((op) => {
        const pb = el('div', 'pblock', SPECS[op].pal); pb.style.background = CAT[cat].color; pb.draggable = true;
        pb.addEventListener('dragstart', (ev) => { DRAG = { kind: 'new', op }; ev.dataTransfer.setData('text/block', op); ev.dataTransfer.effectAllowed = 'copy'; });
        g.appendChild(pb);
      });
      wrap.appendChild(g);
    });
    const note = el('div', 'palette-cat'); note.appendChild(el('div', 'cat-label', 'Sensing'));
    note.appendChild(el('div', 'muted-note', 'Use the condition dropdown inside If / If-else (touching, key, mouse, edge, variable).'));
    wrap.appendChild(note);
  }

  function isDescendantArr(block, arr) {
    if (!block) return false;
    if (block.children === arr || block.children2 === arr) return true;
    const scan = (list) => (list || []).some((b) => isDescendantArr(b, arr));
    return scan(block.children) || scan(block.children2);
  }

  function makeSlot(arr, idx, top) {
    const s = el('div', 'slot' + (top ? ' top' : ''));
    s.addEventListener('dragover', (e) => { e.preventDefault(); s.classList.add('over'); });
    s.addEventListener('dragleave', () => s.classList.remove('over'));
    s.addEventListener('drop', (e) => {
      e.preventDefault(); e.stopPropagation(); s.classList.remove('over');
      if (!DRAG) return;
      if (DRAG.kind === 'new') {
        const spec = SPECS[DRAG.op];
        if (spec.hat && !top) { toast('Event blocks start a new script — drop at the top.', true); return; }
        if (!spec.hat && top) { toast('Drop this inside a script (under an event block).', true); return; }
        arr.splice(idx, 0, newBlock(DRAG.op));
      } else if (DRAG.kind === 'move') {
        if (top) { toast("Can't move event blocks here.", true); return; }
        if (DRAG.block.children === arr || isDescendantArr(DRAG.block, arr)) { toast("Can't drop a block inside itself.", true); return; }
        let tIdx = idx;
        if (DRAG.fromArr === arr && DRAG.fromIdx < idx) tIdx--;
        DRAG.fromArr.splice(DRAG.fromIdx, 1);
        arr.splice(tIdx, 0, DRAG.block);
      }
      DRAG = null; renderScript(); markDirty();
    });
    return s;
  }

  function renderFieldEl(b, f) {
    const a = b.args;
    if (f.t === 'num') { const i = el('input'); i.type = 'number'; i.value = a[f.k]; i.addEventListener('input', () => { a[f.k] = Number(i.value); markDirty(); }); return i; }
    if (f.t === 'text') { const i = el('input'); i.type = 'text'; i.value = a[f.k] == null ? '' : a[f.k]; i.size = 8; i.addEventListener('input', () => { a[f.k] = i.value; markDirty(); }); return i; }
    if (f.t === 'cond') return renderCond(b, f.k);
    // select
    const opts = selectSource(f.src);
    const s = makeSelect(opts, a[f.k], (v) => {
      if (v === '__new__') { const nm = (prompt('New variable name') || '').trim().replace(/[^A-Za-z0-9_]/g, ''); if (nm) { if (!def.variables.includes(nm)) def.variables.push(nm); a[f.k] = nm; } renderScript(); return; }
      a[f.k] = v;
    });
    return s;
  }

  function renderCond(b, k) {
    const c = b.args[k] || (b.args[k] = { kind: 'always' });
    const span = el('span'); span.style.display = 'inline-flex'; span.style.gap = '4px'; span.style.alignItems = 'center'; span.style.flexWrap = 'wrap';
    span.appendChild(makeSelect(COND_KINDS, c.kind, (v) => { c.kind = v; renderScript(); }));
    if (c.kind === 'touching') { span.appendChild(makeSelect(selectSource('objects_any'), c.target || 'any', (v) => { c.target = v; })); }
    else if (c.kind === 'key') { span.appendChild(makeSelect(KEY_OPTS, c.key || 'ArrowUp', (v) => { c.key = v; })); }
    else if (c.kind === 'var') {
      span.appendChild(makeSelect(selectSource('vars').filter(([v]) => v !== '__new__'), c.var || (def.variables[0] || 'score'), (v) => { c.var = v; }));
      span.appendChild(makeSelect(OPS, c.op || '>', (v) => { c.op = v; }));
      const vi = el('input'); vi.type = 'number'; vi.value = c.value == null ? 0 : c.value; vi.addEventListener('input', () => { c.value = Number(vi.value); markDirty(); }); span.appendChild(vi);
    }
    return span;
  }

  function renderBlock(b, arr, idx) {
    const spec = SPECS[b.op]; const wrap = el('div', 'block' + (spec.hat ? ' hat' : ''));
    wrap.style.background = CAT[spec.cat].color;
    const row = el('div', 'block-row');
    // drag handle (non-hat blocks are movable)
    if (!spec.hat) {
      const h = el('span'); h.innerHTML = ICONS.drag; h.style.cursor = 'grab'; h.style.display = 'inline-flex'; h.draggable = true; h.style.opacity = '.7';
      h.addEventListener('dragstart', (ev) => { DRAG = { kind: 'move', block: b, fromArr: arr, fromIdx: idx }; ev.dataTransfer.setData('text/block', 'move'); ev.dataTransfer.effectAllowed = 'move'; ev.stopPropagation(); });
      row.appendChild(h);
    }
    (spec.label || [b.op]).forEach((tok) => {
      if (typeof tok === 'string') row.appendChild(el('span', null, tok));
      else { const f = (spec.fields || []).find((x) => x.k === tok.f); if (f) row.appendChild(renderFieldEl(b, f)); }
    });
    const ctrls = el('div', 'ctrls');
    const del = iconEl('button', null, 'x'); del.title = 'Delete block';
    del.addEventListener('click', () => { arr.splice(idx, 1); renderScript(); markDirty(); });
    ctrls.appendChild(del); row.appendChild(ctrls);
    wrap.appendChild(row);

    if (spec.c) {
      const body = el('div', 'cbody'); fillStack(body, b.children); wrap.appendChild(body);
      if (spec.c2) {
        const elseRow = el('div', 'block-row'); elseRow.appendChild(el('span', null, 'else')); wrap.appendChild(elseRow);
        const body2 = el('div', 'cbody'); fillStack(body2, b.children2); wrap.appendChild(body2);
      }
      const foot = el('div', 'cfoot'); foot.style.background = CAT[spec.cat].color; wrap.appendChild(foot);
    }
    return wrap;
  }

  function fillStack(container, arr) {
    for (let i = 0; i < arr.length; i++) { container.appendChild(makeSlot(arr, i, false)); container.appendChild(renderBlock(arr[i], arr, i)); }
    container.appendChild(makeSlot(arr, arr.length, false));
  }

  function renderScript() {
    const area = $('scriptArea'); area.innerHTML = '';
    const o = selected();
    if (!o) { area.appendChild(el('div', 'logic-hint', 'Select an object (Scene tab or the list) to give it behaviour.')); return; }
    const head = el('div'); head.style.marginBottom = '12px'; head.style.fontWeight = '700'; head.style.color = 'var(--ink-2)';
    head.textContent = 'Logic for: ' + o.name;
    area.appendChild(head);
    o.scripts = o.scripts || [];
    // top-level slots accept hat blocks (new stacks)
    area.appendChild(makeSlot(o.scripts, 0, true));
    for (let i = 0; i < o.scripts.length; i++) {
      const st = o.scripts[i];
      const stackEl = el('div', 'stack'); stackEl.appendChild(renderStack(st, o.scripts, i)); area.appendChild(stackEl);
      area.appendChild(makeSlot(o.scripts, i + 1, true));
    }
    if (!o.scripts.length) area.appendChild(el('div', 'logic-hint', 'Drag an Events block here to start a script.'));
  }
  // a stack is a hat block followed by its children body
  function renderStack(hat, arr, idx) {
    const spec = SPECS[hat.op]; const wrap = el('div', 'block hat'); wrap.style.background = CAT[spec.cat].color;
    const row = el('div', 'block-row');
    (spec.label || [hat.op]).forEach((tok) => {
      if (typeof tok === 'string') row.appendChild(el('span', null, tok));
      else { const f = (spec.fields || []).find((x) => x.k === tok.f); if (f) row.appendChild(renderFieldEl(hat, f)); }
    });
    const ctrls = el('div', 'ctrls'); const del = iconEl('button', null, 'x'); del.title = 'Delete script';
    del.addEventListener('click', () => { arr.splice(idx, 1); renderScript(); markDirty(); }); ctrls.appendChild(del); row.appendChild(ctrls);
    wrap.appendChild(row);
    hat.children = hat.children || [];
    const body = el('div', 'cbody'); fillStack(body, hat.children); wrap.appendChild(body);
    const foot = el('div', 'cfoot'); foot.style.background = CAT[spec.cat].color; wrap.appendChild(foot);
    return wrap;
  }

  // ----------------------------------------------------------------------- //
  // tabs
  // ----------------------------------------------------------------------- //
  function switchTab(t) {
    activeTab = t;
    document.querySelectorAll('.tab').forEach((b) => b.classList.toggle('active', b.dataset.tab === t));
    $('sceneScroll').style.display = t === 'scene' ? 'flex' : 'none';
    $('logicWrap').classList.toggle('show', t === 'logic');
    $('previewWrap').classList.toggle('show', t === 'preview');
    $('leftScene').style.display = t === 'scene' ? '' : 'none';
    $('leftLogic').style.display = t === 'logic' ? '' : 'none';
    $('rightPanel').style.display = t === 'preview' ? 'none' : '';
    $('sceneControls').style.display = t === 'scene' ? '' : 'none';
    $('stageLabel').textContent = t === 'scene' ? 'Scene' : (t === 'logic' ? 'Logic' : 'Preview');
    if (t === 'scene') renderCanvas();
    if (t === 'logic') { renderPalette(); renderScript(); }
    if (t === 'preview') loadPreview();
  }
  document.querySelectorAll('.tab').forEach((b) => b.addEventListener('click', () => switchTab(b.dataset.tab)));
  $('restartPreview').addEventListener('click', loadPreview);
  $('exportBtn').addEventListener('click', exportHtmlFile);

  // ----------------------------------------------------------------------- //
  // game settings modal
  // ----------------------------------------------------------------------- //
  $('settingsBtn').addEventListener('click', () => {
    $('setW').value = stageW(); $('setH').value = stageH();
    $('setLW').value = levelW(); $('setLH').value = levelH();
    $('setGrav').value = def.settings.gravity; $('setBg').value = def.settings.background || '#bfe3ff';
    $('setModal').classList.add('show');
  });
  $('setCancel').addEventListener('click', () => $('setModal').classList.remove('show'));
  $('setModal').addEventListener('click', (e) => { if (e.target === $('setModal')) $('setModal').classList.remove('show'); });
  $('setSave').addEventListener('click', () => {
    def.settings.stageWidth = Number($('setW').value) || 480;
    def.settings.stageHeight = Number($('setH').value) || 320;
    def.settings.levelWidth = Number($('setLW').value) || def.settings.stageWidth;
    def.settings.levelHeight = Number($('setLH').value) || def.settings.stageHeight;
    def.settings.gravity = Math.max(0, Math.min(3000, Number($('setGrav').value) || 0));
    def.settings.background = $('setBg').value;
    migrateSettings(def.settings);
    $('setModal').classList.remove('show'); renderCanvas(); markDirty();
  });

  // ----------------------------------------------------------------------- //
  // save / publish / autosave
  // ----------------------------------------------------------------------- //
  const loggedIn = !!BOOT.loggedIn;
  let dirty = false, autosavePaused = false, creatingDraft = false, autosaving = false, autosaveTimer = null;

  function compileFiles() {
    return { 'index.html': indexHtml(title), 'game.js': buildGameJS(), 'game.json': JSON.stringify(def) };
  }
  function projectBody(extra) {
    return Object.assign({ title: title || 'Untitled game', description, type: 'game', files: compileFiles(), images: assets }, extra || {});
  }
  function handleAuthError(data) {
    if (data && data.login) { toast('Please sign in first — redirecting…', true); const next = encodeURIComponent(location.pathname + location.search); setTimeout(() => { window.location.href = '/login?next=' + next; }, 900); return true; }
    return false;
  }

  // ---- autosave: first edit creates a draft, later edits debounce a PATCH ----
  function setSaveStatus(s) {
    const el = $('saveStatus'); if (!el) return;
    if (s === 'hidden') { el.style.display = 'none'; return; }
    el.style.display = ''; el.classList.toggle('saving', s === 'saving'); el.classList.toggle('saved', s === 'saved');
    el.querySelector('.save-status-text').textContent = s === 'saving' ? 'Saving…' : 'Saved';
  }
  function authPause() { autosavePaused = true; clearTimeout(autosaveTimer); setSaveStatus('hidden'); toast('Sign in to keep your game saved', true); }
  function markDirty() {
    if (!loggedIn || autosavePaused) return;     // anonymous → no autosave; paused after auth loss
    dirty = true;
    if (!projectId) { ensureDraft(); return; }   // first edit creates the draft
    clearTimeout(autosaveTimer); autosaveTimer = setTimeout(autosaveNow, 1500);
  }
  async function ensureDraft() {
    if (projectId || creatingDraft) return;
    creatingDraft = true; setSaveStatus('saving');
    try {
      const res = await fetch('/api/projects', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(projectBody({ published: false })) });
      const data = await res.json();
      if (!res.ok) { if (data && data.login) { authPause(); return; } throw new Error(data.error || 'Autosave failed'); }
      projectId = data.id; published = false;
      try { history.replaceState(null, '', '/edit/' + projectId); } catch (e) {}
      setupTopbar(); dirty = false; setSaveStatus('saved');
    } catch (err) { setSaveStatus('hidden'); toast(err.message || 'Autosave failed', true); }
    finally { creatingDraft = false; if (dirty) markDirty(); }
  }
  async function autosaveNow() {
    if (!projectId || autosavePaused || autosaving) return;
    autosaving = true; dirty = false; setSaveStatus('saving');
    try {
      const res = await fetch('/api/projects/' + projectId, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(projectBody()) });
      const data = await res.json();
      if (!res.ok) { if (data && data.login) { authPause(); return; } throw new Error(data.error || 'Save failed'); }
      setSaveStatus('saved');
    } catch (err) { dirty = true; setSaveStatus('hidden'); toast(err.message || 'Autosave failed', true); }
    finally { autosaving = false; if (dirty && !autosavePaused) { clearTimeout(autosaveTimer); autosaveTimer = setTimeout(autosaveNow, 1500); } }
  }

  const pubModal = $('pubModal');
  function openPub() {
    $('pmName').value = title || ''; $('pmDesc').value = description || '';
    $('pmTitle').textContent = 'Publish your game'; $('pmSub').textContent = 'Give it a name — you can keep editing it any time.'; $('pmConfirm').textContent = 'Publish';
    pubModal.classList.add('show'); $('pmName').focus();
  }
  $('pmCancel').addEventListener('click', () => pubModal.classList.remove('show'));
  pubModal.addEventListener('click', (e) => { if (e.target === pubModal) pubModal.classList.remove('show'); });

  $('publishBtn').addEventListener('click', () => {
    if (projectId && published) { window.location.href = '/p/' + projectId; return; }
    if (projectId && !published) { publishDraft(); return; }
    openPub();
  });
  $('pmConfirm').addEventListener('click', async () => {
    const t = $('pmName').value.trim(); if (!t) { toast('Title is required', true); return; }
    title = t; description = $('pmDesc').value.trim();
    const btn = $('pmConfirm'); btn.disabled = true; btn.textContent = 'Publishing…';
    try {
      let data;
      if (projectId) {   // promote the existing draft — never create a duplicate
        const res = await fetch('/api/projects/' + projectId, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(projectBody({ published: true })) });
        data = await res.json();
        if (!res.ok) { if (handleAuthError(data)) return; throw new Error(data.error || 'Publish failed'); }
        published = true; toast('Published! Redirecting…'); setTimeout(() => { window.location.href = '/p/' + projectId; }, 600);
      } else {
        const res = await fetch('/api/projects', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(projectBody({ published: true })) });
        data = await res.json();
        if (!res.ok) { if (handleAuthError(data)) return; throw new Error(data.error || 'Publish failed'); }
        toast('Published! Redirecting…'); setTimeout(() => { window.location.href = data.url; }, 600);
      }
    } catch (err) { toast(err.message || 'Publish failed', true); btn.disabled = false; btn.textContent = 'Publish'; }
  });

  async function saveEdit(silent, makePublic) {
    if (!projectId) { openPub(); return false; }
    clearTimeout(autosaveTimer);
    const btn = $('saveBtn'); btn.disabled = true; const prev = btn.textContent; btn.textContent = 'Saving…'; setSaveStatus('saving');
    try {
      const res = await fetch('/api/projects/' + projectId, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(projectBody(makePublic ? { published: true } : null)) });
      const data = await res.json();
      if (!res.ok) { if (handleAuthError(data)) return false; throw new Error(data.error || 'Save failed'); }
      if (makePublic) { published = true; setupTopbar(); }
      dirty = false; setSaveStatus('saved');
      if (!silent) toast(makePublic ? 'Published!' : 'Saved');
      return true;
    } catch (err) { toast(err.message || 'Save failed', true); setSaveStatus('hidden'); return false; }
    finally { btn.disabled = false; btn.textContent = prev; }
  }
  async function publishDraft() { if (!title) { openPub(); return; } if (await saveEdit(true, true)) { setTimeout(() => { window.location.href = '/p/' + projectId; }, 500); } }
  $('saveBtn').addEventListener('click', () => saveEdit(false, false));

  function setupTopbar() {
    const pubSpan = $('publishBtn').querySelector('span');
    if (projectId) {
      $('saveBtn').style.display = ''; $('discardBtn').textContent = 'Back'; $('discardBtn').href = '/account';
      if (pubSpan) pubSpan.textContent = published ? 'View' : 'Publish';
      $('titleTag').textContent = (title || 'Untitled game') + (published ? ' · Published' : ' · Draft');
      if (loggedIn) setSaveStatus('saved');
    } else {
      $('titleTag').textContent = 'Untitled game · not saved';
      setSaveStatus('hidden');
    }
  }

  // ----------------------------------------------------------------------- //
  // init
  // ----------------------------------------------------------------------- //
  setupTopbar();
  renderAssets();
  renderObjList();
  renderProps();
  applyZoom();
  renderCanvas();
  if (objs().length) selectObject(objs()[0].id);
})();
