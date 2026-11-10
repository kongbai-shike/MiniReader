"""Chapter splitting - a line-for-line port of the reading kernel in the
dsh-novel-wallpaper plugin (client.js:41-394).

This module is the crown jewel being reused, so it is a *port*, not a rewrite.
Every non-obvious behaviour below was paid for by a real book breaking:

* `[ \\t\\u3000]` (horizontal whitespace only) instead of `\\s`. Using `\\s` lets a
  heading pattern match across a line break and silently eats the first line of
  every chapter.
* `H*` sits INSIDE the `第…章` token. Real dumps write `第 31 章`; a pattern that
  only allows space after `第` matches zero headings and the book becomes one
  giant chapter.
* `score_pattern` / `detect_chapter_re` require `short_ratio >= 0.5`. Matching is
  not the same as being right: a loose rule hits body text and produces thousands
  of "chapters" whose "title" is a whole paragraph. Refusing is better than
  inventing a table of contents.
* `cn-num` is marked `risky` and excluded from the default union, because a bare
  `123` at the start of a line is far more often a year or a count than a heading.
* Ghost headings are merged, not counted (`merge_ghost_headings`). A pirate dump
  writes every chapter title twice: once alone on its line, then again with the
  body attached. Both lines match, so a naive splitter emits an empty chapter
  before every real chapter - 337 empty chapters and every entry listed twice on
  the 771-chapter book that was reported. This is the ONE place where the port is
  deliberately ahead of the JS kernel, so the conformance corpus contains no ghost
  pair: it pins everything else, and this file's own tests pin this.
* `split_chapters_auto` never overrides a working user pattern: it returns
  `rule_id=None` in that case so the UI can say "used your regex" honestly.

The port is verified against the frozen JS kernel by `tests/test_core.py`, which
replays golden samples dumped by `tools/dump_conformance.mjs`.
"""

from __future__ import annotations

import difflib
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass

# Horizontal whitespace only. NOT \s - see the module docstring.
H = "[ \t\u3000]"
CN_NUM = "[0-9０-９一二三四五六七八九十百千零〇两]+"
CN_UNIT = "[章节回卷篇]"


@dataclass(frozen=True)
class ChapterRule:
    id: str
    source: str
    risky: bool = False


CHAPTER_RULES: tuple[ChapterRule, ...] = (
    ChapterRule("cn", f"^{H}*第{H}*{CN_NUM}{H}*{CN_UNIT}"),
    ChapterRule("cn-vol", f"^{H}*第{H}*{CN_NUM}{H}*[卷部篇]"),
    ChapterRule("cn-special", f"^{H}*(?:楔子|序章|序言|引子|尾声|终章|后记|番外|完本感言)"),
    ChapterRule("cn-bracket", f"^{H}*[【〔〖「『（(\\[]{H}*第{H}*{CN_NUM}{H}*{CN_UNIT}"),
    # Deliberately risky: a bare number at line start is usually a year or a count.
    ChapterRule("cn-num", f"^{H}*[0-9０-９]{{1,4}}{H}*(?:[.、,，:：)）]{H}*\\S.*)?$", risky=True),
    ChapterRule("en", f"^{H}*Chapter{H}+[0-9IVXLCivxlc]+"),
)

# The union of every non-risky rule, `^` stripped because the alternation provides it.
DEFAULT_CHAPTER_RE = "^(?:" + "|".join(
    r.source[1:] for r in CHAPTER_RULES if not r.risky
) + ")"

TIGHTEN_HEAD_RE = re.compile(f"^第{H}*({CN_NUM}){H}*({CN_UNIT})")

