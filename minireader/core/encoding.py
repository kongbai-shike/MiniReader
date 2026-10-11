"""TXT decoding.

Ported from `decodeTxt()` in the dsh-novel-wallpaper plugin (client.js:265-287),
with three deliberate improvements that only a desktop app can offer:

1. UTF-32 is checked BEFORE UTF-16, because the UTF-32LE BOM (FF FE 00 00) starts
   with the UTF-16LE BOM (FF FE). The original would silently decode such a file
   as UTF-16LE mojibake.
2. BOM-less UTF-16 and UTF-32 are recognised. The tell is a NUL byte: GBK and UTF-8
   Chinese prose never contains one. The decision is then made by score, NOT by NUL
   parity - see the comment on _candidates() for why parity was measured, tried, and
   thrown away.
3. Instead of "first decoder that does not raise wins", competing candidates are
   *scored*. This matters because GB18030 is extremely permissive - it decodes
   almost any byte sequence without error - so "did not raise" is weak evidence.
   The score counts common Hanzi, which genuine text has and mojibake does not.

The original's core insight is kept: a STRICT utf-8 decode that succeeds is taken
as final, because accidental valid UTF-8 is essentially impossible for CJK text.
It is qualified here, because it is not quite true for UTF-16 - see decode_txt().
"""

from __future__ import annotations

import codecs
from dataclasses import dataclass, field

# The 100 most frequent Hanzi. A wrong-charset decode produces Hanzi that are
# valid code points but almost never these, so this separates real text from
# mojibake far better than "the decoder did not raise".
_COMMON_HANZI = frozenset(
    "的一是了我不人在他有这上们来到时大地为子中你说生国年着就那和要她出也得里"
    "后自以会家可下而过天去能对小多然于心学么之都好看起发当没成只如事把还用"
    "第样道想作种开美总从无情己面最女但现前些所同日手又行意动方期它头经长儿"
    "回位分爱老因很给名法间斯知世什两次使身者被高已亲其进此话常与活正感"
)

_BOMS: tuple[tuple[bytes, str, str], ...] = (
    (codecs.BOM_UTF32_LE, "utf-32-le", "UTF-32 LE (BOM)"),
    (codecs.BOM_UTF32_BE, "utf-32-be", "UTF-32 BE (BOM)"),
    (codecs.BOM_UTF8, "utf-8-sig", "UTF-8 (BOM)"),
    (codecs.BOM_UTF16_LE, "utf-16-le", "UTF-16 LE (BOM)"),
    (codecs.BOM_UTF16_BE, "utf-16-be", "UTF-16 BE (BOM)"),
)

# Tried in order, scored, best wins. utf-8 is handled separately (strict-accept).
_CANDIDATES: tuple[tuple[str, str], ...] = (
    ("gb18030", "GB18030 (简体)"),
    ("big5", "Big5 (繁體)"),
    ("shift_jis", "Shift-JIS (日本語)"),
    ("euc-kr", "EUC-KR (한국어)"),
)

_KNOWN_ENCODINGS = {
    "utf-8": "UTF-8",
    "utf-8-sig": "UTF-8 (BOM)",
    "utf-16": "UTF-16 (BOM)",
    "utf-16-le": "UTF-16 LE",
    "utf-16-be": "UTF-16 BE",
    "utf-32": "UTF-32 (BOM)",
    "utf-32-le": "UTF-32 LE",
    "utf-32-be": "UTF-32 BE",
    "gb18030": "GB18030",
    "gbk": "GBK",
    "big5": "Big5",
    "shift_jis": "Shift-JIS",
    "euc-kr": "EUC-KR",
    "ascii": "ASCII",
}

# A sample is enough to characterise a whole file, and keeps detection O(1).
_SAMPLE = 24000

# Cut-offs below which we admit the guess is weak. Surfaced in the UI so the user
# can switch encoding by hand instead of silently reading garbage.
_CONFIDENT_SCORE = 0.35
_PLAUSIBLE_SCORE = 0.05


class DecodeError(Exception):
    """Raised only when even the lossy fallback produced nothing usable."""


@dataclass(frozen=True)
class Decoded:
    text: str
    encoding: str
    label: str
    confident: bool
    notes: list[str] = field(default_factory=list)


def _score(text: str) -> float:
    """How much does this look like real prose rather than mojibake?

    Each common Hanzi counts +1. Replacement chars and stray control characters
    count -3. Normalised by sample length.
    """
    sample = text[:_SAMPLE]
    if not sample:
        return 0.0
    hits = 0
    for ch in sample:
        if ch in _COMMON_HANZI:
            hits += 1
        elif ch == "\ufffd":
            hits -= 3
        elif ch < " " and ch not in "\n\r\t":
            hits -= 3
    return hits / len(sample)


