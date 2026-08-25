# -*- coding: utf-8 -*-
"""
FAST STORE — TRẠM SINH HỌC, Stage 8 (HS-first)

Mục tiêu:
- Học sinh chỉ đọc đúng hồ sơ của mình và đúng lịch sử của mình.
- Khi nộp bài chỉ ghi 1 row vào student_attempts, không tải/ghi lại toàn bộ lịch sử.
- Ngân hàng HS đọc từ questions_v2 (1 câu = 1 row) khi V2 khớp kho cũ.
- Có fallback an toàn về dữ liệu local/kho cũ ở app.py nếu V2 chưa sẵn sàng.
- Không xóa/migration dữ liệu.
"""

import copy
import json
import os
import re
import threading
import time
import uuid
from datetime import datetime, timezone

from data_store import _supabase_client, _doc_json_local, _luu_json_local

_LOCK = threading.RLock()
_CACHE = {}

_STUDENT_TTL = 300.0
_ATTEMPT_TTL = 30.0
_CLASS_TTL = 60.0
_QUESTIONS_TTL = 180.0


def _norm_sid(value):
    return str(value or "").strip().upper()


def _cache_get(key, ttl):
    now = time.monotonic()
    with _LOCK:
        item = _CACHE.get(key)
        if not item:
            return None, False
        ts, value = item
        if now - ts > ttl:
            _CACHE.pop(key, None)
            return None, False
        return copy.deepcopy(value), True


def _cache_set(key, value):
    with _LOCK:
        _CACHE[key] = (time.monotonic(), copy.deepcopy(value))


def _cache_get_readonly(key, ttl):
    """Đọc cache chỉ-đọc, không deepcopy khối lớn.

    Chỉ dùng cho ngân hàng câu hỏi mà UI không được phép sửa trực tiếp.
    Giảm đáng kể CPU/RAM khi Streamlit rerun/fragment rerun.
    """
    now = time.monotonic()
    with _LOCK:
        item = _CACHE.get(key)
        if not item:
            return None, False
        ts, value = item
        if now - ts > ttl:
            _CACHE.pop(key, None)
            return None, False
        return value, True


def _cache_set_readonly(key, value):
    with _LOCK:
        _CACHE[key] = (time.monotonic(), value)


def _cache_drop_prefix(prefix):
    with _LOCK:
        for key in list(_CACHE):
            if str(key).startswith(prefix):
                _CACHE.pop(key, None)


def clear_fast_cache():
    with _LOCK:
        _CACHE.clear()


def _row_to_student(row):
    item = dict((row or {}).get("data") or {})
    item.setdefault("ma_hoc_sinh", (row or {}).get("student_id", ""))
    item.setdefault("ho_ten", (row or {}).get("full_name", ""))
    item.setdefault("lop", (row or {}).get("class_name", ""))
    if not str(item.get("trang_thai", "")).strip():
        item["trang_thai"] = "Đang học" if (row or {}).get("active", True) else "Tạm khóa"
    return item


def get_student_by_id(student_id, local_student_path=""):
    sid = _norm_sid(student_id)
    if not sid:
        return None
    key = f"student:{sid}"
    cached, ok = _cache_get(key, _STUDENT_TTL)
    if ok:
        return cached

    client = _supabase_client()
    if client is not None:
        try:
            res = (
                client.table("students")
                .select("student_id,full_name,class_name,active,data")
                .eq("student_id", sid)
                .limit(1)
                .execute()
            )
            rows = getattr(res, "data", None) or []
            if rows:
                item = _row_to_student(rows[0])
                _cache_set(key, item)
                return item
        except Exception:
            pass

    if local_student_path:
        data = _doc_json_local(local_student_path, [])
        for hs in data if isinstance(data, list) else []:
            if _norm_sid(hs.get("ma_hoc_sinh")) == sid:
                _cache_set(key, hs)
                return dict(hs)
    return None