# Group 1 = opening bracket, 2 = the title inside it, 3 = closing bracket, 4 = the
# rest of the line. Note the inner class excludes \n: without that, a heading like
# "【第3章" with no closing bracket on the same line would swallow the whole file.
#
# The closing-bracket class ends with \]] - an escaped ] and then the ] that closes
# the class. Drop that second ] and group 4 is silently swallowed into the class:
# the regex still compiles, still matches, and then explodes at run time with
# "no such group". That is exactly what happened here, on a real book, so
# tests/test_core.py now asserts the group count of every pattern.
BRACKET_HEAD_RE = re.compile(
    "^([【〔〖「『（(\\[])([^】〕〗」』）)\\]\\n]{1,24})([】〕〗」』）)\\]])([^\\n]*)$"
)

# UI strings the chapter model needs. Mirrors DICT.zh in the plugin exactly, so the
# conformance dump compares like for like.
TXT_FULL = "全文"
TXT_PREFACE = "前言"
TXT_CHAPTER = "章节"

RULE_LABELS: dict[str, str] = {
    "default": "默认（全部常见格式）",
    "cn": "第 N 章",
    "cn-vol": "卷 / 部 / 篇",
    "cn-special": "楔子 / 番外",
    "cn-bracket": "【第 N 章】",
    "cn-num": "纯数字标题",
    "en": "Chapter N",
}

_GAP_RE = re.compile(f"{H}{{2,}}")
_WS_RE = re.compile(r"\s+")
_LEAD_RE = re.compile(f"^{H}+")

# A heading line longer than this is prose that happened to match.
SHORT_TITLE_LEN = 40

# Safety valve for pathological patterns that match on nearly every character.
_MARK_GUARD = 200_000


@dataclass(frozen=True)
class Chapter:
    title: str
    start: int
    end: int
    preface: bool = False

    @property
    def length(self) -> int:
        return self.end - self.start


@dataclass(frozen=True)
class SplitResult:
    chapters: list[Chapter]
    rule_id: str | None
    count: int

    @property
    def rule_label(self) -> str:
        return RULE_LABELS.get(self.rule_id or "default", self.rule_id or "默认")


def clean_title(line: str) -> str:
    """Turn a heading line into a table-of-contents entry.

    A heading is followed by body text in every dump style, and some of them glue
    the first paragraph onto the heading line with two ideographic spaces. Cut
    there - but only when what follows is long enough to be prose, so a title that
    merely contains a double space survives.
    """
    s = _LEAD_RE.sub("", str(line or ""))
    bm = BRACKET_HEAD_RE.match(s)
    if bm:
        rest = _LEAD_RE.sub("", bm.group(4))
        s = f"{bm.group(2)} {rest}" if rest else bm.group(2)
    gap = _GAP_RE.search(s)
    if gap and len(s) - gap.end() >= 30:
        s = s[: gap.start()]
    s = TIGHTEN_HEAD_RE.sub("第\\1\\2", s)
    s = _WS_RE.sub(" ", s).strip()
    return s[:80]


def compile_pattern(pattern_source: str | None) -> re.Pattern[str]:
    """Compile a user pattern, falling back to the default - exactly as the plugin did."""
    source = pattern_source or DEFAULT_CHAPTER_RE
    try:
        return re.compile(source, re.MULTILINE)
    except re.error:
        return re.compile(DEFAULT_CHAPTER_RE, re.MULTILINE)


def marks_from(text: str, pattern_source: str | None = None) -> list[Chapter]:
    """Every heading line, in document order.

    The pattern only anchors the heading token, so the WHOLE line becomes the
    title: "第一章 开端" is a useful TOC entry, "第一章" alone is not. The line
    around the match is resolved from the match start rather than from the match
    itself, so a leading newline can never produce an empty title.
    """
    rex = compile_pattern(pattern_source)
    marks: list[Chapter] = []
    for n, m in enumerate(rex.finditer(text)):
        if n >= _MARK_GUARD:
            break
        line_start = text.rfind("\n", 0, m.start()) + 1
        line_end = text.find("\n", m.start())
        if line_end == -1:
            line_end = len(text)
        marks.append(Chapter(clean_title(text[line_start:line_end]), line_start, line_end))
    return marks


