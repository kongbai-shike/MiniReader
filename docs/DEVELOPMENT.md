# MiniReader 开发约束

这份文档记录**踩过坑之后才定下来的**几条规矩。每一条都对应一次真实的失败：不是风格偏好，
是"违反它就会坏"的硬约束。改代码前先读一遍，尤其是动 `minireader/ui/reader_base.py`
和 `minireader/core/` 的时候。

跑测试：

```powershell
cd "C:\Users\pc\Desktop\随手文件\MiniReader"
py -3.12 -m unittest discover -s tests -t .
```

---

## 1. `Reader` 必须是普通类，不能是 `abc.ABC` 或 `typing.Protocol`

`minireader/ui/reader_base.py` 的 `Reader` 是一个**普通类**——没有 `abc.ABC`、没有
`typing.Protocol`、没有自定义元类。这不是图省事，是**做不到**：

Qt 控件的元类是 `sip.wrappertype`（PyQt5）或 `Shiboken.ObjectType`（PySide6），
**都不是 `abc.ABCMeta` 的子类**。Python 要求派生类的元类必须是所有基类元类的
（非严格）子类，于是：

```python
class Reader(abc.ABC):          # 元类 = abc.ABCMeta
    ...

class ReaderView(Reader, QTextEdit):   # QTextEdit 的元类 = sip.wrappertype
    ...
```

```
TypeError: metaclass conflict: the metaclass of a derived class must be a
(non-strict) subclass of the metaclasses of all its bases
```

`typing.Protocol` 一样失败（它也有自己的元类 `_ProtocolMeta`，派生自 `ABCMeta`）。

普通类**没有自己的元类**（就是 `type`），`type` 是所有元类的基类，所以混入得很干净。

**接口的强制力从哪来？** 不靠 ABC，靠两条测试：

* `tests/test_book_base.py::TestReaderIsAPlainClass::test_it_is_not_an_abc_or_a_protocol`
  —— 正面钉死"它不能变成 ABC"。
* `test_reader_declares_every_interface_name` —— 遍历 `_INTERFACE` 断言每个名字都在
  `Reader.__dict__` 里，少一个就红。

方法体一律 `raise NotImplementedError`（唯一例外是 `scrollbar()` 返回 `None`）。
**故意的**：漏实现的具体 Reader 应该在自己的测试里响亮地炸掉，而不是在窗口里静默地
渲染出一片空白。

---

## 2. `Reader` 必须排在 MRO 最前面，所以它的方法名不能碰 Qt

```python
class ReaderView(Reader, QTextEdit):   # 原来: class ReaderView(QTextEdit)
class ComicReader(Reader, QWidget):    # 阶段 2
```

`Reader` **必须第一个**列——排在后面的话，Qt 的同名方法会盖掉基类方法，抽象层形同虚设。

但"排最前"是有代价的：**基类里任何与 Qt 重名的方法，会反过来盖掉 Qt 的实现**。
后果是具体的、不是理论的：

```python
class Reader:
    def show(self, book): ...        # ← w.show() 变成
                                     #   TypeError: missing 1 required
                                     #   positional argument: 'book'
                                     #   （Qt 自己要调它）

    def viewport(self): return None  # ← window.py:559 的
                                     #   self.reader.viewport().installEventFilter(self)
                                     #   变成 AttributeError: 'NoneType'
```

`viewport()` 一旦返回 `None`，`window.py` 的 `_edge_surfaces()`（4px 边缘缩放）和
"鼠标悬停暂停自动滚动"会一起失效。

所以：

| 想要的名字 | 实际的名字 | 为什么 |
|---|---|---|
| `show(book)` | **`show_book(book, ...)`** | `QWidget.show()` 是 Qt 的 |
| `viewport()` | **不声明**，留给具体 Reader | `QTextEdit.viewport()` 是 Qt 的；`ReaderView` 从 `QTextEdit` 继承真的那个，`ComicReader` 自己定义一个 |

`ComicReader` 的 `viewport()` 是**自己声明的**（返回 `self._page_view`），不是从基类继承的。
这看起来和"基类不许声明 viewport"矛盾，其实正是一回事：**只有具体类可以声明它**，
因为具体类排在 MRO 前面时，那个名字本来就是它自己该管的。

同样被 Qt 占着、因此被 `Reader` 避开的名字还有：`hide`、`close`、`update`、`repaint`、
`resize`、`move`。完整名单见下一条。

---

## 3. 往 `Reader` 加方法时，先查名字，再加进断言列表

加方法前**必须**确认这个名字在 `QWidget` 和 `QTextEdit` 上都是空的：

```powershell
py -3.12 -c "
from minireader.qt import QWidget, QTextEdit
for n in ('your_new_name',):
    print(n, hasattr(QWidget, n), hasattr(QTextEdit, n))
"
```

两个都必须是 `False`。有一个是 `True`，就得换名字——否则基类会盖掉 Qt 的。

加完之后，**把名字追加到 `tests/test_book_base.py` 的 `_QT_OWNED` 元组**（第 61-96 行）。
那个元组现在是 34 个名字：

```
show, hide, viewport, close, update, repaint, resize, move, setParent, geometry,
parent, children, raise_, lower, isVisible, setVisible, sizeHint, minimumSizeHint,
paintEvent, keyPressEvent, mousePressEvent, wheelEvent, resizeEvent, showEvent,
hideEvent, closeEvent, event, eventFilter, setFocus, hasFocus, grab,
activateWindow, windowTitle, setWindowTitle
```

`test_reader_declares_no_qt_owned_name` 会遍历它，断言 `Reader.__dict__` 里**一个都不许有**。

> **为什么这个列表要手工维护？** 它没法自动生成。`hasattr(QWidget, n)` 只能回答
> "这个具体名字是不是 Qt 的"，回答不了"我有没有漏掉一个 Qt 名字"。这份列表是
> **已经踩过的坑的清单**，每次发现新的冲突就加一条。加接口方法时如果只改
> `_INTERFACE` 不改 `_QT_OWNED`，测试仍然是绿的——**这正是最危险的情况**：
> 双绑定下（PySide6 的名字集合和 PyQt5 不完全一样）你可能要到用户那里才知道坏了。

`_INTERFACE`（第 99-110 行）是另一个元组，列的是 `Reader` **承诺提供**的 10 个名字。
两个元组是互补的：`_INTERFACE` 是"必须有"，`_QT_OWNED` 是"绝不能有"。

`test_interface_names_are_free_on_both_widget_bases` 还会把 `_INTERFACE + ("kind",)`
逐个拿去问 `QWidget` 和 `QTextEdit`——所以**双绑定兼容是被测出来的，不是被声称的**。

---

## 4. `core/` 和 `store.py` 必须无 Qt

`minireader/core/` 下所有模块、以及 `minireader/store.py`，**不许 import Qt**。

理由有两层：

* **可测性**。核心层的测试不需要 `QApplication`，跑得飞快（`test_comic.py` 的 109 条
  0.35 秒），也不受图形环境影响。
* **可移植性**。解析、判型、列页这些逻辑与"画在屏幕上"无关。混在一起之后，
  换个渲染后端就得动核心层。

**最具体的表现**：`Book` 只返回**引用**，不返回 `QPixmap`。

```python
# 对：核心层给出"去哪儿拿这一页"
def read_page(self, index: int) -> bytes: ...

# 错：核心层直接给出解码后的图片
def read_page(self, index: int) -> QPixmap: ...   # ← 引入 Qt，且把缓存策略钉死在核心层
```

`ComicBook.read_page()` 返回 `bytes`，解码与缓存都在 `minireader/ui/comic_cache.py`。
这样 `ComicBook` 在无 Qt 的测试里可以随便造，而"解码后放哪儿、放多久"是 UI 层的决定。

这条规矩由两个测试守着：

* `tests/test_book_base.py::TestCoreStaysQtFree` —— `BANNED = ("PyQt5", "PySide6",
  "minireader.qt", "QtCore", "QtWidgets")`，**读源码**找这些字符串。
* `tests/test_comic.py::TestCoreStaysQtFree` —— 对阶段 2 新增的核心模块做同样的事，
  `MODULES = ("archives", "formats", "images", "comic", "opener", "book_base")`，
  `BANNED` 比上面多一个 `"QtGui"`。

还有一个更强的版本：`test_importing_book_base_does_not_pull_qt_in` 开一个 `subprocess`，
断言 `sys.modules` 里 PyQt5/PySide6 的计数是 0。**"没写 import"和"运行时真的没加载"
是两件事**，这条测的是后者。

---

## 5. `QBuffer(QByteArray(bytes))` 会悬垂指针

**这是本项目踩过的最隐蔽的坑**：不是逻辑错误，是非确定性的内存访问违例。

```python
# 错 —— 会崩
buffer = QBuffer(QByteArray(head))

# 对 —— 见 minireader/ui/comic_cache.py:60
payload = QByteArray(head)     # 具名局部变量，活满整个调用
buffer = QBuffer(payload)
```

