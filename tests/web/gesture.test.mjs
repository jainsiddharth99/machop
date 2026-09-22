/* Exercises the REAL gesture engine from app.js.
 *
 * Multi-touch cannot be synthesised reliably in a headless browser, and this
 * is the layer a phone actually exercises - one-finger drag selecting text
 * instead of scrolling was the first thing a real device exposed. */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const source = readFileSync(join(here, '../../src/machop/web/app.js'), 'utf8');

const start = source.indexOf('  function createGestureEngine(');
if (start < 0) throw new Error('createGestureEngine() not found in app.js');
let depth = 0, end = start;
for (let i = source.indexOf('{', start); i < source.length; i++) {
  if (source[i] === '{') depth++;
  else if (source[i] === '}' && --depth === 0) { end = i + 1; break; }
}
const createGestureEngine = new Function(
  `${source.slice(start, end)}; return createGestureEngine;`)();

let pass = 0, fail = 0;
function check(name, ok, detail) {
  if (ok) { pass++; console.log('  ok   ' + name); }
  else { fail++; console.log('  FAIL ' + name + (detail ? '  -> ' + JSON.stringify(detail) : '')); }
}
const make = () => { const out = []; return [createGestureEngine((a) => out.push(a)), out]; };
const acts = (out) => out.map((a) => a.type === 'scroll' ? 'scroll' : `${a.action}:${a.button}`);

// --- one finger tap = left click, fired on release ---------------------
{
  const [g, out] = make();
  g.down(1, 100, 100, 0);
  check('tap: nothing sent while the finger is still down', out.length === 0, out);
  g.up(1, 100, 100, 80);
  check('tap: click on release', acts(out).join(',') === 'down:left,up:left', acts(out));
  check('tap: click lands at the touch point', out[0].x === 100 && out[0].y === 100);
}

// --- one finger drag ---------------------------------------------------
{
  const [g, out] = make();
  g.down(1, 100, 100, 0);
  g.move(1, 103, 101, 20);
  check('drag: a tiny wobble does not start a drag', out.length === 0, acts(out));
  g.move(1, 160, 140, 60);
  check('drag: press is emitted at the ORIGINAL point', out[0].action === 'down' && out[0].x === 100 && out[0].y === 100, out[0]);
  g.move(1, 200, 180, 90);
  g.up(1, 200, 180, 120);
  check('drag: down, drags, then up', acts(out).join(',') === 'down:left,drag:left,drag:left,up:left', acts(out));
}

// --- two fingers scroll, and never press a button ----------------------
{
  const [g, out] = make();
  g.down(1, 100, 300, 0);
  g.down(2, 140, 300, 10);
  g.move(1, 100, 260, 30); g.move(2, 140, 260, 30);
  g.move(1, 100, 220, 60); g.move(2, 140, 220, 60);
  const scrolls = out.filter((a) => a.type === 'scroll');
  check('scroll: two fingers produce scroll actions', scrolls.length >= 2, acts(out));
  check('scroll: NO mouse button is ever pressed', out.every((a) => a.type === 'scroll'), acts(out));
  check('scroll: content follows the fingers (upward drag = negative dy)', scrolls[0].dy < 0, scrolls[0]);
  g.up(1, 100, 220, 90); g.up(2, 140, 220, 95);
  check('scroll: still no buttons after release', out.every((a) => a.type === 'scroll'), acts(out));
}

// --- the exact bug from the phone: drag begun, then second finger lands --
{
  const [g, out] = make();
  g.down(1, 100, 300, 0);
  g.move(1, 100, 240, 40);            // a drag has started
  check('recovery: drag started', acts(out).includes('down:left'));
  g.down(2, 150, 300, 50);            // user adds a second finger to scroll
  check('recovery: the stray press is released, not left stuck',
        acts(out).filter((a) => a === 'up:left').length === 1, acts(out));
  g.move(1, 100, 200, 70); g.move(2, 150, 200, 70);
  check('recovery: now scrolling', out.some((a) => a.type === 'scroll'), acts(out));
}

// --- two finger tap = right click --------------------------------------
{
  const [g, out] = make();
  g.down(1, 100, 100, 0);
  g.down(2, 140, 100, 10);
  g.up(1, 100, 100, 60);
  g.up(2, 140, 100, 70);
  check('two-finger tap: right click', acts(out).join(',') === 'down:right,up:right', acts(out));
}

// --- press and hold is a right click -----------------------------------
{
  const [g, out] = make();
  g.down(1, 100, 100, 0);
  g.up(1, 100, 100, 900);
  check('long press: right click, the touch convention for a context menu',
    acts(out).join(',') === 'down:right,up:right', acts(out));
  check('long press: lands at the touch point', out[0].x === 100 && out[0].y === 100);
}

{
  const [g, out] = make();
  g.down(1, 100, 100, 0);
  g.up(1, 100, 100, 100);
  check('a quick tap is still a LEFT click', acts(out).join(',') === 'down:left,up:left',
    acts(out));
}