def _printable_ratio(text: str) -> float:
    sample = text[:_SAMPLE]
    if not sample:
        return 0.0
    good = sum(1 for ch in sample if ch >= " " or ch in "\n\r\t")
    return good / len(sample)


_SNIFF_BYTES = 65536


def _has_nul_bytes(data: bytes) -> bool:
    return b"\x00" in data[:_SNIFF_BYTES]


def _candidates(data: bytes) -> list[tuple[str, str]]:
    """Which encodings are worth trying, given the bytes in front of us.

    A NUL byte is the tell. Chinese prose in GBK or UTF-8 never contains one, so its
    presence means the file is almost certainly UTF-16 or UTF-32 whose BOM was lost -
    and in that case the single-byte candidates are still offered, so a file that
    merely has a stray NUL in it is not forced down the wrong path.

    Deciding UTF-16 by NUL *parity* was tried first and does not work for Chinese.
    Every codepoint ending in 0x00 - 一 (U+4E00), 开 (U+5F00), and a good fraction of
    common Hanzi - puts its zero byte on the even offset in UTF-16LE, exactly where
    the NUL of an ASCII character does not. Measured on a realistic sample the two
    parities came out 120 to 120, i.e. no signal at all. Scoring does not have that
    problem: real UTF-16 decoded as UTF-16 scores as prose, and the same bytes read
    as GB18030 score below zero because half the output is control characters.
    """
    found = list(_CANDIDATES)
    if not _has_nul_bytes(data):
        return found
    wide: list[tuple[str, str]] = []
    if len(data) % 2 == 0:
        wide += [("utf-16-le", "UTF-16 LE"), ("utf-16-be", "UTF-16 BE")]
    if len(data) % 4 == 0:
        wide += [("utf-32-le", "UTF-32 LE"), ("utf-32-be", "UTF-32 BE")]
    return wide + found


_DECODE_CHUNK = 4 * 1024 * 1024


def _decode(data: bytes, encoding: str, errors: str) -> str:
    """Decode the bytes, in chunks.

    A single `data.decode(...)` over a 120 MB buffer peaked at **360 MB** - 3.0x the
    file - for a result that is only 0.7x the file, because CPython's unicode decoder
    grows its output buffer as it fills it. Feeding an incremental decoder 4 MB at a
    time peaks at 166 MB instead (the result, plus the chunks being joined into it).

    The incremental decoder does its own boundary handling, so a multi-byte character
    straddling a chunk boundary still decodes correctly, and the output was verified
    character-for-character identical to the one-shot decode for UTF-8, GB18030 and
    UTF-16LE, including input containing invalid bytes and a truncated trailing
    sequence. The cost is ~0.07 s on a 120 MB file.
    """
    if len(data) <= _DECODE_CHUNK:
        return data.decode(encoding, errors=errors)
    decoder = codecs.getincrementaldecoder(encoding)(errors=errors)
    parts = [
        decoder.decode(data[i : i + _DECODE_CHUNK])
        for i in range(0, len(data), _DECODE_CHUNK)
    ]
    parts.append(decoder.decode(b"", final=True))
    return "".join(parts)


# Enough bytes to characterise a file, aligned to 4 so a UTF-32 sample cannot be cut
# mid-character. _score() only reads the first _SAMPLE (24,000) characters, and the
# widest supported encoding needs 4 bytes per character, so 512 KB is ~5x more than
# the scoring window can ever look at.
_SAMPLE_BYTES = 512 * 1024


def _sample_bytes(data: bytes) -> bytes:
    """The prefix of `data` used for scoring candidates.

    This exists for memory, not for speed. Scoring every candidate by decoding the
    WHOLE file and keeping the results alive cost four times the file size: measured
    with tracemalloc, decoding a 120 MB book peaked at 360 MB of transient decoded
    text, because each candidate's full string stayed referenced in `scored` until the
    sort. Since _score() reads only the first 24,000 characters, the sample is enough,
    and the winner is decoded in full exactly once afterwards.

    Equivalence: all supported codecs are stateless, and the sample is aligned to 4
    bytes, so the first 24,000 characters of a sampled decode are identical to the
    first 24,000 characters of the full decode. The score is therefore unchanged -
    tests/test_core.py still has to agree with the frozen JS kernel byte for byte.
    """
    if len(data) <= _SAMPLE_BYTES:
        return data
    return data[: _SAMPLE_BYTES - (_SAMPLE_BYTES % 4)]