**为什么**：`QBuffer` 的 C++ 构造函数签名收的是 `QByteArray *`——一个**借用的**指针。
SIP/Shiboken 不接管所有权，它只是把地址存下来。写成嵌套调用的形式，临时 `QByteArray`
在**那条语句结束**时就被回收，buffer 继续指向已释放的内存。之后 `QImageReader(buffer)`
去读它，读到的是垃圾或直接访问违例。

**为什么难查**：崩溃是概率性的。实测：

| 场景 | 结果 |
|---|---|
| 单次调用，跑 8 遍 | 崩 1 遍 |
| 放进循环 | **5/5 全崩** |
| 退出码 | `-1073741819`（= `0xC0000005` access violation） |

单次调用大概率"正常"，所以本地跑一下、看到尺寸对，很容易以为没事。

**同类站点检查过，这个写法是安全的**：

```python
pixmap.loadFromData(QByteArray(data))    # comic_cache.py:84 —— 安全
```

`loadFromData` 收的是 **const 引用**，临时对象活到整个调用结束。危险的是
**把指针存下来留到调用之后用**的那种 API（`QBuffer`、`QDataStream`、`QTextStream`
的构造函数都是这一类）。

**记忆口诀**：凡是 Qt 里"构造一个流/缓冲去包住一块字节"的 API，
被包的那块字节**必须先落到一个具名局部变量里**。

### 有测试守着（两条，互补）

**1. 运行时：真的跑一遍。**
`tests/test_comic_ui.py::TestProbeSize::test_it_is_stable_when_called_in_a_loop`
—— 对同一个 CBZ 的同一页连续调 `probe_size()` **400 轮**，每轮断言宽高一致。
400 轮远超"1/8 概率"能躲过的范围（那个概率下 400 轮必崩）。

**2. 源码级：写法一出现就红。**
`tests/test_comic.py::TestNoDanglingQBuffer::test_no_qbuffer_wraps_a_temporary_qbytearray`
—— 走 `ast` 扫整个 `minireader/`，找 `QBuffer(QByteArray(...))` 这种嵌套调用。

为什么需要第二条：**运行时那条只覆盖 `probe_size()` 一条路径**，而且崩溃是概率性的。
将来在别处新写一个同样形状的调用，400 轮那条不会变红——它会一直绿到用户那里才崩。
源码级那条是确定性的，任何新增站点当场失败，还给出文件名和行号。

配套的 `test_the_guard_would_actually_catch_it` 是**给守卫本身做的反面校验**：
它把 `QBuffer(QByteArray(head))` 这个字符串喂给同一套 AST 匹配逻辑，断言"认得出"。
没有它的话，`offenders` 恒为空也可能是匹配逻辑写错了，而不是源码干净——
**假绿的守卫比没有守卫更糟**。

这条能留在 `test_comic.py` 里而不违反第 4 条（core 无 Qt）：它只读文件和走 `ast`，
不 import `minireader.qt`，也不需要 `QApplication`。

---

## 6. PDF 渲染：`rev_byteorder` 必须保持 False，且要传带 alpha 的 `fill_color`

`minireader/core/pdfdoc.py` 的 `render_page_bmp()` 把 pdfium 的输出原样当成 32 位 BMP
的像素区。这两个参数都不是随手写的，实测推翻了两次直觉：

**`fill_color` 决定通道数。** pdfium 默认（以及 `fill_color=(255, 255, 255, 255)`）
只给 **3 通道** BGR。**只有传带 alpha 的 `fill_color=(255, 255, 255, 0)` 才给 4 通道**。
本项目要 32bpp BMP（因为 32bpp 的 `BI_RGB` 不需要行填充数学，而且比 24bpp 还快：
scale 1/2/3 实测 3.6 vs 6.4ms、14.5 vs 15.3ms、28.6 vs 40.3ms）。

**`rev_byteorder` 必须保持默认的 `False`。** 默认输出已经是 `B, G, R, A`——正是 32 位
BMP 存的顺序，字节原样拷贝即正确。传 `rev_byteorder=True` 会在屏幕上**把红蓝对调**。
（用红蓝各半的页实测验证过。这条曾经写反过，所以留在这里。）

**不要用逐像素的 Python 循环翻转行序。** 必须用逐行切片：逐像素版在 scale 2.0 实测
**657.7ms**，逐行切片版是同一量级的个位数毫秒。

**取图片覆盖率要用高层 API。** `page.get_objects()` + `obj.type == 3` +
`obj.get_bounds()`。raw 的 `FPDFPageObj_GetBounds` 配 ctypes 浮点数组从 Python 调用
**会写到错的地方**——实测整页扫描件报回 `(792.0, 792.0, 792.0, 792.0)`，四个值全相同，
明显是垃圾值，但不会报错。

## 7. PDF 的四种边界失败长什么样（别照着 message 猜）

实测结果，写 `_load()` 的错误翻译时依赖的是这张表，不是猜测：

| 情况 | 抛什么 | message |
|---|---|---|
| 0 页文档 | `PdfiumError` | `Failed to load document (PDFium: Success).` |
| 加密（用户密码非空） | `PdfiumError` | `(PDFium: Incorrect password error)` |
| 加密（用户密码为空串） | **不抛** | 不给密码也能打开 |
| 损坏 | `PdfiumError` | `(PDFium: Data format error)` |
| 文件不存在 | `builtins.FileNotFoundError` | **不是 `PdfiumError`** |

两处反直觉：0 页那条 message 里写着 `Success`，是误导性的底层串——PDFium 根本不肯
加载无页文档，所以「0 页 PDF」的表现是**打开就抛错**，不是「打开后 `len(doc) == 0`」。
文件不存在抛的是 `FileNotFoundError`，所以 `except PdfiumError` 接不住它。

`pdfium.PdfiumError` 的 MRO 含 `RuntimeError`，所以 `except RuntimeError` 也能接住——
但那样会连别的 `RuntimeError` 一起吞掉，所以 `_load()` 显式写 `PdfiumError`。

## 8. `CURRENT_PHASE` 是「还没做」的闸门，不是产品永久真值

`minireader/core/formats.py` 的 `CURRENT_PHASE` 控制哪些格式能打开：`Format.phase >
CURRENT_PHASE` 的格式，`is_supported()` 为 False、`support_note()` 返回「PDF 文档要等
阶段 3」这类话。

**往格式表里加格式时，既有测试会跟着相位自动改变行为**——这是设计，不是脆弱。
`tests/test_ui_offscreen.py` 那两条拖放测试（断言 `.pdf` 被拒）与
`tests/test_comic.py` 的几条相位测试，断言的都不是「`.pdf` 不可用」这个永久真值，
而是「**在阶段 2 时** `.pdf` 不可用」。所以阶段 3 把它们改成仍锁着的 `.mobi` 是
**保住意图**，不是放宽断言。

这条规矩已经走过两轮，样本一路往后挪：`.pdf`（阶段 2 时锁着）→ 阶段 3 解锁 →
换 `.mobi` → 阶段 6 解锁 → 换 `.cbr`/`.rar`（阶段 8）。**断言一个字没变，换的只是
"当前还打不开的格式"这个样本。** 下次再有格式解锁，照做即可。

真正自动跟随、不用改的：`test_every_format_follows_the_phase_gate`、
`kind_for("a.pdf") == KIND_COMIC`（`kind` 只是默认猜测，见第 9 条）。

## 9. 双重身份的格式：`kind` 只是猜测，拖放不能信格式表

`.pdf` 在 `formats.py` 里声明 `kind=KIND_COMIC`，但一个 PDF 到底是文字书还是扫描漫画
**要读了内容才知道**。所以：

* `DUAL_IDENTITY_EXTS = (".pdf",)` 标出哪些扩展名的 `kind` 不算数。
* 判型的唯一入口是 `opener.pdf_choice(path, prefer)`，它返回 `PdfChoice(kind, needs_ask)`。
* `open_book_kind()` 收 `pdf_kind` 参数但**故意不使用**——它要对拖放载荷里每个文件都跑，
  在决定要不要接受拖放之前去测 PDF 密度不合适（拖进来 20 个文件就会读 20 份文档）。
* `needs_ask=True`（既不像文本也不像扫描）时由 UI 弹一次询问。**取消弹窗 = 按文本打开
  且不写记忆**：被 dismiss 的对话框绝不该比没有对话框更糟，而「用户拒绝回答」的诚实
  表现就是下次再问。
* 手工覆盖写进 `Progress.pdf_kind`，下次开这本书直接用它，不再测量。

## 10. 答案只在「真的开出来之后」才写进 Progress

这条纪律管的是所有「用户表态改变打开方式」的路径（ask-once 弹窗、右键
「按文本打开」/「按漫画打开」）。写成「先写答案，再尝试打开」会造成**永久锁死**：

扫描件没有可提取文字，所以「按文本打开」**必然失败**。如果答案先落盘了，
磁盘上就留下一个指向 `text` 的答案。当时屏幕上还是那本漫画，右键还能改回来——
但**关掉程序重开**时 `_pdf_preference()` 读回 `text`，`Book.from_pdf()` 再抛一次，
`load_path()` 返回 False，屏幕上没有书。而右键菜单的 PDF 项只在**有书且是 PDF** 时
才可见（`window.py` 的 `_sync_pdf_actions()` 要求 `self.book is not None`），
于是这个文件再也没有任何入口能改回来。