def chapters_from_marks(text: str, marks: list[Chapter]) -> list[Chapter]:
    """Turn heading positions into [start, end) chapter ranges."""
    if not marks:
        return [Chapter(TXT_FULL, 0, len(text))]
    chapters: list[Chapter] = []
    if marks[0].start > 0:
        chapters.append(Chapter(TXT_PREFACE, 0, marks[0].start, preface=True))
    for i, mark in enumerate(marks):
        end = marks[i + 1].start if i + 1 < len(marks) else len(text)
        chapters.append(Chapter(mark.title or f"{TXT_CHAPTER} {i + 1}", mark.start, end))
    return chapters


# ---- ghost headings -------------------------------------------------------
#
# A pirate dump writes every chapter title TWICE:
#
#     第1章 沈河
#     　　第1章 沈河
#     　　静寂的房间中……
#
# The first line is the title alone, the second repeats it with the body glued
# on. Both are heading-shaped, so a splitter that counts matches hands back two
# chapters per chapter: the first one's body is empty (chapter_body() strips its
# only line as the duplicated heading), the second one holds the real text. On
# the 771-chapter book this was reported against, that is 337 empty chapters and
# every entry listed twice.
#
# The two titles in a pair are not always identical, because the dump is not one
# export. Seen in the wild:
#
#     第7章，聂风他爹！        /  第7章，聂风.他爹！      (punctuation differs)
#     第六十一章 剧终           /  第62章 剧终             (Chinese vs Arabic numeral)
#     第103章 进军实体书行业？（上） / 第104章 进军实体书行业？（上）  (renumbered)
#     第37章 仙法砂秘术天碍震星！   /  第37章 仙法·砂秘术·天碍震星！ (punctuation inserted)
#     第135章，‘别人笑我太疯癫，我笑他人 / ‘…看不穿’的星爷      (title truncated)
#
# So the match is on a normalised "family": full-width folded, punctuation and
# zero-width junk dropped, unit (章/节/卷/回) equal, and the remaining title text
# equal - or one a prefix of the other, which is the truncated-title case.
#
# The number is allowed to differ ONLY when the number STYLE differs (Chinese
# 六十一 vs Arabic 62). Two consecutive Arabic numbers with the same title is a
# renumbered pair that is worth merging only if the titles agree; a bare
# "第二十章 / 第十九章" pair with NO title text is a countdown inside the prose
# (【第二十章】【第十九章】【第十一章】 in 残血玩家), not a heading, and the rule
# below refuses to touch it: with an empty title there is nothing to confirm it
# with.

_HSPACE = "[ \t\u3000]"

#: Unicode general categories that carry no meaning in a title comparison:
#: punctuation (P*), separators other than the spaces already collapsed (Z*),
#: symbols (S*) - which includes the 【】 the dump wraps some titles in - and
#: format characters (Cf: zero-width space, ZWJ, soft hyphen).
_GHOST_DROP_CATEGORIES = frozenset("PZSC")

#: The characters that mean "this number is written in Chinese", for the style
#: test. A digit string like "62" is the other style.
_CN_DIGITS = frozenset("一二三四五六七八九十百千零〇两")

_GHOST_HEAD_RE = re.compile(
    f"^{_HSPACE}*第{_HSPACE}*([0-9]+|[一二三四五六七八九十百千零〇两]+){_HSPACE}*([章节回卷篇])?(.*)$"
)


def _ghost_flat(title: str) -> str:
    """Fold a title down to the characters that decide whether two are related.

    NFKC first, because the dumps mix full-width and half-width punctuation for
    the same title; then casefold, because the bare copy is often title-cased
    and the real one was lower-cased ("第14章 杰瑞泰森，J形形意！" vs
    "第14章 杰瑞·泰森，j形形意！", "第73章 CP0全灭" vs "第73章 cp0全灭");
    then every character whose general category carries no meaning in a
    comparison is dropped.
    """
    folded = unicodedata.normalize("NFKC", title or "").casefold()
    return "".join(
        ch for ch in folded if unicodedata.category(ch)[0] not in _GHOST_DROP_CATEGORIES
    )


