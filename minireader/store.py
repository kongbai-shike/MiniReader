"""书签存储：程序自己攒的、会长大的数据放 SQLite。

「你要手改的 → JSON；程序自己攒的、会长大的 → SQLite。」设置继续留在
settings.json：它小、用户可能拿记事本打开、手改一个字段不该整份文件陪葬。
书签相反——它是程序自己写出来的、随书库一起长大，而且一次只写一行，
坏了也只坏一行，这正好是事务能给而「整份 JSON 重写」给不了的。

`offset` 是**章内的字符下标**，不是滚动条的像素值：换字号、改行高、拖窗口
都会让同一个像素位置指到别的字上去，而字符下标永远指着同一个字。

**不要往这里加目录/章节标题缓存。** 现在拆一本 19 MB 的书只要 0.15 秒，
而缓存的失效条件有一堆（mtime、编码、用户改过的拆分规则），为这点时间
背上这些 bug 不划算。

和 `minireader.core.*` 一样，这里不导入任何 Qt 绑定：书签是数据不是控件，
能脱离界面单独跑，才有一整套不用 QApplication 的测试。

    py -3.12 -m unittest tests.test_store -v
"""

from __future__ import annotations

import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from .storage import data_dir

# 默认库名。设置是 settings.json，书签另起一个文件：让用户能一眼看出
# 「哪个文件删了会丢什么」。
DB_NAME = "reading.db"

_MEMORY = ":memory:"

# 书签列表每行只有一个行高：标签或摘要里混进换行就会把那一行撑破，所以长度
# 必须有上限，而且要在写库之前就截——超长文本一旦落库，每次读还要再截一次。
_MAX_LABEL = 120
_MAX_EXCERPT = 200

# book_id 是分组键，这里一旦截断就等于把两本书的书签合成一本，所以上限给得
# 远大于任何真实 id（真实 id 是 "mr-" + 16 位十六进制）。
_MAX_BOOK_ID = 512

# 表名、列名固定。这个库只有程序自己写，Schema 只可能往前加列，所以没有迁移
# 代码：`CREATE TABLE IF NOT EXISTS` 就是全部，永远不 DROP、不 ALTER。
# "offset" 加引号是因为它是 SQL 关键字，省得哪天解析器收紧语法就把 SELECT 弄坏。
_SCHEMA_TABLE = """
CREATE TABLE IF NOT EXISTS bookmarks (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id  TEXT    NOT NULL,
    chapter  INTEGER NOT NULL,          -- 0 基章序号
    "offset" INTEGER NOT NULL,          -- 章内字符下标，不是像素
    label    TEXT    NOT NULL DEFAULT '',
    excerpt  TEXT    NOT NULL DEFAULT '',
    created  REAL    NOT NULL           -- time.time()
)
"""

# (book_id, chapter, offset) 就是 list_for 的排序键，索引跟着它建。
_SCHEMA_INDEX = (
    'CREATE INDEX IF NOT EXISTS idx_bookmarks_book_pos'
    ' ON bookmarks (book_id, chapter, "offset")'
)

_INSERT = (
    'INSERT INTO bookmarks (book_id, chapter, "offset", label, excerpt, created)'
    " VALUES (?, ?, ?, ?, ?, ?)"
)
_SELECT_FOR_BOOK = 'SELECT * FROM bookmarks WHERE book_id = ? ORDER BY chapter, "offset", id'
_DELETE_ONE = "DELETE FROM bookmarks WHERE id = ?"
_DELETE_BOOK = "DELETE FROM bookmarks WHERE book_id = ?"
_COUNT_ALL = "SELECT COUNT(*) FROM bookmarks"
_COUNT_BOOK = "SELECT COUNT(*) FROM bookmarks WHERE book_id = ?"


# ---------------------------------------------------------------------------
# 取值与清洗
# ---------------------------------------------------------------------------