def get_students_by_class(class_name, local_student_path=""):
    lop = str(class_name or "").strip()
    key = f"students_class:{lop}"
    cached, ok = _cache_get(key, _CLASS_TTL)
    if ok:
        return cached

    client = _supabase_client()
    if client is not None:
        try:
            query = client.table("students").select("student_id,full_name,class_name,active,data")
            if lop and lop != "Tất cả":
                query = query.eq("class_name", lop)
            res = query.order("student_id").execute()
            rows = getattr(res, "data", None) or []
            out = [_row_to_student(r) for r in rows]
            _cache_set(key, out)
            return out
        except Exception:
            pass

    data = _doc_json_local(local_student_path, [])
    out = [dict(x) for x in data if isinstance(x, dict)] if isinstance(data, list) else []
    if lop and lop != "Tất cả":
        out = [x for x in out if str(x.get("lop", "")).strip() == lop]
    _cache_set(key, out)
    return out


def _attempt_item(row):
    item = dict((row or {}).get("data") or {})
    if not str(item.get("id", "")).strip():
        item["id"] = str((row or {}).get("id", "") or "").strip()
    if not str(item.get("thoi_gian_iso", "")).strip():
        item["thoi_gian_iso"] = str((row or {}).get("submitted_at", "") or "").strip()
    return item


def _fetch_attempts_query(query, page_size=1000):
    out = []
    start = 0
    while True:
        rr = query.range(start, start + page_size - 1).execute()
        rows = getattr(rr, "data", None) or []
        out.extend(_attempt_item(r) for r in rows)
        if len(rows) < page_size:
            break
        start += page_size
    return out


def get_attempts_by_student(student_id, local_history_path=""):
    sid = _norm_sid(student_id)
    if not sid:
        return []
    key = f"attempt_student:{sid}"
    cached, ok = _cache_get(key, _ATTEMPT_TTL)
    if ok:
        return cached

    client = _supabase_client()
    if client is not None:
        try:
            q = (
                client.table("student_attempts")
                .select("id,student_id,class_name,submitted_at,data")
                .eq("student_id", sid)
                .order("submitted_at")
            )
            out = _fetch_attempts_query(q)
            _cache_set(key, out)
            return out
        except Exception:
            pass

    data = _doc_json_local(local_history_path, []) if local_history_path else []
    out = [
        dict(x) for x in (data if isinstance(data, list) else [])
        if _norm_sid(x.get("hoc_sinh_id") or x.get("ma_hoc_sinh")) == sid
    ]
    _cache_set(key, out)
    return out


def get_attempts_by_class(class_name, local_history_path=""):
    lop = str(class_name or "").strip()
    key = f"attempt_class:{lop}"
    cached, ok = _cache_get(key, _CLASS_TTL)
    if ok:
        return cached

    client = _supabase_client()
    if client is not None and lop and lop != "Tất cả":
        try:
            # Ưu tiên cột class_name. Dữ liệu cũ có thể chưa có class_name,
            # nên nếu rỗng sẽ truy vấn theo student_id của đúng lớp.
            q = (
                client.table("student_attempts")
                .select("id,student_id,class_name,submitted_at,data")
                .eq("class_name", lop)
                .order("submitted_at")
            )
            out = _fetch_attempts_query(q)
            if not out:
                ds_hs = get_students_by_class(lop)
                ids = [_norm_sid(x.get("ma_hoc_sinh")) for x in ds_hs if _norm_sid(x.get("ma_hoc_sinh"))]
                out = []
                for start in range(0, len(ids), 80):
                    batch = ids[start:start + 80]
                    if not batch:
                        continue
                    qq = (
                        client.table("student_attempts")
                        .select("id,student_id,class_name,submitted_at,data")
                        .in_("student_id", batch)
                        .order("submitted_at")
                    )
                    out.extend(_fetch_attempts_query(qq))
            _cache_set(key, out)
            return out
        except Exception:
            pass

    data = _doc_json_local(local_history_path, []) if local_history_path else []
    out = [dict(x) for x in (data if isinstance(data, list) else [])]
    if lop and lop != "Tất cả":
        out = [
            x for x in out
            if str(x.get("lop") or (x.get("pham_vi") or {}).get("lop") or "").strip() == lop
        ]
    _cache_set(key, out)
    return out


def _attempt_uuid(value, item):
    try:
        return str(uuid.UUID(str(value)))
    except Exception:
        raw = json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
        return str(uuid.uuid5(uuid.NAMESPACE_URL, raw))


