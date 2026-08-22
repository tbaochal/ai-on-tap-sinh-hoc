# ==========================================================
# QUY TẮC NHẬN DIỆN MỨC ĐỘ NHẬN THỨC
# ==========================================================

DONG_TU_MUC_DO = {

    "Nhận biết": [
        "nhận biết",
        "kể tên",
        "phát biểu",
        "nêu",
        "trình bày",
        "xác định",
        "mô tả"
    ],

    "Thông hiểu": [
        "phân loại",
        "phân biệt",
        "phân tích",
        "so sánh",
        "lựa chọn",
        "giải thích",
        "kết nối thông tin",
        "nhận ra điểm sai",
        "chỉnh sửa",
        "thảo luận",
        "đưa ra nhận định"
    ],

    "Vận dụng": [
        "vận dụng",
        "giải thích vấn đề thực tiễn",
        "giải thích mô hình công nghệ",
        "đưa ra bằng chứng",
        "phản biện",
        "đánh giá",
        "đề xuất giải pháp",
        "đưa ra giải pháp",
        "thực hiện giải pháp",
        "xử lí tình huống",
        "giải quyết vấn đề"
    ]
}


# ==========================================================
# HÀM NHẬN DIỆN MỨC ĐỘ TỪ YCCĐ
# ==========================================================
def xac_dinh_muc_do(yccd):

    if not yccd:
        return "Nhận biết"

    noi_dung = yccd.lower().strip()

    # ======================================================
    # ƯU TIÊN KIỂM TRA VẬN DỤNG TRƯỚC
    # vì nhiều cụm có thể chứa các động từ như "giải thích"
    # ======================================================
    for dong_tu in DONG_TU_MUC_DO["Vận dụng"]:

        if dong_tu in noi_dung:
            return "Vận dụng"

    # ======================================================
    # THÔNG HIỂU
    # ======================================================
    for dong_tu in DONG_TU_MUC_DO["Thông hiểu"]:

        if dong_tu in noi_dung:
            return "Thông hiểu"

    # ======================================================
    # NHẬN BIẾT
    # ======================================================
    for dong_tu in DONG_TU_MUC_DO["Nhận biết"]:

        if dong_tu in noi_dung:
            return "Nhận biết"

    # ======================================================
    # MẶC ĐỊNH
    # ======================================================
    return "Nhận biết"