def _ghost_key(title: str) -> tuple[str, str, str]:
    """(number, unit, title text) - the three parts a ghost pair must agree on."""
    flat = _ghost_flat(title)
    m = _GHOST_HEAD_RE.match(flat)
    if not m:
        return ("", "", flat)
    return (m.group(1) or "", m.group(2) or "", (m.group(3) or "").strip())


def _numeral_style(number: str) -> str:
    """'cn' or 'ascii'. Two numbers of the same style naming one chapter is a
    renumbering; two of different styles is one heading written two ways."""
    if not number:
        return ""
    return "cn" if any(ch in _CN_DIGITS for ch in number) else "ascii"


#: How close two titles must be, after `_ghost_flat`, to count as the same
#: heading typed two ways. The pairs observed in real dumps land at 0.90-0.98
#: (`超级棒棒糖`/`超级棒棒`, `铁窗泪`/`の铁窗泪`, `☆′益` c`/`☆´益` c`), while
#: unrelated neighbours sit below 0.6.
_GHOST_TEXT_SIMILAR = 0.85

#: Beyond this, a "title" is a paragraph of body text that a loose rule matched,
#: not a heading - the pair is never a ghost pair no matter how alike it looks.
_GHOST_MAX_TITLE = 60


def _ghost_similarity(a: str, b: str) -> float:
    """How much of the two title texts is shared, 0.0 to 1.0."""
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def is_ghost_pair(ghost: str, real: str) -> bool:
    """Is `ghost` a bare duplicate of the heading on `real`?

    Both titles are table-of-contents strings, already through clean_title().
    The caller has already established that no body text sits between them, so
    the only question left is whether they name the same chapter.
    """
    g_num, g_unit, g_text = _ghost_key(ghost)
    r_num, r_unit, r_text = _ghost_key(real)
    if g_unit != r_unit:
        return False
    if not g_text or not r_text:
        # One of them carries no title text at all, so there is nothing to
        # confirm the pair with beyond the number. 【第二十章】 followed by
        # 【第十九章】 in 残血玩家 is a countdown inside the prose, not a heading,
        # and it stays out of here because the numbers differ.
        return g_text == r_text and g_num == r_num
    if g_text == r_text:
        # Same words. A different number written the same way is a renumber
        # (`第103章 进军实体书行业？（上）` / `第104章 ...`); a different number
        # written differently is the same heading twice
        # (`第六十一章 剧终` / `第62章 剧终`).
        return g_num == r_num or _numeral_style(g_num) != _numeral_style(r_num)
    if not g_unit:
        # No 章/节/卷 marker, so a line starting with 第N部/第N回 could just as
        # well be prose (`第一部的拍摄分为两部分。`). Only an exact repeat above
        # is trusted for these.
        return False
    if len(g_text) > _GHOST_MAX_TITLE or len(r_text) > _GHOST_MAX_TITLE:
        # A "title" longer than a heading is a paragraph some loose rule hit.
        return False
    if g_num and g_num == r_num:
        # One title is the other typed slightly wrong or cut short:
        # `让我来看看怎么个事` / `让我来看看怎幺个事`, `关意vs老年斑` / `关意v老年斑`,
        # `...的人笑我太疯癫...` truncated mid-title.
        return (
            g_text.startswith(r_text)
            or r_text.startswith(g_text)
            or _ghost_similarity(g_text, r_text) >= _GHOST_TEXT_SIMILAR
        )
    return (
        _numeral_style(g_num) == _numeral_style(r_num)
        and _ghost_similarity(g_text, r_text) >= _GHOST_TEXT_SIMILAR
    )


