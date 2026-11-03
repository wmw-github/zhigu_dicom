# -*- coding: utf-8 -*-
"""报告质控 SQLite 数据层（7 张表）。

设计原则
--------
1. **独立库文件**：``reportqc/config/report_qc.db``，与 CT3D 主程序既有数据层
   完全隔离，互不影响（规划 §R7）。
2. **线程安全**：OCR 在后台 QThread 跑，落库也在同一线程。sqlite3 的连接默认
   ``check_same_thread=True``，跨线程复用会抛异常。这里用 ``threading.local()``
   **每线程一个连接**，主线程查询、后台线程写入互不干扰。
3. **WAL 模式**：读写不互斥，历史查询不会阻塞正在进行的检测落库。
4. **空值保护**：所有字段可空。PACS 报告经常缺影像号/性别，缺什么存什么，
   绝不因为字段缺失让整条记录写不进去。
5. **原子写入**：一次检测 = 1 条 record + N 条 issue（+ 可选截图记录），
   包在一个事务里，避免「记录了但没有问题明细」的半截数据。

表结构（7 张）
--------------
=============  ==========================================================
qc_records     一次检测的主记录（患者信息 + 整体结论 + 耗时）
qc_issues      问题明细，带 user_verdict（误判标记）
learning_records 误判学习流水
custom_typos   用户自定义错别字（热更新，优先于 JSON 规则）
qc_settings    键值设置
qc_images      截图存档（只存路径，不存 BLOB，避免库文件膨胀）
qc_stats_daily 每日统计缓存
=============  ==========================================================
"""

import os
import re
import sqlite3
import threading
import time

