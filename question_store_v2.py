# -*- coding: utf-8 -*-
"""
Kho câu hỏi V2 chạy SONG SONG với kho cũ.

Nguyên tắc an toàn:
- KHÔNG đọc thay kho cũ trong app chính.
- KHÔNG xóa/chỉnh app_documents/ngan_hang_cau_hoi.json.
- Chỉ ghi vào bảng Supabase riêng: questions_v2.
- 1 câu = 1 row; upsert theo question_id.
"""

import hashlib
import json
from datetime import datetime, timezone

from data_store import _supabase_client

TABLE_NAME = "questions_v2"


def _text(value):
    return str(value or "").strip()


def _stable_core(question):
    q = dict(question or {})
    return {
        "khoi": q.get("khoi", ""),
        "chuong": q.get("chuong", ""),
        "bai": q.get("bai", ""),
        "yccd": q.get("yccd", ""),
        "muc_do": q.get("muc_do", ""),
        "dang_cau": q.get("dang_cau", ""),
        "thanh_phan_nang_luc": q.get("thanh_phan_nang_luc", ""),
        "cau_hoi": q.get("cau_hoi", ""),
        "tinh_huong": q.get("tinh_huong", ""),
        "lua_chon": q.get("lua_chon", []),
        "dap_an": q.get("dap_an", ""),
        "nhan_dinh_meta": q.get("nhan_dinh_meta", []),
    }


def question_fingerprint(question):
    raw = json.dumps(
        _stable_core(question),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def question_id(question):
    """Giữ ID kho cũ nếu có; câu legacy thiếu ID dùng ID xác định từ nội dung."""
    old_id = _text((question or {}).get("id"))
    if old_id:
        return old_id
    return "legacy_" + question_fingerprint(question)[:48]


def question_to_row(question):
    q = dict(question or {})
    now_iso = datetime.now(timezone.utc).isoformat()
    return {
        "question_id": question_id(q),
        "fingerprint": question_fingerprint(q),
        "khoi": _text(q.get("khoi")) or None,
        "chuong": _text(q.get("chuong")) or None,
        "bai": _text(q.get("bai")) or None,
        "yccd": _text(q.get("yccd")) or None,
        "muc_do": _text(q.get("muc_do")) or None,
        "dang_cau": _text(q.get("dang_cau")) or None,
        "thanh_phan_nang_luc": _text(q.get("thanh_phan_nang_luc")) or None,
        "cau_hoi": _text(q.get("cau_hoi")) or None,
        "data": q,
        "updated_at": now_iso,
    }


def _fetch_v2_index(page_size=1000):
    client = _supabase_client()
    if client is None:
        raise RuntimeError("Không kết nối được Supabase. Kiểm tra SUPABASE_URL/SUPABASE_SECRET_KEY.")

    out = {}
    start = 0
    while True:
        res = (
            client.table(TABLE_NAME)
            .select("question_id,fingerprint")
            .range(start, start + page_size - 1)
            .execute()
        )
        rows = getattr(res, "data", None) or []
        for row in rows:
            qid = _text(row.get("question_id"))
            if qid:
                out[qid] = _text(row.get("fingerprint"))
        if len(rows) < page_size:
            break
        start += page_size
    return out


def table_status():
    client = _supabase_client()
    if client is None:
        return {"ok": False, "count": 0, "error": "Không kết nối được Supabase."}
    try:
        idx = _fetch_v2_index()
        return {"ok": True, "count": len(idx), "error": ""}
    except Exception as e:
        return {"ok": False, "count": 0, "error": str(e)}


def compare_with_legacy(legacy_questions):
    legacy = list(legacy_questions or [])
    legacy_idx = {
        question_id(q): question_fingerprint(q)
        for q in legacy
        if isinstance(q, dict)
    }
    cloud_idx = _fetch_v2_index()

    legacy_ids = set(legacy_idx)
    cloud_ids = set(cloud_idx)
    missing = sorted(legacy_ids - cloud_ids)
    extra = sorted(cloud_ids - legacy_ids)
    mismatch = sorted(
        qid for qid in (legacy_ids & cloud_ids)
        if legacy_idx.get(qid) != cloud_idx.get(qid)
    )

    return {
        "legacy_count": len(legacy_idx),
        "v2_count": len(cloud_idx),
        "missing_count": len(missing),
        "extra_count": len(extra),
        "mismatch_count": len(mismatch),
        "missing_sample": missing[:10],
        "extra_sample": extra[:10],
        "mismatch_sample": mismatch[:10],
        "matched": not missing and not extra and not mismatch,
    }


def sync_questions(legacy_questions, limit=None, batch_size=50, progress_callback=None):
    """Sao chép/upsert sang V2. Không đụng kho cũ."""
    client = _supabase_client()
    if client is None:
        raise RuntimeError("Không kết nối được Supabase.")

    source = [q for q in list(legacy_questions or []) if isinstance(q, dict)]
    if limit is not None:
        source = source[: max(0, int(limit))]

    rows = [question_to_row(q) for q in source]
    total = len(rows)
    written = 0

    for start in range(0, total, max(1, int(batch_size))):
        batch = rows[start:start + max(1, int(batch_size))]
        if not batch:
            continue
        table = client.table(TABLE_NAME)
        try:
            table.upsert(
                batch,
                on_conflict="question_id",
                returning="minimal",
            ).execute()
        except TypeError:
            # Tương thích supabase-py/postgrest cũ.
            table.upsert(
                batch,
                on_conflict="question_id",
            ).execute()
        written += len(batch)
        if progress_callback is not None:
            try:
                progress_callback(written, total)
            except Exception:
                pass

    return {
        "requested": total,
        "written": written,
    }


def sync_delta(legacy_questions, batch_size=50, progress_callback=None):
    """
    Đồng bộ BÙ sang questions_v2: chỉ upsert câu thiếu hoặc có nội dung thay đổi.
    KHÔNG xóa câu dư trong V2 để kho song song luôn an toàn như một bản dự phòng.
    """
    client = _supabase_client()
    if client is None:
        raise RuntimeError("Không kết nối được Supabase.")

    source = [q for q in list(legacy_questions or []) if isinstance(q, dict)]
    cloud_idx = _fetch_v2_index()

    delta = []
    unchanged = 0
    for q in source:
        qid = question_id(q)
        fp = question_fingerprint(q)
        if cloud_idx.get(qid) == fp:
            unchanged += 1
            continue
        delta.append(q)

    if not delta:
        return {
            "legacy_count": len(source),
            "changed": 0,
            "written": 0,
            "unchanged": unchanged,
            "v2_before": len(cloud_idx),
        }

    result = sync_questions(
        delta,
        limit=None,
        batch_size=batch_size,
        progress_callback=progress_callback,
    )
    return {
        "legacy_count": len(source),
        "changed": len(delta),
        "written": int(result.get("written", 0) or 0),
        "unchanged": unchanged,
        "v2_before": len(cloud_idx),
    }
