from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path


class DomainError(ValueError):
    """Business rule violation."""


WITNESS_KINDS = {"version", "fragment", "transcription"}
SPECIAL_TOKENS = {"[缺页]", "[不可辨]", "[残损]", "[插入]", "[删除]"}


def validate_transcription(text: str) -> str:
    text = text.strip()
    if not text:
        raise DomainError("文本不能为空")
    unclosed = text.count("[") - text.count("]")
    if unclosed:
        raise DomainError("校勘标记括号不匹配")
    return text


class CollationDB:
    """SQLite-backed textual collation service with optimistic revisions."""

    def __init__(self, path: str = "collation.db") -> None:
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        if path != ":memory:":
            self.conn.execute("PRAGMA journal_mode=WAL")
        self._schema()

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def transaction(self):
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            yield
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def _schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              name TEXT NOT NULL UNIQUE,
              role TEXT NOT NULL CHECK(role IN ('owner','editor','reviewer'))
            );
            CREATE TABLE IF NOT EXISTS works (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              title TEXT NOT NULL,
              description TEXT NOT NULL DEFAULT '',
              owner_id INTEGER NOT NULL REFERENCES users(id),
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS work_access (
              work_id INTEGER NOT NULL REFERENCES works(id) ON DELETE CASCADE,
              user_id INTEGER NOT NULL REFERENCES users(id),
              permission TEXT NOT NULL CHECK(permission IN ('view','review')),
              PRIMARY KEY(work_id,user_id)
            );
            CREATE TABLE IF NOT EXISTS witnesses (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              work_id INTEGER NOT NULL REFERENCES works(id) ON DELETE CASCADE,
              siglum TEXT NOT NULL,
              kind TEXT NOT NULL CHECK(kind IN ('version','fragment','transcription')),
              source_note TEXT NOT NULL DEFAULT '',
              missing_sections TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL,
              UNIQUE(work_id,siglum)
            );
            CREATE TABLE IF NOT EXISTS witness_editors (
              witness_id INTEGER NOT NULL REFERENCES witnesses(id) ON DELETE CASCADE,
              user_id INTEGER NOT NULL REFERENCES users(id),
              granted_by INTEGER NOT NULL REFERENCES users(id),
              PRIMARY KEY(witness_id,user_id)
            );
            CREATE TABLE IF NOT EXISTS passages (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              work_id INTEGER NOT NULL REFERENCES works(id) ON DELETE CASCADE,
              label TEXT NOT NULL,
              base_text TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','locked')),
              revision INTEGER NOT NULL DEFAULT 0,
              updated_by INTEGER NOT NULL REFERENCES users(id),
              updated_at TEXT NOT NULL,
              UNIQUE(work_id,label)
            );
            CREATE TABLE IF NOT EXISTS alignments (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              passage_id INTEGER NOT NULL REFERENCES passages(id) ON DELETE CASCADE,
              witness_id INTEGER NOT NULL REFERENCES witnesses(id) ON DELETE CASCADE,
              aligned_text TEXT NOT NULL,
              sort_order INTEGER NOT NULL,
              note TEXT NOT NULL DEFAULT '',
              created_by INTEGER NOT NULL REFERENCES users(id),
              created_at TEXT NOT NULL,
              UNIQUE(passage_id,witness_id)
            );
            CREATE TABLE IF NOT EXISTS variants (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              passage_id INTEGER NOT NULL REFERENCES passages(id) ON DELETE CASCADE,
              witness_id INTEGER NOT NULL REFERENCES witnesses(id),
              base_text TEXT NOT NULL,
              proposed_text TEXT NOT NULL,
              reason TEXT NOT NULL,
              layer INTEGER NOT NULL DEFAULT 1,
              created_by INTEGER NOT NULL REFERENCES users(id),
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS revisions (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              passage_id INTEGER NOT NULL REFERENCES passages(id) ON DELETE CASCADE,
              variant_id INTEGER REFERENCES variants(id) ON DELETE CASCADE,
              revision_no INTEGER NOT NULL,
              layer INTEGER NOT NULL,
              snapshot_json TEXT NOT NULL,
              author_id INTEGER NOT NULL REFERENCES users(id),
              created_at TEXT NOT NULL,
              UNIQUE(passage_id, revision_no)
            );
            CREATE TABLE IF NOT EXISTS notes (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              variant_id INTEGER NOT NULL REFERENCES variants(id) ON DELETE CASCADE,
              body TEXT NOT NULL,
              author_id INTEGER NOT NULL REFERENCES users(id),
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS passage_locks (
              passage_id INTEGER PRIMARY KEY REFERENCES passages(id) ON DELETE CASCADE,
              locked_by INTEGER NOT NULL REFERENCES users(id),
              reason TEXT NOT NULL DEFAULT '',
              locked_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS editions (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              work_id INTEGER NOT NULL REFERENCES works(id) ON DELETE CASCADE,
              version_no INTEGER NOT NULL,
              note TEXT NOT NULL DEFAULT '',
              alignment_count INTEGER NOT NULL,
              variant_count INTEGER NOT NULL,
              note_count INTEGER NOT NULL,
              gap_count INTEGER NOT NULL,
              revision_heads_json TEXT NOT NULL,
              snapshot_json TEXT NOT NULL,
              signature TEXT NOT NULL DEFAULT '',
              published_by INTEGER NOT NULL REFERENCES users(id),
              published_at TEXT NOT NULL,
              UNIQUE(work_id,version_no)
            );
            """
        )
        self.conn.commit()

    def seed_demo(self) -> None:
        if self.conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
            return
        owner = self.add_user("项目负责人", "owner")
        editor = self.add_user("校勘编辑", "editor")
        work = self.create_work("一则残卷", "演示不同版本的校勘", owner)
        w1 = self.add_witness(work, "甲本", "version", "馆藏胶片", "")
        w2 = self.add_witness(work, "乙本", "fragment", "残片转录", "第二句残损")
        self.grant_witness_editor(w2, editor, owner)
        passage = self.add_passage(work, "第1节", "春水东流，故人南去。", owner)
        self.align_passage(passage, w1, "春水东流，故人南去。", 1, owner)
        self.align_passage(passage, w2, "春水东流，[不可辨][不可辨]。", 2, owner)
        variant = self.create_variant(passage, w2, "春水东流，故人南去。", "综合语义与行款补足", owner, 0)
        self.add_note(variant, "补字仍需参照纸背墨迹。", editor)
        self.publish_edition(work, "评审会首轮交付定本", owner)

    def add_user(self, name: str, role: str) -> int:
        if not name.strip() or role not in {"owner", "editor", "reviewer"}:
            raise DomainError("用户名或角色无效")
        with self.transaction():
            try:
                cur = self.conn.execute("INSERT INTO users(name,role) VALUES(?,?)", (name.strip(), role))
            except sqlite3.IntegrityError as exc:
                raise DomainError("用户名已存在") from exc
        return int(cur.lastrowid)

    def create_work(self, title: str, description: str, owner_id: int) -> int:
        owner = self.conn.execute("SELECT role FROM users WHERE id=?", (owner_id,)).fetchone()
        if not owner or owner["role"] != "owner" or not title.strip():
            raise DomainError("作品标题或负责人无效")
        with self.transaction():
            cur = self.conn.execute(
                "INSERT INTO works(title,description,owner_id,created_at) VALUES(?,?,?,?)",
                (title.strip(), description.strip(), owner_id, datetime.now().isoformat()),
            )
        return int(cur.lastrowid)

    def grant_work_access(self, work_id: int, user_id: int, permission: str, granted_by: int) -> None:
        if permission not in {"view", "review"}:
            raise DomainError("权限必须为 view 或 review")
        self._require_owner(work_id, granted_by)
        if not self.conn.execute("SELECT 1 FROM users WHERE id=?", (user_id,)).fetchone():
            raise DomainError("用户不存在")
        with self.transaction():
            self.conn.execute(
                "INSERT INTO work_access(work_id,user_id,permission) VALUES(?,?,?) "
                "ON CONFLICT(work_id,user_id) DO UPDATE SET permission=excluded.permission",
                (work_id, user_id, permission),
            )

    def _require_owner(self, work_id: int, user_id: int) -> None:
        row = self.conn.execute("SELECT 1 FROM works WHERE id=? AND owner_id=?", (work_id, user_id)).fetchone()
        if not row:
            raise DomainError("只有项目负责人可以执行此操作")

    def can_view_work(self, work_id: int, user_id: int) -> bool:
        return bool(self.conn.execute(
            "SELECT 1 FROM works WHERE id=? AND owner_id=? "
            "UNION ALL SELECT 1 FROM work_access WHERE work_id=? AND user_id=? "
            "UNION ALL SELECT 1 FROM witnesses w JOIN witness_editors e ON e.witness_id=w.id "
            "WHERE w.work_id=? AND e.user_id=? LIMIT 1",
            (work_id, user_id, work_id, user_id, work_id, user_id),
        ).fetchone())

    def can_edit_witness(self, witness_id: int, user_id: int) -> bool:
        row = self.conn.execute(
            "SELECT w.work_id,wa.permission FROM witnesses w LEFT JOIN work_access wa ON wa.work_id=w.work_id AND wa.user_id=? WHERE w.id=?",
            (user_id, witness_id),
        ).fetchone()
        if not row:
            return False
        owner = self.conn.execute("SELECT 1 FROM works WHERE id=? AND owner_id=?", (row["work_id"], user_id)).fetchone()
        editor = self.conn.execute("SELECT 1 FROM witness_editors WHERE witness_id=? AND user_id=?", (witness_id, user_id)).fetchone()
        return bool(owner or editor)

    def add_witness(self, work_id: int, siglum: str, kind: str, source_note: str = "", missing_sections: str = "") -> int:
        if not self.conn.execute("SELECT 1 FROM works WHERE id=?", (work_id,)).fetchone():
            raise DomainError("作品不存在")
        if not siglum.strip() or kind not in WITNESS_KINDS:
            raise DomainError("版本标识或类型无效")
        with self.transaction():
            try:
                cur = self.conn.execute(
                    "INSERT INTO witnesses(work_id,siglum,kind,source_note,missing_sections,created_at) VALUES(?,?,?,?,?,?)",
                    (work_id, siglum.strip(), kind, source_note.strip(), missing_sections.strip(), datetime.now().isoformat()),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("同一作品中的版本标识不能重复") from exc
        return int(cur.lastrowid)

    def grant_witness_editor(self, witness_id: int, user_id: int, granted_by: int) -> None:
        witness = self.conn.execute("SELECT work_id FROM witnesses WHERE id=?", (witness_id,)).fetchone()
        if not witness:
            raise DomainError("版本不存在")
        self._require_owner(witness["work_id"], granted_by)
        if not self.conn.execute("SELECT 1 FROM users WHERE id=?", (user_id,)).fetchone():
            raise DomainError("用户不存在")
        with self.transaction():
            self.conn.execute(
                "INSERT OR IGNORE INTO witness_editors(witness_id,user_id,granted_by) VALUES(?,?,?)",
                (witness_id, user_id, granted_by),
            )

    def add_passage(self, work_id: int, label: str, base_text: str, user_id: int) -> int:
        self._require_owner(work_id, user_id)
        text = validate_transcription(base_text)
        if not label.strip():
            raise DomainError("段落标签不能为空")
        with self.transaction():
            try:
                cur = self.conn.execute(
                    "INSERT INTO passages(work_id,label,base_text,updated_by,updated_at) VALUES(?,?,?,?,?)",
                    (work_id, label.strip(), text, user_id, datetime.now().isoformat()),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("段落标签已存在") from exc
        return int(cur.lastrowid)

    def align_passage(self, passage_id: int, witness_id: int, aligned_text: str, sort_order: int, user_id: int) -> int:
        passage = self.conn.execute("SELECT * FROM passages WHERE id=?", (passage_id,)).fetchone()
        witness = self.conn.execute("SELECT * FROM witnesses WHERE id=?", (witness_id,)).fetchone()
        if not passage or not witness or passage["work_id"] != witness["work_id"]:
            raise DomainError("段落与版本不属于同一作品")
        if not self.can_edit_witness(witness_id, user_id):
            raise DomainError("无权编辑该版本")
        if sort_order <= 0:
            raise DomainError("排序号必须大于0")
        text = validate_transcription(aligned_text)
        with self.transaction():
            try:
                cur = self.conn.execute(
                    "INSERT INTO alignments(passage_id,witness_id,aligned_text,sort_order,created_by,created_at) VALUES(?,?,?,?,?,?)",
                    (passage_id, witness_id, text, sort_order, user_id, datetime.now().isoformat()),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("该版本已经对齐此段落") from exc
        return int(cur.lastrowid)

    def create_variant(self, passage_id: int, witness_id: int, proposed_text: str, reason: str,
                       user_id: int, expected_revision: int) -> int:
        with self.transaction():
            passage, lock = self._editable_passage(passage_id, witness_id, user_id, expected_revision)
            text = validate_transcription(proposed_text)
            if len(reason.strip()) < 3:
                raise DomainError("取舍理由至少3个字符")
            if not self.conn.execute("SELECT 1 FROM alignments WHERE passage_id=? AND witness_id=?", (passage_id, witness_id)).fetchone():
                raise DomainError("该版本尚未对齐此段落")
            cur = self.conn.execute(
                "INSERT INTO variants(passage_id,witness_id,base_text,proposed_text,reason,created_by,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (passage_id, witness_id, passage["base_text"], text, reason.strip(), user_id, datetime.now().isoformat(), datetime.now().isoformat()),
            )
            variant_id = int(cur.lastrowid)
            revision = self._record_revision(passage_id, variant_id, 1, user_id)
            self.conn.execute("UPDATE passages SET revision=?,updated_by=?,updated_at=? WHERE id=?", (revision, user_id, datetime.now().isoformat(), passage_id))
        return variant_id

    def update_variant(self, variant_id: int, proposed_text: str, reason: str, user_id: int,
                       expected_revision: int) -> int:
        with self.transaction():
            variant = self.conn.execute("SELECT * FROM variants WHERE id=?", (variant_id,)).fetchone()
            if not variant:
                raise DomainError("异文记录不存在")
            passage, _ = self._editable_passage(variant["passage_id"], variant["witness_id"], user_id, expected_revision)
            text = validate_transcription(proposed_text)
            if len(reason.strip()) < 3:
                raise DomainError("取舍理由至少3个字符")
            layer = int(self.conn.execute("SELECT COALESCE(MAX(layer),0)+1 FROM variants WHERE passage_id=? AND witness_id=?", (variant["passage_id"], variant["witness_id"])).fetchone()[0])
            self.conn.execute(
                "UPDATE variants SET proposed_text=?,reason=?,layer=?,updated_at=? WHERE id=?",
                (text, reason.strip(), layer, datetime.now().isoformat(), variant_id),
            )
            revision = self._record_revision(variant["passage_id"], variant_id, layer, user_id)
            self.conn.execute("UPDATE passages SET revision=?,updated_by=?,updated_at=? WHERE id=?", (revision, user_id, datetime.now().isoformat(), variant["passage_id"]))
        return revision

    def _editable_passage(self, passage_id: int, witness_id: int, user_id: int, expected_revision: int):
        passage = self.conn.execute("SELECT * FROM passages WHERE id=?", (passage_id,)).fetchone()
        witness = self.conn.execute("SELECT * FROM witnesses WHERE id=?", (witness_id,)).fetchone()
        if not passage or not witness or passage["work_id"] != witness["work_id"]:
            raise DomainError("段落与版本不属于同一作品")
        if passage["status"] == "locked" or self.conn.execute("SELECT 1 FROM passage_locks WHERE passage_id=?", (passage_id,)).fetchone():
            raise DomainError("段落已锁定，不能修改")
        if not self.can_edit_witness(witness_id, user_id):
            raise DomainError("无权编辑该版本")
        if passage["revision"] != expected_revision:
            raise DomainError(f"版本冲突：当前修订为 {passage['revision']}，提交基于 {expected_revision}")
        return passage, None

    def _record_revision(self, passage_id: int, variant_id: int, layer: int, user_id: int) -> int:
        revision = int(self.conn.execute("SELECT COALESCE(MAX(revision_no),0)+1 FROM revisions WHERE passage_id=?", (passage_id,)).fetchone()[0])
        snapshot = {
            "passage": dict(self.conn.execute("SELECT * FROM passages WHERE id=?", (passage_id,)).fetchone()),
            "variant": dict(self.conn.execute("SELECT * FROM variants WHERE id=?", (variant_id,)).fetchone()),
            "alignments": [dict(r) for r in self.conn.execute(
                "SELECT a.*,w.siglum,w.kind FROM alignments a JOIN witnesses w ON w.id=a.witness_id WHERE a.passage_id=? ORDER BY a.sort_order",
                (passage_id,),
            ).fetchall()],
        }
        self.conn.execute(
            "INSERT INTO revisions(passage_id,variant_id,revision_no,layer,snapshot_json,author_id,created_at) VALUES(?,?,?,?,?,?,?)",
            (passage_id, variant_id, revision, layer, json.dumps(snapshot, ensure_ascii=False), user_id, datetime.now().isoformat()),
        )
        return revision

    def add_note(self, variant_id: int, body: str, author_id: int) -> int:
        variant = self.conn.execute("SELECT * FROM variants WHERE id=?", (variant_id,)).fetchone()
        if not variant or not self.can_view_work(
            self.conn.execute("SELECT work_id FROM passages WHERE id=?", (variant["passage_id"],)).fetchone()["work_id"], author_id
        ):
            raise DomainError("异文不存在或无权评论")
        if not body.strip():
            raise DomainError("注释不能为空")
        with self.transaction():
            cur = self.conn.execute(
                "INSERT INTO notes(variant_id,body,author_id,created_at) VALUES(?,?,?,?)",
                (variant_id, body.strip(), author_id, datetime.now().isoformat()),
            )
        return int(cur.lastrowid)

    def lock_passage(self, passage_id: int, user_id: int, reason: str = "") -> None:
        passage = self.conn.execute("SELECT * FROM passages WHERE id=?", (passage_id,)).fetchone()
        if not passage:
            raise DomainError("段落不存在")
        self._require_owner(passage["work_id"], user_id)
        with self.transaction():
            self.conn.execute("UPDATE passages SET status='locked',updated_by=?,updated_at=? WHERE id=?", (user_id, datetime.now().isoformat(), passage_id))
            self.conn.execute(
                "INSERT OR REPLACE INTO passage_locks(passage_id,locked_by,reason,locked_at) VALUES(?,?,?,?)",
                (passage_id, user_id, reason.strip(), datetime.now().isoformat()),
            )

    def get_snapshot(self, passage_id: int, revision_no: int, user_id: int) -> dict:
        passage = self.conn.execute("SELECT work_id FROM passages WHERE id=?", (passage_id,)).fetchone()
        if not passage or not self.can_view_work(passage["work_id"], user_id):
            raise DomainError("无权查看该快照")
        row = self.conn.execute("SELECT * FROM revisions WHERE passage_id=? AND revision_no=?", (passage_id, revision_no)).fetchone()
        if not row:
            raise DomainError("快照不存在")
        return {"revision_no": row["revision_no"], "layer": row["layer"], "created_at": row["created_at"], "snapshot": json.loads(row["snapshot_json"])}

    def _build_collation(self, work_id: int) -> dict:
        """Assemble the current draft collation (alignments/variants/notes/gaps)."""
        work = self.conn.execute("SELECT * FROM works WHERE id=?", (work_id,)).fetchone()
        if not work:
            raise DomainError("作品不存在")
        witnesses = [dict(r) for r in self.conn.execute("SELECT * FROM witnesses WHERE work_id=? ORDER BY id", (work_id,))]
        passages = []
        alignment_count = variant_count = note_count = gaps = 0
        for passage in self.conn.execute("SELECT * FROM passages WHERE work_id=? ORDER BY id", (work_id,)).fetchall():
            alignments = []
            for row in self.conn.execute(
                "SELECT a.*,w.siglum,w.kind,w.missing_sections FROM alignments a JOIN witnesses w ON w.id=a.witness_id "
                "WHERE a.passage_id=? ORDER BY a.sort_order", (passage["id"],)
            ).fetchall():
                item = dict(row)
                if "[缺页]" in item["aligned_text"] or "[残损]" in item["aligned_text"]:
                    item["has_gap"] = True
                    gaps += 1
                alignments.append(item)
            variants = []
            for row in self.conn.execute("SELECT * FROM variants WHERE passage_id=? ORDER BY witness_id,layer,id", (passage["id"],)).fetchall():
                variant = dict(row)
                variant["notes"] = [dict(r) for r in self.conn.execute("SELECT * FROM notes WHERE variant_id=? ORDER BY id", (row["id"],))]
                note_count += len(variant["notes"])
                variants.append(variant)
            alignment_count += len(alignments)
            variant_count += len(variants)
            passages.append({**dict(passage), "alignments": alignments, "variants": variants})
        revision_heads = {
            str(row["id"]): int(row["revision"])
            for row in self.conn.execute("SELECT id,revision FROM passages WHERE work_id=? ORDER BY id", (work_id,))
        }
        return {
            "work": dict(work),
            "witnesses": witnesses,
            "passages": passages,
            "gap_count": gaps,
            "alignment_count": alignment_count,
            "variant_count": variant_count,
            "note_count": note_count,
            "revision_heads": revision_heads,
        }

    @staticmethod
    def _edition_signature(collation: dict) -> str:
        """Fingerprint of what an edition freezes; used to reject re-publishing an unchanged draft."""
        return json.dumps(
            {
                "heads": collation["revision_heads"],
                "alignment_count": collation["alignment_count"],
                "variant_count": collation["variant_count"],
                "note_count": collation["note_count"],
                "gap_count": collation["gap_count"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )

    def publish_edition(self, work_id: int, note: str, user_id: int) -> dict:
        """Freeze the current draft revisions of every passage into a new read-only edition."""
        self._require_owner(work_id, user_id)
        collation = self._build_collation(work_id)
        signature = self._edition_signature(collation)
        last = self.conn.execute(
            "SELECT version_no,signature FROM editions WHERE work_id=? ORDER BY version_no DESC LIMIT 1",
            (work_id,),
        ).fetchone()
        if last and last["signature"] == signature:
            raise DomainError("当前各段落没有新修订，不能重复发布定本")
        version_no = int(last["version_no"]) + 1 if last else 1
        published_at = datetime.now().isoformat()
        with self.transaction():
            cur = self.conn.execute(
                "INSERT INTO editions(work_id,version_no,note,alignment_count,variant_count,note_count,gap_count,"
                "revision_heads_json,snapshot_json,signature,published_by,published_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    work_id, version_no, note.strip(), collation["alignment_count"], collation["variant_count"],
                    collation["note_count"], collation["gap_count"],
                    json.dumps(collation["revision_heads"], ensure_ascii=False),
                    json.dumps(collation, ensure_ascii=False), signature, user_id, published_at,
                ),
            )
            edition_id = int(cur.lastrowid)
        return self._edition_meta(self.conn.execute("SELECT * FROM editions WHERE id=?", (edition_id,)).fetchone())

    def _edition_meta(self, row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "work_id": row["work_id"],
            "version_no": row["version_no"],
            "note": row["note"],
            "alignment_count": row["alignment_count"],
            "variant_count": row["variant_count"],
            "note_count": row["note_count"],
            "gap_count": row["gap_count"],
            "revision_heads": json.loads(row["revision_heads_json"]),
            "published_by": row["published_by"],
            "published_at": row["published_at"],
        }

    def list_editions(self, work_id: int, user_id: int) -> dict:
        """List immutable editions, draft revisions after the newest edition, and a merged timeline."""
        if not self.can_view_work(work_id, user_id):
            raise DomainError("无权查看该校勘项目")
        work = self.conn.execute("SELECT id,title FROM works WHERE id=?", (work_id,)).fetchone()
        if not work:
            raise DomainError("作品不存在")
        rows = self.conn.execute(
            "SELECT e.*,u.name AS publisher_name FROM editions e JOIN users u ON u.id=e.published_by "
            "WHERE e.work_id=? ORDER BY e.version_no", (work_id,),
        ).fetchall()
        editions = [{**self._edition_meta(row), "publisher_name": row["publisher_name"], "readonly": True} for row in rows]
        latest = rows[-1] if rows else None
        latest_heads = json.loads(latest["revision_heads_json"]) if latest else {}

        draft_revisions = []
        revision_rows = self.conn.execute(
            "SELECT r.id,r.passage_id,r.revision_no,r.layer,r.created_at,r.author_id,"
            "p.label AS passage_label,u.name AS author_name "
            "FROM revisions r JOIN passages p ON p.id=r.passage_id JOIN users u ON u.id=r.author_id "
            "WHERE p.work_id=? ORDER BY r.created_at,r.id",
            (work_id,),
        ).fetchall()
        for row in revision_rows:
            baseline = int(latest_heads.get(str(row["passage_id"]), 0))
            if row["revision_no"] <= baseline:
                continue
            draft_revisions.append({
                "revision_id": row["id"],
                "passage_id": row["passage_id"],
                "passage_label": row["passage_label"],
                "revision_no": row["revision_no"],
                "layer": row["layer"],
                "author_id": row["author_id"],
                "author_name": row["author_name"],
                "created_at": row["created_at"],
            })

        def revision_item(row: dict, frozen_in: int | None) -> dict:
            suffix = f"已并入定本 v{frozen_in}" if frozen_in else "草稿"
            return {"kind": "revision", "at": row["created_at"], "seq": row["revision_no"],
                    "label": f"修订 #{row['revision_no']}（{row['passage_label']} · 第{row['layer']}层）· {suffix}",
                    "revision_no": row["revision_no"], "passage_id": row["passage_id"],
                    "author_name": row["author_name"], "frozen_in": frozen_in,
                    "is_draft": frozen_in is None}

        # 逻辑排序：定本按版本号；每版之下是它新冻结的修订；最新版之后才是草稿修订。
        timeline = []
        all_revisions = [dict(r) for r in revision_rows]
        prev_heads: dict[str, int] = {}
        for e in editions:
            heads = {k: int(v) for k, v in e["revision_heads"].items()}
            for row in all_revisions:
                rev_no = row["revision_no"]
                if prev_heads.get(str(row["passage_id"]), 0) < rev_no <= heads.get(str(row["passage_id"]), 0):
                    timeline.append(revision_item(row, e["version_no"]))
            timeline.append({"kind": "edition", "at": e["published_at"], "seq": e["version_no"],
                             "label": f"定本 v{e['version_no']}", "version_no": e["version_no"],
                             "edition_id": e["id"], "note": e["note"], "gap_count": e["gap_count"]})
            prev_heads = heads
        for row in all_revisions:
            if row["revision_no"] > prev_heads.get(str(row["passage_id"]), 0):
                timeline.append(revision_item(row, None))

        if latest:
            has_draft_changes = latest["signature"] != self._edition_signature(self._build_collation(work_id))
        else:
            has_draft_changes = bool(revision_rows)
        return {
            "work": dict(work),
            "editions": editions,
            "draft_revisions": draft_revisions,
            "has_draft_changes": has_draft_changes,
            "timeline": timeline,
        }

    def get_edition(self, edition_id: int, user_id: int) -> dict:
        """Return one frozen, read-only edition snapshot."""
        row = self.conn.execute(
            "SELECT e.*,u.name AS publisher_name,w.title AS work_title FROM editions e "
            "JOIN users u ON u.id=e.published_by JOIN works w ON w.id=e.work_id WHERE e.id=?",
            (edition_id,),
        ).fetchone()
        if not row:
            raise DomainError("定本不存在")
        if not self.can_view_work(row["work_id"], user_id):
            raise DomainError("无权查看该定本")
        return {**self._edition_meta(row), "publisher_name": row["publisher_name"],
                "work_title": row["work_title"], "readonly": True,
                "collation": json.loads(row["snapshot_json"])}

    def export_collation(self, work_id: int, user_id: int) -> dict:
        if not self.can_view_work(work_id, user_id):
            raise DomainError("无权查看该校勘项目")
        return self._build_collation(work_id)

    def snapshot(self) -> dict:
        return {
            "users": [dict(r) for r in self.conn.execute("SELECT id,name,role FROM users ORDER BY id")],
            "works": [dict(r) for r in self.conn.execute("SELECT * FROM works ORDER BY id")],
            "witnesses": [dict(r) for r in self.conn.execute("SELECT * FROM witnesses ORDER BY id")],
            "passages": [dict(r) for r in self.conn.execute("SELECT * FROM passages ORDER BY id")],
            "editions": [dict(r) for r in self.conn.execute(
                "SELECT id,work_id,version_no,note,published_by,published_at FROM editions ORDER BY id")],
        }