def _attempt_to_row(attempt):
    lan = dict(attempt or {})
    sid = _norm_sid(lan.get("hoc_sinh_id") or lan.get("ma_hoc_sinh"))
    if not sid:
        raise ValueError("Lượt làm thiếu mã học sinh.")
    row_id = _attempt_uuid(lan.get("id"), lan)
    lan["id"] = row_id
    pham_vi = lan.get("pham_vi", {}) or {}
    if not isinstance(pham_vi, dict):
        pham_vi = {}
    submitted_at = str(
        lan.get("nop_bai_iso")
        or lan.get("thoi_gian_iso")
        or datetime.now(timezone.utc).isoformat()
    ).strip()
    try:
        score = lan.get("diem_chinh_thuc", lan.get("diem"))
        score = float(score) if score is not None else None
    except Exception:
        score = None
    try:
        score_scale = lan.get("thang_diem", 10)
        score_scale = float(score_scale) if score_scale is not None else None
    except Exception:
        score_scale = None
    row = {
        "id": row_id,
        "student_id": sid,
        "class_name": str(lan.get("lop") or pham_vi.get("lop") or "").strip(),
        "mode": str(lan.get("che_do", "") or "").strip(),
        "exam_id": str(pham_vi.get("de_id") or pham_vi.get("mau_id") or "").strip() or None,
        "test_session_id": str(pham_vi.get("dot_kiem_tra_id") or "").strip() or None,
        "submitted_at": submitted_at,
        "score": score,
        "score_scale": score_scale,
        "data": lan,
    }
    return lan, row


def append_attempt(attempt, local_history_path=""):
    """Ghi đúng 1 lượt làm. Cloud là đích chính; local chỉ là bản phụ nếu có."""
    lan, row = _attempt_to_row(attempt)
    client = _supabase_client()
    cloud_available = client is not None
    cloud_ok = False
    cloud_error = ""

    if client is not None:
        try:
            client.table("student_attempts").upsert([row], on_conflict="id").execute()
            cloud_ok = True
        except Exception as e:
            cloud_error = str(e)

    # Không ghi lại cả file lịch sử local nếu cloud đã ghi thành công.
    # Đây là một nút thắt lớn khi nhiều HS nộp bài đồng thời. Local chỉ làm
    # phương án cứu hộ khi không có cloud hoặc cloud đang lỗi.
    local_ok = False
    if local_history_path and (not cloud_available or not cloud_ok):
        try:
            ds = _doc_json_local(local_history_path, [])
            if not isinstance(ds, list):
                ds = []
            rid = str(lan.get("id", ""))
            found = False
            for i, old in enumerate(ds):
                if str((old or {}).get("id", "")) == rid:
                    ds[i] = lan
                    found = True
                    break
            if not found:
                ds.append(lan)
            local_ok = _luu_json_local(local_history_path, ds)
        except Exception:
            local_ok = False

    sid = _norm_sid(lan.get("hoc_sinh_id"))
    lop = str(lan.get("lop") or (lan.get("pham_vi") or {}).get("lop") or "").strip()
    _cache_drop_prefix(f"attempt_student:{sid}")
    if lop:
        _cache_drop_prefix(f"attempt_class:{lop}")

    ok = cloud_ok if cloud_available else local_ok
    return {
        "ok": bool(ok),
        "cloud_available": cloud_available,
        "cloud_ok": cloud_ok,
        "local_ok": local_ok,
        "error": cloud_error,
        "id": lan.get("id", ""),
    }


def questions_v2_count():
    client = _supabase_client()
    if client is None:
        return None
    key = "qv2_count"
    cached, ok = _cache_get(key, 120.0)
    if ok:
        return cached
    try:
        res = client.table("questions_v2").select("question_id", count="exact").limit(1).execute()
        count = getattr(res, "count", None)
        if count is not None:
            count = int(count)
            _cache_set(key, count)
            return count
    except Exception:
        pass
    try:
        total = 0
        start = 0
        while True:
            rr = client.table("questions_v2").select("question_id").range(start, start + 999).execute()
            rows = getattr(rr, "data", None) or []
            total += len(rows)
            if len(rows) < 1000:
                break
            start += 1000
        _cache_set(key, total)
        return total
    except Exception:
        return None