def decode_txt(data: bytes, force: str | None = None) -> Decoded:
    """Decode a novel file's bytes into text.

    `force` skips detection entirely - the caller picked an encoding by hand.
    """
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise TypeError("decode_txt expects bytes")
    data = bytes(data)
    if not data:
        return Decoded("", "utf-8", "UTF-8", True, ["空文件"])

    if force:
        try:
            text = _normalize(_decode(data, force, "replace"))
        except LookupError as exc:
            raise DecodeError(f"未知编码: {force}") from exc
        label = _KNOWN_ENCODINGS.get(force, force.upper())
        return Decoded(text, force, label, True, ["手动指定编码"])

    notes: list[str] = []

    # 1) BOM. Longest BOM first - see the module docstring.
    for bom, enc, label in _BOMS:
        if data.startswith(bom):
            text = _normalize(_decode(data, enc, "replace"))
            return Decoded(text, enc, label, True, ["按 BOM 判定"])

    # 2) Strict UTF-8 wins outright, but only when no NUL byte is in sight.
    #
    # A UTF-16 file can BE valid UTF-8: "第" is the bytes 2C 7B, which decode cleanly
    # as ",{". Taking the fast path there would hand back confident mojibake, so a
    # file with NULs skips straight to scoring, where UTF-16 competes on merit.
    if not _has_nul_bytes(data):
        try:
            # _decode, not data.decode: this is the path every UTF-8 book takes, and a
            # single-shot decode of a large buffer is what produced the 3.0x memory
            # peak this module now avoids.
            text = _decode(data, "utf-8", "strict")
        except UnicodeDecodeError:
            pass
        else:
            return Decoded(_normalize(text), "utf-8", "UTF-8", True, ["严格 UTF-8 解码成功"])

    # 3) Score the candidates. This is the whole decision for a BOM-less UTF-16 file.
    #
    # errors="replace", not "strict": a real-world dump often carries a handful of
    # invalid bytes (truncated download, stray byte in the middle of a chapter) and
    # one bad byte must not disqualify the correct encoding for the whole file. This
    # also matches what the JS kernel does - TextDecoder is non-fatal by default - so
    # the decoded text stays byte-comparable with the original.
    #
    # Scoring runs on a byte sample and only the winner is decoded in full; see
    # _sample_bytes() for why, and for why the outcome is identical.
    sample = _sample_bytes(data)
    scored: list[tuple[float, str, str]] = []
    for enc, label in _candidates(data):
        try:
            text = sample.decode(enc, errors="replace")
        except (UnicodeDecodeError, LookupError):
            continue
        scored.append((_score(text), enc, label))

    if scored:
        scored.sort(key=lambda row: row[0], reverse=True)
        best_score, enc, label = scored[0]
        confident = best_score >= _CONFIDENT_SCORE
        if not confident:
            notes.append("置信度偏低，可能是罕见编码")
            if best_score < _PLAUSIBLE_SCORE and len(scored) > 1:
                notes.append(
                    "候选差距很小(" + "、".join(f"{r[1]}={r[0]:.3f}" for r in scored[:3]) + ")"
                )
        notes.append("按常用字密度判定")
        text = _normalize(_decode(data, enc, "replace"))
        return Decoded(text, enc, label, confident, notes)

    # 4) Last resort: lossy GB18030 never raises, so this always produces something.
    text = _normalize(_decode(data, "gb18030", "replace"))
    ratio = _printable_ratio(text)
    notes.append("所有编码均解码失败，已用 GB18030 有损解码")
    if ratio < 0.9:
        notes.append(f"仍有 {round((1 - ratio) * 100)}% 字节无法识别")
    return Decoded(text, "gb18030", "GB18030 (有损)", False, notes)


def _normalize(text: str) -> str:
    """BOM leftovers and line endings, and nothing else.

    Deliberately minimal, and identical to what the JS kernel does
    (`text.replace(/\\r\\n?/g, '\\n')`). Stripping trailing spaces or NULs would be
    tempting, but it shifts every character offset and would make the Python port
    disagree with the frozen JS kernel on the same file - which is exactly what
    tests/test_core.py checks, byte for byte, on real novels.
    """
    if text.startswith("\ufeff"):
        text = text[1:]
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text


def detect_encoding(data: bytes) -> str:
    """Convenience wrapper returning just the encoding name."""
    return decode_txt(data).encoding


def reencode_options() -> list[tuple[str, str]]:
    """Encodings offered in the "换个编码重开" menu: (codec, label)."""
    return [
        ("utf-8", "UTF-8"),
        ("gb18030", "GB18030 / GBK (简体)"),
        ("big5", "Big5 (繁體)"),
        ("utf-16", "UTF-16 (BOM)"),
        ("shift_jis", "Shift-JIS (日本語)"),
        ("euc-kr", "EUC-KR (한국어)"),
    ]
