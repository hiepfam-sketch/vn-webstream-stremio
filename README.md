# VN WebStream (Multi-Source) — Stremio Addon v2.0

Stremio Addon tổng hợp **nhiều nguồn phim Việt** phát trực tiếp HTTP (HLS `.m3u8`) ngay trên Stremio mà không cần trình duyệt, có **tự động lọc & loại trừ phim trùng lặp** và **chạy 24/7 trên Cloud từ GitHub** để không cần bật máy tính.

## 1. Nguồn phim tích hợp sẵn
| Nguồn | Địa chỉ mặc định | Kiểu luồng |
|---|---|---|
| **KKPhim** | `https://phimapi.com` | Direct HLS `.m3u8` (nhanh, ổn định) |
| **OPhim** | `https://ophim1.com` | Direct HLS `.m3u8` (có thể đổi domain khi cần) |
| **MotchillV** | `https://motchillv.online` | Direct HLS + CloudBeta HD (giải mã AES + bóc header PNG) + StreamC |
| **MotchillTi** | `https://motchillti.mx` | Direct HLS `.m3u8` + StreamC |

- **Lọc trùng lặp (Deduplication):** Tự động chuẩn hóa tên phim (bỏ dấu tiếng Việt, bỏ `(Vietsub)` / `(Thuyết Minh)` / năm) khi gộp danh mục & tìm kiếm từ nhiều nguồn — mỗi phim chỉ hiện **1 thẻ duy nhất** kèm danh sách các nguồn có phim đó.
- **Gộp đa nguồn khi phát:** Khi bấm vào 1 phim/tập, addon tự động tìm và gộp tất cả server phát từ cả KKPhim + OPhim + MotchillV + MotchillTi, loại bỏ link trùng và ưu tiên Direct HLS lên đầu.

---

## 2. Chạy 24/7 từ GitHub (không cần bật máy)

> Lưu ý: GitHub Pages chỉ host web tĩnh nên không chạy được backend Python/HLS Proxy. Bạn đẩy code lên GitHub rồi kết nối miễn phí với **Hugging Face Spaces** hoặc **Render.com** (tự động deploy mỗi khi bạn cập nhật GitHub).

### Cách A: Hugging Face Spaces (Khuyên dùng — Docker miễn phí 2 vCPU / 16GB RAM)
1. Tạo tài khoản tại https://huggingface.co → bấm **New Space**.
2. Chọn **Space SDK: Docker (Blank)**, đặt tên Space (vd: `vn-webstream`), chọn **Public** hoặc **Private**.
3. Upload toàn bộ file trong thư mục dự án (hoặc push từ GitHub lên Space).
4. Sau ~1 phút build xong, bạn sẽ có URL dạng `https://<username>-vn-webstream.hf.space`.
5. Mở `https://<username>-vn-webstream.hf.space/configure` → bấm **Cài đặt vào Stremio** (hoặc dán `/manifest.json` vào Stremio).

### Cách B: Render.com (Kết nối trực tiếp GitHub Repo)
1. Push toàn bộ thư mục dự án lên một GitHub repository của bạn.
2. Vào https://render.com → **New → Blueprint** (hoặc **Web Service**) → chọn GitHub repo vừa tạo (Render tự nhận diện [render.yaml](render.yaml) và [Dockerfile](Dockerfile)).
3. Chọn gói **Free** → bấm **Deploy**.
4. Sau khi deploy xong, dùng link `https://<ten-service>.onrender.com/manifest.json` để cài vào Stremio.

### Đổi tên miền định kỳ ngay trên GitHub (không cần deploy lại!)
1. Copy link **Raw** của file `domains.json` trên GitHub repo của bạn, ví dụ:
   `https://raw.githubusercontent.com/<user>/<repo>/main/domains.json`
2. Đặt biến môi trường `REMOTE_CONFIG_URL` trên Render / Hugging Face Spaces bằng link Raw đó (hoặc dán vào mục **Đồng bộ tên miền tự động từ GitHub** trên trang `/configure`).
3. Mỗi khi website nào đổi tên miền, bạn chỉ cần mở `domains.json` trực tiếp trên GitHub web (bằng điện thoại hoặc trình duyệt), sửa URL và bấm **Commit**. Server sẽ tự động cập nhật trong vòng 5 phút (hoặc ngay lập tức khi bấm **Đồng bộ ngay từ GitHub**).

---

## 3. Chạy thử trên máy tính Windows (Local)
1. Nhấp đúp `run.bat` (hoặc chạy `python app.py`).
2. Mở `http://127.0.0.1:7000/configure` để bật/tắt nguồn, kiểm tra kết nối, đổi tên miền hoặc cài vào Stremio (`http://127.0.0.1:7000/manifest.json`).