def merge_ghost_headings(text: str, marks: list[Chapter]) -> list[Chapter]:
    """Drop a heading that has no text of its own and whose next heading repeats it.

    A mark is a ghost when the text between it and the NEXT mark is blank and that
    next mark names the same chapter (see `is_ghost_pair`). The pair is one
    chapter: it starts at the ghost's line and takes the real heading's title, so
    no text is orphaned in front of it and `chapter_body` only has to strip the
    duplicated heading lines.

    Only adjacent pairs are considered, and a pair is consumed once, so a title
    repeated three times is one chapter (first two are ghosts) rather than zero.

    `text` is only read between marks - never rewritten - so every offset the
    caller already has stays valid.
    """
    if len(marks) < 2:
        return marks
    work = list(marks)
    kept: list[Chapter] = []
    i = 0
    total = len(work)
    while i < total:
        cur = work[i]
        nxt = work[i + 1] if i + 1 < total else None
        if nxt is not None and not text[cur.end : nxt.start].strip():
            if is_ghost_pair(cur.title, nxt.title):
                # The pair becomes ONE heading at the ghost's position, carrying the
                # real title: chapter_body() then strips the duplicated heading lines
                # off the front of the body. Keeping the ghost's start is what stops
                # a leading ghost from turning into a stray 前言 chapter.
                work[i + 1] = Chapter(nxt.title, cur.start, nxt.end)
                i += 1
                continue
        kept.append(cur)
        i += 1
    return kept


def split_chapters(text: str, pattern_source: str | None = None) -> list[Chapter]:
    return chapters_from_marks(text, merge_ghost_headings(text, marks_from(text, pattern_source)))


def score_pattern(text: str, rule: ChapterRule) -> dict | None:
    """How plausible is this rule as *the* chapter pattern for this text?

    Matching is not the same as being right: a rule loose enough to hit body text
    yields plenty of "chapters" whose "title" is a whole paragraph. Real headings
    are short lines, so a large majority of short matches is required before the
    count is trusted.
    """
    return score_rules(text, (rule,)).get(rule.id)


def _score_pattern_slow(text: str, rule: ChapterRule) -> dict | None:
    """The original one-rule implementation: a full finditer() over the whole text.

    Kept as the reference the fast path is checked against, and as the only correct
    route for a rule that is NOT `^`-anchored (a hand-written user pattern may match
    in the middle of a line, which the line-start scan cannot see).
    """
    marks = marks_from(text, rule.source)
    if len(marks) < 2:
        return None
    short = sum(1 for mk in marks if len(mk.title) <= SHORT_TITLE_LEN)
    return {
        "id": rule.id,
        "source": rule.source,
        "count": len(marks),
        "shortRatio": short / len(marks),
    }


# The characters that can begin a heading line. Every built-in rule starts with
# `^[ \t\u3000]*` followed by one of these, and this set is deliberately a superset
# (it also contains the horizontal whitespace itself) because it is only ever used to
# SKIP a line: missing a character here would silently drop real chapters.
#
# It is the whole reason the scan below is fast. Almost every line of a novel starts
# with a Hanzi that is not in this set, so those lines cost one set lookup instead of
# seven regex attempts.
_FIRST_CHARS = frozenset(
    " \t\u3000"          # a heading may be indented
    "第"                  # cn, cn-vol, cn-bracket, tighten
    "楔序引尾终后番完"    # cn-special
    "【〔〖「『（(["       # cn-bracket
    "0123456789０１２３４５６７８９"  # cn-num
    "Cc"                  # en
)