正确顺序：把答案交给 `load_path(pdf_kind=...)`，由它在 `open_book()` **成功返回之后**
才 `_remember_pdf_kind()`。失败就什么都不写，屏幕上留着的还是能看的那一本，
外加一句「已保持原来的打开方式」。

`remember_pdf_kind()` 因此**不自己写 Progress**——它只转发给 `load_path()`。
这是探针（不是测试）抓到的真缺陷：既有 60 条 PDF 测试当时全绿。

## 11. PDF 的页尺寸来自页框，不是渲染出来的像素

`ComicReader._render()` 原来无条件把解码出的 pixmap 尺寸写回 `ComicPage`：

```python
if pixmap is not None:
    book.set_page_size(self._index, pixmap.width(), pixmap.height())
```

对 CBZ 这是对的——不解码就无从得知页有多大。对 PDF 是错的：页框本来就知道
（`open_pdf()` 用文档自己的页框填好了，所以 `sizes_known()` 在画任何东西之前就是
True），而 pixmap 是页框乘 `RENDER_SCALE`（2.0）。后果是**同一份文档的第 1 页**
（被访问过）在底栏报 1224×1584，第 2、3 页（没被访问）报 612×792——同一本书报出
两种尺寸，而且先看到的那页是错的那种。

修法：`and not page.size_known` 才写回。已知道的不再覆盖。

**判据**：一个 `ComicBook` 如果 `sizes_known()` 在渲染之前就是 True（目前只有
PDF 是这样），那么 `set_page_size()` 就永远不该被调用。

## 12. 模式 B：Qt 的滚轮事件只投给指针下最深的那层控件，且不向父控件传播

连续滚动模式里，鼠标停在**页面槽位**上（读者看到的就是页面），而 `QScrollArea`
自己的滚轮处理是从它内部的 viewport 驱动的。两者对不上，于是"滚轮滚漫画"这个
最核心的手势会什么都不做。

实测（`_manual/qt_wheel_propagation_probe.py`，三层嵌套 + 事件过滤器）：

| 投递方式 | 谁收到 |
|---|---|
| `sendEvent(grandchild, wheel)` | 只有 grandchild |
| `postEvent` + `processEvents` | 只有 grandchild |
| 手工逐层冒泡 | grandchild -> child -> parent |

也就是说 **Qt 不会把未接受的滚轮事件交给父控件**（和 `Enter`/`Leave` 不同，
后者会沿祖先链发一圈）。所以：

* `StripView` 在每个槽位上装自己的 `eventFilter`，把滚轮收回 `wheelEvent`。
  装在**祖先**上没用——Qt 的事件过滤器是按"事件投递到的那个对象"查的，
  祖先的过滤器不会被咨询。
* `StripView.wheelEvent()` 因此**直接驱动滚动条**，不调 `super()`：
  继承来的处理只对"到达内部 viewport"的事件有效，对这个转发路径是空操作。
  如果留着 `super()`，会出现"滚在页边空白处有效、滚在页面上无效"——正好反了。

**判据**：`test_stage4_scroll.py::TestStripBoundary::test_a_wheel_over_a_slot_reaches_the_strip`
（滚在槽位上必须真的滚动）。

## 13. 模式 B：`QScrollArea` 的滚动范围是延迟算的，不能信

`setWidgetResizable(True)` 下，`QScrollArea` 要到**下一轮事件循环**才根据内容
控件的大小重算 `verticalScrollBar()` 的范围。在那之前 `maximum()` 是 0，
`setValue()` 会被**静默**夹到 0。

后果是实测到的真缺陷：在第 200 页切进模式 B，屏幕上显示第 1 页、而底栏还写着
200；第一次滚轮也纹丝不动（`bar.maximum() > bar.minimum()` 为假，边界判断整段
跳过）。

修法：`StripView._relayout()` 之后自己调 `_sync_scroll_range()`，用槽位高度之和
（这是我们的数，已经算完了）直接 `setRange(0, total - viewport_height)`。Qt 之后
自己重算会得到同一个数，所以这不是在跟布局打架。

**判据**：`test_stage4_scroll.py::TestStripScrollRange::test_the_range_is_right_immediately_after_set_pages`
（不 pump 就得对）+ `TestComicReaderScrollMode::test_entering_the_mode_lands_on_the_current_page`。

## 14. 模式 B：一页一页问尺寸要 252 ms，批量问只要 25 ms

规格要求"任何页面在加载前都要有真实 width/height"，这是模式 B 不抖动的唯一保证。
代价是打开时得读 300 页的文件头。阶段 2 的 `probe_size()` 每页重开一次
`ZipFile`，在 300 页上实测 **252.8 ms**——正好卡在"200ms 一次预取"的临界线外。

贵的地方不是解压，是**每页都重建一次中央目录和 300 个 `ZipInfo`**。一次打开、
逐页读头则只要 **24.8 ms**（10.19×），拆开看是 21.4 ms 读容器 + 4.6 ms 问
`QImageReader`。

所以 `probe_sizes()`（批量）是仓库里真正用的那个，`probe_size()`（单页）保留给
"只想知道一页"的场景。两条路都走同一个 `size_from_head()`，答案必须完全一致——
不一致就意味着占位块高度和实际绘制尺寸对不上。

**判据**：`test_stage4_scroll.py::TestBatchProbe::test_it_agrees_with_the_one_page_at_a_time_path`。
实测脚本：`_manual/stage4_probe_bench.py`、`_manual/stage4_manual_300.py`。

## 15. 模式 B：整本 = 一话，到底只提示不翻话

`ComicBook.chapter_count` 是页数、`chapters` 返回 `[]`，整本即一话是既有语义。
所以滚到底没有"下一话"可接，`_on_autoscroll_end()` 在漫画上**必须直接停下并报边界**，
不能走 `step_chapter(1)`：那会在末页原地打转，而 `_resume_autoscroll()` 又会把
滚动器重新启动到已经到底的条上——按钮亮着、屏幕不动。

边界提示只发一次（`StripView._boundary_latched`）：触控板一次轻扫连发十几个滚轮
事件，同一句话刷十遍比沉默更糟。滚回中间再到底会重新报一次。

到顶/到底**不写 Progress**：`index` 没变，没什么可写的。

**判据**：`test_stage4_scroll.py::TestStripBoundary`（4 条只发一次 + 重新武装）、
`TestWindowAutoscrollInScrollMode::test_the_end_of_a_comic_does_not_turn_pages`。

## 16. 待定项：书库导航（跳到下一本）是独立功能，不在漫画里做

阶段 4 定模式 B 的"到底怎么办"时，考虑过三个选项：

1. **整本 = 一话，无跨话**（**采用**）——`ComicBook.chapter_count` 是页数、
   `chapters` 返回 `[]`，这就是既有语义。
2. 文件夹按子文件夹切话——对 CBZ 无效（压缩包里没有子文件夹概念），而且会破坏
   阶段 2 的 `list_folder_pages()` 平铺行为。
3. 跳到书库的下一本——**这是"书库导航"功能，不限于漫画**，应该独立做，不应该
   藏在漫画的滚动边界里。

第 3 条记在这里，避免以后有人以为"漫画滚到底不能跳下一本"是漏做。要做的话，
它属于书库层：`library.json` 里已经有序，`MiniWindow` 只需要一个"打开下一本"的
动作，和书的类型无关。届时漫画的边界提示可以从"已经是最后一页了"扩展成
"已经是最后一页了（还有下一本）"，但**模式 B 本身不需要改**。

## 17. 双绑定：结构保证是日常手段，实跑是关键时刻的验收

项目要在 PySide6 和 PyQt5 上都能跑（见 `minireader/qt.py` 的模块 docstring）。
本机长期只装了 PyQt5，所以日常改动的兼容性靠**结构**保证，而不是靠每次都跑两遍。

### 日常手段（每次改完做）

