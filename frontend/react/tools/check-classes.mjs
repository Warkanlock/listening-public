// Class contract check (plan 050, step 1a): every class name the
// app's TSX names must have a rule in src/styles.css. The React
// conversion's recurring defect was markup inventing class names the
// retargeted stylesheet never defined (the activity dialog's first
// draft, `tracks-tools`), which builds and serves fine and only
// shows up as broken layout on a rendered page. This check makes
// that drift a build failure instead.
//
// What is checked, honestly:
// - Plain className strings and the string literals inside
//   className expressions (ternary branches included) are extracted
//   token by token; each token must appear as a class selector in
//   styles.css.
// - A token glued to an interpolation (`ui-state--${kind}`) cannot
//   be resolved statically; it passes when the stylesheet defines
//   at least one class in that static-prefix family. The summary
//   line counts these as family-checked, never as verified.
// - A token that is entirely an interpolation (`${track.status}`)
//   and className values passed through as props are out of static
//   reach: counted in the summary, never failed, never claimed as
//   checked. The rendered gate (make verify-ui) covers what this
//   check cannot see.
// - Only classes named in the app's own TSX are in scope. Classes
//   the @ui-sdk primitives render themselves are not the app's
//   classes (the app does not import the SDK stylesheet), so they
//   are neither extracted nor required.

import { readdirSync, readFileSync } from "node:fs";
import { dirname, join, relative } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const srcDir = join(root, "src");
const cssPath = join(srcDir, "styles.css");

// -- styles.css: the class selectors it defines ----------------------

