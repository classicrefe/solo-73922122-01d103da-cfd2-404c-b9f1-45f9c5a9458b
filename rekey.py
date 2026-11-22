"""结批后的重录核对: 换稿、沿用旧核对判定、待复核清单。

业务规则:
- 仅已结批(放行/待全检)的批次可上传重录稿; 按稿号上传, 同稿号再次上传即整份替换。
- CSV 三列: 新条目号、铭牌编号、原条目号; 新条目号稿内唯一, 原条目号可留空。
- 一行同时满足以下条件才可沿用旧核对(不进待复核清单):
  1) 原条目号在本批原清单中存在;
  2) 该原条目在抽查中被抽中且已录入核对结果;
  3) 旧核对结果为「正确」;
  4) 稿内铭牌编号与原条目铭牌编号逐字相同;
  5) 该原条目号在本批所有现行稿中只被引用一次。
- 其余行进入该稿待复核清单并注明原因; 任一稿增删/替换后, 全部现行稿按现状重新判定。
- 换稿在单事务内完成, 失败回滚, 各稿及清单保持原状。
"""

import csv
import hashlib
import io

from core import (ConflictError, NotFoundError, ValidationError, now_iso)

REKEY_HEADER = ("新条目号", "铭牌编号", "原条目号")
DRAFT_NO_MAX = 64


# ---------------------------------------------------------------- CSV 解析

def parse_rekey_csv(text):
    """解析重录稿 CSV 文本。

    接受带/不带表头; 每行恰好 3 列(原条目号可空)。
    返回 [(new_entry_no, nameplate_no, original_entry_no_or_None), ...], 保留行序。
    """
    if text is None or not text.strip():
        raise ValidationError("重录稿内容为空, 至少需要 1 行")
    text = text.lstrip("﻿").replace("，", ",")
    reader = csv.reader(io.StringIO(text))
    rows = []
    seen_new = set()
    data_lineno = 0
    for raw_lineno, raw in enumerate(reader, 1):
        if not raw or all(not cell.strip() for cell in raw):
            continue  # 跳过空行
        cells = [cell.strip() for cell in raw]
        if data_lineno == 0 and tuple(cells) == REKEY_HEADER:
            continue  # 表头
        data_lineno += 1
        if len(cells) != 3:
            raise ValidationError(
                f"重录稿第 {raw_lineno} 行应为 3 列(新条目号,铭牌编号,原条目号), "
                f"实际 {len(cells)} 列")
        new_entry_no, nameplate_no, original_entry_no = cells
        if not new_entry_no:
            raise ValidationError(f"重录稿第 {raw_lineno} 行新条目号为空")
        if not nameplate_no:
            raise ValidationError(f"重录稿第 {raw_lineno} 行铭牌编号为空")
        if new_entry_no in seen_new:
            raise ValidationError(f"重录稿内新条目号重复: {new_entry_no}")
        seen_new.add(new_entry_no)
        rows.append((new_entry_no, nameplate_no, original_entry_no or None))
    if not rows:
        raise ValidationError("重录稿内容为空, 至少需要 1 行数据")
    return rows


# ---------------------------------------------------------------- 查询

def get_draft(conn, batch_id: int, draft_no: str):
    return conn.execute(
        "SELECT * FROM rekey_drafts WHERE batch_id = ? AND draft_no = ?",
        (batch_id, draft_no)).fetchone()


def list_drafts(conn, batch_id: int):
    return conn.execute(
        "SELECT * FROM rekey_drafts WHERE batch_id = ? ORDER BY id",
        (batch_id,)).fetchall()


def draft_rows(conn, draft_id: int):
    return conn.execute(
        "SELECT * FROM rekey_rows WHERE draft_id = ? ORDER BY line_no, id",
        (draft_id,)).fetchall()


def reference_counts(conn, batch_id: int):
    """本批所有现行稿中, 各原条目号被引用的次数(仅统计非空引用)。"""
    out = {}
    for r in conn.execute(
            "SELECT original_entry_no, COUNT(*) AS c FROM rekey_rows "
            "WHERE batch_id = ? AND original_entry_no IS NOT NULL "
            "GROUP BY original_entry_no", (batch_id,)):
        out[r["original_entry_no"]] = r["c"]
    return out


# ---------------------------------------------------------------- 待复核判定

