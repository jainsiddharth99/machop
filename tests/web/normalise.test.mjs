/* Exercises the REAL normalise() from web/app.js - extracted from source, not
 * copied - so the mapping cannot drift from what ships. Coordinate errors here
 * put clicks in the wrong place, which is silent and maddening to debug. */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const source = readFileSync(join(here, '../../src/machop/web/app.js'), 'utf8');

const start = source.indexOf('  function normalise(');
if (start < 0) throw new Error('normalise() not found in app.js - did it get renamed?');
let depth = 0, end = start;
for (let i = source.indexOf('{', start); i < source.length; i++) {
  if (source[i] === '{') depth++;
  else if (source[i] === '}' && --depth === 0) { end = i + 1; break; }
}
const body = source.slice(start, end);

// Minimal stand-ins for the two globals normalise() closes over.
const makeNormalise = (usingRelay, surface) =>
  new Function('usingRelay', 'canvas', 'video', `${body}; return normalise;`)(
    usingRelay,
    usingRelay ? surface : { getBoundingClientRect: () => ({}) },
    usingRelay ? { getBoundingClientRect: () => ({}) } : surface,
  );

let pass = 0, fail = 0;
const near = (a, b) => Math.abs(a - b) < 1e-6;
const check = (name, ok) => ok ? (pass++, console.log('  ok   ' + name))
                               : (fail++, console.log('  FAIL ' + name));

const VW = 1440, VH = 936;
const videoSurface = (rect) => ({ getBoundingClientRect: () => rect, videoWidth: VW, videoHeight: VH });
const canvasSurface = (rect) => ({ getBoundingClientRect: () => rect, width: VW, height: VH });

// Phone in portrait: video letterboxes with bars above and below.
const tall = { left: 0, top: 0, width: 390, height: 844 };
const renderH = 390 / (VW / VH);
const padY = (844 - renderH) / 2;

for (const [mode, surface] of [['webrtc', videoSurface(tall)], ['relay', canvasSurface(tall)]]) {
  const n = makeNormalise(mode === 'relay', surface);
  check(`${mode}: centre -> (0.5, 0.5)`,
    (() => { const r = n(195, 422); return r && near(r.x, 0.5) && near(r.y, 0.5); })());
  check(`${mode}: top-left of image -> (0, 0)`,
    (() => { const r = n(0, padY); return r && near(r.x, 0) && near(r.y, 0); })());
  check(`${mode}: bottom-right of image -> (1, 1)`,
    (() => { const r = n(390, padY + renderH); return r && near(r.x, 1) && near(r.y, 1); })());
  check(`${mode}: tap above the image is rejected`, n(195, 10) === null);
  check(`${mode}: tap below the image is rejected`, n(195, 834) === null);
}

// Wide window: bars on the left and right instead.
const wide = { left: 0, top: 0, width: 2000, height: 600 };
const padX = (2000 - 600 * (VW / VH)) / 2;
const w = makeNormalise(false, videoSurface(wide));
check('pillarbox: centre -> (0.5, 0.5)',
  (() => { const r = w(1000, 300); return r && near(r.x, 0.5) && near(r.y, 0.5); })());
check('pillarbox: left bar is rejected', w(10, 300) === null);
check('pillarbox: left edge of image -> x = 0',
  (() => { const r = w(padX, 300); return r && near(r.x, 0); })());

// Element offset by a toolbar or page scroll.
const offset = { left: 40, top: 120, width: 800, height: 520 };
const o = makeNormalise(false, videoSurface(offset));
check('offset element: centre -> (0.5, 0.5)',
  (() => { const r = o(440, 380); return r && near(r.x, 0.5) && near(r.y, 0.5); })());

// Before metadata arrives, a tap must not land at (0, 0).
const cold = makeNormalise(false, { getBoundingClientRect: () => tall, videoWidth: 0, videoHeight: 0 });
check('no video metadata yet -> null, not a phantom click', cold(100, 100) === null);

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
