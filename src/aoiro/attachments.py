"""領収書/証憑ファイル保存。

実ファイルは ./receipts/<year>/<id>_<filename> に保存。
DB には メタ情報のみ (path, mime, ocr_text, byte_size 等)。
仕訳エントリ (journal_entry_id) と任意で紐付ける。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

_RECEIPTS_DIR = Path("receipts")


def _safe_filename(name: str) -> str:
    """パス・トラバーサル防止 + Unicode を保持。"""
    bad = ('/', '\\', '..', '\x00')
    out = name
    for b in bad:
        out = out.replace(b, '_')
    return out[:200] or "upload"


def save_attachment(con: sqlite3.Connection, *, file_bytes: bytes, filename: str,
                    mime_type: str = "", journal_entry_id: int | None = None,
                    ocr_text: str | None = None) -> dict:
    """ファイルを receipts/ に保存して aoiro_attachments に登録。

    Returns: {"id", "filename", "stored_path", "byte_size"}
    """
    if not file_bytes:
        raise ValueError("empty file")
    year = datetime.now().year
    year_dir = _RECEIPTS_DIR / str(year)
    year_dir.mkdir(parents=True, exist_ok=True)

    safe_name = _safe_filename(filename)
    cur = con.execute(
        "INSERT INTO aoiro_attachments "
        "(journal_entry_id, filename, stored_path, mime_type, byte_size, ocr_text) "
        "VALUES (?,?,?,?,?,?)",
        (journal_entry_id, safe_name, "", mime_type, len(file_bytes), ocr_text),
    )
    aid = cur.lastrowid
    # 実 path は id 入りのため insert 後に決定 → update
    stored_path = year_dir / f"{aid:06d}_{safe_name}"
    stored_path.write_bytes(file_bytes)
    con.execute(
        "UPDATE aoiro_attachments SET stored_path=? WHERE id=?",
        (str(stored_path), aid),
    )
    con.commit()
    return {
        "id": aid, "filename": safe_name,
        "stored_path": str(stored_path), "byte_size": len(file_bytes),
    }


def list_attachments(con: sqlite3.Connection,
                      journal_entry_id: int | None = None) -> list[dict]:
    if journal_entry_id is not None:
        rows = con.execute(
            "SELECT id, journal_entry_id, filename, mime_type, byte_size, "
            "       uploaded_at FROM aoiro_attachments "
            "WHERE journal_entry_id=? ORDER BY id DESC", (journal_entry_id,)
        ).fetchall()
    else:
        rows = con.execute(
            "SELECT id, journal_entry_id, filename, mime_type, byte_size, "
            "       uploaded_at FROM aoiro_attachments ORDER BY id DESC LIMIT 200"
        ).fetchall()
    return [dict(r) for r in rows]


def get_attachment(con: sqlite3.Connection, attachment_id: int) -> dict | None:
    r = con.execute(
        "SELECT * FROM aoiro_attachments WHERE id=?", (attachment_id,)
    ).fetchone()
    return dict(r) if r else None


def link_attachment(con: sqlite3.Connection, attachment_id: int,
                     journal_entry_id: int | None) -> None:
    con.execute(
        "UPDATE aoiro_attachments SET journal_entry_id=? WHERE id=?",
        (journal_entry_id, attachment_id),
    )
    con.commit()


def delete_attachment(con: sqlite3.Connection, attachment_id: int) -> bool:
    r = get_attachment(con, attachment_id)
    if r is None:
        return False
    p = Path(r["stored_path"])
    if p.exists():
        try:
            p.unlink()
        except Exception:
            pass
    con.execute("DELETE FROM aoiro_attachments WHERE id=?", (attachment_id,))
    con.commit()
    return True
