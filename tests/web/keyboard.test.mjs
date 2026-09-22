/* Extracts the REAL keyCodeForInputType() from app.js so the mapping cannot
 * drift from what ships. iOS never gives a usable KeyboardEvent.code for the
 * soft keyboard, so this map is the only way backspace and Enter work. */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const source = readFileSync(join(here, '../../src/machop/web/app.js'), 'utf8');

const start = source.indexOf('  function keyCodeForInputType(');
if (start < 0) throw new Error('keyCodeForInputType() not found in app.js');
let depth = 0, end = start;
for (let i = source.indexOf('{', start); i < source.length; i++) {
  if (source[i] === '{') depth++;
  else if (source[i] === '}' && --depth === 0) { end = i + 1; break; }
}
const keyCodeForInputType = new Function(
  `${source.slice(start, end)}; return keyCodeForInputType;`)();

let pass = 0, fail = 0;
const check = (name, ok) => ok ? (pass++, console.log('  ok   ' + name))
                               : (fail++, console.log('  FAIL ' + name));

check('deleteContentBackward -> Backspace', keyCodeForInputType('deleteContentBackward') === 'Backspace');
check('deleteContentForward -> Delete', keyCodeForInputType('deleteContentForward') === 'Delete');
check('insertLineBreak -> Enter', keyCodeForInputType('insertLineBreak') === 'Enter');
check('insertParagraph -> Enter', keyCodeForInputType('insertParagraph') === 'Enter');
check('insertText -> null (goes through the literal-text path)', keyCodeForInputType('insertText') === null);
check('insertFromPaste -> null', keyCodeForInputType('insertFromPaste') === null);
check('undefined -> null, no throw', keyCodeForInputType(undefined) === null);
check('empty string -> null', keyCodeForInputType('') === null);

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