__all__ = ["QCDatabase", "get_database"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS qc_records (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      TEXT NOT NULL,
    patient_name    TEXT,
    gender          TEXT,
    age             TEXT,
    exam_no         TEXT,
    accession_no    TEXT,
    modality        TEXT,
    body_part       TEXT,
    exam_time       TEXT,
    overall_level   TEXT,
    issue_count     INTEGER DEFAULT 0,
    critical_count  INTEGER DEFAULT 0,
    high_count      INTEGER DEFAULT 0,
    text_length     INTEGER DEFAULT 0,
    elapsed_ms      INTEGER DEFAULT 0,
    rect_x          INTEGER,
    rect_y          INTEGER,
    rect_w          INTEGER,
    rect_h          INTEGER,
    full_text       TEXT,
    note            TEXT
);

CREATE TABLE IF NOT EXISTS qc_issues (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id       INTEGER NOT NULL,
    level           TEXT,
    kind            TEXT,
    message         TEXT,
    suggestion      TEXT,
    source          TEXT,
    confidence      REAL,
    ocr_conf        REAL,
    ocr_suspect     INTEGER DEFAULT 0,
    user_verdict    TEXT,
    verdict_time    TEXT,
    verdict_note    TEXT,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS learning_records (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_id        INTEGER,
    kind            TEXT,
    word            TEXT,
    verdict         TEXT,
    note            TEXT,
    marked_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS custom_typos (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    typo            TEXT UNIQUE NOT NULL,
    correction      TEXT,
    severity        TEXT DEFAULT 'medium',
    enabled         INTEGER DEFAULT 1,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS qc_settings (
    key             TEXT PRIMARY KEY,
    value           TEXT,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS qc_images (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id       INTEGER,
    file_path       TEXT,
    width           INTEGER,
    height          INTEGER,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS qc_stats_daily (
    day             TEXT PRIMARY KEY,
    records         INTEGER DEFAULT 0,
    issues          INTEGER DEFAULT 0,
    criticals       INTEGER DEFAULT 0,
    updated_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_issues_record   ON qc_issues(record_id);
CREATE INDEX IF NOT EXISTS idx_issues_verdict  ON qc_issues(user_verdict);
CREATE INDEX IF NOT EXISTS idx_records_created ON qc_records(created_at);
CREATE INDEX IF NOT EXISTS idx_records_patient ON qc_records(patient_name);
"""


def _now_iso():
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


def extract_issue_word(kind, message):
    """从一条问题里抠出「该学哪个词」。

    误判学习要能复用，就必须落到**词**上，而不是整条 message ——
    message 带上下文（「「纵膈」应为「纵隔」」），不同报告的措辞不会完全一样，
    拿整条去比对永远命中不了，等于没学。

    格式与 ``pacs/pipeline.py`` 生成 message 的口径对应：

    * 错别字（有正确写法）：「「纵膈」应为「纵隔」」→ ``纵膈``
    * 错别字（无正确写法）：「「ECT」疑似错别字」→ ``ECT``
    * 噪音告警：``检测到可能的无意义输入: 'asdf'`` → ``asdf``
    * 其他类型（逻辑/语义/上下文）：没有可复用关键词，返回空串

    Returns
    -------
    str
        提取到的词；提取不到返回 ``""``（该条不参与词级学习）。
    """
    if not message:
        return ""
    msg = str(message)
    # 1) 中文引号包起来的词：「纵膈」应为「纵隔」/「ECT」疑似错别字
    m = re.search(r"「\s*([^」]+?)\s*」", msg)
    if m:
        return m.group(1).strip()
    # 2) 单引号包起来的字母串：检测到…: 'asdf'
    m = re.search(r"'([^']{2,})'", msg)
    if m:
        return m.group(1).strip()
    # 3) 双引号兜底
    m = re.search(r'"([^"]{2,})"', msg)
    if m:
        return m.group(1).strip()
    return ""


def _today():
    return time.strftime("%Y-%m-%d", time.localtime())


class QCDatabase(object):
    """报告质控数据库访问层（进程内单例，见 :func:`get_database`）。"""

    def __init__(self, db_path=None):
        self._local = threading.local()
        self._init_lock = threading.Lock()
        self._initialized = False
        self.db_path = db_path or self._default_path()
        self._ensure_schema()

    # ------------------------------------------------------------------
    # 路径与连接
    # ------------------------------------------------------------------

    @staticmethod
    def _default_path():
        """默认库路径：软件目录下 ``reportqc/config/report_qc.db``。

        安装目录只读时（如装在 Program Files 且未提权）回退到
        ``%APPDATA%/CT3D/report_qc.db``，保证功能可用。
        """
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        primary = os.path.join(root, "reportqc", "config", "report_qc.db")
        try:
            os.makedirs(os.path.dirname(primary), exist_ok=True)
            # 真正探测可写：建一个临时文件
            probe = primary + ".probe"
            with open(probe, "w") as fh:
                fh.write("ok")
            os.remove(probe)
            return primary
        except Exception:                              # noqa: BLE001
            appdata = os.environ.get("APPDATA") or os.path.expanduser("~")
            fallback = os.path.join(appdata, "CT3D", "report_qc.db")
            try:
                os.makedirs(os.path.dirname(fallback), exist_ok=True)
            except Exception:                          # noqa: BLE001
                pass
            return fallback

    def _conn(self):
        """当前线程的 sqlite3 连接（懒创建）。"""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.db_path, timeout=10.0)
            conn.row_factory = sqlite3.Row
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                conn.execute("PRAGMA foreign_keys=ON")
            except Exception:                          # noqa: BLE001
                pass
            self._local.conn = conn
        return conn

    def _ensure_schema(self):
        with self._init_lock:
            if self._initialized:
                return
            conn = self._conn()
            with conn:
                conn.executescript(_SCHEMA)
            self._initialized = True

    def close(self):
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:                          # noqa: BLE001
                pass
            self._local.conn = None

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------

    def save_result(self, result, rect=None, image_path=None):
        """把一次 :class:`~reportqc.pacs.pipeline.QCResult` 落库。

        一个事务内写 record + issues（+ 可选 image），并刷新日统计。
        返回 ``record_id``；失败返回 ``None``（不向上抛，质控结果照常显示）。
        """
        conn = self._conn()
        fields = result.fields
        f = fields if fields is not None else None

        def g(name):
            return getattr(f, name, "") if f is not None else ""

        levels = {}
        for it in result.issues:
            levels[it.level] = levels.get(it.level, 0) + 1

        rect_x = rect_y = rect_w = rect_h = None
        if rect is not None:
            try:
                rect_x, rect_y = int(rect.left()), int(rect.top())
                rect_w, rect_h = int(rect.width()), int(rect.height())
            except Exception:                          # noqa: BLE001
                rect_x = rect_y = rect_w = rect_h = None

        try:
            with conn:                                  # 事务
                cur = conn.execute(
                    """INSERT INTO qc_records
                       (created_at, patient_name, gender, age, exam_no,
                        accession_no, modality, body_part, exam_time,
                        overall_level, issue_count, critical_count, high_count,
                        text_length, elapsed_ms,
                        rect_x, rect_y, rect_w, rect_h, full_text, note)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (_now_iso(), g("patient_name"), g("gender"), g("age"),
                     g("exam_no"), g("accession_no"), g("modality"),
                     g("body_part"), g("exam_time"),
                     result.level, len(result.issues),
                     levels.get("critical", 0), levels.get("high", 0),
                     len(result.text or ""), int(result.elapsed * 1000),
                     rect_x, rect_y, rect_w, rect_h,
                     result.text or "", result.note or ""),
                )
                record_id = int(cur.lastrowid)

                for it in result.issues:
                    conn.execute(
                        """INSERT INTO qc_issues
                           (record_id, level, kind, message, suggestion, source,
                            confidence, ocr_conf, ocr_suspect, user_verdict,
                            verdict_time, verdict_note, created_at)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (record_id, it.level, it.kind, it.message,
                         it.suggestion or "", it.source or "",
                         float(it.confidence or 0.0),
                         None if it.ocr_conf is None else float(it.ocr_conf),
                         1 if it.ocr_suspect else 0,
                         None, None, None, _now_iso()),
                    )

                if image_path:
                    conn.execute(
                        """INSERT INTO qc_images
                           (record_id, file_path, width, height, created_at)
                           VALUES (?,?,?,?,?)""",
                        (record_id, str(image_path), None, None, _now_iso()),
                    )

                self._bump_daily(conn, n_issues=len(result.issues),
                                 n_criticals=levels.get("critical", 0))
            return record_id
        except Exception as exc:                        # noqa: BLE001
            print("[reportqc] 结果落库失败: %s" % exc)
            return None

    def _bump_daily(self, conn, n_issues, n_criticals):
        day = _today()
        row = conn.execute(
            "SELECT records, issues, criticals FROM qc_stats_daily WHERE day=?",
            (day,)).fetchone()
        if row is None:
            conn.execute(
                """INSERT INTO qc_stats_daily (day, records, issues, criticals, updated_at)
                   VALUES (?,?,?,?,?)""",
                (day, 1, n_issues, n_criticals, _now_iso()))
        else:
            conn.execute(
                """UPDATE qc_stats_daily
                   SET records=records+1, issues=issues+?, criticals=criticals+?,
                       updated_at=?
                   WHERE day=?""",
                (n_issues, n_criticals, _now_iso(), day))

    def set_verdict(self, issue_id, verdict, note=""):
        """给一条问题打标：``false_positive``（误报）/ ``confirmed``（确实有问题）。

        **标记为误报时会把问题里的关键词学下来**（``learned_words``），
        下一次检测直接跳过这个词，实现「标记一次，以后不再报」。

        为什么在这里学词而不是在 UI 里学
        --------------------------------
        详情窗、历史窗、小窗都能触发打标，学习逻辑放在数据层只有一处，
        不会出现「从这个入口标记会学、从那个入口标记不学」。

        Returns
        -------
        bool
            是否成功。
        """
        if verdict not in ("false_positive", "confirmed", "none"):
            return False
        conn = self._conn()
        learned = ""
        try:
            with conn:
                row = conn.execute(
                    "SELECT kind, message FROM qc_issues WHERE id=?",
                    (issue_id,)).fetchone()
                if row is None:
                    return False
                now = _now_iso()
                learned = extract_issue_word(row["kind"], row["message"])
                conn.execute(
                    """UPDATE qc_issues
                       SET user_verdict=?, verdict_time=?, verdict_note=?
                       WHERE id=?""",
                    (verdict, now, note or "", issue_id))
                conn.execute(
                    """INSERT INTO learning_records
                       (issue_id, kind, word, verdict, note, marked_at)
                       VALUES (?,?,?,?,?,?)""",
                    # 能提取到词就存词（可复用），提不到才退回整条 message
                    (issue_id, row["kind"], learned or row["message"], verdict,
                     note or "", now))
        except Exception as exc:                        # noqa: BLE001
            print("[reportqc] 标记误判失败: %s" % exc)
            return False

        # 误报 → 学下这个词（事务已提交，避免嵌套事务）
        if verdict == "false_positive" and learned:
            self.add_learned_word(learned)
        return True

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def query_records(self, patient=None, level=None, days=None, limit=200):
        """按条件查检测记录（新的在前）。"""
        sql = "SELECT * FROM qc_records WHERE 1=1"
        args = []
        if patient:
            sql += " AND patient_name LIKE ?"
            args.append("%" + str(patient) + "%")
        if level:
            sql += " AND overall_level = ?"
            args.append(level)
        if days:
            sql += " AND created_at >= datetime('now', 'localtime', ?)"
            args.append("-%d days" % int(days))
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(int(limit))
        try:
            return [dict(r) for r in self._conn().execute(sql, args).fetchall()]
        except Exception as exc:                        # noqa: BLE001
            print("[reportqc] 查询记录失败: %s" % exc)
            return []

    def query_issues(self, record_id):
        """取某次检测的全部问题。"""
        try:
            return [dict(r) for r in self._conn().execute(
                "SELECT * FROM qc_issues WHERE record_id=? ORDER BY id",
                (record_id,)).fetchall()]
        except Exception as exc:                        # noqa: BLE001
            print("[reportqc] 查询问题失败: %s" % exc)
            return []

    def query_verdicts(self, kind=None, limit=500):
        """查误判标记流水（可给详情窗做「同类问题历史判定」参考）。"""
        sql = "SELECT * FROM learning_records WHERE 1=1"
        args = []
        if kind:
            sql += " AND kind=?"
            args.append(kind)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(int(limit))
        try:
            return [dict(r) for r in self._conn().execute(sql, args).fetchall()]
        except Exception as exc:                        # noqa: BLE001
            return []

    def query_stats(self, days=30):
        """最近 N 天每日统计（供历史窗画趋势）。"""
        try:
            return [dict(r) for r in self._conn().execute(
                """SELECT * FROM qc_stats_daily
                   WHERE day >= date('now', 'localtime', ?)
                   ORDER BY day""",
                ("-%d days" % int(days),)).fetchall()]
        except Exception:                               # noqa: BLE001
            return []

    # ------------------------------------------------------------------
    # 自定义错别字（热更新）
    # ------------------------------------------------------------------

    def list_custom_typos(self, only_enabled=True):
        sql = "SELECT * FROM custom_typos"
        if only_enabled:
            sql += " WHERE enabled=1"
        sql += " ORDER BY id"
        try:
            return [dict(r) for r in self._conn().execute(sql).fetchall()]
        except Exception:                               # noqa: BLE001
            return []

    def add_custom_typo(self, typo, correction, severity="medium"):
        if not typo or not correction:
            return False
        try:
            with self._conn():
                self._conn().execute(
                    # 用 INSERT OR REPLACE 而不是 UPSERT：语法自 SQLite 3.0
                    # 就有，换到老版本 DLL 也不会挂
                    """INSERT OR REPLACE INTO custom_typos
                       (id, typo, correction, severity, enabled, created_at)
                       VALUES (
                         (SELECT id FROM custom_typos WHERE typo=?),
                         ?,?,?,1,?)""",
                    (str(typo), str(typo), str(correction),
                     str(severity), _now_iso()))
            return True
        except Exception as exc:                        # noqa: BLE001
            print("[reportqc] 添加自定义错别字失败: %s" % exc)
            return False

    def remove_custom_typo(self, typo):
        try:
            with self._conn():
                self._conn().execute("DELETE FROM custom_typos WHERE typo=?",
                                     (str(typo),))
            return True
        except Exception:                               # noqa: BLE001
            return False

    # ------------------------------------------------------------------
    # 忽略词（用户标记「以后别再报这个词」）
    # ------------------------------------------------------------------

    def get_ignore_words(self):
        """读取忽略词表（存在 qc_settings 的 ``ignore_words`` 键里，JSON 数组）。"""
        import json as _json
        raw = self.get_setting("ignore_words", "[]") or "[]"
        try:
            data = _json.loads(raw)
            return [str(w) for w in data if str(w).strip()]
        except Exception:                              # noqa: BLE001
            return []

    def add_ignore_word(self, word):
        if not word:
            return False
        words = self.get_ignore_words()
        if str(word) in words:
            return True
        words.append(str(word))
        import json as _json
        return self.set_setting("ignore_words", _json.dumps(words, ensure_ascii=False))

    def remove_ignore_word(self, word):
        words = self.get_ignore_words()
        if str(word) not in words:
            return True
        words = [w for w in words if w != str(word)]
        import json as _json
        return self.set_setting("ignore_words", _json.dumps(words, ensure_ascii=False))

    # ------------------------------------------------------------------
    # 误判学习词表（在详情窗点「标记为误报」自动积累）
    # ------------------------------------------------------------------

    def get_learned_words(self):
        """读过误判学习学到的词（``qc_settings`` 的 ``learned_words``）。

        和 ``ignore_words`` 分开存：前者是系统学到的，后者是用户主动忽略的，
        用户在设置窗里要能分辨，也要能只撤销其中一类。
        """
        import json as _json
        raw = self.get_setting("learned_words", "[]") or "[]"
        try:
            data = _json.loads(raw)
            return [str(w) for w in data if str(w).strip()]
        except Exception:                              # noqa: BLE001
            return []

    def add_learned_word(self, word):
        """学一个词（点「标记为误报」时自动调用）。"""
        word = str(word or "").strip()
        if not word:
            return False
        words = self.get_learned_words()
        if word in words:
            return True
        words.append(word)
        import json as _json
        return self.set_setting("learned_words",
                                _json.dumps(words, ensure_ascii=False))

    def remove_learned_word(self, word):
        """撤销学习（用户发现这个词其实该报）。"""
        words = self.get_learned_words()
        if str(word) not in words:
            return True
        words = [w for w in words if w != str(word)]
        import json as _json
        return self.set_setting("learned_words",
                                _json.dumps(words, ensure_ascii=False))

    # ------------------------------------------------------------------
    # 医学术语白名单（英文缩写 / 术语，用户可自行补充）
    # ------------------------------------------------------------------

    def get_medical_terms(self):
        """读用户补充的医学术语白名单（``qc_settings`` 的 ``medical_terms``）。

        用途：报告里的英文缩写（ECT / PICC / TACE / VATS ...）不该被当成
        「无意义字母」报出来。内置表已经覆盖常见项，这里放用户自己补的。
        """
        import json as _json
        raw = self.get_setting("medical_terms", "[]") or "[]"
        try:
            data = _json.loads(raw)
            return [str(w).strip() for w in data if str(w).strip()]
        except Exception:                              # noqa: BLE001
            return []

    def add_medical_term(self, term):
        term = str(term or "").strip()
        if not term:
            return False
        terms = self.get_medical_terms()
        if term.upper() in [t.upper() for t in terms]:
            return True
        terms.append(term)
        import json as _json
        return self.set_setting("medical_terms",
                                _json.dumps(terms, ensure_ascii=False))

    def remove_medical_term(self, term):
        terms = self.get_medical_terms()
        if str(term).upper() not in [t.upper() for t in terms]:
            return True
        terms = [t for t in terms if t.upper() != str(term).upper()]
        import json as _json
        return self.set_setting("medical_terms",
                                _json.dumps(terms, ensure_ascii=False))

    # ------------------------------------------------------------------
    # 设置
    # ------------------------------------------------------------------

    def get_setting(self, key, default=None):
        try:
            row = self._conn().execute(
                "SELECT value FROM qc_settings WHERE key=?", (key,)).fetchone()
            return row["value"] if row is not None else default
        except Exception:                               # noqa: BLE001
            return default

    def set_setting(self, key, value):
        try:
            with self._conn():
                self._conn().execute(
                    """INSERT OR REPLACE INTO qc_settings (key, value, updated_at)
                       VALUES (?,?,?)""",
                    (str(key), "" if value is None else str(value), _now_iso()))
            return True
        except Exception:                               # noqa: BLE001
            return False


# ---------------------------------------------------------------------------
# 单例
# ---------------------------------------------------------------------------

_DB = None
_DB_LOCK = threading.Lock()


def get_database():
    """数据库进程内单例。"""
    global _DB
    with _DB_LOCK:
        if _DB is None:
            _DB = QCDatabase()
        return _DB