{
  // Between TAP_MS and LONG_PRESS_MS: too slow to mean a tap, too quick to
  // mean a hold. Guessing either way would be worse than doing nothing.
  const [g, out] = make();
  g.down(1, 100, 100, 0);
  g.up(1, 100, 100, 400);
  check('an in-between press does nothing', out.length === 0, acts(out));
}

{
  const [g, out] = make();
  g.down(1, 100, 100, 0);
  g.move(1, 160, 100, 200);
  g.up(1, 160, 100, 900);
  check('a long press that MOVED is a drag, not a right click',
    !out.some((a) => a.button === 'right'), acts(out));
}

// --- two-finger tap is judged on whether it scrolled, not on speed -----
{
  const [g, out] = make();
  g.down(1, 100, 300, 0);
  g.down(2, 140, 300, 30);
  // Real fingers always wobble a little; that must not cost a right click.
  g.move(1, 101, 301, 60);
  g.move(2, 141, 301, 60);
  g.up(1, 101, 301, 420);
  g.up(2, 141, 301, 450);
  const right = out.filter((a) => a.button === 'right');
  check('two-finger tap survives a wobble and a slow release',
    right.length === 2, acts(out));
}

{
  const [g, out] = make();
  g.down(1, 100, 300, 0);
  g.down(2, 140, 300, 5);
  g.move(1, 100, 240, 40);
  g.move(2, 140, 240, 40);
  g.up(1, 100, 240, 80);
  g.up(2, 140, 240, 90);
  check('a real two-finger scroll never ends in a right click',
    !out.some((a) => a.button === 'right'), out.map((a) => a.type));
}

{
  // scrollEmitted has to reset, or one scroll would suppress every later
  // two-finger tap for the life of the session.
  const [g, out] = make();
  g.down(1, 100, 300, 0); g.down(2, 140, 300, 5);
  g.move(1, 100, 240, 40); g.move(2, 140, 240, 40);
  g.up(1, 100, 240, 80); g.up(2, 140, 240, 90);
  out.length = 0;
  g.down(1, 100, 300, 200); g.down(2, 140, 300, 205);
  g.up(1, 100, 300, 300); g.up(2, 140, 300, 310);
  check('a tap after a scroll still right clicks',
    out.some((a) => a.button === 'right'), acts(out));
}

// --- pointercancel must not leave the button stuck ---------------------
{
  const [g, out] = make();
  g.down(1, 100, 100, 0);
  g.move(1, 200, 200, 40);
  g.cancel(1);
  check('cancel during a drag releases the button',
        acts(out)[acts(out).length - 1] === 'up:left', acts(out));
  check('cancel returns to idle', g.mode === 'idle');
}

// --- a second finger during the tap window cancels the click -----------
{
  const [g, out] = make();
  g.down(1, 100, 100, 0);
  g.down(2, 150, 100, 20);
  g.up(2, 150, 100, 40);
  g.up(1, 100, 100, 60);
  check('a two-finger gesture never emits a left click',
        !acts(out).includes('down:left'), acts(out));
}

// --- scroll phases: what makes it feel like a trackpad ----------------
{
  const [g, out] = make();
  g.down(1, 100, 300, 0); g.down(2, 140, 300, 10);
  g.move(1, 100, 280, 30); g.move(2, 140, 280, 30);
  g.move(1, 100, 260, 60); g.move(2, 140, 260, 60);
  const phases = out.filter((a) => a.type === 'scroll').map((a) => a.phase);
  check('phases: first scroll opens the gesture', phases[0] === 'begin', phases);
  check('phases: subsequent scrolls continue it', phases[1] === 'move', phases);
  g.up(1, 100, 260, 90); g.up(2, 140, 260, 95);
  const all = out.filter((a) => a.type === 'scroll').map((a) => a.phase);
  check('phases: releasing closes the gesture', all[all.length - 1] === 'end', all);
  check('phases: exactly one begin and one end',
        all.filter((p) => p === 'begin').length === 1 &&
        all.filter((p) => p === 'end').length === 1, all);
}

// --- three fingers = system gesture, never scroll or click -------------
for (const [name, dx, dy, dir] of [
  ['up', 0, -80, 'up'], ['down', 0, 80, 'down'],
  ['left', -80, 0, 'left'], ['right', 80, 0, 'right'],
]) {
  const [g, out] = make();
  g.down(1, 150, 400, 0); g.down(2, 190, 400, 5); g.down(3, 230, 400, 10);
  for (let step = 1; step <= 3; step++) {
    const fx = (dx / 3) * step, fy = (dy / 3) * step;
    g.move(1, 150 + fx, 400 + fy, 20 * step);
    g.move(2, 190 + fx, 400 + fy, 20 * step);
    g.move(3, 230 + fx, 400 + fy, 20 * step);
  }
  const swipes = out.filter((a) => a.type === 'swipe');
  check(`three fingers ${name}: emits swipe ${dir}`,
        swipes.length === 1 && swipes[0].direction === dir, out);
  check(`three fingers ${name}: no scroll or click`,
        out.every((a) => a.type === 'swipe'), acts(out));
  g.up(1, 0, 0, 200); g.up(2, 0, 0, 205); g.up(3, 0, 0, 210);
  check(`three fingers ${name}: fires once, not repeatedly`,
        out.filter((a) => a.type === 'swipe').length === 1);
}

