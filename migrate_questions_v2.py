# -*- coding: utf-8 -*-
"""
Công cụ Streamlit riêng để sao chép Ngân hàng câu hỏi cũ -> questions_v2.
KHÔNG sửa app chính và KHÔNG xóa kho cũ.
Chạy: streamlit run migrate_questions_v2.py
"""

import os
import time
import streamlit as st

from data_store import configure_paths, _doc_document_shared
from question_store_v2 import table_status, sync_questions, compare_with_legacy

st.set_page_config(page_title="Trạm Sinh học - Di chuyển kho V2", page_icon="🧪", layout="wide")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BANK_PATH = os.path.join(BASE_DIR, "ngan_hang_cau_hoi.json")
STUDENT_PATH = os.path.join(BASE_DIR, "danh_sach_hoc_sinh.json")
HS_HISTORY_PATH = os.path.join(BASE_DIR, "lich_su_luyen_tap_hoc_sinh.json")
configure_paths(student_path=STUDENT_PATH, hs_history_path=HS_HISTORY_PATH)

st.title("🧪 GIAI ĐOẠN 4 — KHO CÂU HỎI V2 CHẠY SONG SONG")
st.warning(
    "Công cụ này chỉ SAO CHÉP dữ liệu sang bảng questions_v2. "
    "Không thay app chính, không xóa ngân hàng cũ và không đụng dữ liệu học sinh."
)

bank = _doc_document_shared(BANK_PATH, [])
if not isinstance(bank, list):
    st.error("Không đọc được Ngân hàng câu hỏi hiện tại.")
    st.stop()

c1, c2 = st.columns(2)
with c1:
    st.metric("Ngân hàng hiện tại", len(bank))

status = table_status()
with c2:
    if status.get("ok"):
        st.metric("questions_v2", int(status.get("count", 0)))
    else:
        st.metric("questions_v2", "Chưa sẵn sàng")

if not status.get("ok"):
    st.error("Chưa truy cập được bảng questions_v2.")
    st.code(status.get("error", ""))
    st.info(
        "Hãy mở Supabase → SQL Editor, chạy file setup_questions_v2.sql đúng 1 lần, "
        "sau đó Refresh trang này."
    )
    st.stop()

st.success("Bảng questions_v2 đã sẵn sàng. Kho cũ vẫn đang là kho chính của app.")

col_a, col_b = st.columns(2)
with col_a:
    if st.button("🔍 ĐỐI CHIẾU KHO CŨ ↔ V2", use_container_width=True):
        with st.spinner("Đang đối chiếu theo ID và fingerprint..."):
            kq = compare_with_legacy(bank)
        st.session_state["v2_compare"] = kq

with col_b:
    if st.button("🧪 SAO CHÉP THỬ 20 CÂU", use_container_width=True):
        progress = st.progress(0)
        t0 = time.perf_counter()

        def cb(done, total):
            progress.progress(1.0 if total <= 0 else min(1.0, done / total))

        try:
            kq = sync_questions(bank, limit=20, batch_size=20, progress_callback=cb)
            elapsed = time.perf_counter() - t0
            st.success(f"Đã sao chép thử {kq['written']} câu trong {elapsed:.1f} giây. Kho cũ không thay đổi.")
        except Exception as e:
            st.error(f"Sao chép thử thất bại: {e}")

kq_compare = st.session_state.get("v2_compare")
if kq_compare:
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Kho cũ", kq_compare.get("legacy_count", 0))
    c2.metric("V2", kq_compare.get("v2_count", 0))
    c3.metric("Thiếu", kq_compare.get("missing_count", 0))
    c4.metric("Dư", kq_compare.get("extra_count", 0))
    c5.metric("Khác nội dung", kq_compare.get("mismatch_count", 0))
    if kq_compare.get("matched"):
        st.success("✅ Hai kho khớp 100% theo ID + fingerprint.")
    else:
        st.info("Đây là bình thường nếu mới sao chép thử một phần. Chưa chuyển app sang V2.")

st.divider()
st.subheader("📦 Sao chép toàn bộ — chỉ thực hiện sau khi thử 20 câu thành công")
confirm = st.checkbox(
    "Tôi xác nhận đây chỉ là sao chép sang questions_v2; không xóa hoặc thay thế Ngân hàng hiện tại.",
    key="confirm_full_v2",
)

if st.button(
    "📤 SAO CHÉP TOÀN BỘ SANG V2",
    type="primary",
    disabled=not confirm,
    use_container_width=True,
):
    progress = st.progress(0)
    status_text = st.empty()
    t0 = time.perf_counter()

    def cb_full(done, total):
        progress.progress(1.0 if total <= 0 else min(1.0, done / total))
        status_text.write(f"Đã ghi {done}/{total} câu...")

    try:
        kq = sync_questions(bank, batch_size=50, progress_callback=cb_full)
        elapsed = time.perf_counter() - t0
        st.success(f"Đã sao chép {kq['written']}/{kq['requested']} câu trong {elapsed:.1f} giây.")
        with st.spinner("Đang đối chiếu sau sao chép..."):
            verify = compare_with_legacy(bank)
        st.session_state["v2_compare"] = verify
        if verify.get("matched"):
            st.success("🎯 XÁC MINH THÀNH CÔNG: kho V2 khớp 100%. App chính vẫn chưa chuyển sang V2.")
        else:
            st.error(
                "Chưa khớp 100%. KHÔNG chuyển app sang V2. "
                f"Thiếu {verify.get('missing_count',0)}, dư {verify.get('extra_count',0)}, "
                f"khác nội dung {verify.get('mismatch_count',0)}."
            )
    except Exception as e:
        st.error(f"Sao chép toàn bộ thất bại: {e}")
        st.info("Kho cũ vẫn nguyên vẹn; app chính vẫn tiếp tục dùng kho cũ.")

st.caption("Sau khi V2 khớp 100%, mới sang giai đoạn kế tiếp: thử đọc V2 bằng feature flag, vẫn có nút quay về kho cũ.")
