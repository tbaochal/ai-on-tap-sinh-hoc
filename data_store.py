# -*- coding: utf-8 -*-
"""
Tầng dữ liệu dùng chung của TRẠM SINH HỌC — Giai đoạn 2 (cache an toàn, không đổi dữ liệu).

Mục tiêu của giai đoạn này:
- GIỮ tầng dữ liệu đã tách và thêm cache RAM ngắn hạn để giảm đọc lặp.
- GIỮ NGUYÊN cơ chế và schema dữ liệu hiện có; KHÔNG di chuyển/xóa dữ liệu.
- JSON local vẫn là bản dự phòng như bản gốc.
- Supabase vẫn dùng các bảng app_documents / students / student_attempts như bản gốc.

Không đổi tên document_key, không đổi cấu trúc dữ liệu và không tự chạy migration.
"""

import json
import os
import hashlib
import uuid
import copy
import time
import threading
from datetime import datetime, timezone

import streamlit as st

try:
    from supabase import create_client
except Exception:
    create_client = None


# ------------------------------------------------------------------
# Cấu hình đường dẫn được app.py truyền vào sau khi xác định BASE_DIR.
# ------------------------------------------------------------------
_STUDENT_PATH = ""
_HS_HISTORY_PATH = ""


def configure_paths(student_path="", hs_history_path=""):
    global _STUDENT_PATH, _HS_HISTORY_PATH
    _STUDENT_PATH = str(student_path or "")
    _HS_HISTORY_PATH = str(hs_history_path or "")


# ------------------------------------------------------------------
# CACHE NHẸ TRONG BỘ NHỚ — Giai đoạn 2
# Không đổi schema, không đổi nơi lưu. Chỉ tránh đọc Supabase lặp lại
# trong các lần Streamlit rerun liên tiếp. Mọi hàm ghi đều xóa cache ngay.
# ------------------------------------------------------------------
_CACHE_LOCK = threading.RLock()
_CACHE = {}
_DOCUMENT_CACHE_TTL = 60.0
_STUDENT_CACHE_TTL = 20.0
_ATTEMPT_CACHE_TTL = 15.0


def _cache_get(key, ttl):
    now = time.monotonic()
    with _CACHE_LOCK:
        item = _CACHE.get(key)
        if not item:
            return None, False
        ts, value = item
        if now - ts > float(ttl):
            _CACHE.pop(key, None)
            return None, False
        # Trả bản sao để code giao diện không vô tình sửa dữ liệu đang cache.
        return copy.deepcopy(value), True


def _cache_set(key, value):
    with _CACHE_LOCK:
        _CACHE[key] = (time.monotonic(), copy.deepcopy(value))


def _cache_invalidate(*keys):
    with _CACHE_LOCK:
        if not keys:
            _CACHE.clear()
            return
        for key in keys:
            _CACHE.pop(key, None)


def clear_runtime_cache():
    """Xóa cache RAM; không xóa dữ liệu local/Supabase."""
    _cache_invalidate()


# ------------------------------------------------------------------
# SUPABASE CLIENT
# ------------------------------------------------------------------
_SUPABASE_CLIENT = None
_SUPABASE_TRIED = False


def _supabase_client():
    global _SUPABASE_CLIENT, _SUPABASE_TRIED

    if _SUPABASE_TRIED:
        return _SUPABASE_CLIENT

    _SUPABASE_TRIED = True

    if create_client is None:
        return None

    try:
        url = str(st.secrets.get("SUPABASE_URL", "") or "").strip()
        key = str(st.secrets.get("SUPABASE_SECRET_KEY", "") or "").strip()
        if not url or not key:
            return None
        _SUPABASE_CLIENT = create_client(url, key)
    except Exception:
        _SUPABASE_CLIENT = None

    return _SUPABASE_CLIENT


# ------------------------------------------------------------------
# JSON LOCAL
# ------------------------------------------------------------------
def _doc_json_local(path, default=None):
    try:
        if not os.path.exists(path) or os.path.getsize(path) == 0:
            return default
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _luu_json_local(path, data):
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


def _document_key(path):
    return os.path.basename(str(path or "")).strip()


# ------------------------------------------------------------------
# DOCUMENT SHARED / CHUNK — giữ nguyên cơ chế bản gốc
# ------------------------------------------------------------------
_CLOUD_CHUNK_MARKER = "__tram_sinh_hoc_chunked__"
_CLOUD_CHUNK_TARGET_BYTES = 1_500_000
_SHARED_WRITE_ERRORS = {}


