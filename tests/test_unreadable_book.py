"""一个"看起来是文本、其实不是"的文件必须被当场拒绝，不许交给排版。

这是真机事故的回归测试。`D:/novel/从死神开始打穿世界.txt` 在某次下载中断后变成
14 MB 的 0xFF（逐字节统计：100%）。gb18030 什么都不报错，于是"解码成功"，得到
1448 万个 U+FFFD，而且整本没有一个换行——分章器只能给出一个覆盖全文的"全文"章。
这段文本交给 Qt 之后实测：10 万字符 4.2 秒、30 万字符 44 秒（每个替换字符都要走
一次字体回退，耗时不是线性的），14 MB 那本等于把 GUI 线程永久钉死。用户看到的就是
"程序打开了，但读不了书"——窗口在屏幕上，却一个字都没有、点也点不动。

两道断言缺一不可：坏文件必须抛 `DecodeError`（不是静默返回乱码），而且它必须在
极短的时间内抛（不能"先把它排一遍版再判断"）——慢在这里就等于死机。
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from minireader.core import encoding as E  # noqa: E402
from minireader.core import model as M  # noqa: E402

#: U+FFFD，写成 chr() 而不是字面量：这个字符在源码里肉眼分不清，而且转义序列在
#: 不同的编辑器/检查器之间会被悄悄改写，测试就失去了意义。
REPLACEMENT = chr(0xFFFD)


class TestUnreadableFiles(unittest.TestCase):
    #: 真实文件的大小，用它让"耗时"这条断言有意义。
    CORRUPT_SIZE = 2 * 1024 * 1024
    #: 这个上限比实测慢两个数量级还多，只用来抓"又去排版了"这种量级的倒退。
    DEADLINE_SECONDS = 5.0

    def corrupt_bytes(self) -> bytes:
        """和事故现场同形：整篇 0xFF，没有换行。"""
        return b"\xff" * self.CORRUPT_SIZE

    def test_a_file_of_padding_bytes_is_refused(self) -> None:
        with self.assertRaises(E.DecodeError) as caught:
            E.decode_txt(self.corrupt_bytes())
        message = str(caught.exception)
        # 人话，而且给出下一步动作——不是"编码错误"这种没法照做的提示。
        self.assertIn("不是可读的文本", message)
        self.assertIn("重新下载", message)

    def test_refusing_it_is_fast(self) -> None:
        """慢在这里等于死机：这条断言守的是"别把它交给排版"。"""
        started = time.perf_counter()
        with self.assertRaises(E.DecodeError):
            E.decode_txt(self.corrupt_bytes())
        elapsed = time.perf_counter() - started
        self.assertLess(elapsed, self.DEADLINE_SECONDS, f"拒绝一本坏书用了 {elapsed:.1f}s")

    def test_a_forced_encoding_does_not_bypass_the_check(self) -> None:
        """手动指定编码是为了修"认错字"，不是为了绕过损坏检查。"""
        with self.assertRaises(E.DecodeError):
            E.decode_txt(self.corrupt_bytes(), force="gb18030")

    def test_a_handful_of_bad_bytes_still_opens(self) -> None:
        """反面：真实世界里的书偶尔有几个坏字节，那种必须照常打开。"""
        good = ("第一章 甲\n" + "正文内容。" * 200 + "\n").encode("gb18030") * 20
        broken = good[:1000] + b"\xff\xff\xff" + good[1000:]
        d = E.decode_txt(broken)
        self.assertIn(d.encoding, ("gb18030", "gbk"))
        self.assertIn("正文内容", d.text)

    def test_the_threshold_leaves_normal_books_alone(self) -> None:
        """闸门只在"整本都不是字"时落下，不碰正常的书。"""
        self.assertEqual(E.unreadable_share("正文内容。\n" * 1000), 0.0)
        self.assertGreaterEqual(E.unreadable_share(REPLACEMENT * 100), E._UNREADABLE_RATIO)
        # 边界两侧都要有留白：49% 放过、51% 拦下。
        self.assertLess(E.unreadable_share(REPLACEMENT * 49 + "字" * 51), E._UNREADABLE_RATIO)
        with self.assertRaises(E.DecodeError):
            E.check_readable(REPLACEMENT * 51 + "字" * 49)

    def test_open_book_turns_it_into_a_book_error(self) -> None:
        """窗口只 catch `BookError`；`DecodeError` 必须在这一层被翻译掉。"""
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as fh:
            fh.write(self.corrupt_bytes())
            path = fh.name
        try:
            with self.assertRaises(M.BookError) as caught:
                M.Book.from_path(path)
            self.assertIn("不是可读的文本", str(caught.exception))
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