1. **零处直接 import 绑定。** 所有 Qt 名字只从 `minireader.qt` 取。改完用
   grep 核对：

   ```powershell
   Select-String -Path minireader\*.py,minireader\ui\*.py,minireader\core\*.py `
     -Pattern "PyQt5|PySide6"
   ```

   命中只该出现在 `qt.py`（选择逻辑本身）和注释里。任何别的地方命中都是 bug。
2. **新增 Qt 名字必须先在 `qt.py` 导出。** 不要在业务代码里写
   `from PySide6.QtWidgets import ...`。导出时顺手确认两个绑定上都有这个名字
   （`hasattr` 核对一遍）。
3. **用 flat enum 形式**（`Qt.ScrollBarAlwaysOn`），不用 PyQt6 的
   `Qt.ScrollBarPolicy.X`——项目明确不支持 PyQt6。
4. **枚举不要直接 `int()`。** 见下面第 18 条。

### 结构保证的边界：它挡不住 Qt 5 -> Qt 6 的 API 差异

**结构保证能防"import 错绑定"，防不了"两个绑定上同一个 API 行为不同"。**
收尾 2 装了 PySide6 6.12 跑全量，387 条测试直接变红——而这些代码在 PyQt5 上
1216 条全绿，grep 也一处违规都没有。

所以：**关键节点必须装另一个绑定实跑一遍**，不能只看 grep 通过就宣布兼容。
需要实跑的节点：新增模式/大功能之后、动 `qt.py` 之后、发版前。

实跑办法（不需要改生产代码，用 import 屏蔽强制走另一条路）：

```powershell
py -3.12 _manual\run_both_bindings.py      # 两个绑定各跑一遍全量
py -3.12 _manual\probe_binding_parity.py   # 五个关键行为逐条并排比对
```

`run_both_bindings.py` 在 `import minireader` **之前**把 `PySide6` 打成
ImportError，`minireader.qt` 于是自然回落到 PyQt5。故意不加"选绑定"的环境变量：
那会为测试往生产代码里塞一个开关。

### 判据

`probe_binding_parity.py` 覆盖阶段 4 依赖的五个行为：`setWidgetResizable` 的
滚动范围时机、`wheelEvent` 的投递与传播、`QTimer` 去抖、`QStackedWidget` 切换、
信号连接。实测两个绑定**逐条一致**（定时器那项按 20ms 容差判，因为定时器是
"不早于"不是"正好"）。

## 18. Qt 6 的作用域枚举不是 int，`int(Qt.X)` 会抛

PySide6 6.12 起，`Qt.LeftEdge`、`Qt.NoModifier`、`QTextBlockFormat.ProportionalHeight`
这类枚举**不再是 int 的子类**，`int()` 直接 TypeError。而 PyQt5 5.15 恰好相反：
`int()` 能用，但 `.value` **不存在**。两个方向都是不对称的：

| 写法 | PyQt5 5.15 | PySide6 6.12 |
|---|---|---|
| `int(Qt.LeftEdge)` | OK (2) | **TypeError** |
| `Qt.LeftEdge.value` | **AttributeError** | OK (2) |
| `enum_value(Qt.LeftEdge)` | OK (2) | OK (2) |

所以一律用 `minireader.qt.enum_value()`，它按 `.value` -> `int()` 的顺序试，
并且对普通 int 和 None 都安全（很多调用点传的就是 int）。

已导出的常量：`EDGE_LEFT/EDGE_RIGHT/EDGE_TOP/EDGE_BOTTOM`（`edges_at()` 和缩放
数学用）。**测试代码同样受这条约束**——收尾 2 里有 20 处失败是测试自己写
`int(Qt.LeftEdge)` 造成的。

还有两个 Qt 6 的移除/收紧，一并记在这里：

* **`QApplication.desktop()` 没了**（Qt 6 移除）。取屏用
  `minireader.qt.available_screen_geometry(widget)`，它按
  `widget.screen()` -> `QGuiApplication.primaryScreen()` -> Qt5 的 `desktop()`
  顺序试。
* **`QWidget.enterEvent` 的签名收紧了**：Qt 6 要求 `QEnterEvent`，给它一个普通
  `QEvent(QEvent.Enter)` 会 TypeError（Qt 5 容忍）。测试里用
  `tests/test_ui_offscreen.py::_enter_event()` 造真事件——这本来也是更忠实的
  替身，真实指针给的就是 `QEnterEvent`。

**判据**：全量 discover 在**两个绑定**下各 1482 条全绿
（`_manual/run_both_bindings.py`）。

## 19. 模式 C：一跨页 = 两页，翻页步长属于 reader 不属于窗口

双页模式里"位置"的单位是**跨页**，不是页。而 `MiniWindow.step_chapter()` 一直是
`chapter_index + delta`——加**一页**。两者相乘的后果实测是这样：

```
从封面起连按"下一页"：
  step 0: reader.index=0  pair=(0, None)
  step 1: reader.index=1  pair=(1, 2)
  step 2: reader.index=1  pair=(1, 2)   ← chapter_index=2，屏幕上却不动
  step 3: reader.index=3  pair=(3, 4)
```

窗口加一页到 `index=2`，reader 收到 2 之后把它塌回它所属的跨页 `(1,2)`——所以
按两次只换了一次跨页。`chapter_index` 一直在涨、屏幕不动，是最难查的那种症状。

修法：模式 C 下 `step_chapter()` 把步长交给 `ComicReader.next_page()` /
`prev_page()`，窗口只采纳落点。**跨页算术只在 `ui/spread.py` 一处实现**，窗口那
一层完全不需要懂"跨页"这个概念。

判据：`test_stage5_spread.py::TestWindowSpreadMode::test_a_spread_turn_moves_two_pages`
（断言连按三次得到 `(1,2) (3,4) (5,6)`）。

## 20. 模式 C：两页共享一个高度，不各自宽度适应

双页视图里如果每页各自宽度适应，两个形状不同的页会得到两个不同的高度，接缝就是
锯齿状——这是双页视图看起来"坏掉"的典型样子，而且只在不同形状的书上出现，很容易
漏掉。

`spread.pair_size()` 取**一个**高度给两页，宽度按各自宽高比推出，三个约束同时满足：

```
高度 = min( 视口高, (视口宽 - 间距) / Σ(宽/高), 最小源高 )
```

"不放大"和 `comic_cache.fit_size()` 是同一个承诺；"两页加间距不超过视口宽"保证
第二页不会被挤出屏幕。

**这条规则刻意不做成可配置**：让"接缝水平"可以关掉，就等于允许用户配出一个锯齿
状的视图，而那不是任何人想要的。`comic_default_zoom` 在模式 C 下只影响高度上限。

判据：`test_stage5_spread.py::TestPairSize`（8 条）+
`TestSpreadViewGeometry::test_both_pages_get_the_same_height`。

## 21. 删一个常量/符号时，要 grep 所有引用点，而不是只删定义

**删定义容易，删引用点难。** 删定义是删一行；引用点散在别的文件里，而且删掉定义
之后**没有任何东西会报错**——Python 只在那一行真的执行到时才抛 `NameError`。

阶段 5 就踩了这个坑：`MODE_PENDING` 这个常量按设计被整个删除（三种模式都实现了），
但 `minireader/ui/comic_reader.py:500` 的 `set_mode()` 里还留着

```python
if mode not in IMPLEMENTED_MODES:
    self.notice.emit(MODE_PENDING.get(mode, "这个阅读模式还没做"))
```

**为什么测试没红。** 这行**不可达**：`IMPLEMENTED_MODES` 已经覆盖了 `READING_MODES`
全集，`mode not in IMPLEMENTED_MODES` 永远为假。守着它的两条测试在 `pending` 为空时
`skipTest`。于是出现最坏的一种组合——**一边有测试断言「`MODE_PENDING` 不存在」，一边
有代码在引用它，两边都是绿的**。

**危害不是"现在坏了"，是"将来某个阶段一加新模式就崩"**：任何"先往 `READING_MODES`
加词表、下个阶段再实现"的改动，都会让这行立刻变成 `NameError`，而且崩在"用户点了
一个还没做的模式"这条路上。

**「断言符号不存在」只守住一半。** `hasattr(module, "MODE_PENDING") is False` 证明的是
**定义**没了，它对**引用点**一无所知。完整的两半是：

1. 定义不存在（原来的 `hasattr` 断言，保留）；
2. **没有任何代码引用它**——这一半要用 AST 扫源码，因为运行时的 `hasattr` 看不见
   别的文件里的名字。

正确做法参考 `test_comic.py::TestNoDanglingQBuffer` 的写法：`ast.parse` 整个
`minireader/`，遍历所有 `Name`/`Attribute` 节点，把命中的 `文件:行号` 收集起来断言为空。
那种守卫是 **Qt-free** 的（只读文件、走 ast），所以能留在测试里而不破坏"core 无 Qt"。

**判据**：`test_stage5_spread.py::TestDeletedSymbolsAreNotReferenced`
——扫描 `minireader/` 下所有源码，断言没有任何地方引用已删除的符号
（`MODE_PENDING`，以及同类已删符号）。它带一条反面自检
（`test_the_guard_would_actually_catch_it`），确认 AST 匹配逻辑本身没坏，
否则"offenders 恒为空"也可能只是匹配写错了。

## 22. 可选依赖：import 写在函数体内，缺失时的提示要能照着做

阶段 6 的 MOBI/AZW3 走的是 6-B（`mobi` 作可选依赖）。这条约束把 `pdfdoc.py`
立下的做法固化下来，因为它现在有三个遵守者（`pdfdoc`、`mobidoc`，以及将来任何
一个引入可选依赖的模块）：

1. **模块级不许 `import` 可选依赖。** 一律写进函数体内。理由是启动路径：只读
   TXT/EPUB 的用户不该为 PDF 付 `pypdfium2` 的 import 钱，也不该为 Kindle 书付
   `mobi`（它还会连带拉 `loguru`）。`import minireader.core` 必须永远成功。
2. **有没有装，要能"不 import 就问出来"**：`is_available()` 用
   `importlib.util.find_spec`，而且必须 `try/except (ImportError, ValueError)`
   ——父模块不存在时 `find_spec` 是**抛异常**而不是返回 `None`。
3. **缺失时的提示必须包含能照抄的命令**（`pip install pypdfium2` /
   `pip install mobi`），不能只说"需要某个库"。用户得离开程序去装，句子必须自足。
4. **判断"能不能开"的逻辑要尽量排在依赖之前。** `open_mobi()` 先嗅探 DRM 再
   import `mobi`：一本带 DRM 的书在**没装 `mobi`** 的机器上，得到的应该是
   「请用 Calibre 脱壳」，而不是「请安装 mobi」——后者会让人白装一个库，然后
   仍然打不开。顺序本身就是设计，所以 `test_mobi.py` 专门为它留了一条断言
   （`test_drm_is_reported_even_without_the_mobi_library`）。
5. **可选依赖不进 `requirements.txt` 的必装行**，但要在那里留注释说明为什么
   （`mobi` 是 GPL-3.0、且带一个 3.13 已废弃的 `imghdr`）。

**判据**：`test_mobi.py::TestCoreStaysQtFree::test_the_mobi_library_is_imported_lazily`
（AST 扫模块顶层，断言没有 `import mobi`）、`TestIsAvailable`（3 条，含父模块
缺失的两种异常）、`_manual/stage6_no_mobi.py`——后者把 `mobi` 所在的
site-packages 从 `sys.path` 摘掉再跑整个 `test_mobi.py`，实测
`ran=57 skipped=15 failures=0 errors=0`：**判定类断言一条都不需要那个 GPL 库**。

## 23. 第三方函数"先建目录再干活"时，`try/finally` 抓不住那个目录

阶段 6 修掉的一个真泄漏，值得单独记一条，因为它长得完全像正确的代码：

```python
tempdir = ""
try:
    tempdir, extracted = mobi.extract(target)   # ← 看起来没问题
    return _read_extracted(extracted, info)
