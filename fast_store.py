# -*- coding: utf-8 -*-
"""
FAST STORE — TRẠM SINH HỌC, Stage 6 (HS-first)

Mục tiêu:
- Học sinh chỉ đọc đúng hồ sơ của mình và đúng lịch sử của mình.
- Khi nộp bài chỉ ghi 1 row vào student_attempts, không tải/ghi lại toàn bộ lịch sử.
- Ngân hàng HS đọc từ questions_v2 (1 câu = 1 row) khi V2 khớp kho cũ.
- questions_v2 CHỈ là Ngân hàng chung; tuyệt đối không trộn Ngân hàng tốt nghiệp vào đây.
- Ngân hàng tốt nghiệp được app.py đọc từ kho riêng và chỉ ghép tạm khi HS chọn Luyện tốt nghiệp THPT.
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

_STUDENT_TTL = 60.0
_ATTEMPT_TTL = 12.0
_CLASS_TTL = 20.0
_QUESTIONS_TTL = 90.0


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
    cached, ok = _cache_get(key, 30.0)
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
    cached, ok = _cache_get(key, _QUESTIONS_TTL)
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
                # Phòng thủ kiến trúc: questions_v2 là kho chung, không trả record
                # tốt nghiệp nếu dữ liệu cũ từng vô tình bị trộn vào bảng này.
                if _question_is_grad_only(q):
                    continue
                out.append(q)
            if len(rows) < 1000:
                break
            start += 1000
    except Exception:
        return None

    if exp is not None and exp > 0 and len(out) != exp:
        return None
    _cache_set(key, out)
    return out


def get_questions_v2_by_ids(question_ids):
    ids = [str(x or "").strip() for x in question_ids if str(x or "").strip()]
    if not ids:
        return []
    client = _supabase_client()
    if client is None:
        return []
    out = []
    try:
        for start in range(0, len(ids), 100):
            batch = ids[start:start + 100]
            rr = (
                client.table("questions_v2")
                .select("question_id,data")
                .in_("question_id", batch)
                .execute()
            )
            for row in (getattr(rr, "data", None) or []):
                q = dict(row.get("data") or {})
                q.setdefault("id", str(row.get("question_id", "") or ""))
                out.append(q)
    except Exception:
        return []
    return out

# ==========================================================
# QUESTIONS V2 — TRUY VẤN THEO PHẠM VI
# QUY TẮC KIẾN TRÚC:
# - questions_v2 chỉ chứa/đọc NGÂN HÀNG CHUNG.
# - Không đọc, không ghi, không đồng bộ Ngân hàng tốt nghiệp tại đây.
# - App chỉ ghép kho tốt nghiệp ở chế độ Luyện tốt nghiệp THPT.
# ==========================================================
def _norm_scope_value(value):
    """Chuẩn hóa nhẹ chuỗi phạm vi để so khớp ổn định nhưng không đổi nhãn hiển thị."""
    return " ".join(str(value or "").strip().split()).casefold()


def _question_is_grad_only(q):
    """Chặn phòng thủ nếu dữ liệu tốt nghiệp từng vô tình lọt vào questions_v2.

    Dữ liệu tốt nghiệp thật của app dùng muc_dich_su_dung='tot_nghiep' và/hoặc
    metadata nguồn đề thật. Những record này không được xem là Ngân hàng chung.
    """
    if not isinstance(q, dict):
        return False

    muc_dich = _norm_scope_value(q.get("muc_dich_su_dung", ""))
    if muc_dich in {"tot_nghiep", "tốt nghiệp", "tốt nghiệp thpt", "luyen_tot_nghiep"}:
        return True

    # Chỉ coi là grad-only khi có dấu hiệu rõ ràng của kho đề thật.
    # Không dùng riêng trường "nguon" vì câu ngân hàng chung cũng có thể ghi nguồn.
    if str(q.get("so_cau_goc", "") or "").strip() and (
        str(q.get("nguon_file", "") or "").strip()
        or list(q.get("nguon_files", []) or [])
    ):
        return True

    return False


def _question_scopes(q):
    """Trả các phạm vi Khối/Chương/Bài của một câu common-bank.

    Tương thích cả record chuẩn (khoi/chuong/bai) và câu hạt giống cũ có
    pham_vi_hat_giong. Không suy đoán metadata còn thiếu.
    """
    if not isinstance(q, dict):
        return []

    out = []
    seen = set()

    direct = {
        "khoi": str(q.get("khoi", "") or ""),
        "chuong": str(q.get("chuong", "") or ""),
        "bai": str(q.get("bai", "") or ""),
    }
    if any(str(v).strip() for v in direct.values()):
        key = tuple(_norm_scope_value(direct[k]) for k in ("khoi", "chuong", "bai"))
        if key not in seen:
            seen.add(key)
            out.append(direct)

    for pv in list(q.get("pham_vi_hat_giong", []) or []):
        if not isinstance(pv, dict):
            continue
        item = {
            "khoi": str(pv.get("khoi", "") or ""),
            "chuong": str(pv.get("chuong", "") or ""),
            "bai": str(pv.get("bai", "") or ""),
        }
        if not any(str(v).strip() for v in item.values()):
            continue
        key = tuple(_norm_scope_value(item[k]) for k in ("khoi", "chuong", "bai"))
        if key not in seen:
            seen.add(key)
            out.append(item)

    return out


def _question_matches_scope(q, khoi="", chuong="", bai=""):
    if _question_is_grad_only(q):
        return False

    target = {
        "khoi": _norm_scope_value(khoi),
        "chuong": _norm_scope_value(chuong),
        "bai": _norm_scope_value(bai),
    }

    # Không có bộ lọc -> nhận mọi câu common-bank.
    if not any(target.values()):
        return True

    for pv in _question_scopes(q):
        ok = True
        for field in ("khoi", "chuong", "bai"):
            if target[field] and _norm_scope_value(pv.get(field, "")) != target[field]:
                ok = False
                break
        if ok:
            return True
    return False


def _rows_to_questions_v2(rows):
    out = []
    for row in rows or []:
        q = dict((row or {}).get("data") or {})
        q.setdefault("id", str((row or {}).get("question_id", "") or ""))
        if _question_is_grad_only(q):
            continue
        out.append(q)
    return out


def _fetch_questions_v2_direct_scope(khoi="", chuong="", bai=""):
    """Thử lọc JSONB ngay trên Supabase; lỗi/không hỗ trợ thì trả None để fallback.

    PostgREST hỗ trợ đường dẫn JSON `data->>field`. Vì một số deployment cũ có
    schema/policy khác, hàm này tuyệt đối không phải điểm lỗi duy nhất.
    """
    client = _supabase_client()
    if client is None:
        return None

    # Chỉ dùng direct query cho record chuẩn. Câu có pham_vi_hat_giong sẽ được
    # tìm thấy ở fallback toàn kho nếu direct query không đủ.
    filters = {
        "khoi": str(khoi or "").strip(),
        "chuong": str(chuong or "").strip(),
        "bai": str(bai or "").strip(),
    }

    try:
        out = []
        start = 0
        while True:
            query = client.table("questions_v2").select("question_id,data")
            for field, value in filters.items():
                if value:
                    query = query.eq(f"data->>{field}", value)
            rr = query.range(start, start + 999).execute()
            rows = getattr(rr, "data", None) or []
            out.extend(_rows_to_questions_v2(rows))
            if len(rows) < 1000:
                break
            start += 1000
        return out
    except Exception:
        return None


def get_questions_v2_by_scope(khoi="", chuong="", bai="", expected_total=None):
    """Đọc câu NGÂN HÀNG CHUNG theo Khối/Chương/Bài.

    `expected_total` nếu có chỉ dùng để xác minh tổng số row V2 so với kho chung
    legacy; KHÔNG so số câu của phạm vi với tổng kho.
    """
    exp = None if expected_total is None else int(expected_total or 0)
    key = "qv2_scope:" + "|".join([
        _norm_scope_value(khoi),
        _norm_scope_value(chuong),
        _norm_scope_value(bai),
        str(exp),
    ])
    cached, ok = _cache_get(key, _QUESTIONS_TTL)
    if ok:
        return cached

    if exp is not None and exp > 0:
        count = questions_v2_count()
        if count is None or count != exp:
            return None

    # Thử truy vấn hẹp trước cho tốc độ.
    direct = _fetch_questions_v2_direct_scope(khoi=khoi, chuong=chuong, bai=bai)

    # Direct query có thể trả rỗng vì dữ liệu cũ chỉ có pham_vi_hat_giong hoặc
    # nhãn chưa khớp tuyệt đối. Khi đó fallback một lần qua get_all_questions_v2.
    if direct:
        result = [
            q for q in direct
            if _question_matches_scope(q, khoi=khoi, chuong=chuong, bai=bai)
        ]
        _cache_set(key, result)
        return result

    all_q = get_all_questions_v2(expected_count=exp)
    if all_q is None:
        return None

    result = [
        q for q in all_q
        if _question_matches_scope(q, khoi=khoi, chuong=chuong, bai=bai)
    ]
    _cache_set(key, result)
    return result


def get_question_index_v2_by_scope(khoi="", chuong="", bai="", expected_total=None):
    """Trả index nhẹ của NGÂN HÀNG CHUNG theo phạm vi.

    Giữ hàm này để tương thích app.py Stage 10. Index không chứa nội dung dài và
    tuyệt đối không chứa câu từ Ngân hàng tốt nghiệp.
    """
    key = "qv2_index:" + "|".join([
        _norm_scope_value(khoi),
        _norm_scope_value(chuong),
        _norm_scope_value(bai),
        str(None if expected_total is None else int(expected_total or 0)),
    ])
    cached, ok = _cache_get(key, _QUESTIONS_TTL)
    if ok:
        return cached

    questions = get_questions_v2_by_scope(
        khoi=khoi,
        chuong=chuong,
        bai=bai,
        expected_total=expected_total,
    )
    if questions is None:
        return None

    fields = (
        "id", "khoi", "chuong", "bai", "yccd", "muc_do", "dang_cau",
        "thanh_phan_nang_luc", "chi_bao", "trang_thai", "duoc_dung_luyen_hs",
    )
    out = []
    for q in questions:
        item = {field: q.get(field, "") for field in fields}
        item["id"] = str(item.get("id", "") or "")
        # Giữ pham_vi_hat_giong để menu cũ vẫn nhận diện câu đa phạm vi.
        if q.get("pham_vi_hat_giong"):
            item["pham_vi_hat_giong"] = copy.deepcopy(q.get("pham_vi_hat_giong"))
        out.append(item)

    _cache_set(key, out)
    return out



# ==========================================================
# ĐƯỜNG NHANH RIÊNG CHO LUYỆN TỐT NGHIỆP
# questions_v2 vẫn CHỈ là NGÂN HÀNG CHUNG.
# Chỉ lấy một tập ứng viên nhỏ Khối 12 để bổ sung tối đa vài câu
# cho đề tốt nghiệp, tránh tải cả kho chung khi HS vừa mở chế độ.
# ==========================================================
def get_grad_supplement_candidates_v2(khoi="Khối 12", limit_per_type=48):
    """Lấy ứng viên nhỏ từ NGÂN HÀNG CHUNG cho đề tốt nghiệp.

    - Không đọc/ghi Ngân hàng tốt nghiệp.
    - Ưu tiên lọc trực tiếp trên Supabase theo Khối + Dạng câu.
    - Chỉ khi deployment cũ không hỗ trợ lọc JSONB mới fallback về kho Khối 12.
    - Cache 5 phút vì đây chỉ là nguồn bổ sung, không cần tải lại ở mỗi rerun.
    """
    khoi = str(khoi or "Khối 12").strip() or "Khối 12"
    try:
        limit_per_type = max(8, min(int(limit_per_type or 48), 120))
    except Exception:
        limit_per_type = 48

    key = f"qv2_grad_candidates:{_norm_scope_value(khoi)}:{limit_per_type}"
    cached, ok = _cache_get(key, 300.0)
    if ok:
        return cached

    dang_list = [
        "Trắc nghiệm 4 lựa chọn",
        "Đúng / Sai",
        "Trả lời ngắn",
    ]
    client = _supabase_client()
    out = []

    if client is not None:
        direct_ok = True
        try:
            for dang in dang_list:
                rr = (
                    client.table("questions_v2")
                    .select("question_id,data")
                    .eq("data->>khoi", khoi)
                    .eq("data->>dang_cau", dang)
                    .range(0, limit_per_type - 1)
                    .execute()
                )
                rows = getattr(rr, "data", None) or []
                out.extend(_rows_to_questions_v2(rows))
        except Exception:
            direct_ok = False
            out = []

        if direct_ok and out:
            # Lọc thêm ở Python để tương thích dữ liệu cũ / chống record lẫn kho TN.
            result = []
            seen = set()
            for q in out:
                qid = str(q.get("id", "") or "").strip()
                if qid and qid in seen:
                    continue
                if qid:
                    seen.add(qid)
                if _question_is_grad_only(q):
                    continue
                if not _question_matches_scope(q, khoi=khoi):
                    continue
                if q.get("dang_cau") not in dang_list:
                    continue
                if q.get("trang_thai", "Đã duyệt") in {"Ngừng sử dụng", "Thiếu đáp án", "Cần GV xem"}:
                    continue
                if q.get("duoc_dung_luyen_hs", True) is False:
                    continue
                result.append(q)
            _cache_set(key, result)
            return result

    # Fallback an toàn cho Supabase/PostgREST cũ. Có thể nặng hơn nhưng chỉ xảy ra
    # khi direct JSONB filter không hoạt động.
    all_scope = get_questions_v2_by_scope(khoi=khoi, expected_total=None)
    if all_scope is None:
        return []

    result = []
    counts = {d: 0 for d in dang_list}
    for q in all_scope:
        dang = q.get("dang_cau")
        if dang not in counts or counts[dang] >= limit_per_type:
            continue
        if q.get("trang_thai", "Đã duyệt") in {"Ngừng sử dụng", "Thiếu đáp án", "Cần GV xem"}:
            continue
        if q.get("duoc_dung_luyen_hs", True) is False:
            continue
        result.append(q)
        counts[dang] += 1
        if all(counts[d] >= limit_per_type for d in dang_list):
            break

    _cache_set(key, result)
    return result
