/* Exercises the REAL AudioWorklet ring buffer from app.js.
 *
 * This is the piece of the sound path with no other safety net: it runs on
 * the audio thread inside a Blob module, so nothing else can see it, and
 * every way it can be wrong - starting before it has enough, playing a
 * backlog that is already late, reading past what has been written - sounds
 * like "the audio is broken" rather than showing an error anywhere.
 */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const source = readFileSync(join(here, '../../src/machop/web/app.js'), 'utf8');

const open = source.indexOf('const WORKLET_SOURCE = `');
if (open < 0) throw new Error('WORKLET_SOURCE not found in app.js');
const from = source.indexOf('`', open) + 1;
const to = source.indexOf('`', from);
const workletSource = source.slice(from, to);

/* The worklet runs against globals the browser provides. Supply the two it
   uses, then take the class back out. */
let Registered = null;
new Function('AudioWorkletProcessor', 'registerProcessor', workletSource)(
  class { constructor() { this.port = { onmessage: null, postMessage() {} }; } },
  (_name, cls) => { Registered = cls; },
);
if (!Registered) throw new Error('registerProcessor was never called');

let pass = 0, fail = 0;
function check(name, ok, detail) {
  if (ok) { pass++; console.log('  ok   ' + name); }
  else { fail++; console.log('  FAIL ' + name + (detail !== undefined ? '  -> ' + JSON.stringify(detail) : '')); }
}

const make = (options) => {
  const node = new Registered({
    processorOptions: { channels: 2, capacity: 1000, target: 40, maxDepth: 200, ...options },
  });
  return node;
};
const feed = (node, values) => {
  const plane = Float32Array.from(values);
  node.port.onmessage({ data: [plane, plane] });
};
const render = (node, frames) => {
  const out = [new Float32Array(frames), new Float32Array(frames)];
  node.process([], [out]);
  return Array.from(out[0]);
};
const ramp = (n, from = 0) => Array.from({ length: n }, (_, i) => from + i + 1);

// --- priming ------------------------------------------------------------
{
  const node = make();
  feed(node, ramp(10));
  check('silent until it has a lead', render(node, 8).every((v) => v === 0));
  check('nothing was consumed while priming', node.readPos === 0);

  feed(node, ramp(40, 10));
  const first = render(node, 8);
  check('plays from the start once primed', first[0] === 1 && first[7] === 8, first);
}

// --- continuity ---------------------------------------------------------
{
  const node = make();
  feed(node, ramp(100));
  render(node, 8);
  const next = render(node, 8);
  check('no gap or repeat between blocks', next[0] === 9 && next[7] === 16, next);
}

// --- underrun -----------------------------------------------------------
{
  const node = make();
  feed(node, ramp(50));
  render(node, 40);
  const tail = render(node, 20);
  check('runs dry into silence, not into stale samples',
    tail.slice(10).every((v) => v === 0), tail);
  check('re-primes rather than stuttering sample by sample', node.playing === false);
}

// --- a backlog is thrown away, not played late --------------------------
{
  const node = make({ maxDepth: 200, target: 40 });
  feed(node, ramp(400));           // a burst after a stall
  check('backlog trimmed to the target lead', node.writePos - node.readPos === 40,
    node.writePos - node.readPos);
  const played = render(node, 8);
  check('carries on from the newest audio, not the oldest',
    played[0] === 361, played[0]);
}

// --- flush ---------------------------------------------------------------
{
  const node = make();
  feed(node, ramp(100));
  render(node, 8);
  node.port.onmessage({ data: 'flush' });
  check('flush empties the ring', node.readPos === 0 && node.writePos === 0);
  check('flush re-arms priming', node.playing === false);
}

// --- mono in, stereo out -------------------------------------------------
{
  const node = make();
  const plane = Float32Array.from(ramp(100));
  node.port.onmessage({ data: [plane] });      // one plane only
  const out = [new Float32Array(8), new Float32Array(8)];
  node.process([], [out]);
  check('a mono stream fills both output channels',
    out[1][0] === out[0][0] && out[0][0] === 1, [out[0][0], out[1][0]]);
}

// --- wraparound ----------------------------------------------------------
{
  const node = make({ capacity: 64, target: 8, maxDepth: 48 });
  let expected = 1;
  let ok = true;
  for (let round = 0; round < 20; round++) {
    feed(node, ramp(16, round * 16));
    const block = render(node, 16);
    for (const value of block) {
      if (value === 0) continue;              // priming block
      if (value !== expected) { ok = false; break; }
      expected++;
    }
    if (!ok) break;
  }
  check('samples survive wrapping round the ring many times', ok, expected);
}

console.log(`\n${pass} passed, ${fail} failed`);
if (fail) process.exit(1);