finally:
    if tempdir:
        shutil.rmtree(tempdir, ignore_errors=True)
```

**这段在成功路径上是对的，在失败路径上漏。** `mobi.extract()` 内部先
`mkdtemp()`，再开始解包；解包中途抛异常时它**不会返回**，所以 `tempdir` 这个名字
还是 `""`，`finally` 里的 `if` 判定为假，半个解包目录就留在 `%TEMP%` 里。用户拿到
一个损坏的下载文件，每试一次漏一个目录。

**通例**：一个第三方函数如果"先创建资源、再做事、把资源路径**作为返回值**给你"，
那么它失败时那个资源就已经存在了，而你手上没有它的名字——**返回值不是资源存在的
证明**。这时候不能靠返回值清理，要让资源的**创建位置由我们决定**：

```python
scratch = tempfile.mkdtemp(prefix="minireader_mobi_")
previous = tempfile.tempdir
tempfile.tempdir = scratch          # 库内部的 mkdtemp 会落在我们这里
try:
    ...
finally:
    tempfile.tempdir = previous     # 先还原全局状态
    shutil.rmtree(scratch, ignore_errors=True)
```

成功、部分失败、还没开始就失败，三种情况一次 `rmtree` 全覆盖。**注意
`tempfile.tempdir` 是进程级全局**，这个手法不线程安全；MiniReader 全局无
`QThread`/executor（`grep` 可证），所以可以接受，但将来若引入后台线程，这里要
改成传 `dir=` 参数或加锁。

**判据**：`test_mobi.py::TestOpenUncompressed::test_a_failed_unpack_does_not_leak_its_directory`
和 `::test_the_scratch_root_is_put_back_even_when_unpacking_blows_up`。
**为什么原来的泄漏测试没抓到**：`test_the_temporary_directory_is_removed` 只在
**成功**路径上比对前后快照，一直是绿的——**清理类测试必须同时覆盖失败路径，
否则它测的只是"顺利时我清理了"**。

## 24. 源格式的边界标记不等于读者的章节边界

阶段 6 的探针在 mobi7 上抓到的第二个缺陷，通用于任何"从别人的结构里猜章节"的路径。

mobi7 是一整块 HTML，没有 spine，唯一自带的边界是分页标记 `<mbp:pagebreak/>`。
于是第一版把分页标记当章节边界，看起来完全合理。但 mobi7 里**极常见**的排版是：

```
前言 …… <mbp:pagebreak/> 第1章 …… 第2章 …… 第3章 …… （再没有标记）
```

只认分页标记的结果是 **2 章**，其中第 2 章有三章那么长：目录里两个条目，一次翻页
跨过三章。实测（`_html_segments` 修之前）：`segments=2`，标题 `['T', '']`——连章名
都是空的，因为那一段里根本没有 heading。

**通例**：源格式的边界标记说明的是**排版**（这一页结束了），不是**语义**（这一章结束
了）。两者经常重合，所以"只用标记"在多数书上看起来是对的，在少数书上错得很隐蔽。
正确做法是**两级判定**：先用标记切粗块，再对每一块跑一遍真正的内容检测器，检测器
找到多于一个章节时才采纳它的答案。

```python
pieces = [p for p in PAGEBREAK_RE.split(data) if p.strip()] or [data]
segments = []
for piece in pieces:
    segments.extend(_segments_from_piece(piece))   # 检测器 >1 章才细分
```

**反面同样要守**：如果检测器**无条件**覆盖标记，一个分页标记一章的规整书就会被切碎。
所以判据是双向的——`test_a_piece_holding_several_chapters_is_split_further`（该细分）
与 `test_a_well_formed_book_is_not_split_further`（不该细分）成对存在。

**判据**：`test_mobi.py::TestExtractedShapes::test_a_piece_holding_several_chapters_is_split_further`
+ `::test_a_well_formed_book_is_not_split_further`。

## 25. 新增一种"来源"时，`== "旧来源"` 的分派点会安静地落到 else

阶段 7 给 `ComicBook` 加了第三种来源 `"epub"`（固定版式 EPUB 也是 zip，和 `.cbz`
一样按成员名取页）。代码里有五处分派 `self.source`：

```python
if self.source == "cbz":
    return archives.read_member_heads(self.path, refs, limit)
return images.read_file_heads(refs, limit)      # ← EPUB 掉到这里
```

`read_page` / `read_page_head` 那几处改了，`read_page_heads` 漏了一处。后果不是
异常，而是**每一页尺寸都返回 `b""`**：`images.read_file_heads()` 把 zip 成员名
（`OEBPS/p1.png`）当文件系统路径去开，开不到就交一个空头。上层拿到空头会理解成
"尺寸未知"，于是整本书的页面布局全按 0 算——不崩、不报错、只是所有页都挤在一起。

**通例**：`== "某来源"` 是**闭集判断**，新增来源时它不报错，只是把新来源归进
"其他"。所以

1. 分派点写成 `in ("cbz", "epub")` 这类**开集**形式，并在注释里写清"为什么这个
   新来源和旧来源同路"；
2. 加新来源时，用 `grep 'source =='` / `grep 'source in'` 把**每一个**分派点过
   一遍，而不是只改报错的那一处（和约束 21 同源：删符号要 grep 引用点，加来源
   也要 grep 分派点）；
3. 测试要断言**值的正确性**，不能只断言"没抛异常"。这一处漏掉的直接证据是
   `test_reading_several_heads_keeps_them_aligned` 断言返回的每一条都是真头
   （含正确的宽高），而不是 `len(...) == 3`。

**判据**：`test_epub_fixed.py::TestOpenFixed::test_reading_several_heads_keeps_them_aligned`。

## 26. "没意见"和"有意见，选了默认值"必须能区分

阶段 7 读 EPUB 的 `page-progression-direction`。规范说：**没写就是 `ltr`**。于是
第一版让 `_read_rendition()` 直接返回 `"ltr"`，`ComicBook.binding` 就拿到了
`"ltr"`——而 `binding` 的语义是"这本书要求怎么装订"，优先级**高于**用户的全局
设置。

后果：一本**什么都没说**的日漫 EPUB（绝大多数 mobi7/EPUB2 转换来的书都不写这个
属性）会悄悄把用户的"右綴じ"改成左綴じ。用户没改任何设置，书却翻反了，而且下次
打开另一本书又正常——最难查的那种。

**通例**：当一个字段既可以是"用户/文件给出的值"又可以是"缺省值"时，**不要用缺省
值表示缺省**。给"没写"一个独立的表示（这里是空串 `""`），让下游自己决定退回什么：

```python
class FixedContent:
    progression: str = ""   # 空串 = 这本书对装订方向没有意见
```

```python
own = getattr(book, "binding", "")
if own in ("rtl", "ltr"):
    return own                      # 书有意见，听书的
return self._binding_setting        # 书没意见，听设置的
```

注意 `_read_rendition()` 本身**仍然返回 `"ltr"`**——那是对 EPUB 规范的忠实实现，
换掉它反而会让函数不再符合规范。区分发生在**另一层**：`_read_progression_declared()`
回答"这本书写了吗"，`_read_fixed()` 用 `progression if declared else ""` 落成
`FixedContent`。**两个问题分开问，不要为了省一个函数把语义压扁。**

**判据**：`test_epub_fixed.py::TestOpenFixed::test_a_book_without_a_progression_has_no_opinion`
（没写 → `""`）与 `::test_a_declared_ltr_is_an_opinion`（写了 ltr → `"ltr"`）成对存在。
只测后者会漏掉整条约束。

## 27. "能不能开"的判定要排在"怎么读"之前，且判定失败必须退回默认

固定版式和流式共用 `.epub` 一个扩展名，区别只在 OPF 的 metadata 里。所以
`opener.open_book()` 必须在调 `parse_epub()` **之前**先问一次 `epub_layout()`：

```
.epub → epub_layout(path)  ──pre-paginated──→ ComicBook.from_epub(path)
                           └──其他──────────→ Book.from_path(path)   # 阶段 1 起一行没改