def _as_int(value: object, fallback: int) -> int:
    """能接住任何垃圾的 int 转换（写法跟 storage._as_int 一致）。

    章号、位置有两个来源：界面算出来的，和被人手改过的库文件。前者偶尔是
    字符串（从 QLineEdit 或配置里来），后者可能是任何东西，而 `int("三")`
    一抛就会让整个书签列表打不开。
    """
    if isinstance(value, bool):
        # bool 是 int 的子类，但布尔出现在章号里是数据坏了，不是 0/1。
        return fallback
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value == value and abs(value) != float("inf") else fallback
    if isinstance(value, str):
        try:
            return int(value.strip(), 10)
        except ValueError:
            return fallback
    return fallback


def _as_float(value: object) -> float:
    """时间戳。垃圾一律当 0.0：列表上显示成 1970 年也比打不开好。"""
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        number = float(value)
        return number if number == number and abs(number) != float("inf") else 0.0
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return 0.0
    return 0.0


def _clean_text(value: object, limit: int) -> str:
    """把任意输入压成一行、可打印、长度有上限的文本。

    换行必须消失：书签行只有一个行高，标签里一个 \\n 就把整行撑破（这正是
    settings 里 disguise_title 踩过的同一个坑）。控制字符也要消失：\\x00
    传给 sqlite3 会直接抛 ValueError，其余的在标签里画成一堆方框。空白统一
    折成一个半角空格，顺带掐掉首尾。
    """
    if not isinstance(value, str):
        # None、数字、列表都当"没写"。str(None) == "None" 会真的存进库里，
        # 然后用户就看到一条叫 None 的书签。
        return ""
    folded = "".join(ch if ch.isprintable() else (" " if ch.isspace() else "") for ch in value)
    return " ".join(folded.split())[:limit]


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------

@dataclass
class Bookmark:
    """一条书签。字段顺序就是 `to_dict()` 给界面的顺序。"""

    id: int
    book_id: str
    chapter: int          # 0 基章序号
    offset: int           # 章内字符下标（不是像素：字号一改像素就指错了）
    label: str
    excerpt: str
    created: float        # time.time()

    def to_dict(self) -> dict:
        """转成 `toc_panel.set_bookmarks()` 认的字典：键恰好是下面这七个。

        `created` 保持 float，不在这里格式化成日期：列表要的是日期，别处可能
        要排序或算"多久以前"，一转成字符串这些信息就没了，而格式化是界面的事。
        """
        return {
            "id": self.id,
            "book_id": self.book_id,
            "chapter": self.chapter,
            "offset": self.offset,
            "label": self.label,
            "excerpt": self.excerpt,
            "created": self.created,
        }


def _row_to_bookmark(row: sqlite3.Row) -> Bookmark:
    """把一行变成 Bookmark，取值时全部再夹一次。

    库文件放在用户能碰到的地方，而 SQLite 的列类型是"亲和"不是"约束"：
    往 INTEGER 列写 '第三章' 照样写得进去。所以读的时候必须假设每个值都可能是
    垃圾，否则一条手改的行就能让书签面板整个空掉。
    """
    return Bookmark(
        id=_as_int(row["id"], 0),
        book_id=_clean_text(row["book_id"], _MAX_BOOK_ID),
        chapter=max(0, _as_int(row["chapter"], 0)),
        offset=max(0, _as_int(row["offset"], 0)),
        label=_clean_text(row["label"], _MAX_LABEL),
        excerpt=_clean_text(row["excerpt"], _MAX_EXCERPT),
        created=_as_float(row["created"]),
    )


# ---------------------------------------------------------------------------
# 存储
# ---------------------------------------------------------------------------

