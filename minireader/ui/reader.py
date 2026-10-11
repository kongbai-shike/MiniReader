"""The reading pane: one chapter at a time, pixel-scrolled with a short glide.

Widget choice was decided by measurement, not preference:

* `QPlainTextEdit` looks like the obvious choice for large text, but its vertical
  scrollbar is measured in BLOCKS. `setValue(200)` put block 200 at the top, and
  the range was 0..508 for a 500-block document. You cannot glide inside a line,
  and a 3-line wheel notch becomes 3 visible jumps.
* `QTextEdit` is measured in PIXELS: range 0..33835 for the same document, with
  `singleStep` equal to one line height (20 px) and `pageStep` equal to the
  viewport height. That makes `verticalScrollBar().value()` a direct stand-in for
  the plugin's `runtime.offset`, which the whole wheel model was built on.

`QTextEdit` is only slow for huge documents; a chapter is a few thousand
characters and the book is sliced per chapter, so this costs nothing.

The wheel model is ported from client.js:711-779:

* 180 ms ease-out cubic glide, RETARGETED from the currently painted position
  rather than from the previous target, so a second notch mid-glide continues
  smoothly instead of restarting.
* The glide and page-turning are independently cancellable.
* Crossing a chapter boundary is a separate gesture: a notch that would go past
  the end stops at the end, and the NEXT notch turns the page. Predictable beats
  clever - an overshoot that silently jumps chapters loses the reader's place.
"""

from __future__ import annotations

from ..qt import (
    PROPORTIONAL_HEIGHT,
    QEasingCurve,
    QFont,
    QFrame,
    QGraphicsOpacityEffect,
    QPoint,
    QTextBlockFormat,
    QTextCursor,
    QTextEdit,
    QTextOption,
    QTimer,
    QVariantAnimation,
    Qt,
    Signal,
    event_global_pos,
)

# 让锚点行不要紧贴着视口上边缘，否则那行看起来像被切掉了半个字。
_ANCHOR_PAD = 4
# 探测"顶端是哪个字符"时往下探这么远，好让取样点落在锚点行内部而不是行间的空隙里。
# 注意：这个值是 4 还是 1 并不改变锚点的精度——精度受限于**行的量化**，见
# first_visible_char() 的说明。
_ANCHOR_PROBE_Y = _ANCHOR_PAD + 1

_SCROLLBAR_QSS = """
QScrollBar:vertical { background: transparent; width: 8px; margin: 0; }
QScrollBar::handle:vertical { background: rgba(255,255,255,0.18); border-radius: 4px; min-height: 24px; }
QScrollBar::handle:vertical:hover { background: rgba(255,255,255,0.30); }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }
"""