// --- a small three-finger wobble must not fire a swipe -----------------
{
  const [g, out] = make();
  g.down(1, 150, 400, 0); g.down(2, 190, 400, 5); g.down(3, 230, 400, 10);
  g.move(1, 152, 403, 30); g.move(2, 192, 403, 30); g.move(3, 232, 403, 30);
  check('three-finger wobble does not fire a swipe', out.length === 0, out);
}

// --- a third finger during a scroll closes the scroll cleanly ----------
{
  const [g, out] = make();
  g.down(1, 100, 300, 0); g.down(2, 140, 300, 5);
  g.move(1, 100, 280, 20); g.move(2, 140, 280, 20);
  const before = out.filter((a) => a.type === 'scroll').length;
  g.down(3, 180, 300, 30);
  const ended = out.filter((a) => a.type === 'scroll' && a.phase === 'end');
  check('third finger closes the open scroll gesture', ended.length === 1, out.map((a) => a.phase));
  check('third finger switches to swipe mode', g.mode === 'swipe');
}

// --- pan mode: a magnified view moves the viewport, not the mouse -----
{
  const [g, out] = make();
  g.setPan(true);
  g.down(1, 200, 200, 0);
  g.move(1, 240, 230, 20);
  check('pan: one-finger drag emits pan, never a mouse button',
    out.every((a) => a.type === 'pan'), out);
  check('pan: first delta is measured from the anchor, not the far point',
    out[0].dx === 40 && out[0].dy === 30, out[0]);
  g.move(1, 250, 230, 40);
  check('pan: later deltas are incremental',
    out[1].dx === 10 && out[1].dy === 0, out[1]);
  g.up(1, 250, 230, 60);
  check('pan: releasing does not emit a click',
    out.every((a) => a.type === 'pan'), acts(out));
  check('pan: returns to idle', g.mode === 'idle');
}

{
  const [g, out] = make();
  g.setPan(true);
  g.down(1, 200, 200, 0);
  g.up(1, 201, 201, 80);
  check('pan: a tap still clicks while magnified',
    acts(out).join(',') === 'down:left,up:left', acts(out));
}

{
  const [g, out] = make();
  g.setPan(true);
  g.down(1, 100, 300, 0); g.down(2, 140, 300, 5);
  g.move(1, 100, 280, 20); g.move(2, 140, 280, 20);
  check('pan: two fingers still scroll while magnified',
    out.some((a) => a.type === 'scroll'), out);
  check('pan: two fingers never emit a pan',
    !out.some((a) => a.type === 'pan'), out);
}

{
  const [g, out] = make();
  g.setPan(true);
  g.down(1, 150, 400, 0); g.down(2, 190, 400, 5); g.down(3, 230, 400, 10);
  g.move(1, 150, 320, 30); g.move(2, 190, 320, 30); g.move(3, 230, 320, 30);
  check('pan: three fingers still gesture while magnified',
    out.some((a) => a.type === 'swipe' && a.direction === 'up'), out);
}

{
  const [g, out] = make();
  g.setPan(true);
  g.down(1, 200, 200, 0);
  g.move(1, 240, 230, 20);
  g.setPan(false);
  g.up(1, 240, 230, 40);
  g.down(2, 200, 200, 60);
  g.move(2, 240, 230, 80);
  check('pan: turning magnification off restores the mouse drag',
    out.some((a) => a.type === 'mouse' && a.action === 'down'), out);
}

{
  const [g, out] = make();
  g.setPan(true);
  g.down(1, 200, 200, 0);
  g.move(1, 240, 230, 20);
  g.cancel(1);
  check('pan: a cancelled pan leaves no button held',
    !out.some((a) => a.type === 'mouse'), out);
  check('pan: cancel returns to idle', g.mode === 'idle');
}

// --- the scroll dead zone --------------------------------------------
{
  const [g, out] = make();
  g.down(1, 100, 300, 0); g.down(2, 140, 300, 5);
  g.move(1, 103, 302, 20); g.move(2, 143, 302, 20);
  check('a small wobble does not scroll', out.length === 0, out);
  // Each finger reports separately, so crossing the zone emits as the
  // pointers arrive - what matters is that the total is not short.
  g.move(1, 100, 260, 40); g.move(2, 140, 260, 40);
  const scrolls = out.filter((a) => a.type === 'scroll');
  check('crossing the dead zone starts scrolling', scrolls.length > 0, out);
  const travelled = scrolls.reduce((sum, a) => sum + a.dy, 0);
  check('the held-back movement is not lost, so nothing jumps',
    Math.abs(travelled + 40) < 0.001, travelled);
  check('exactly one begin phase is opened',
    scrolls.filter((a) => a.phase === 'begin').length === 1, scrolls.map((a) => a.phase));
}

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