class BookmarkStore:
    """书签库。打开失败只会降级，不会抛异常。

    「降级」是这里最重要的一条：书签是锦上添花的功能，而它自己可能因为目录
    只读、路径被别的文件占了、库文件损坏而完全不可用。这些都不该让阅读器
    打不开——退回内存库，把原因写进 `note`，让界面去告诉用户。
    """

    def __init__(self, db_path: str | os.PathLike[str] | None = None) -> None:
        self._conn: sqlite3.Connection | None = None
        self._persistent = False
        self._note = ""
        # 先把路径留空：传进来的东西可能连 os.fspath() 都过不了（数字、列表），
        # 而 _fall_back_to_memory() 要读这个字段拼给用户看的说明，不能让它在
        # 赋值之前就抛。
        self._db_path = ""
        try:
            if db_path is None or db_path == "":
                # None 和 "" 是同一个意思：用默认位置。空串如果照字面当路径，
                # Path("") 是 "."——一个目录，于是每次打开都会"降级"、安静地丢
                # 书签，不如直接当默认值。
                self._db_path = str(data_dir() / DB_NAME)
            else:
                self._db_path = os.fspath(db_path)
            self._conn = self._connect(self._db_path)
            self._persistent = True
        except (sqlite3.Error, OSError, ValueError, TypeError) as exc:
            self._fall_back_to_memory(exc)

    # ---- 状态 ------------------------------------------------------------

    @property
    def persistent(self) -> bool:
        """True = 用的就是调用方指定的那个库（`:memory:` 也算，它只是不落盘）。

        False = 打开失败、退回了内存库，原因在 `note` 里。注意这个标志说的是
        "数据库是不是当初要的那个"，所以 `:memory:` 是 True——它是个能用的库，
        只是不会活过这次进程。
        """
        return self._persistent

    @property
    def note(self) -> str:
        """降级原因，中文，可以直接给用户看；没降级就是空串。"""
        return self._note

    # ---- 打开 / 关闭 ------------------------------------------------------

    @staticmethod
    def _connect(target: str) -> sqlite3.Connection:
        """建目录、建表，返回连接。失败就抛，由调用方决定怎么降级。"""
        if target == _MEMORY:
            conn = sqlite3.connect(_MEMORY)
        else:
            path = Path(target)
            # 父目录不在就建。父路径是个文件、或者目标本身就是目录时，这里或
            # 下面的 connect() 会抛 OSError / sqlite3.OperationalError。
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.is_dir():
                raise sqlite3.OperationalError(f"这是个目录，不能当数据库文件：{target}")
            conn = sqlite3.connect(target)
        try:
            # 按列名取值比按下标稳：将来加列不会让字段整体错位。
            conn.row_factory = sqlite3.Row
            with conn:
                conn.execute(_SCHEMA_TABLE)
                conn.execute(_SCHEMA_INDEX)
        except BaseException:
            # 连上了不等于能用：损坏的文件要到第一条语句才报错，所以收尾必须
            # 自己做，否则降级之后还留着一个坏连接占着文件句柄。
            try:
                conn.close()
            except Exception:
                pass
            raise
        return conn

    def _close_connection(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass

    def _fall_back_to_memory(self, exc: BaseException) -> None:
        """把用不了的库换成内存库，并把原因记下来。

        代价是磁盘上原有的书签这次运行看不到了；换来的是启动路径上永远不会
        因为书签而失败。这个取舍是故意的。
        """
        self._close_connection()
        self._persistent = False
        target = self._db_path or "书签库（默认位置）"
        self._note = (
            f"书签库用不了（{target}）：{exc}。"
            "本次运行的书签只放在内存里，退出程序后会全部丢失（阅读本身不受影响）。"
        )
        try:
            self._conn = self._connect(_MEMORY)
        except (sqlite3.Error, OSError, ValueError):
            # 内存库都开不了几乎不可能；真发生了也只是没有书签，不是崩溃。
            self._conn = None

    def close(self) -> None:
        """关掉连接，幂等。

        关掉之后 add/remove/clear_book 变成空操作（各自返回 0/False/0），
        list_for/count 返回空。窗口销毁和库关闭没有固定先后，与其在退出时抛
        ProgrammingError，不如安静地什么都不做。
        """
        self._close_connection()

    # ---- 写 --------------------------------------------------------------

    def add(
        self,
        book_id: str,
        chapter: int,
        offset: int = 0,
        label: str = "",
        excerpt: str = "",
    ) -> int:
        """加一条书签，返回新行的 id；写不进去返回 0（0 不是合法 id，界面一看就知道）。

        时间戳在这里取，不让调用方传：书签的"时间"只有"加的时候"一个含义。
        """
        params = (
            _clean_text(book_id, _MAX_BOOK_ID),
            max(0, _as_int(chapter, 0)),
            max(0, _as_int(offset, 0)),
            _clean_text(label, _MAX_LABEL),
            _clean_text(excerpt, _MAX_EXCERPT),
            float(time.time()),
        )

        def write(conn: sqlite3.Connection) -> int:
            with conn:
                cursor = conn.execute(_INSERT, params)
                return int(cursor.lastrowid or 0)

        return int(self._write(write) or 0)

    def remove(self, bookmark_id: int) -> bool:
        """删掉一条书签，返回"真的删掉了没有"。

        界面靠这个返回值区分「已删除」和「这个书签已经不在了」——用户点了两次
        删除时，后一次不该显示成功。
        """
        target = _as_int(bookmark_id, 0)

        def write(conn: sqlite3.Connection) -> bool:
            with conn:
                cursor = conn.execute(_DELETE_ONE, (target,))
                return int(cursor.rowcount or 0) > 0

        return bool(self._write(write))

    def clear_book(self, book_id: str) -> int:
        """清掉一本书的全部书签，返回删掉的条数（界面要说「已清空 N 条」）。"""
        target = _clean_text(book_id, _MAX_BOOK_ID)

        def write(conn: sqlite3.Connection) -> int:
            with conn:
                cursor = conn.execute(_DELETE_BOOK, (target,))
                return int(cursor.rowcount or 0)

        return int(self._write(write) or 0)

    def _write(self, action):
        """跑一次写操作；磁盘库写失败就退回内存库再试一次，返回 action 的结果。

        之所以要重试，是因为"能建表"和"能写"是两件事：只读目录、磁盘写满、
        文件被别的程序占住，全都只在第一条 INSERT 上才现形，那时表早就建好了。
        """
        for _ in range(2):
            if self._conn is None:
                return None
            try:
                return action(self._conn)
            except sqlite3.Error as exc:
                if not self._persistent:
                    # 已经在内存库上还写不进去，这一条只能放弃，但绝不往上抛。
                    return None
                self._fall_back_to_memory(exc)
        return None

    # ---- 读 --------------------------------------------------------------

    def list_for(self, book_id: str) -> list[Bookmark]:
        """一本书的全部书签，按 (章号, 章内位置, id) 从旧到新。

        排序键里带上 id，是为了让同一位置的两条书签有确定的先后：列表每次打开
        都重排的话，用户会以为书签自己在动。
        """
        if self._conn is None:
            return []
        target = _clean_text(book_id, _MAX_BOOK_ID)
        try:
            rows = self._conn.execute(_SELECT_FOR_BOOK, (target,)).fetchall()
        except sqlite3.Error:
            # 读都读不了，就当没有书签——这里没有"再退一步"的地方了。
            return []
        return [_row_to_bookmark(row) for row in rows]

    def count(self, book_id: str | None = None) -> int:
        """书签数。`book_id=None` 时数所有书。"""
        if self._conn is None:
            return 0
        try:
            if book_id is None:
                row = self._conn.execute(_COUNT_ALL).fetchone()
            else:
                row = self._conn.execute(
                    _COUNT_BOOK, (_clean_text(book_id, _MAX_BOOK_ID),)
                ).fetchone()
        except sqlite3.Error:
            return 0
        return int(row[0]) if row else 0