class ReaderView(QTextEdit):
    """Scrollable chapter body with wheel-driven page turning."""

    chapterRequested = Signal(int)   # +1 next chapter, -1 previous chapter
    scrolled = Signal()              # position changed (debounce before persisting)
    # 正文区右键：把事件的**全局**坐标交出去，菜单由主窗在那个位置弹出（阶段 3）。
    contextMenuRequested = Signal(QPoint)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setReadOnly(True)
        self.setFrameStyle(0)                      # QFrame.NoFrame
        self.setWordWrapMode(QTextOption.WrapAtWordBoundaryOrAnywhere)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.setCursorWidth(0)                     # no blinking caret in a read-only pane
        # Mouse selection only. Enabling TextSelectableByKeyboard would let a stray
        # keystroke move an invisible caret and scroll the reader somewhere random;
        # the scroll keys are handled explicitly below instead.
        self.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.setTabChangesFocus(True)
        self.verticalScrollBar().setStyleSheet(_SCROLLBAR_QSS)

        self._wheel_lines = 3
        self._animation_ms = 180
        self._line_height_pct = 210
        # 首行缩进的字符数；像素值在 _apply_line_height() 里按当前字体实时换算，
        # 所以换字号后缩进仍然是"整整 N 个字"，不会变成 1.7 个字这种怪样子。
        self._indent_chars = 2
        self._target_offset: int | None = None     # None = nothing pending
        self._target_char: int | None = None       # 待恢复的字符锚点（书签 / 换字号）

        # One animation object for the life of the widget. Reconfiguring it avoids
        # any chance of deleting a QVariantAnimation from inside its own signal.
        self._anim = QVariantAnimation(self)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self._anim.valueChanged.connect(self._on_anim_value)
        self._anim.finished.connect(self._on_anim_finished)

        self.verticalScrollBar().valueChanged.connect(self._on_value_changed)

        # 内容不透明度用的 effect。懒创建：满不透明（255）时不挂 effect，正文就能走
        # 正常的 ClearType 次像素抗锯齿；一旦调低才建，之后只 setOpacity。
        self._content_effect: QGraphicsOpacityEffect | None = None

    # ---- typography ------------------------------------------------------

    def apply_style(self, settings) -> None:
        # 换字号/行距/页边距会让文字重新折行，此时"同样的像素偏移"指向的是**别处的
        # 文字**：字号调大一档，读者就被弹到几段之外。所以先记下视口顶端是哪个字符，
        # 重新排版后再回到那个字符——换字体不丢位置，这是读者唯一能接受的行为。
        anchor = self.first_visible_char() if self._has_text() else None

        font = QFont()
        if settings.font_family:
            font.setFamily(settings.font_family)
        # 字号走像素：设置面板的单位一直标的是 px，25px 默认也是按屏幕 CSS 像素量出来
        # 的 QQ 实测值。pointSize 在不同 DPI/缩放设置下会漂，像素在任何缩放下都等于
        # 一个 CSS 像素，中文阅读要的就是这个确定性。
        font.setPixelSize(int(settings.font_size))
        self.setFont(font)

        self._line_height_pct = int(settings.line_height)
        self._indent_chars = int(settings.paragraph_indent)
        self._wheel_lines = int(settings.wheel_lines)
        self._animation_ms = int(settings.wheel_animation_ms)

        # 正文控件必须是透明的：窗口的底色（含 alpha）由顶层 paintEvent 统一画，
        # 正文只负责画字。这里如果还给它一个不透明底色，alpha=0 时就会看到一块黑的
        # 正文板，桌面透不过来。边框也去掉，免得在圆角窗口里画出一个直角描边。
        self.setStyleSheet(
            f"QTextEdit {{ background: transparent; border: none; color: {settings.fg};"
            f" selection-background-color: #444a63;"
            f" selection-color: {settings.fg}; }}"
        )
        self.setFrameShape(QFrame.NoFrame)
        self.document().setDocumentMargin(float(settings.margin))
        self._apply_line_height()
        if anchor is not None:
            self._target_char = anchor
            self._target_offset = None
        QTimer.singleShot(0, self._settle_offset)

    # ---- content opacity -------------------------------------------------

    def apply_content_alpha(self, alpha: int) -> None:
        """正文（文字）整体的不透明度，0-255。

        用 QGraphicsOpacityEffect 而不是改调色板或样式表：effect 只作用于正文这一棵
        子树，窗口底色和 paintEvent 画的背景完全不受影响——这正是"背景/内容两个独立
        维度"要的效果。effect 挂在**正文子控件**上，绝不挂顶层窗口。

        满不透明时把 effect 摘掉：graphics effect 会让正文走离屏合成，白白丢掉
        ClearType 次像素抗锯齿，而那时它什么也没做。
        """
        alpha = max(0, min(int(alpha), 255))
        if alpha >= 255:
            if self._content_effect is not None:
                self.setGraphicsEffect(None)
                self._content_effect = None
            return
        if self._content_effect is None:
            self._content_effect = QGraphicsOpacityEffect(self)
            self.setGraphicsEffect(self._content_effect)
        self._content_effect.setOpacity(alpha / 255.0)

    def preview_content_alpha(self, alpha: int) -> None:
        """拖动内容透明度滑块时的实时预览。

        只 setOpacity，不重设 graphicsEffect、不碰样式表：重建 effect 会触发一次完整
        重绘，拖滑块时会闪。effect 只在第一次调低时创建一次。
        """
        alpha = max(0, min(int(alpha), 255))
        if self._content_effect is None:
            if alpha >= 255:
                return                      # 已经满不透明，没有可预览的
            self._content_effect = QGraphicsOpacityEffect(self)
            self.setGraphicsEffect(self._content_effect)
        self._content_effect.setOpacity(alpha / 255.0)

    def content_opacity(self) -> float:
        """当前正文的 effect 不透明度（0.0-1.0）；没有 effect 时是 1.0。"""
        return self._content_effect.opacity() if self._content_effect is not None else 1.0

    def _apply_line_height(self) -> None:
        """Line spacing AND first-line indent live in the block format: Qt stylesheets
        have neither line-height nor text-indent."""
        block_format = QTextBlockFormat()
        block_format.setLineHeight(self._line_height_pct, PROPORTIONAL_HEIGHT)
        # 首行缩进 N 个汉字宽。用"国"而不是平均字宽：等宽感对中文最重要，em 方块字
        # 宽度一致；量的是**当前**字体（setFont 之后），所以字号变了缩进跟着变。
        # QFontMetrics.width 在 Qt6 被移除，horizontalAdvance 在 Qt 5.11 起可用，
        # 取到哪个用哪个，两套绑定都能跑。
        if self._indent_chars > 0:
            metrics = self.fontMetrics()
            advance = getattr(metrics, "horizontalAdvance", None)
            char_w = float(advance("国")) if advance is not None else float(metrics.width("国"))
            block_format.setTextIndent(char_w * self._indent_chars)
        else:
            block_format.setTextIndent(0.0)
        cursor = self.textCursor()
        cursor.select(QTextCursor.Document)
        cursor.mergeBlockFormat(block_format)
        cursor.clearSelection()
        self.setTextCursor(cursor)

    def _line_px(self) -> float:
        """Rendered height of ONE visual line, used as the wheel step.

        Measured from the font metric, deliberately NOT from
        `blockBoundingRect(firstBlock())`. That call looks like it returns a line
        height but actually returns the height of the whole *block*, and a block is a
        paragraph - it wraps. Measured on a 420 px window: lineSpacing was 28 px, one
        paragraph wrapped to 3 lines, and the block rect came back as 84 px (134 px at
        190% line height). Multiplying that by 3 lines per wheel notch moved ~400 px
        per notch, about 14 lines - the wheel felt like a page-flip button.
        """
        base = float(self.fontMetrics().lineSpacing())
        return max(1.0, base * (self._line_height_pct / 100.0))

    # ---- chapters --------------------------------------------------------

    def set_chapter(
        self,
        body: str,
        *,
        offset: int = 0,
        at_end: bool = False,
        char_offset: int | None = None,
    ) -> None:
        """Replace the visible text and restore a position within it.

        Two kinds of position, and the difference matters:
        `offset` is a scrollbar PIXEL value (that is what `Progress` has always
        stored), while `char_offset` is a character index into `body` that survives a
        font-size change or a window resize. Bookmarks use the character form - a
        bookmark that lands on the wrong paragraph after the reader bumps the font
        size once is worse than no bookmark.
        """
        self._cancel_anim()
        self.setPlainText(body or "")
        # MUST follow setPlainText, which resets block formats.
        self._apply_line_height()
        self._target_char = None if char_offset is None else max(0, int(char_offset))
        self._target_offset = None if self._target_char is not None else (-1 if at_end else max(0, int(offset)))
        # Deferred: scrollbar maximum() is only meaningful after the layout pass.
        QTimer.singleShot(0, self._settle_offset)

    def _settle_offset(self) -> None:
        char = self._target_char
        if char is not None:
            self._target_char = None
            self._scroll_to_char_now(char)
            self.scrolled.emit()
            return
        target = self._target_offset
        if target is None:
            return
        self._target_offset = None
        bar = self.verticalScrollBar()
        bar.setValue(bar.maximum() if target < 0 else min(target, bar.maximum()))
        self.scrolled.emit()

    def current_offset(self) -> int:
        return int(self.verticalScrollBar().value())

    def max_offset(self) -> int:
        return int(self.verticalScrollBar().maximum())

    # ---- 字符锚点（书签 / 换字体用）-------------------------------------

    def _has_text(self) -> bool:
        """空文档的 characterCount() 是 1（那一个段落分隔符），不算有正文。"""
        return self.document().characterCount() > 1

    def _pending(self) -> bool:
        return self._target_offset is not None or self._target_char is not None

    def char_count(self) -> int:
        return max(0, self.document().characterCount() - 1)

    def first_visible_char(self) -> int:
        """视口顶端那一行的第一个字符的位置。

        这是"我读到哪儿了"唯一不会随字号漂移的表示法。取视口靠上处的最近字符：
        `cursorForPosition` 收的是视口坐标并且自己算进了滚动量，所以在顶部边距里
        探测得到的就是第一行；如果换成 `textCursor().position()`，读者没点过鼠标时
        它永远是 0（光标宽度还设成了 0，看不见也想不到）。

        **精度到行为止**：返回的是行首，不是"顶边往下第几个字"。所以锚点在一次换字号
        后可能往回挪不到一行（实测 7,652 字的章节里，字号在 12/17/22 之间来回换三次
        共漂 15 字 ≈ 每次不到一行）。这是"把行首对齐到顶边"这个做法的固有量化，不是
        误差累积；比起按像素恢复位置（同样的操作会让读者跑到几百字之外）已经不是一
        个量级的问题。要消掉它就得让文档布局汇报"顶边那个字的半行偏移"，Qt 没给。
        """
        if not self._has_text():
            return 0
        return max(0, self.cursorForPosition(QPoint(2, _ANCHOR_PROBE_Y)).position())

    def scroll_to_char(self, pos: int) -> None:
        """把某个字符滚到视口顶端。用户主动跳转用，会中断滚动动画。"""
        self._cancel_anim()
        self._target_offset = None
        self._target_char = None
        self._scroll_to_char_now(pos)
        self.scrolled.emit()

    def _scroll_to_char_now(self, pos: int) -> None:
        cursor = self.textCursor()
        # 悬停在文档末尾会建出一个"第 N+1 个"位置，光标就不可见了。
        cursor.setPosition(min(max(0, int(pos)), self.char_count()))
        self.setTextCursor(cursor)
        # 两步走：先让 Qt 把光标弄进视口，再按它的实际矩形把它顶到上边缘。两步都发生在
        # 同一次事件循环里，下一次重绘之前就已经定好，所以看不到闪烁。
        self.ensureCursorVisible()
        bar = self.verticalScrollBar()
        rect = self.cursorRect(cursor)
        target = bar.value() + rect.top() - _ANCHOR_PAD
        bar.setValue(max(bar.minimum(), min(int(target), bar.maximum())))

    # ---- animation -------------------------------------------------------

    @property
    def animating(self) -> bool:
        return self._anim.state() == QVariantAnimation.Running

    def _cancel_anim(self) -> None:
        if self._anim.state() != QVariantAnimation.Stopped:
            self._anim.stop()

    def _on_value_changed(self, _value: int = 0) -> None:
        self.scrolled.emit()

    def _on_anim_value(self, value) -> None:
        self.verticalScrollBar().setValue(int(value))

    def _on_anim_finished(self) -> None:
        self.scrolled.emit()

    def _animate_to(self, target: int) -> None:
        bar = self.verticalScrollBar()
        target = max(bar.minimum(), min(int(target), bar.maximum()))
        start = bar.value()
        if target == start:
            return
        if self._animation_ms <= 0:
            bar.setValue(target)
            return
        self._cancel_anim()
        # Retarget from where the pixels actually are, not from the old target.
        self._anim.setStartValue(int(start))
        self._anim.setEndValue(int(target))
        self._anim.setDuration(self._animation_ms)
        self._anim.start()

    # ---- input -----------------------------------------------------------

    def wheelEvent(self, event) -> None:  # noqa: N802 - Qt override
        delta = event.angleDelta().y()
        if delta == 0:
            event.ignore()
            return
        bar = self.verticalScrollBar()
        forward = delta < 0
        if forward and bar.value() >= bar.maximum():
            self._cancel_anim()
            self.chapterRequested.emit(1)
            event.accept()
            return
        if not forward and bar.value() <= bar.minimum():
            self._cancel_anim()
            self.chapterRequested.emit(-1)
            event.accept()
            return
        step = max(1.0, self._line_px() * max(1, self._wheel_lines))
        notches = delta / 120.0
        self._animate_to(int(round(bar.value() - notches * step)))
        event.accept()

    def contextMenuEvent(self, event) -> None:  # noqa: N802 - Qt override
        # 阶段 3：正文区只要 QQ 那三项菜单（上一页/下一页｜重新加载本页）。
        # 绝不调 super()——QTextEdit 默认菜单会带"复制/全选"，规格明确是纯文字三项。
        # 坐标随事件一起发：弹出位置必须是右键点，不是窗口角落。
        self.contextMenuRequested.emit(event_global_pos(event))
        event.accept()

    # Keys are handled explicitly rather than delegated: with
    # TextSelectableByMouse the widget ignores keys, and enabling keyboard
    # selection would move an invisible caret around instead of scrolling.
    _SCROLL_KEYS = (Qt.Key_Down, Qt.Key_Up, Qt.Key_PageDown, Qt.Key_PageUp, Qt.Key_Home, Qt.Key_End)

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt override
        key = event.key()
        if key not in self._SCROLL_KEYS:
            super().keyPressEvent(event)
            return
        self._cancel_anim()
        if self._pending():
            self._settle_offset()
        bar = self.verticalScrollBar()
        if key == Qt.Key_Home:
            bar.setValue(0)
            event.accept()
            return
        if key == Qt.Key_End:
            bar.setValue(bar.maximum())
            event.accept()
            return
        forward = key in (Qt.Key_Down, Qt.Key_PageDown)
        step = int(max(1.0, self._line_px())) if key in (Qt.Key_Down, Qt.Key_Up) else max(1, bar.pageStep())
        if forward:
            if bar.value() >= bar.maximum():
                self.chapterRequested.emit(1)
            else:
                bar.setValue(min(bar.value() + step, bar.maximum()))
        else:
            if bar.value() <= bar.minimum():
                self.chapterRequested.emit(-1)
            else:
                bar.setValue(max(bar.value() - step, bar.minimum()))
        event.accept()

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        # Re-wrapping changes the scrollbar range; a restore that is still pending
        # must be re-settled against the new range.
        if self._pending():
            QTimer.singleShot(0, self._settle_offset)