```

顺序反了会得到一句**误导人的错话**：流式那条路对"整本都是图片"的书会报
「这本书里没有能读的正文（可能整本都是图片）」。对固定版式书，这不是错误——那是
它的**正常形态**。用户会以为自己下载坏了。

两条附带规则：

* **判定必须便宜**。`epub_layout()` 只读 OPF（几 KB），不解压正文、不解码图片。
  一本流式书为此多付一次小读取，而不是多付一次全文解析。
* **判定绝不抛异常**。`epub_layout()` 里 `except Exception: return 默认值`。理由是
  同一句错话原则：同一个坏文件，无论从哪条路进来，用户看到的必须是**同一句**
  中文错误。让判定函数抛自己的异常，用户就会在"判断能不能开"这一步收到一句
  没头没尾的话，而真正的打开路径（带 `_open_zip()` 的中文文案）根本没机会说话。

**判据**：`test_epub_fixed.py::TestDispatch::test_the_text_path_is_not_touched`
（断言 `parse_epub(path)` 对固定版式书抛「没有能读的正文」，而 `open_book(path)`
给出 `KIND_COMIC`——两者同时成立，正是"判定在前"的证据）+
`TestDetectionNeverRaises`（5 条：缺文件 / 目录 / 非 zip / 坏 OPF / 带 DRM 全部
安静地答 `reflowable`）。

## 28. 一个能力"存在"不等于"能用"——写好了 API 还要 grep 调用点

阶段 5 就写了 `ComicReader.cycle_binding()`，注释、文档、测试全都有，唯独**没有
任何界面入口**调它。于是"用户仍可在界面切换装订方向"这句规格在实现上是空的：
书里声明的 `page-progression-direction` 是**不可推翻**的。单测全绿，因为单测直接
调那个方法。

**通例**：给一个能力收尾时，问的不是"这个方法存在吗"，而是"**从界面能走到它吗**"。

```
grep 'cycle_binding' → 只有定义、测试、和它自己的内部调用
                     → 没有任何 ui/window.py 的调用点 → 缺入口
```

这和约束 21 是同一个动作的两个方向：约束 21 是"删了符号要确认没人引用"，这一条
是"加了符号要确认有人引用"。两者都靠 grep，都靠"不要相信测试通过"。

**判据**：`test_comic_ui.py::TestComicBindingButton`（9 条）。其中
`test_a_window_too_narrow_for_two_pages_hides_it_again` 守的是另一半——窗口拖窄后
`ComicReader.resizeEvent()` 会自动退回单页，按钮必须跟着消失，否则它会留在一个
"点了什么都不改"的窗口上，比不放更糟。

## 29. 外部程序不是依赖：探测结果要缓存两份，但**失败不许缓存**

`.cbr`/`.rar` 是唯一不能用纯 Python 解压的格式（压缩算法专有）。`rarfile` 只把
**头部**用 Python 解析（RAR3/RAR5 都能），取成员字节要靠 `unrar.exe` 或 `7z.exe`。
这两个程序不打包进 exe（unrar 的分发许可不允许，7-Zip 会给 exe 加 ~1.5 MB），
所以 `core/rar.py` 的职责比 `core/archives.py` 多两件：**找工具**、**记住找到过**。

缓存必须有两份，理由不同：

| 位置 | 生命周期 | 解决什么 |
|---|---|---|
| `_CACHED` + `_PROBED`（模块级） | 一个进程 | 每翻一页都探测一次是荒谬的（每次探测要跑一次 `unrar -?`） |
| `Settings.unrar_path/unrar_kind/unrar_checked` | 跨启动 | 否则**每个会话的第一页**都比后面的慢 |

启动时直接信任磁盘上的答案（`__main__.py` 里 `rar.remember(...)`，它会重新 stat，
路径没了就回落到真探测）。用户要强制重探，只有「重新检测」按钮那条路
（`rar.find(force=True)`）。

**关键的一半是反过来的：失败不缓存。** `find()` 只在成功时写 `_CACHED`/`_PROBED`。
缓存一次 miss 的后果是：用户读到「请先安装 7-Zip」→ 装好 → 每次仍然说「请先安装」，
而且**没有任何界面能把它救回来**（设置页那个按钮也只会看到缓存）。多跑四次 `which`
的代价是微秒级的，把用户锁在错误答案里是永久的。

同理，`Settings.clamp()` 里「空路径强制清空 kind」不是洁癖：这一对是被
`rar.remember()` **当整体读**的，有 kind 没 path 会让阅读器相信自己有工具，然后去
启动 `""`。

**判据**：`test_rar.py::TestCache::test_a_miss_is_not_cached`（连调两次，`probe()`
必须被调两次）+ `::test_a_success_is_cached`（连调两次，只调一次）+
`::test_a_failed_remember_does_not_mark_probed`。三条缺一不可——只测成功缓存会漏掉
整条约束。另加 `TestConstants::test_the_kinds_agree_with_storage` 守两处常量的重复。

## 30. 「书有意见」和「设备形状」是两个输入，别合并成一个开关

`rendition:spread` 的四个值（`none` / `landscape` / `both` / `auto`）**全部只回答
"可否渲染合成跨页"**，与页的形状无关。这是阶段 8 最容易做错的一处，第一版设计就
做错了：

```
错：landscape → 横版的页才配对，竖版页单独显示     （把 device 当成了 page）
对：landscape → 仅当阅读区是横屏时才配对；竖版页照样配对
```

W3C 原文是 *"only when the device is in landscape orientation"* / *"regardless of
device orientation"*——说的是**阅读设备的朝向**，不是页面的长宽比。按页形分叉会让
一本横竖混排的书写出"有些页两页一组、有些页一页"的排法，而规范要的是全有或全无。

于是实现收成一个布尔闸门 `spread.pairs_pages(policy, landscape)`，`landscape` 由
调用方**传入**而不是自己去量（这样没有窗口也能测；`ComicReader` 用
`needs_spread_width()` 回答它）。六个锚点函数因此只多了一个 `*, pairs: bool = True`
——默认值让阶段 5 的每一条断言原样成立，`pairs=False` 自然退化成"一页一个 spread"，
所以**模块其余部分零改动**：一页的 spread 仍然是 spread，阅读顺序不变。

两个附带规则：

* **不变量必须保住**：无论哪个策略，每页仍要**恰好**落进一个 spread。
  `test_epub_spread.py::TestPairingOffDegrades::test_every_page_still_lands_in_exactly_one_spread`
  对 total 0..24 × 封面两态扫一遍。这是"配对"与"不配对"之间唯一不能变的东西。
* **查询路径不许发通知**。`_resolve_pairs()` 只算不播，播报放在 `_say_pairs_once()`
  里由渲染路径调用。原因是 `current_position()` 也调 `_resolve_pairs()`，而窗口画
  底栏时会调 `current_position()`——从查询里 `emit` 会让底栏刷新变成弹提示。

**判据**：`test_epub_spread.py::TestPairsPolicy`（9 条，含 `none` 与视口无关、
`landscape` 两态、非法值回落）+ `TestPairingOffDegrades`（12 条，含完整性证明）
+ `TestReaderConsumesThePolicy::test_the_notice_is_said_once_not_on_every_render`。

## 31. 待定项：`rendition:page-spread-left/right` 与 spread 策略正交

阶段 7 报告 §10 列过一条「`rendition:spread` 读进来了但 UI 没有消费者」，那条
**已经关闭**：阶段 8 把它接进了 `ComicReader`，见上面第 30 条。这里记的是同一族
里**另一个还没做**的属性，别把它和第 30 条混起来。

EPUB 3 在 spine 的每个 `itemref` 上允许写 `rendition:page-spread-left` /
`page-spread-right`（词表见 EPUB 3.3 §D.7）。它回答的是**摆放位置**：

> 这一页在合成跨页里应该落在左边还是右边。

而 `rendition:spread`（第 30 条）回答的是**能不能配对**。两者**正交**：

| | `spread=none` | `spread=both` |
|---|---|---|
| `page-spread-right` | 这页单独显示（位置属性无人消费） | 这页放在右槽 |
| 没写位置属性 | 同上 | 由装订方向决定（现状） |

所以「我们已经支持 spread 了」**不蕴含**「位置属性也支持了」。现状是：`FixedPage`
没有这个字段，`epub.py` 的 `_read_fixed()` 不读 `itemref` 上的属性，`spread.py` 的
`visual_order()` 只看 `binding`（rtl 交换 / ltr 不换）。

**要做的话，第一步不是改 `spread.py`**，而是让 `FixedPage` 记住位置：

1. `epub.py` 解析 `itemref` 时读 `rendition:page-spread-left/right`，存进
   `FixedPage.position: str = ""`（空 = 没意见，与第 26 条同一个约定）。
2. `ComicBook` 透传（`_page_images` 旁边再加一张 `_page_positions`）。
3. **然后**才是 `visual_order()` 收一个可选的逐页位置表，优先于 `binding`。

跳过第 1、2 步直接在 UI 里猜（比如"奇数页在右"）会做出一本**看起来对**的实现：
大多数日漫确实如此，但一本写了显式位置属性的书会被摆错，而且错得没有任何提示。
这和阶段 7 拒绝半实现 `spread` 是同一个理由——**半实现的规范比不实现更糟**，
因为它让错的书写错得更隐蔽。

判据（将来做的时候）：`test_epub_fixed.py` 里加一条读 `itemref` 属性的解析测试 +
`test_epub_spread.py` 里加一条「位置属性压过装订方向」的配对测试。**现在没有测试
守着它**，因为它还不存在——这正是把它写进文档而不是写进代码的原因。

## 32. 别把购物车当书：正文 body 只认"没名字"的那一个

FB2 允许一个文件里放多个 `<body>`。正文**不带 `name`**，注释和尾注挂在
`<body name="notes">` / `<body name="comments">` 上。把带名字的也当正文，读者会在
第 3 章中间读到一整页译者注——那不是"多给了一些内容"，是**错的顺序**：注释是
按页码挂的，脱离页码之后每一句都指向一个读者已经翻过去的地方。

这条之所以要编号，是因为它是「格式边界 ≠ 章节边界」（第 24 条）的一个特例：
FB2 的 `<body>` 是它的文件级边界，而其中只有一个属于故事。判据因此不只是"读了
几个 section"，而是**断言那条注释的正文一个字都没出现**。

`core/fb2.py` 的 `_main_body()` 做这件事：优先取第一个没有 `name` 属性的，一个都
没有时才退回第一个（没有规范说必须有，但"退回第一个"比"退回最后一个"安全——
第一个通常是正文）。多个 body 时 `parse_fb2()` 记一条 note，让读者知道有东西没读。

**判据**：`test_text_formats.py::TestFb2::test_a_named_body_is_not_read_as_the_main_text`
（断言 `译者注` 不在任何 segment 的 text 里）+ `::test_a_second_body_is_reported_as_a_note`
+ `::test_a_single_body_has_no_such_note`（不该记的时候别记）。

## 33. 绕过第三方时，也要把"它在帮你"传下去

`core/rar.py` 的兜底子进程路径有一段很长的注释：`read_page_heads()` 走不到它，
因为那会为 300 页起 300 个进程。第二批的 `core/cb7.py` 全部走 `7z` 命令行，
`read_page_heads()` 干脆**什么都不做**，直接返回全 `b""`。

于是出现一个只在 CB7 上成立的性质：**"我不知道"是所有取字节入口的合法回答**。
`b""` 表示"这个页的尺寸不知道"，调用方（`comic_cache.probe_sizes()`）会留一个
占位高度；它**不是**"这一页是空的"。把 `b""` 当空页会做出一页空白——比占位高度
不准严重得多。

反过来，`list_pages()` 在这里没有兜底可走：7z 的文件名在头里是 LZMA 压的，不跑
外部程序就一个名字都拿不到。所以 CB7 的"列页"**必须**有工具，错误文案必须是
"请先安装 7-Zip"，而且这条文案要在**打开时**就说清楚（不是翻到第 7 页才说）。

**判据**：`test_cb7.py::TestReading::test_read_page_heads_returns_nothing_for_every_page`
（`b""` 是刻意的）+ `TestListing::test_a_missing_tool_says_what_to_install`（列页不糊）+ 
`::test_a_listing_that_cannot_be_parsed_falls_back_to_the_tool`（`rar.list_names_by_tool()`
失败时报的是原来那个错，不是"没有图片"——别把"出错"说成"没内容"）。

## 34. 活动状态要**就地改写**，不能重新绑定名字

主题（暗夜/白昼）是**进程级可变状态**：五个模块都是 `from .theme import PALETTE`，
之后按下标取值。如果 `theme.apply_theme()` 写成

```python
PALETTE = THEMES[name]          # ✗ 重新绑定名字
```

那五个模块手里那个名字仍然指着**旧字典**——切换**静默失效且不报错**：调色板换了，
界面没换，日志里一个字都没有。这种 bug 在真桌面上表现为"按了 🌙 什么也没发生"，
但单测里只要没专门断言就查不出来。

正确做法是改写**同一个** dict 对象：

```python
PALETTE.clear()
PALETTE.update(THEMES[name])    # ✓ 就地改写
```

`ACTIVE_EXTRAS`（那些不进 11 键调色板、但同样按主题翻方向的颜色）同款处理。

这条约束一般化成一句话：**跨模块共享的可变配置，切换时改内容，不改名字**。

**判据**：`test_theme_switch.py::TestPaletteSwitching::test_switching_rewrites_the_same_dict_rather_than_rebinding_it`
（断言 `id(theme.PALETTE)` 切换前后不变——**id 相等才是"就地改写"的唯一直接证据**）+
`::test_an_alias_held_by_another_module_sees_the_switch`（模拟 `from ... import PALETTE`
拿到的别名看得见切换）。

## 35. 模块级 QSS 常量在 import 时就把颜色插值死了，必须变成函数

原来有三个常量是模块级的：`READER_MENU_QSS`、`TOOLTIP_QSS`、`PDF_ASK_QSS`。
前两个是普通字符串，第三个是 **f-string**——f-string 在 **import 时**就求值完了，
里面已经写死了那一档颜色。切主题时它们不会重算。

所以：**凡是要跟着活动状态变的样式表，都必须是函数**（`reader_menu_qss()` 等），
让每次调用重新求值。本来就写成函数的 `_qss()` / `_stylesheet()` 天然没问题。

配套的第二个坑在 **幂等标记**上。原来的写法是

```python
if "MiniReaderTooltip" not in app.styleSheet():   # ✗
    app.setStyleSheet(app.styleSheet() + TOOLTIP_QSS)