def _pending_sync_path(path):
    return str(path) + ".pending_cloud_sync"


def _mark_pending_sync(path, error_text=""):
    try:
        with open(_pending_sync_path(path), "w", encoding="utf-8") as f:
            f.write(str(error_text or "Cloud write failed"))
    except Exception:
        pass


def _clear_pending_sync(path):
    try:
        pp = _pending_sync_path(path)
        if os.path.exists(pp):
            os.remove(pp)
    except Exception:
        pass


def _co_pending_sync(path):
    try:
        return os.path.exists(_pending_sync_path(path))
    except Exception:
        return False


def _chia_list_theo_dung_luong(data, target_bytes=_CLOUD_CHUNK_TARGET_BYTES):
    chunks = []
    cur = []
    cur_size = 2
    for item in list(data or []):
        try:
            item_size = len(json.dumps(item, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")) + 1
        except Exception:
            item_size = 4096
        if cur and cur_size + item_size > target_bytes:
            chunks.append(cur)
            cur = []
            cur_size = 2
        cur.append(item)
        cur_size += item_size
    if cur or not chunks:
        chunks.append(cur)
    return chunks


def _doc_cloud_document(client_sb, key):
    """Đọc đúng bản cloud; hỗ trợ cả kiểu cũ một-row và kiểu chunk mới."""
    res = (
        client_sb.table("app_documents")
        .select("document_key,data,updated_at")
        .eq("document_key", key)
        .limit(1)
        .execute()
    )
    rows = getattr(res, "data", None) or []
    if not rows:
        return None
    root = rows[0]
    data = root.get("data")
    if not (isinstance(data, dict) and data.get(_CLOUD_CHUNK_MARKER)):
        return data

    n = int(data.get("chunk_count", 0) or 0)
    expected_count = int(data.get("item_count", 0) or 0)
    expected_sha = str(data.get("sha256", "") or "")
    if n <= 0:
        return []

    keys = [f"{key}::chunk::{i:05d}" for i in range(n)]
    found = {}
    try:
        for start in range(0, len(keys), 60):
            batch = keys[start:start + 60]
            rr = (
                client_sb.table("app_documents")
                .select("document_key,data")
                .in_("document_key", batch)
                .execute()
            )
            for row in (getattr(rr, "data", None) or []):
                found[str(row.get("document_key", ""))] = row.get("data")
    except Exception:
        found = {}
        for ck in keys:
            rr = (
                client_sb.table("app_documents")
                .select("document_key,data")
                .eq("document_key", ck)
                .limit(1)
                .execute()
            )
            rr_rows = getattr(rr, "data", None) or []
            if rr_rows:
                found[ck] = rr_rows[0].get("data")

    out = []
    for ck in keys:
        payload = found.get(ck)
        if isinstance(payload, dict) and isinstance(payload.get("items"), list):
            out.extend(payload.get("items") or [])
        elif isinstance(payload, list):
            out.extend(payload)
        else:
            raise RuntimeError(f"Thiếu chunk dữ liệu: {ck}")

    if expected_count and len(out) != expected_count:
        raise RuntimeError(
            f"Dữ liệu cloud chưa đầy đủ: cần {expected_count} mục, đọc được {len(out)} mục."
        )
    if expected_sha:
        raw = json.dumps(out, ensure_ascii=False, separators=(",", ":"), sort_keys=True, default=str)
        got = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        if got != expected_sha:
            raise RuntimeError("Checksum dữ liệu cloud không khớp.")
    return out


def _doc_document_shared(path, default=None):
    """Đọc Supabase/local như cũ, nhưng cache ngắn để tránh tải lặp khi rerun."""
    local_data = _doc_json_local(path, default)

    # Nếu app vừa ghi cloud thất bại, ưu tiên local mới nhất và không dùng cache cũ.
    if _co_pending_sync(path) and local_data is not None:
        return local_data

    key = _document_key(path)
    cache_key = ("document", key)
    cached, ok = _cache_get(cache_key, _DOCUMENT_CACHE_TTL)
    if ok:
        return cached

    client_sb = _supabase_client()
    if client_sb is not None and key:
        try:
            data = _doc_cloud_document(client_sb, key)
            if data is not None:
                _cache_set(cache_key, data)
                return copy.deepcopy(data)
        except Exception as e:
            _SHARED_WRITE_ERRORS[key] = f"Lỗi đọc cloud: {e}"

    # Cache cả fallback local trong thời gian ngắn để tránh đọc file liên tục.
    _cache_set(cache_key, local_data)
    return copy.deepcopy(local_data)

def document_count_shared(path, default=0):
    """Đếm nhanh số phần tử của document mà không tải các chunk lớn.

    Dùng cho badge/sidebar. Không thay dữ liệu và không thay schema.
    """
    key = _document_key(path)
    cache_key = ("document_count", key)
    cached, ok = _cache_get(cache_key, _DOCUMENT_CACHE_TTL)
    if ok:
        try:
            return int(cached)
        except Exception:
            pass

    # Nếu có lần ghi cloud đang pending, local là bản mới hơn.
    if _co_pending_sync(path):
        local_data = _doc_json_local(path, None)
        count = len(local_data) if isinstance(local_data, (list, dict)) else int(default or 0)
        _cache_set(cache_key, count)
        return count

    client_sb = _supabase_client()
    if client_sb is not None and key:
        try:
            rr = (
                client_sb.table("app_documents")
                .select("data")
                .eq("document_key", key)
                .limit(1)
                .execute()
            )
            rows = getattr(rr, "data", None) or []
            if rows:
                data = rows[0].get("data")
                if isinstance(data, dict) and data.get(_CLOUD_CHUNK_MARKER):
                    count = int(data.get("item_count", 0) or 0)
                elif isinstance(data, (list, dict)):
                    count = len(data)
                else:
                    count = int(default or 0)
                _cache_set(cache_key, count)
                return count
        except Exception:
            pass

    local_data = _doc_json_local(path, None)
    count = len(local_data) if isinstance(local_data, (list, dict)) else int(default or 0)
    _cache_set(cache_key, count)
    return count


def _luu_document_shared(path, data):
    """Ghi local + Supabase có kiểm chứng; danh sách lớn được tách chunk."""
    key = _document_key(path)
    _cache_invalidate(("document", key), ("document_count", key))
    local_ok = _luu_json_local(path, data)
    client_sb = _supabase_client()

    if client_sb is None or not key:
        return local_ok

    try:
        now_iso = datetime.now(timezone.utc).isoformat()
        raw = json.dumps(data, ensure_ascii=False, separators=(",", ":"), sort_keys=True, default=str)
        raw_bytes = raw.encode("utf-8")

        if isinstance(data, list) and len(raw_bytes) > _CLOUD_CHUNK_TARGET_BYTES:
            chunks = _chia_list_theo_dung_luong(data)
            sha = hashlib.sha256(raw_bytes).hexdigest()

            for i, chunk in enumerate(chunks):
                ck = f"{key}::chunk::{i:05d}"
                client_sb.table("app_documents").upsert(
                    {
                        "document_key": ck,
                        "data": {"items": chunk},
                        "updated_at": now_iso,
                    },
                    on_conflict="document_key",
                ).execute()

            manifest = {
                _CLOUD_CHUNK_MARKER: True,
                "version": 1,
                "chunk_count": len(chunks),
                "item_count": len(data),
                "sha256": sha,
            }
            client_sb.table("app_documents").upsert(
                {
                    "document_key": key,
                    "data": manifest,
                    "updated_at": now_iso,
                },
                on_conflict="document_key",
            ).execute()
        else:
            client_sb.table("app_documents").upsert(
                {
                    "document_key": key,
                    "data": data,
                    "updated_at": now_iso,
                },
                on_conflict="document_key",
            ).execute()

        if isinstance(data, list) and len(raw_bytes) > _CLOUD_CHUNK_TARGET_BYTES:
            rr = (
                client_sb.table("app_documents")
                .select("document_key,data")
                .eq("document_key", key)
                .limit(1)
                .execute()
            )
            rows = getattr(rr, "data", None) or []
            if not rows:
                raise RuntimeError("Xác minh sau ghi thất bại: không đọc được manifest cloud.")
            manifest_verify = rows[0].get("data") or {}
            if not (
                isinstance(manifest_verify, dict)
                and manifest_verify.get(_CLOUD_CHUNK_MARKER)
                and int(manifest_verify.get("item_count", -1)) == len(data)
                and str(manifest_verify.get("sha256", "")) == sha
            ):
                raise RuntimeError("Xác minh sau ghi thất bại: manifest cloud không khớp.")
        else:
            verify = _doc_cloud_document(client_sb, key)
            if isinstance(data, list):
                if not isinstance(verify, list) or len(verify) != len(data):
                    raise RuntimeError(
                        f"Xác minh sau ghi thất bại: local {len(data)} mục, cloud {len(verify) if isinstance(verify, list) else 'không phải danh sách'} mục."
                    )
            elif verify != data:
                raise RuntimeError("Xác minh sau ghi thất bại: dữ liệu cloud khác dữ liệu vừa lưu.")

        _SHARED_WRITE_ERRORS.pop(key, None)
        _clear_pending_sync(path)
        _cache_set(("document", key), data)
        if isinstance(data, (list, dict)):
            _cache_set(("document_count", key), len(data))
        return True

    except Exception as e:
        _cache_invalidate(("document", key), ("document_count", key))
        _SHARED_WRITE_ERRORS[key] = str(e)
        _mark_pending_sync(path, e)
        return False


# ------------------------------------------------------------------
# STUDENTS — giữ nguyên schema bảng students
# ------------------------------------------------------------------
def _doc_students_shared():
    cache_key = ("students", "all")
    cached, ok = _cache_get(cache_key, _STUDENT_CACHE_TTL)
    if ok:
        return cached

    client_sb = _supabase_client()
    if client_sb is not None:
        try:
            res = (
                client_sb.table("students")
                .select("student_id,full_name,class_name,active,data")
                .order("student_id")
                .execute()
            )
            rows = getattr(res, "data", None) or []
            if rows:
                ds = []
                for row in rows:
                    item = dict(row.get("data") or {})
                    item.setdefault("ma_hoc_sinh", row.get("student_id", ""))
                    item.setdefault("ho_ten", row.get("full_name", ""))
                    item.setdefault("lop", row.get("class_name", ""))
                    if not str(item.get("trang_thai", "")).strip():
                        item["trang_thai"] = (
                            "Đang học" if row.get("active", True) else "Tạm khóa"
                        )
                    ds.append(item)
                _cache_set(cache_key, ds)
                return copy.deepcopy(ds)
        except Exception:
            pass

    data = _doc_json_local(_STUDENT_PATH, [])
    data = data if isinstance(data, list) else []
    _cache_set(cache_key, data)
    return copy.deepcopy(data)

def _luu_students_shared(ds):
    _cache_invalidate(("students", "all"))
    ds = list(ds or [])
    local_ok = _luu_json_local(_STUDENT_PATH, ds)
    cloud_ok = False
    client_sb = _supabase_client()

    if client_sb is not None:
        try:
            rows = []
            now_iso = datetime.now(timezone.utc).isoformat()
            for hs in ds:
                if not isinstance(hs, dict):
                    continue
                sid = str(
                    hs.get("ma_hoc_sinh")
                    or hs.get("hoc_sinh_id")
                    or hs.get("student_id")
                    or ""
                ).strip()
                if not sid:
                    continue
                trang_thai = str(hs.get("trang_thai", "Đang học") or "").strip().casefold()
                active = trang_thai not in {
                    "tạm khóa", "tam khoa", "nghỉ học", "nghi hoc",
                    "đã nghỉ", "da nghi", "inactive"
                }
                rows.append({
                    "student_id": sid,
                    "full_name": str(hs.get("ho_ten", "") or "").strip(),
                    "class_name": str(hs.get("lop", "") or "").strip(),
                    "active": active,
                    "data": hs,
                    "updated_at": now_iso,
                })

            for i in range(0, len(rows), 200):
                batch = rows[i:i + 200]
                if batch:
                    client_sb.table("students").upsert(
                        batch, on_conflict="student_id"
                    ).execute()
            cloud_ok = True
        except Exception:
            cloud_ok = False

    ok = cloud_ok or local_ok
    if ok:
        _cache_set(("students", "all"), ds)
    return ok


def _xoa_student_shared(student_id):
    """Chỉ xóa hồ sơ trong bảng students; lịch sử làm bài được giữ nguyên."""
    sid = str(student_id or "").strip()
    if not sid:
        return False
    client_sb = _supabase_client()
    if client_sb is None:
        return False
    try:
        client_sb.table("students").delete().eq("student_id", sid).execute()
        _cache_invalidate(("students", "all"))
        return True
    except Exception:
        return False


# ------------------------------------------------------------------
# STUDENT ATTEMPTS — giữ nguyên schema bảng student_attempts
# ------------------------------------------------------------------
def _doc_attempts_shared():
    cache_key = ("attempts", "all")
    cached, ok = _cache_get(cache_key, _ATTEMPT_CACHE_TTL)
    if ok:
        return cached

    client_sb = _supabase_client()
    if client_sb is not None:
        try:
            res = (
                client_sb.table("student_attempts")
                .select("id,submitted_at,data")
                .order("submitted_at")
                .execute()
            )
            rows = getattr(res, "data", None) or []
            if rows:
                ds = []
                for row in rows:
                    item = dict(row.get("data") or {})
                    if not str(item.get("id", "")).strip():
                        item["id"] = str(row.get("id", "") or "").strip()
                    if not str(item.get("thoi_gian_iso", "")).strip():
                        item["thoi_gian_iso"] = str(row.get("submitted_at", "") or "").strip()
                    ds.append(item)
                _cache_set(cache_key, ds)
                return copy.deepcopy(ds)
        except Exception:
            pass

    data = _doc_json_local(_HS_HISTORY_PATH, [])
    data = data if isinstance(data, list) else []
    _cache_set(cache_key, data)
    return copy.deepcopy(data)

def _attempt_uuid(value, item):
    try:
        return str(uuid.UUID(str(value)))
    except Exception:
        raw = json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
        return str(uuid.uuid5(uuid.NAMESPACE_URL, raw))


def _luu_attempts_shared(ds):
    """Upsert từng lượt làm bài, không xóa lượt của học sinh khác."""
    _cache_invalidate(("attempts", "all"))
    ds = list(ds or [])
    local_ok = _luu_json_local(_HS_HISTORY_PATH, ds)
    cloud_ok = False
    client_sb = _supabase_client()

    if client_sb is not None:
        try:
            rows = []
            for lan in ds:
                if not isinstance(lan, dict):
                    continue
                sid = str(
                    lan.get("hoc_sinh_id")
                    or lan.get("ma_hoc_sinh")
                    or ""
                ).strip()
                if not sid:
                    continue

                row_id = _attempt_uuid(lan.get("id"), lan)
                lan = dict(lan)
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

                rows.append({
                    "id": row_id,
                    "student_id": sid,
                    "class_name": str(
                        lan.get("lop") or pham_vi.get("lop") or ""
                    ).strip(),
                    "mode": str(lan.get("che_do", "") or "").strip(),
                    "exam_id": str(
                        pham_vi.get("de_id") or pham_vi.get("mau_id") or ""
                    ).strip() or None,
                    "test_session_id": str(
                        pham_vi.get("dot_kiem_tra_id") or ""
                    ).strip() or None,
                    "submitted_at": submitted_at,
                    "score": score,
                    "score_scale": score_scale,
                    "data": lan,
                })

            for i in range(0, len(rows), 100):
                batch = rows[i:i + 100]
                if batch:
                    client_sb.table("student_attempts").upsert(
                        batch, on_conflict="id"
                    ).execute()
            cloud_ok = True
        except Exception:
            cloud_ok = False

    ok = cloud_ok or local_ok
    if ok:
        _cache_set(("attempts", "all"), ds)
    return ok


# ------------------------------------------------------------------
# CHẨN ĐOÁN CHỈ ĐỌC — không thay đổi dữ liệu
# ------------------------------------------------------------------
def storage_health(document_paths=None):
    """Trả trạng thái kết nối/dữ liệu để kiểm tra, tuyệt đối không ghi/xóa."""
    result = {
        "supabase_connected": False,
        "documents": {},
        "students": {"ok": False, "count": None, "error": ""},
        "attempts": {"ok": False, "count": None, "error": ""},
    }
    client_sb = _supabase_client()
    result["supabase_connected"] = client_sb is not None
    if client_sb is None:
        return result

    for path in list(document_paths or []):
        key = _document_key(path)
        try:
            data = _doc_cloud_document(client_sb, key)
            result["documents"][key] = {
                "ok": True,
                "type": type(data).__name__,
                "count": len(data) if isinstance(data, (list, dict)) else None,
                "error": "",
            }
        except Exception as e:
            result["documents"][key] = {"ok": False, "count": None, "error": str(e)}

    try:
        rr = client_sb.table("students").select("student_id", count="exact").execute()
        result["students"] = {"ok": True, "count": getattr(rr, "count", None), "error": ""}
    except Exception as e:
        result["students"]["error"] = str(e)

    try:
        rr = client_sb.table("student_attempts").select("id", count="exact").execute()
        result["attempts"] = {"ok": True, "count": getattr(rr, "count", None), "error": ""}
    except Exception as e:
        result["attempts"]["error"] = str(e)

    return result