def evaluate_row(row, items, ref_counts):
    """判定一行是否可沿用旧核对。返回原因字符串; None 表示可沿用。"""
    original = row["original_entry_no"]
    if not original:
        return "原条目号留空, 需翻原件复核"
    original_item = items.get(original)
    if original_item is None:
        return f"原条目号 {original} 不在本批原清单中, 需翻原件复核"
    if original_item["position"] is None:
        return f"原条目号 {original} 抽查时未被抽中, 无旧核对可沿用"
    if original_item["result"] is None:
        return f"原条目号 {original} 尚未录入核对结果, 无旧核对可沿用"
    if original_item["result"] != "ok":
        return f"原条目号 {original} 旧核对为「错误」, 不可沿用"
    if original_item["nameplate_no"] != row["nameplate_no"]:
        return "铭牌编号与原条目未逐字相同, 需翻原件复核"
    times = ref_counts.get(original, 0)
    if times != 1:
        return (f"原条目号 {original} 在本批现行稿中被引用 {times} 次, "
                f"无法唯一沿用旧核对")
    return None


def draft_review(conn, batch_id: int, draft):
    """计算一稿的现行判定: 待复核行(含原因)与可沿用行。"""
    items = {r["entry_no"]: r for r in conn.execute(
        "SELECT entry_no, nameplate_no, position, result FROM items "
        "WHERE batch_id = ?", (batch_id,))}
    ref_counts = reference_counts(conn, batch_id)
    pending, inherited = [], []
    for row in draft_rows(conn, draft["id"]):
        reason = evaluate_row(row, items, ref_counts)
        payload = {
            "line_no": row["line_no"],
            "new_entry_no": row["new_entry_no"],
            "nameplate_no": row["nameplate_no"],
            "original_entry_no": row["original_entry_no"] or "",
        }
        if reason is None:
            inherited.append(payload)
        else:
            payload["reason"] = reason
            pending.append(payload)
    return pending, inherited


def pending_token(pending) -> str:
    """待复核清单(行与原因)的摘要, 下载时用于核对页面是否已过期。"""
    h = hashlib.sha256()
    for p in pending:
        line = "\x1f".join([str(p["line_no"]), p["new_entry_no"], p["nameplate_no"],
                            p["original_entry_no"], p["reason"]])
        h.update((line + "\n").encode("utf-8"))
    return h.hexdigest()[:16]


def pending_csv_text(pending) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["新条目号", "铭牌编号", "原条目号", "待复核原因"])
    for p in pending:
        writer.writerow([p["new_entry_no"], p["nameplate_no"],
                         p["original_entry_no"], p["reason"]])
    return buf.getvalue()


# ---------------------------------------------------------------- 换稿(上传/整份替换)

def upload_draft(conn, batch, draft_no: str, text: str):
    """按稿号上传重录稿; 同稿号再次上传整份替换。单事务, 失败回滚。

    返回 (draft_row, replaced: bool)。
    """
    if batch["status"] == "open":
        raise ConflictError("批次尚未结批, 结批后才能进行重录核对")
    draft_no = (draft_no or "").strip()
    if not draft_no:
        raise ValidationError("稿号不能为空")
    if len(draft_no) > DRAFT_NO_MAX:
        raise ValidationError(f"稿号过长(不超过 {DRAFT_NO_MAX} 字符)")
    rows = parse_rekey_csv(text)  # 解析失败时不写库

    cur = conn.cursor()
    try:
        existing = get_draft(conn, batch["id"], draft_no)
        now = now_iso()
        if existing is not None:
            cur.execute("DELETE FROM rekey_rows WHERE draft_id = ?", (existing["id"],))
            cur.execute(
                "UPDATE rekey_drafts SET version = version + 1, row_count = ?, "
                "updated_at = ? WHERE id = ?", (len(rows), now, existing["id"]))
            draft_id = existing["id"]
            replaced = True
        else:
            cur.execute(
                "INSERT INTO rekey_drafts (batch_id, draft_no, version, row_count, "
                "created_at, updated_at) VALUES (?, ?, 1, ?, ?, ?)",
                (batch["id"], draft_no, len(rows), now, now))
            draft_id = cur.lastrowid
            replaced = False
        cur.executemany(
            "INSERT INTO rekey_rows (draft_id, batch_id, line_no, new_entry_no, "
            "nameplate_no, original_entry_no) VALUES (?, ?, ?, ?, ?, ?)",
            [(draft_id, batch["id"], i, new, np, orig)
             for i, (new, np, orig) in enumerate(rows, 1)])
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return get_draft(conn, batch["id"], draft_no), replaced


def review_model(conn, batch, draft):
    """供网页/API 使用的单稿视图模型。"""
    pending, inherited = draft_review(conn, batch["id"], draft)
    return {
        "draft_no": draft["draft_no"],
        "version": draft["version"],
        "row_count": draft["row_count"],
        "updated_at": draft["updated_at"],
        "pending": pending,
        "pending_count": len(pending),
        "inherited_count": len(inherited),
        "inherited": inherited,
        "token": pending_token(pending),
    }


def all_review_models(conn, batch):
    return [review_model(conn, batch, d) for d in list_drafts(conn, batch["id"])]