```

切主题后标记还在，所以新样式**永远挂不进去**——表现为"主窗变白、tooltip 仍是黑的"。
正确做法是**先摘旧块再挂新块**（`_install_tooltip_theme()`）：按标记切掉旧的，再追加新的。

**判据**：`test_theme_switch.py::TestStylesheetsFollowTheTheme::test_the_three_converted_constants_differ_between_themes`
（两套主题下产出不同串——"它真的按调用求值"）+
`::TestWindowFollowsTheTheme::test_the_tooltip_block_is_replaced_rather_than_stacked`
（`app.styleSheet()` 里 tooltip 块**恰好一份**且是新色，深色那份不许残留）+
`::test_no_stylesheet_invents_a_colour_of_its_own`（六个产出样式表的函数里，出现的每一个
6 位 hex 都必须来自活动调色板 / 活动 extras / 主按钮橙——白昼的"白底白字"只有真桌面撞得见，
所以在源码级堵住）。

## 36. `getattr(obj, name, default)` 的默认值会把拼写错误吞成"没这个属性"

切主题时右键菜单要重挂样式表，代码写的是

```python
menu = getattr(self, "_reader_menu", None)      # ✗ 属性其实叫 reader_menu
if menu is not None:
    menu.setStyleSheet(reader_menu_qss())