def score_rules(text: str, rules: Sequence[ChapterRule]) -> dict[str, dict]:
    """Score several rules in ONE pass over the text.

    Scoring each rule with its own `finditer()` meant seven full scans of the whole
    book, plus a `clean_title()` per match per rule. Measured on a 120 MB file that
    was 14.65 s, and any book the default union misses pays it in full.

    The rewrite is exact, not approximate:

    * Every built-in rule is `^`-anchored, so it matches at a line start or not at
      all. The marks a rule finds are therefore precisely the line starts where it
      matches - which one linear walk over the line starts can test for every rule at
      once, with no `finditer` and no per-match objects.
    * A mark's title is derived from its whole line and from nothing else, so the
      cleaned title is computed once per matching line and shared by every rule that
      matched there.
    * A rule whose source is not `^`-anchored falls back to `_score_pattern_slow`,
      because a mid-line match is invisible to a line-start scan.

    The `_MARK_GUARD` cap is preserved per rule: the first `_MARK_GUARD` matches of
    each rule are the ones counted and measured, exactly as `marks_from` would.
    """
    compiled: list[tuple[ChapterRule, re.Pattern[str]]] = []
    for rule in rules:
        if not rule.source.startswith("^"):
            # Mixed pool: one unanchored rule makes the whole batch unreliable, so
            # score every rule the slow, obviously-correct way.
            out: dict[str, dict] = {}
            for one in rules:
                scored_one = _score_pattern_slow(text, one)
                if scored_one:
                    out[one.id] = scored_one
            return out
        compiled.append((rule, compile_pattern(rule.source)))

    counts = [0] * len(compiled)
    shorts = [0] * len(compiled)
    capped = [False] * len(compiled)

    first_chars = _FIRST_CHARS
    n = len(text)
    pos = 0
    while pos < n:
        nl = text.find("\n", pos)
        end = n if nl == -1 else nl

        if text[pos] in first_chars:
            matched: list[int] = []
            for i, (_rule, rex) in enumerate(compiled):
                if capped[i]:
                    continue
                if rex.match(text, pos) is not None:
                    counts[i] += 1
                    if counts[i] >= _MARK_GUARD:
                        capped[i] = True
                    matched.append(i)
            if matched:
                title = clean_title(text[pos:end])
                if len(title) <= SHORT_TITLE_LEN:
                    for i in matched:
                        shorts[i] += 1

        if nl == -1:
            break
        pos = nl + 1

    out = {}
    for i, (rule, _rex) in enumerate(compiled):
        if counts[i] < 2:
            continue
        out[rule.id] = {
            "id": rule.id,
            "source": rule.source,
            "count": counts[i],
            "shortRatio": shorts[i] / counts[i],
        }
    return out


def detect_chapter_re(text: str) -> dict | None:
    """Pick the built-in rule that best explains this text, or None.

    This is what turns "one 1.06M-character 全文 chapter" back into a table of
    contents. A rule that only ever matches paragraph-length lines is describing
    body text, not headings - returning None is better than inventing a TOC.
    """
    pool: list[ChapterRule] = [ChapterRule("default", DEFAULT_CHAPTER_RE), *CHAPTER_RULES]
    # One pass for the whole pool. The list stays in pool order because the sort below
    # is stable, so ties are broken by pool order exactly as they were before.
    scored_by_id = score_rules(text, pool)
    scored = [scored_by_id[rule.id] for rule in pool if rule.id in scored_by_id]
    if not scored:
        return None
    usable = [s for s in scored if s["shortRatio"] >= 0.5]
    if not usable:
        return None
    usable.sort(key=lambda s: (-s["count"], -s["shortRatio"]))
    return usable[0]


def split_chapters_auto(text: str, pattern_source: str | None = None) -> SplitResult:
    """Split, and when the configured pattern found nothing usable, auto-detect.

    A working user pattern is NEVER overridden - in that case `rule_id` is None so
    the caller can honestly report "used your regex" instead of silently swapping
    in a different one.

    Ghost headings are merged here rather than in `marks_from`, so that scoring
    (`score_rules`, `detect_chapter_re`) still counts exactly what the regex found.
    The verdict and the table of contents are two different questions.
    """
    marks = merge_ghost_headings(text, marks_from(text, pattern_source))
    if len(marks) >= 2:
        return SplitResult(chapters_from_marks(text, marks), None, len(marks))
    hit = detect_chapter_re(text)
    if hit and hit["count"] > len(marks):
        better = merge_ghost_headings(text, marks_from(text, hit["source"]))
        return SplitResult(chapters_from_marks(text, better), hit["id"], len(better))
    return SplitResult(chapters_from_marks(text, marks), None, len(marks))