function cssClasses(css) {
  const text = css.replace(/\/\*[\s\S]*?\*\//g, "");
  const names = new Set();
  let pos = 0;
  for (;;) {
    const open = text.indexOf("{", pos);
    if (open === -1) break;
    // The selector is what follows the previous rule's closing brace
    // (and any declaration text before it) up to this opening brace.
    let chunk = text.slice(pos, open);
    chunk = chunk.slice(chunk.lastIndexOf("}") + 1);
    chunk = chunk.slice(chunk.lastIndexOf(";") + 1);
    for (const match of chunk.matchAll(/\.(-?[_a-zA-Z][_a-zA-Z0-9-]*)/g)) {
      names.add(match[1]);
    }
    pos = open + 1;
  }
  return names;
}

// -- TSX scanning -----------------------------------------------------
// A small hand scanner: enough of the JSX/TS surface to find
// className attributes and walk their expressions without a parser
// dependency. Strings and template literals are read exactly;
// comments are skipped. Anything the scanner cannot resolve is
// counted, not guessed at.

function skipString(text, i) {
  // i is at the opening quote (' or "): return the index past the close.
  const quote = text[i];
  i += 1;
  while (i < text.length) {
    if (text[i] === "\\") i += 2;
    else if (text[i] === quote) return i + 1;
    else i += 1;
  }
  return i;
}

function readTemplate(text, i, sink) {
  // i is at the opening backtick. Static chunks and interpolations
  // are appended to sink.parts as {text} / {expr} items; returns the
  // index past the closing backtick.
  let chunk = "";
  i += 1;
  while (i < text.length) {
    const ch = text[i];
    if (ch === "\\") {
      chunk += text.slice(i, i + 2);
      i += 2;
    } else if (ch === "`") {
      sink.parts.push({ text: chunk });
      return i + 1;
    } else if (ch === "$" && text[i + 1] === "{") {
      sink.parts.push({ text: chunk });
      chunk = "";
      const inner = { parts: [] };
      i = readExpression(text, i + 2, inner, true);
      sink.parts.push({ expr: inner.parts });
    } else {
      chunk += ch;
      i += 1;
    }
  }
  sink.parts.push({ text: chunk });
  return i;
}

// Only string literals in value position are class text: the
// branches of a ternary, the operands of || / && / +, a call or
// array argument. A literal that is the operand of a comparison
// (`store.view === "all" ? ...`) is a condition, not a class, and
// collecting it would glue condition words onto real class names.
// `prev` tracks the last significant token category; a literal is
// class text when prev is one of the value openers.
const VALUE_OPENERS = new Set([
  "start", "?", ":", "||", "&&", "??", "(", ",", "+", "[", "=>", "!",
]);

function readExpression(text, i, sink) {
  // Walk an expression, collecting value-position string literals
  // as {text} parts and template literals as their own part lists.
  // Stops just past the "}" that closes the brace this call was
  // opened for (the className attribute or a ${} interpolation).
  let depth = 0;
  let prev = "start";
  while (i < text.length) {
    const ch = text[i];
    if (/\s/.test(ch)) {
      i += 1;
    } else if (ch === '"' || ch === "'") {
      const end = skipString(text, i);
      if (VALUE_OPENERS.has(prev)) {
        sink.parts.push({ text: text.slice(i + 1, end - 1) });
      }
      prev = "value";
      i = end;
    } else if (ch === "`") {
      const inner = { parts: [] };
      i = readTemplate(text, i, inner);
      sink.parts.push({ template: inner.parts });
      prev = "value";
    } else if (ch === "/" && text[i + 1] === "/") {
      const end = text.indexOf("\n", i);
      i = end === -1 ? text.length : end;
    } else if (ch === "/" && text[i + 1] === "*") {
      const end = text.indexOf("*/", i + 2);
      i = end === -1 ? text.length : end + 2;
    } else if (ch === "{") {
      depth += 1;
      prev = "{";
      i += 1;
    } else if (ch === "}") {
      if (depth === 0) return i + 1;
      depth -= 1;
      prev = "value";
      i += 1;
    } else if (text.startsWith("===", i) || text.startsWith("!==", i)) {
      prev = "cmp";
      i += 3;
    } else if (text.startsWith("=>", i)) {
      prev = "=>";
      i += 2;
    } else if (
      text.startsWith("&&", i) || text.startsWith("||", i) ||
      text.startsWith("??", i) || text.startsWith("==", i) ||
      text.startsWith("!=", i) || text.startsWith("<=", i) ||
      text.startsWith(">=", i)
    ) {
      prev = text.slice(i, i + 2) === "&&" || text.slice(i, i + 2) === "||" ||
        text.slice(i, i + 2) === "??"
        ? text.slice(i, i + 2)
        : "cmp";
      i += 2;
    } else if ("?:(,+[!".includes(ch)) {
      prev = ch;
      i += 1;
    } else if (ch === ")" || ch === "]") {
      prev = "value";
      i += 1;
    } else if (ch === "<" || ch === ">") {
      prev = "cmp";
      i += 1;
    } else {
      // Identifiers, numbers, member dots, other operators: an
      // operand or glue that ends any value-opener state.
      prev = "value";
      i += 1;
    }
  }
  return i;
}

function firstText(list) {
  // The first literal chunk in a part list, or null when an
  // interpolation (or nothing) comes first.
  for (const part of list) {
    if (part.text !== undefined) {
      if (part.text !== "") return part.text;
    } else if (part.template) {
      const inner = firstText(part.template);
      if (inner !== null) return inner;
    } else {
      return null;
    }
  }
  return null;
}

function hasLiteral(list) {
  return list.some(
    (part) =>
      (part.text !== undefined && part.text.trim() !== "") ||
      (part.template !== undefined && hasLiteral(part.template)) ||
      (part.expr !== undefined && hasLiteral(part.expr)),
  );
}

function tokensFromParts(parts, out) {
  // Flatten a part list into class tokens. A token bounded by
  // whitespace or the ends of the value is complete and checked
  // exactly. A token glued to an interpolation that contributes no
  // literal (`ui-state--${kind}`) keeps its static prefix and is
  // dynamic: only its family can be checked. String literals inside
  // an interpolation (ternary branches) join the stream as complete
  // tokens, exactly like literals outside one.
  let current = null;
  const flush = () => {
    if (current !== null && (current.name !== "" || current.dynamic)) {
      out.push(current);
    }
    current = null;
  };
  const feedText = (value) => {
    for (const piece of value.split(/(\s+)/)) {
      if (!piece) continue;
      if (/^\s+$/.test(piece)) flush();
      else {
        if (current === null) current = { name: "", dynamic: false };
        current.name += piece;
      }
    }
  };
  const walk = (list) => {
    for (const part of list) {
      if (part.text !== undefined) feedText(part.text);
      else if (part.template) walk(part.template);
      else if (part.expr) {
        if (!hasLiteral(part.expr)) {
          // A pure interpolation: it glues onto the token in
          // progress, or stands alone as a wholly dynamic token.
          if (current === null) current = { name: "", dynamic: true };
          else current.dynamic = true;
        } else {
          const lead = firstText(part.expr);
          if (current !== null && lead !== null && !/^\s/.test(lead)) {
            current.dynamic = true;
          }
          walk(part.expr);
        }
      }
    }
  };
  walk(parts);
  flush();
  return out;
}

function classNamesIn(source) {
  const found = [];
  let unresolved = 0;
  const pattern = /\bclassName\b/g;
  let match;
  while ((match = pattern.exec(source)) !== null) {
    let i = match.index + match[0].length;
    while (/\s/.test(source[i] ?? "")) i += 1;
    if (source[i] !== "=") continue;
    i += 1;
    while (/\s/.test(source[i] ?? "")) i += 1;
    const sink = { parts: [] };
    if (source[i] === '"' || source[i] === "'") {
      const end = skipString(source, i);
      sink.parts.push({ text: source.slice(i + 1, end - 1) });
      i = end;
    } else if (source[i] === "{") {
      i = readExpression(source, i + 1, sink);
    } else {
      continue;
    }
    const before = found.length;
    tokensFromParts(sink.parts, found);
    if (found.length === before) unresolved += 1;
    pattern.lastIndex = Math.max(pattern.lastIndex, i);
  }
  return { found, unresolved };
}

function tsxFiles(dir) {
  const files = [];
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) files.push(...tsxFiles(path));
    else if (entry.isFile() && entry.name.endsWith(".tsx")) files.push(path);
  }
  return files.sort();
}

// -- the check --------------------------------------------------------

const defined = cssClasses(readFileSync(cssPath, "utf8"));
const failures = [];
let checked = 0;
let familyChecked = 0;
let whollyDynamic = 0;
let unresolvedValues = 0;

for (const file of tsxFiles(srcDir)) {
  const { found, unresolved } = classNamesIn(readFileSync(file, "utf8"));
  unresolvedValues += unresolved;
  for (const token of found) {
    if (token.dynamic) {
      if (token.name === "") {
        whollyDynamic += 1; // wholly interpolated: out of reach
        continue;
      }
      const family = [...defined].some((name) => name.startsWith(token.name));
      if (family) familyChecked += 1;
      else failures.push(`${relative(root, file)}: class family "${token.name}…" has no rule in styles.css`);
    } else if (defined.has(token.name)) {
      checked += 1;
    } else {
      failures.push(`${relative(root, file)}: class "${token.name}" has no rule in styles.css`);
    }
  }
}

if (failures.length) {
  console.error("check-classes: FAILED");
  for (const failure of [...new Set(failures)]) console.error(`  ${failure}`);
  process.exit(1);
}
console.log(
  `check-classes: ${checked} classes verified against styles.css, ` +
    `${familyChecked} dynamic families present, ` +
    `${whollyDynamic} wholly dynamic tokens and ${unresolvedValues} values out of static reach`,
);