```

属性真名是 `self.reader_menu`。`getattr` 的 `None` 默认值把这个拼写错误**变成了一个合法的
"没有菜单"状态**：不报 AttributeError、不报 KeyError，菜单永远重挂不上，白昼下就是
"主窗是白的、右键菜单还是黑的"。测试也没抓到——因为既有测试都通过 `self.menu` 这个
夹具属性访问菜单，没有任何一条断言"切主题后 `window.reader_menu.styleSheet()` 变了"。

这条和约束 21（删符号要 grep 引用点）是同一族：**名字写错时，`getattr` 的默认值、
`dict.get()` 的默认值、`**kwargs` 的静默吸收，都会把错误伪装成"没意见"**。
需要"有就处理、没有就算了"的场合，用 `hasattr` 先判存在，或干脆用 `self.reader_menu`
直接访问——**让 AttributeError 真的抛出来**。

**判据**：`test_theme_switch.py::TestWindowFollowsTheTheme::test_the_reader_menu_is_reskinned`
（切两个方向各断言一次 `window.reader_menu.styleSheet() == reader_menu_qss()`——
这条是**回归测试**，它存在的唯一理由就是上面那个 `getattr` 拼错）。

---

## 附：这几条各自守在哪

| 约束 | 守它的测试 |
|---|---|
| 1. `Reader` 是普通类 | `test_book_base.py::TestReaderIsAPlainClass::test_it_is_not_an_abc_or_a_protocol` |
| 2. `Reader` 排 MRO 最前 | `test_book_base.py::test_reader_view_puts_reader_before_qt`（断言 `mro[:4] == ["ReaderView", "Reader", "QTextEdit", "QAbstractScrollArea"]`） |
| 3a. 不声明 Qt 名字 | `test_book_base.py::test_reader_declares_no_qt_owned_name`（遍历 `_QT_OWNED` 34 个） |
| 3b. 接口名在两个绑定上都空 | `test_book_base.py::test_interface_names_are_free_on_both_widget_bases` |
| 3c. 接口一个不少 | `test_book_base.py::test_reader_declares_every_interface_name`（遍历 `_INTERFACE` 10 个） |
| 4a. core 无 Qt（源码） | `test_book_base.py::TestCoreStaysQtFree`、`test_comic.py::TestCoreStaysQtFree` |
| 4b. core 无 Qt（运行时） | `test_book_base.py::test_importing_book_base_does_not_pull_qt_in`（subprocess） |
| 5a. `QBuffer` 陷阱（运行时） | `test_comic_ui.py::TestProbeSize::test_it_is_stable_when_called_in_a_loop`（400 轮） |
| 5b. `QBuffer` 陷阱（源码级） | `test_comic.py::TestNoDanglingQBuffer::test_no_qbuffer_wraps_a_temporary_qbytearray`（AST 扫全包）+ `test_the_guard_would_actually_catch_it`（守卫自检） |
| 6. PDF 渲染参数 | `test_pdf.py::PdfComicPathTestCase::test_a_page_renders_to_bytes_qt_can_decode` + `test_the_rendered_page_is_the_expected_shape` |
| 7. PDF 失败模式 | `test_pdf.py::PdfClassifyTestCase::test_a_zero_page_pdf_is_reported_as_zero_pages`、`test_an_encrypted_pdf_is_reported_as_password_protected`、`test_a_missing_file_says_so_in_chinese` |
| 8. 相位闸门 | `test_comic.py::test_every_format_follows_the_phase_gate` |
| 9. 双重身份 | `test_pdf.py::PdfDispatchTestCase`、`PdfChoiceTestCase`、`PdfUiTestCase` |
| 10. 开出来才写答案 | `test_pdf.py::test_an_override_that_cannot_open_does_not_get_written` + `test_that_poisoned_file_still_opens_next_launch`（后者模拟下次启动，是真正证明「没被锁死」的那条） |
| 11. PDF 页尺寸取页框 | `test_pdf.py::test_a_pdf_reports_its_page_box_not_the_rendered_pixels`（+ `test_a_cbz_still_learns_its_sizes_by_decoding` 作对照） |
| 12. 滚轮投递到槽位 | `test_stage4_scroll.py::TestStripBoundary::test_a_wheel_over_a_slot_reaches_the_strip` |
| 13. 滚动范围立刻正确 | `test_stage4_scroll.py::TestStripScrollRange`（3 条）+ `TestComicReaderScrollMode::test_entering_the_mode_lands_on_the_current_page` |
| 14. 批量探测答案一致 | `test_stage4_scroll.py::TestBatchProbe`（8 条） |
| 15. 整本 = 一话 | `test_stage4_scroll.py::TestStripBoundary`、`TestWindowAutoscrollInScrollMode`、`TestWindowBoundaryCommands` |
| 16. 书库导航是独立功能 | 待定项记录，非测试 |
| 17. 双绑定 | `_manual/run_both_bindings.py`（两个绑定各 1741 条全绿）+ `_manual/probe_binding_parity.py`（五个行为逐条一致） |
| 18. 枚举用 `enum_value()` | 两个绑定下的全量 discover（PySide6 下 387 条会因 `int(Qt.X)` 变红） |
| 19. 模式 C 的步长归 reader | `test_stage5_spread.py::TestWindowSpreadMode::test_a_spread_turn_moves_two_pages` |
| 20. 模式 C 共享高度 | `test_stage5_spread.py::TestPairSize` + `TestSpreadViewGeometry::test_both_pages_get_the_same_height` |
| 21. 删符号要扫引用点 | `test_stage5_spread.py::TestDeletedSymbolsAreNotReferenced::test_no_source_file_references_a_deleted_symbol`（AST 扫全包）+ `test_the_guard_would_actually_catch_it`（守卫自检） |
| 22. 可选依赖延迟 import | `test_mobi.py::TestCoreStaysQtFree::test_the_mobi_library_is_imported_lazily`（AST 扫模块顶层）+ `TestIsAvailable`（3 条）+ `_manual/stage6_no_mobi.py`（摘掉 site-packages 后 57 条里 15 skip、0 失败） |
| 22b. DRM 只提示不解密 | `test_mobi.py::TestCoreStaysQtFree::test_no_decryption_code_exists`（AST 扫标识符，不扫文本——否则会误伤解释这件事的注释） |
| 23. 清理要覆盖失败路径 | `test_mobi.py::TestOpenUncompressed::test_a_failed_unpack_does_not_leak_its_directory` + `::test_the_scratch_root_is_put_back_even_when_unpacking_blows_up` |
| 24. 格式边界 ≠ 章节边界 | `test_mobi.py::TestExtractedShapes::test_a_piece_holding_several_chapters_is_split_further`（该细分）+ `::test_a_well_formed_book_is_not_split_further`（不该细分） |
| 25. 新来源别落进 else | `test_epub_fixed.py::TestOpenFixed::test_reading_several_heads_keeps_them_aligned`（断言每一条都是真头，不是"没抛异常"） |
| 26. "没意见"要能区分 | `test_epub_fixed.py::TestOpenFixed::test_a_book_without_a_progression_has_no_opinion` + `::test_a_declared_ltr_is_an_opinion`（成对） |
| 27. 判定排在读之前 | `test_epub_fixed.py::TestDispatch::test_the_text_path_is_not_touched` + `TestDetectionNeverRaises`（5 条） |
| 28. 能力存在 ≠ 能用 | `test_comic_ui.py::TestComicBindingButton`（9 条，含"窗口拖窄后按钮跟着消失"） |
| 29. 失败不缓存 | `test_rar.py::TestCache::test_a_miss_is_not_cached` + `::test_a_success_is_cached` + `::test_a_failed_remember_does_not_mark_probed` |
| 30. 书有意见 ≠ 设备形状 | `test_epub_spread.py::TestPairsPolicy`（9 条）+ `TestPairingOffDegrades`（12 条，含完整性证明）+ `TestReaderConsumesThePolicy`（10 条） |
| 31. 位置属性与策略正交 | 待定项记录，非测试（还不存在的东西没有测试守着它） |
| 32. 别把购物车当书 | `test_text_formats.py::TestFb2::test_a_named_body_is_not_read_as_the_main_text` + `test_a_second_body_is_reported_as_a_note` |
| 33. 绕过第三方时留下路标 | `test_cb7.py::TestListing::test_a_listing_that_cannot_be_parsed_falls_back_to_the_tool` + `test_the_fallback_really_reports_the_original_problem` |
| 34. 活动状态就地改写 | `test_theme_switch.py::TestPaletteSwitching::test_switching_rewrites_the_same_dict_rather_than_rebinding_it`（`id(theme.PALETTE)` 前后不变）+ `::test_an_alias_held_by_another_module_sees_the_switch` |
| 35. 跟着状态变的样式表必须是函数 | `test_theme_switch.py::TestStylesheetsFollowTheTheme::test_the_three_converted_constants_differ_between_themes` + `::test_no_stylesheet_invents_a_colour_of_its_own` + `::TestWindowFollowsTheTheme::test_the_tooltip_block_is_replaced_rather_than_stacked` |
| 36. `getattr` 默认值吞掉拼写错误 | `test_theme_switch.py::TestWindowFollowsTheTheme::test_the_reader_menu_is_reskinned`（回归测试，存在的唯一理由就是那个 `_reader_menu` 拼错） |

---

## 附二：`skipped=8` 的来历

第二批（FB2/DOCX/HTML/CB7）没有动这个数字，那 8 条还是原来那 8 条。

全量 discover 的 `skipped=8` 由两部分组成，**都不是"坏了"**：

| 条数 | 来源 | 为什么跳过 |
|---:|---|---|
| 6 | `tests/test_win32.py::TestOnARealDesktop` | `needs a real Windows window handle`——离屏平台拿不到真窗口句柄。**阶段 1 起就是这 6 条**，与任何后续阶段无关。 |
| 2 | `tests/test_comic_ui.py` 的两条「未实现模式」守卫 | 阶段 5 把三种阅读模式全部实现后，`pending` 列表变空，两条测试自动 `skipTest`。 |

那两条守卫是**哨兵，不要删**：

* `TestComicReaderAlone::test_asking_for_an_unimplemented_mode_says_so`
* `TestComicDefaultModeAtStartup::test_a_pending_mode_still_announces_itself_when_asked_directly`

它们守的规则是「**未实现的模式不许被切过去，但必须给用户一个解释**」。阶段 5 之后
`READING_MODES == IMPLEMENTED_MODES`，没有未实现的模式，所以它们无事可做——但**规则
本身仍然有效**。将来任何阶段往 `READING_MODES` 加一个新模式、却不实现它时，`pending`
重新非空，这两条会**自动活过来**（`if not pending: skipTest(...)`），不需要有人记得
回来改。这正是当初把它们写成"动态取 `pending`"而不是硬编码 `"spread"` 的原因。

**改回断言的时机**：如果你给 `READING_MODES` 加了新模式但暂不实现，不要去改这两条
测试——它们已经会自己生效。只有当你想**永久**放弃某个模式、把它从 `READING_MODES`
里删掉时，才需要动它们（那时它们应该一起被删，因为"未实现模式"这个概念不存在了）。
