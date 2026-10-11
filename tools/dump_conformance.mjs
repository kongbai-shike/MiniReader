/**
 * Dump golden samples from the FROZEN JavaScript reading kernel, so that the
 * Python port can be checked against it exactly instead of "roughly".
 *
 *   node tools/dump_conformance.mjs [book.txt ...]
 *
 * Writes tests/conformance.json:
 *
 *   cases[]   - small hand-written texts, embedded in full. These run everywhere,
 *               including on a machine with no novels on it.
 *   books[]   - real novels. The text is NOT embedded (megabytes, and it is the
 *               reader's own library, not ours); only the decoded text's SHA-256
 *               and the chapter title/offset list are recorded. The Python test
 *               skips a book it cannot find on disk.
 *
 * The kernel is imported exactly the way the plugin's own tests do it, through
 * `window.__ModuleLoader__`, so we test the shipped file and not a copy.
 */
import { createHash } from 'node:crypto';
import { existsSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const repo = resolve(here, '..');

const DEFAULT_PLUGIN = resolve(
  'C:/Users/pc/Desktop/随手文件/dsh-novel-wallpaper/client.js'
);
const pluginPath = process.env.MINIREADER_KERNEL || DEFAULT_PLUGIN;
if (!existsSync(pluginPath)) {
  console.error('kernel not found: ' + pluginPath);
  process.exit(1);
}

/* ── load the shipped kernel ─────────────────────────────────────────────── */
let captured = null;
globalThis.window = { __ModuleLoader__: { load: (cfg) => { captured = cfg; } } };
const ReactStub = {
  createElement: () => null,
  Fragment: {},
  useState: () => [undefined, () => {}],
  useEffect: () => {},
  useRef: () => ({ current: null }),
  useCallback: (f) => f,
  useSyncExternalStore: () => undefined
};
await import(pathToFileURL(pluginPath).href);
if (!captured) throw new Error('client.js did not register with the module loader');
const mod = captured.factory((name) => {
  if (name === 'react') return ReactStub;
  throw new Error('unexpected require: ' + name);
});
const K = mod.__internals;
const {
  decodeTxt, splitChaptersAuto, splitChapters, marksFrom, cleanTitle,
  detectChapterRe, CHAPTER_RULES, DEFAULT_CHAPTER_RE
} = K;

/**
 * scorePattern() is not exposed through __internals, so the ratio is mirrored here
 * from the plugin source. The MATCHING half - marksFrom() - is the shipped code, and
 * that is the half that could differ between engines; the arithmetic is validated
 * end-to-end by comparing the shipped detectChapterRe() result, which uses the real
 * scorePattern and its 0.5 short-ratio gate.
 */
function scoreLocal(text, rule) {
  const marks = marksFrom(text, rule.source);
  if (marks.length < 2) return null;
  const short = marks.filter((mk) => mk.title.length <= 40).length;
  return { id: rule.id, count: marks.length, shortRatio: short / marks.length };
}

const sha = (s) => createHash('sha256').update(s, 'utf8').digest('hex');

/* ── the small corpus ────────────────────────────────────────────────────── */
/* Every entry here exists because some real dump style broke the splitter. */

const body = (n, marker) =>
  Array.from({ length: n }, (_, i) => `${marker}第${i + 1}段正文内容，用于把章节撑到足够长。`).join('\n');

const CASES = [
  {
    name: 'plain 第N章 no space',
    text: ['第一章 开端', body(3, 'a'), '第二章 转折', body(3, 'b'), '第三章 结束', body(3, 'c')].join('\n')
  },
  {
    name: 'spaced 第 N 章 (H* inside the token)',
    text: ['第 1 章 开端', body(3, 'a'), '第 2 章 转折', body(3, 'b'), '第 31 章 远章', body(3, 'c')].join('\n')
  },
  {
    name: 'ideographic-space indent before heading',
    text: ['\u3000\u3000第一章 缩进标题', body(3, 'a'), '\u3000第二章 还是缩进', body(3, 'b')].join('\n')
  },
  {
    name: 'chinese numerals + 零〇两',
    text: ['第一百零八章 大章', body(3, 'a'), '第两百章 整百', body(3, 'b'), '第二〇三章 怪写法', body(3, 'c')].join('\n')
  },
  {
    name: 'bracket headings 【第N章】',
    text: ['【第3章 金属风暴】', body(3, 'a'), '【第4章 离开】', body(3, 'b')].join('\n')
  },
  {
    name: 'english Chapter N',
    text: ['Chapter 1 The Beginning', body(3, 'a'), 'Chapter 2 The Middle', body(3, 'b')].join('\n')
  },
  {
    name: 'special headings 楔子/番外',
    text: ['楔子', body(3, 'a'), '第一章 正文开始', body(3, 'b'), '番外 后来的事', body(3, 'c')].join('\n')
  },
  {
    name: 'volume headings 第N卷',
    text: ['第一卷 风起', body(3, 'a'), '第一章 开端', body(3, 'b'), '第二章 转折', body(3, 'c')].join('\n')
  },
  {
    name: 'body glued onto the heading with two ideographic spaces',
    text: [
      '第3章 标题　　正文从标题后面就开始了，这一段的长度必须超过三十个字符才会被当作正文切开，否则标题要留住。',
      body(3, 'a'),
      '第4章 另一个　　同样，这一段也要足够长，才能证明粘连检测真的生效了，不是碰巧。',
      body(3, 'b')
    ].join('\n')
  },
  {
    name: 'mid-line 第N章 must NOT match',
    text: ['第一章 开端', '他说：我在第3章里提过这件事，但这行不是标题。', body(3, 'a'), '第二章 转折', body(3, 'b')].join('\n')
  },
  {
    name: 'missing chapter numbers (pirate gaps)',
    text: ['第1章 一', body(2, 'a'), '第2章 二', body(2, 'b'), '第5章 五', body(2, 'c'), '第9章 九', body(2, 'd')].join('\n')
  },
  {
    name: 'preface before the first heading',
    text: ['本文件来自某书苑，仅供测试。', '-----------------------------------', '第一章 开端', body(3, 'a'), '第二章 转折', body(3, 'b')].join('\n')
  },
  {
    name: 'no headings at all -> single 全文 chapter',
    text: body(40, 'x')
  },
  {
    name: 'bare numeric lines must not be trusted (cn-num is risky)',
    text: ['2024', body(2, 'a'), '123', body(2, 'b'), '7', body(2, 'c'), '第1章 真的标题', body(2, 'd'), '第2章 又一个', body(2, 'e')].join('\n')
  },
  {
    name: 'paragraph-length matches must make detectChapterRe refuse',
    text: Array.from({ length: 12 }, (_, i) => `第${i + 1}章 ${'很长的正文内容'.repeat(12)}`).join('\n')
  },
  {
    name: 'heading with a very long title (>40 chars -> not short)',
    text: [
      '第1章 ' + '超长标题'.repeat(12),
      body(3, 'a'),
      '第2章 正常标题',
      body(3, 'b'),
      '第3章 正常标题二',
      body(3, 'c')
    ].join('\n')
  },
  {
    name: 'CRLF line endings',
    text: ['第一章 开端', body(3, 'a'), '第二章 转折', body(3, 'b')].join('\r\n')
  },
  {
    name: 'trailing whitespace inside lines (offset parity guard)',
    text: ['第一章 开端   ', body(3, 'a'), '第二章 转折\t', body(3, 'b')].join('\n')
  },
  {
    name: 'empty-ish text',
    text: '   \n\n  '
  },
  {
    name: 'one heading only',
    text: ['第一章 孤独的一章', body(5, 'a')].join('\n')
  }
];

/* ── helpers ─────────────────────────────────────────────────────────────── */

function dumpChapters(text, chapters) {
  return chapters.map((ch) => ({
    title: ch.title,
    start: ch.start,
    end: ch.end,
    preface: !!ch.preface,
    sha256: sha(text.slice(ch.start, ch.end))
  }));
}

function snapshot(text, pattern) {
  const auto = splitChaptersAuto(text, pattern ?? null);
  return {
    auto: {
      ruleId: auto.ruleId ?? null,
      count: auto.count,
      chapters: dumpChapters(text, auto.chapters)
    },
    rules: [
      { id: 'default', source: DEFAULT_CHAPTER_RE },
      ...CHAPTER_RULES.map((r) => ({ id: r.id, source: r.source, risky: !!r.risky }))
    ].map((r) => {
      const score = scoreLocal(text, r);
      return { id: r.id, count: score ? score.count : null, shortRatio: score ? score.shortRatio : null };
    }),
    detected: (() => {
      const hit = detectChapterRe(text);
      return hit ? { id: hit.id, count: hit.count, shortRatio: hit.shortRatio } : null;
    })()
  };
}

const hasAstral = (s) => {
  for (const ch of s) if (ch.codePointAt(0) > 0xffff) return true;
  return false;
};

/* ── build the file ──────────────────────────────────────────────────────── */

const problems = [];
const cases = CASES.map((c) => {
  if (hasAstral(c.text)) problems.push(`case "${c.name}" contains astral chars; offsets will not match Python`);
  const snap = snapshot(c.text);
  return { name: c.name, text: c.text, ...snap };
});

const books = [];
for (const arg of process.argv.slice(2)) {
  const path = resolve(arg);
  if (!existsSync(path)) {
    problems.push(`book not found: ${path}`);
    continue;
  }
  const raw = readFileSync(path);
  const { text: decoded, encoding } = decodeTxt(raw.buffer.slice(raw.byteOffset, raw.byteOffset + raw.byteLength));
  const text = decoded.replace(/\r\n?/g, '\n');
  const t0 = Date.now();
  const snap = snapshot(text);
  const ms = Date.now() - t0;
  if (hasAstral(text)) problems.push(`book ${path} contains astral chars; offsets will not match Python`);
  books.push({
    path,
    name: path.split(/[\\/]/).pop(),
    encoding,
    bytes: raw.length,
    chars: text.length,
    textSha256: sha(text),
    splitMs: ms,
    ruleId: snap.auto.ruleId,
    count: snap.auto.count,
    chapters: snap.auto.chapters,
    rules: snap.rules,
    detected: snap.detected
  });
  console.log(
    `  ${path}\n    ${encoding}  ${text.length} chars  ${snap.auto.count} chapters  ` +
      `rule=${snap.auto.ruleId ?? 'default'}  ${ms}ms`
  );
}

const out = {
  generatedBy: 'tools/dump_conformance.mjs',
  kernel: pluginPath,
  note:
    'Generated by running the frozen JavaScript reading kernel from the ' +
    'dsh-novel-wallpaper plugin. Python must reproduce every value exactly. ' +
    'Offsets are compared as code points; the corpus is astral-free so Python ' +
    'code points and JS UTF-16 units agree.',
  // The literal pattern sources, so the Python port proves its regexes are the
  // same strings rather than merely behaving similarly on today's corpus.
  kernelConstants: {
    DEFAULT_CHAPTER_RE,
    rules: CHAPTER_RULES.map((r) => ({ id: r.id, source: r.source, risky: !!r.risky }))
  },
  cases,
  books
};

const target = join(repo, 'tests', 'conformance.json');
writeFileSync(target, JSON.stringify(out, null, 1), 'utf8');
console.log(`\nwrote ${target}`);
console.log(`  ${cases.length} cases, ${books.length} books`);
if (problems.length) {
  console.log('\nWARNINGS:');
  for (const p of problems) console.log('  - ' + p);
}