def get_all_questions_v2(expected_count=None):
    """Đọc kho V2 một lần/90 giây; chỉ dùng nếu số câu khớp kho legacy khi expected_count được truyền."""
    exp = None if expected_count is None else int(expected_count or 0)
    key = f"qv2_all:{exp}"
    cached, ok = _cache_get_readonly(key, _QUESTIONS_TTL)
    if ok:
        return cached

    count = questions_v2_count()
    if count is None or count <= 0:
        return None
    if exp is not None and exp > 0 and count != exp:
        return None

    client = _supabase_client()
    if client is None:
        return None
    out = []
    start = 0
    try:
        while True:
            rr = (
                client.table("questions_v2")
                .select("question_id,data")
                .range(start, start + 999)
                .execute()
            )
            rows = getattr(rr, "data", None) or []
            for row in rows:
                q = dict(row.get("data") or {})
                q.setdefault("id", str(row.get("question_id", "") or ""))
                out.append(q)
            if len(rows) < 1000:
                break
            start += 1000
    except Exception:
        return None

    if exp is not None and exp > 0 and len(out) != exp:
        return None
    _cache_set_readonly(key, out)
    return out



def _grade_variants(value):
    """Các biến thể thường gặp của khối để tương thích dữ liệu cũ."""
    raw = str(value or "").strip()
    if not raw:
        return []
    m = re.search(r"(?:khối|khoi|lớp|lop)?\s*(10|11|12)", raw, flags=re.IGNORECASE)
    if not m:
        return [raw]
    n = m.group(1)
    vals = [f"Khối {n}", n, f"Lớp {n}"]
    if raw not in vals:
        vals.insert(0, raw)
    # giữ thứ tự, bỏ trùng
    return list(dict.fromkeys(vals))



def get_question_index_v2_by_scope(
    khoi="",
    chuong="",
    bai="",
    yccd="",
    muc_do="",
    dang_cau="",
):
    """Đọc CHỈ MỤC nhẹ của questions_v2, không kéo cột data JSON lớn.

    Dùng cho màn hình chọn bài/chương. Chỉ khi HS đã chọn đúng phạm vi
    app mới gọi get_questions_v2_by_scope() để lấy nội dung câu đầy đủ.
    """
    khoi_vals = _grade_variants(khoi)
    cache_key = (
        "qv2_index:",
        tuple(khoi_vals),
        str(chuong or "").strip(),
        str(bai or "").strip(),
        str(yccd or "").strip(),
        str(muc_do or "").strip(),
        str(dang_cau or "").strip(),
    )
    cached, ok = _cache_get_readonly(cache_key, 300.0)
    if ok:
        return cached

    client = _supabase_client()
    if client is None:
        return None

    out = []
    start = 0
    page_size = 1000
    try:
        while True:
            query = client.table("questions_v2").select(
                "question_id,khoi,chuong,bai,yccd,muc_do,dang_cau"
            )
            if khoi_vals:
                query = query.in_("khoi", khoi_vals)
            if str(chuong or "").strip():
                query = query.eq("chuong", str(chuong).strip())
            if str(bai or "").strip():
                query = query.eq("bai", str(bai).strip())
            if str(yccd or "").strip():
                query = query.eq("yccd", str(yccd).strip())
            if str(muc_do or "").strip():
                query = query.eq("muc_do", str(muc_do).strip())
            if str(dang_cau or "").strip():
                query = query.eq("dang_cau", str(dang_cau).strip())

            rr = query.range(start, start + page_size - 1).execute()
            rows = getattr(rr, "data", None) or []
            for row in rows:
                out.append({
                    "id": str(row.get("question_id", "") or ""),
                    "khoi": str(row.get("khoi", "") or ""),
                    "chuong": str(row.get("chuong", "") or ""),
                    "bai": str(row.get("bai", "") or ""),
                    "yccd": str(row.get("yccd", "") or ""),
                    "muc_do": str(row.get("muc_do", "") or ""),
                    "dang_cau": str(row.get("dang_cau", "") or ""),
                    # Các giá trị dưới đây giúp các hàm lọc cũ vẫn dùng được.
                    "trang_thai": "Đã duyệt",
                    "duoc_dung_luyen_hs": True,
                    "_chi_muc_only": True,
                })
            if len(rows) < page_size:
                break
            start += page_size
    except Exception:
        return None

    _cache_set_readonly(cache_key, out)
    return out

