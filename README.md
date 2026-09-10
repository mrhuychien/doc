# Feed Tri Thức — Phase 1

Feed đọc sách cá nhân, **có đáy**: mỗi ngày N ý trọn vẹn theo đúng thứ tự sách + ôn lại D+1 / D+7 / D+30. Hết là hết. Không like, không counter, không badge đỏ, không nút "đọc thêm".

Single-user. Thí nghiệm 60 ngày trên 1 người đọc, 1 cuốn sách.

```
TanStack Start · React 19 · Tailwind v4 · Supabase (Postgres + Auth + RLS) · Python 3.12 ingest
```

---

## 1. Chạy app

```bash
npm install
cp .env.example .env.local        # điền VITE_SUPABASE_URL + VITE_SUPABASE_ANON_KEY
npm run dev                       # → http://localhost:3000
```

Đăng nhập bằng magic link ở `/login`. Email phải có sẵn trong bảng `allowed_user` (chữ thường), nếu không RLS chặn sạch mọi query.

## 2. Dựng Supabase

1. Tạo project trên [supabase.com](https://supabase.com).
2. Chạy `supabase/migrations/001_init.sql` trong SQL Editor.
3. Thêm email của bạn:
   ```sql
   insert into allowed_user (email) values ('ban@example.com');
   ```
4. Auth → URL Configuration → thêm `http://localhost:3000` và domain Vercel vào Redirect URLs.

## 3. Nạp 1 cuốn sách mới

Chuẩn bị: một file **PDF có text layer** (không phải bản scan — pipeline không OCR).

### Cách A — Ingest Studio (có giao diện)

```bash
ingest/.venv/bin/python ingest/studio.py     # → http://127.0.0.1:8765
```

Kéo thả PDF vào, chọn khoảng trang (có xem trước nội dung từng trang để biết mục lục nằm ở đâu), rồi bấm qua 4 bước: parse → duyệt GATE A → chạy thử 5 chunk → duyệt GATE B → enrich toàn bộ → nạp Supabase. Sửa `idea_summary` và 3 câu hỏi ngay trong bảng, không cần mở Excel.

Studio **chạy trên máy bạn, không deploy**: nó đọc `.env.local` (có service role key và Anthropic key) và chạy lệnh trên máy — chỉ bind `127.0.0.1`, đừng bao giờ expose ra ngoài. Nó chỉ gọi ba script dưới qua subprocess, không tự parse và không tự gọi LLM.

### Cách B — 3 lệnh CLI

```bash
cd ingest && source .venv/bin/activate     # lần đầu: python3.12 -m venv .venv && pip install -r requirements.txt

python parse_pdf.py  books/<slug>/source.pdf      # → chunks.json + parse_report.md + frontmatter.txt
python enrich.py     books/<slug>/chunks.json     # → enriched.json + review.csv
python load.py       books/<slug>/enriched.json   # → Supabase (idempotent theo (book_id, seq))
```

Giữa bước 1 và 2: **đọc `parse_report.md`**. Giữa bước 2 và 3: chạy `enrich.py --dry-run` xem thử 5 chunk, rồi mở `review.csv` bằng Excel sửa tay câu nào chướng — `load.py` ưu tiên giá trị trong CSV.

Hai cách dùng chung một pipeline và cùng bộ file. Mỗi bước ghi ra đúng 1 file, chạy lại được độc lập. `enrich.py` cache theo `sha1(text)` trong `ingest/.cache/` nên chạy lại không tốn thêm tiền API; sửa `prompts/*.md` thì cache tự hỏng.

### Cấu hình cho từng cuốn

`ingest/books/<slug>/book.yaml` khai báo phạm vi trang và kỳ vọng cấu trúc (mục lục, số chương/mục) — Studio sinh file này từ form, hoặc bạn viết tay. `overrides.yaml` (tuỳ chọn) ghim thủ công những mục không có heading trong thân sách.

**Đừng bỏ trống phần `expect:`.** Đó là lưới an toàn: `parse_pdf.py` fail loud khi số chương/mục bắt được không khớp mục lục, thay vì im lặng nạp một cuốn sách thiếu chương.

---

## 4. Gotcha khi parse PDF — đọc trước khi debug

**PyMuPDF cho ranh giới đoạn, pdfplumber cho chữ. Dùng cả hai, đúng việc của từng cái.**

| | PyMuPDF (`fitz`) | pdfplumber |
|---|---|---|
| Ranh giới đoạn | ✅ `page.get_text("blocks")` — mỗi đoạn 1 block, heading là block riêng, số trang là block riêng ở đáy | ❌ mất |
| Tách chữ tiếng Việt | ❌ **rớt khoảng trắng**: `"đầy đủsựbảo đảm"`, `"ởcấp cơ sở"`, `"tựbiện hộ"` | ✅ đúng |

Cách làm trong `parse_pdf.py`: lấy bbox từng block bằng PyMuPDF → `pdfplumber.page.crop((x0, y0, x1, y1)).extract_text()` cho phần chữ. Cả hai thư viện dùng chung hệ toạ độ gốc trên-trái, đơn vị pt, nên bbox dùng thẳng được, chỉ nới 1pt mỗi phía.

Sanity check tự động: `word_count(pdfplumber)` phải nằm trong ±10% `word_count(PyMuPDF)`. Lệch hơn → nới bbox 2pt, vẫn lệch → ghi vào `parse_report.md` để soi tay.

**Không bao giờ** lấy text của PyMuPDF làm `chunk.text`. Trường `chunk.text` là văn bản sách nguyên văn — chỉ được normalize Unicode (NFC), thay NBSP, gộp khoảng trắng, nối dòng trong đoạn bằng 1 space. Không tóm tắt, không sửa chính tả, không "làm sạch".

---

## 5. Cấu trúc

```
app/                      # TanStack Start (srcDirectory = "app")
  routes/index.tsx        # Feed hôm nay — màn hình duy nhất
  routes/login.tsx        # Magic link
  components/             # ContextCard · ChunkCard · ReviewCard · EndOfDayCard · SettingsSheet
  lib/plan.ts             # buildDailyPlan(today)
  lib/schedule.ts         # onChunkRead / onReviewGraded — D+1/7/30
  lib/tracking.ts         # reading_event qua IntersectionObserver
  styles/app.css          # Tailwind v4 @theme tokens
ingest/                   # parse_pdf.py · enrich.py · load.py · prompts/
  studio.py + studio.html # Ingest Studio — UI nạp sách, chạy local, KHÔNG deploy
supabase/migrations/      # 001_init.sql — 7 bảng + RLS + seed
```

## 6. Deploy

Push GitHub → import vào Vercel (framework tự nhận `tanstack-start` qua `vercel.json`) → set env `VITE_SUPABASE_URL`, `VITE_SUPABASE_ANON_KEY`.

Ingest và Ingest Studio **chỉ chạy local** — service role key (bypass sạch RLS) và Anthropic key không bao giờ lên Vercel. App deploy chỉ có đúng 2 route: `/` và `/login`.
