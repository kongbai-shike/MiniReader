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
* `split_chapters_auto` never overrides a working user pattern: it returns
  `rule_id=None` in that case so the UI can say "used your regex" honestly.

The port is verified against the frozen JS kernel by `tests/test_core.py`, which
replays golden samples dumped by `tools/dump_conformance.mjs`.
"""

from __future__ import annotations

import re
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


def split_chapters(text: str, pattern_source: str | None = None) -> list[Chapter]:
    return chapters_from_marks(text, marks_from(text, pattern_source))


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
    """
    marks = marks_from(text, pattern_source)
    if len(marks) >= 2:
        return SplitResult(chapters_from_marks(text, marks), None, len(marks))
    hit = detect_chapter_re(text)
    if hit and hit["count"] > len(marks):
        better = marks_from(text, hit["source"])
        return SplitResult(chapters_from_marks(text, better), hit["id"], len(better))
    return SplitResult(chapters_from_marks(text, marks), None, len(marks))