def get_questions_v2_by_scope(
    khoi="",
    chuong="",
    bai="",
    yccd="",
    muc_do="",
    dang_cau="",
    limit=None,
    expected_total=None,
):
    """Đọc đúng PHẠM VI cần thiết từ questions_v2, không tải toàn bộ kho.

    `expected_total` chỉ dùng để xác nhận bảng V2 đang khớp tổng số câu legacy.
    Nếu V2 chưa đồng bộ đủ, trả None để app fallback kho cũ an toàn.
    """
    exp = None if expected_total is None else int(expected_total or 0)

    # Kiểm tra nhanh tính toàn vẹn của kho V2 trước khi dùng đường query nhỏ.
    if exp is not None and exp > 0:
        count = questions_v2_count()
        if count is None or count != exp:
            return None

    khoi_vals = _grade_variants(khoi)
    cache_key = (
        "qv2_scope:",
        tuple(khoi_vals),
        str(chuong or "").strip(),
        str(bai or "").strip(),
        str(yccd or "").strip(),
        str(muc_do or "").strip(),
        str(dang_cau or "").strip(),
        None if limit is None else int(limit),
        exp,
    )
    cached, ok = _cache_get_readonly(cache_key, _QUESTIONS_TTL)
    if ok:
        return cached

    client = _supabase_client()
    if client is None:
        return None

    out = []
    start = 0
    page_size = 1000
    max_rows = None if limit is None else max(0, int(limit))

    try:
        while True:
            query = client.table("questions_v2").select("question_id,data")
            if khoi_vals:
                query = query.in_("khoi", khoi_vals)
            if str(chuong or "").strip():
                query = query.eq("chuong", str(chuong).strip())
            if str(bai or "").strip():
                query = query.eq("bai", str(bai).strip())
            if str(yccd or "").strip():
                query = query.eq("yccd", str(yccd).strip())
            if str(muc_do or "").strip():
                query = query.eq("muc_do", str(muc_do).strip())
            if str(dang_cau or "").strip():
                query = query.eq("dang_cau", str(dang_cau).strip())

            end = start + page_size - 1
            if max_rows is not None:
                if len(out) >= max_rows:
                    break
                end = min(end, start + (max_rows - len(out)) - 1)
            rr = query.range(start, end).execute()
            rows = getattr(rr, "data", None) or []
            for row in rows:
                q = dict(row.get("data") or {})
                q.setdefault("id", str(row.get("question_id", "") or ""))
                out.append(q)
                if max_rows is not None and len(out) >= max_rows:
                    break
            if len(rows) < (end - start + 1):
                break
            if max_rows is not None and len(out) >= max_rows:
                break
            start = end + 1
    except Exception:
        return None

    _cache_set_readonly(cache_key, out)
    return out

def get_questions_v2_by_ids(question_ids):
    ids = [str(x or "").strip() for x in question_ids if str(x or "").strip()]
    if not ids:
        return []

    # Cùng một số câu lịch sử cũ có thể được hỏi lại nhiều lần khi HS đổi màn hình.
    # Chuẩn hóa thứ tự để cache trúng dù đầu vào khác thứ tự.
    ids_unique = list(dict.fromkeys(ids))
    cache_ids = tuple(sorted(ids_unique))
    key = ("qv2_ids", cache_ids)
    cached, ok = _cache_get_readonly(key, _QUESTIONS_TTL)
    if ok:
        return cached

    client = _supabase_client()
    if client is None:
        return []
    by_id = {}
    try:
        for start in range(0, len(ids_unique), 200):
            batch = ids_unique[start:start + 200]
            rr = (
                client.table("questions_v2")
                .select("question_id,data")
                .in_("question_id", batch)
                .execute()
            )
            for row in (getattr(rr, "data", None) or []):
                q = dict(row.get("data") or {})
                qid = str(row.get("question_id", "") or "")
                q.setdefault("id", qid)
                by_id[qid] = q
    except Exception:
        return []

    # Giữ thứ tự đầu vào để caller không thay đổi hành vi.
    out = [by_id[x] for x in ids_unique if x in by_id]
    _cache_set_readonly(key, out)
    return out
