# epub-to-web — Full Automation Stack

Hệ thống tự động chuyển đổi EPUB → website đọc truyện tĩnh, hoạt động 100% trên dịch vụ **miễn phí**.

## Kiến trúc tổng quan

```
[Admin UI] → [Cloudflare Worker] → [GitHub Repo] → [CF Pages build] → [Reading Site]
  /admin/      (giữ GitHub token)    (lưu EPUB)       (chạy Python)      /slug/...
```

**Các dịch vụ dùng (đều free):**
| Dịch vụ | Vai trò | Free tier |
|---|---|---|
| GitHub (private repo) | Lưu EPUB + trigger build | Unlimited private repos |
| Cloudflare Pages | Build Python + host site | Unlimited bandwidth |
| Cloudflare Workers | API proxy (giữ secrets) | 100k req/ngày |

---

## Cấu trúc repo

```
your-repo/
├── epub_to_web.py         ← Python converter (chạy bởi CF Pages)
├── admin.html             ← Admin SPA (được copy vào output/admin/)
├── requirements.txt
├── .gitignore
├── books/                 ← EPUB files (commit vào đây)
│   └── .gitkeep
└── worker/
    ├── index.js           ← Cloudflare Worker source
    └── wrangler.toml      ← Worker config
```

---

## Setup từng bước

### Bước 1: Tạo GitHub repo (private)

```bash
git init my-epub-site
cd my-epub-site
# Copy tất cả files từ package này vào đây
mkdir books
touch books/.gitkeep
git add .
git commit -m "initial setup"
git remote add origin https://github.com/YOUR_USERNAME/YOUR_REPO.git
git push -u origin main
```

### Bước 2: Deploy Cloudflare Pages

1. Vào [Cloudflare Pages](https://pages.cloudflare.com/) → **Create project** → **Connect to Git**
2. Chọn repo vừa tạo
3. Cài đặt build:
   - **Build command:** `pip install -r requirements.txt && python epub_to_web.py`
   - **Build output directory:** `output`
   - **Root directory:** `/`
4. Click **Save and Deploy**

> Site của bạn sẽ live tại `https://your-project.pages.dev`

### Bước 3: Deploy Cloudflare Worker

```bash
cd worker
npm install -g wrangler         # Cài Wrangler CLI
wrangler login                  # Đăng nhập CF account

# Đặt secrets (thay YOUR_... bằng giá trị thực)
wrangler secret put ADMIN_SECRET
# → Nhập mật khẩu admin (ví dụ: mySecurePass123)

wrangler secret put GITHUB_TOKEN
# → Nhập GitHub Personal Access Token (scope: repo)
# Tạo tại: https://github.com/settings/tokens

wrangler secret put GITHUB_REPO
# → Nhập "username/repo-name"

# Deploy Worker
wrangler deploy
# → Worker URL: https://epub-to-web-api.YOUR_SUBDOMAIN.workers.dev
```

**Tùy chọn — hiển thị CF Pages deployment status:**
```bash
wrangler secret put CF_API_TOKEN      # CF API token (scope: Pages:Read)
wrangler secret put CF_ACCOUNT_ID     # Account ID từ CF dashboard
wrangler secret put CF_PAGES_PROJECT  # Tên CF Pages project
```

### Bước 4: Mở Admin UI

Truy cập `https://your-project.pages.dev/admin/`

Lần đầu mở sẽ hiện màn hình Setup:
1. Nhập **Worker URL** (từ bước 3)
2. Nhập **Admin Secret** (mật khẩu bạn đã đặt)
3. Click **Lưu & Kiểm tra kết nối**

---

## Workflow hàng ngày

### Thêm truyện mới (qua Admin UI)

1. Vào `/admin/` → tab **Tải lên EPUB**
2. Kéo thả file `.epub` vào drop zone
3. Click **Tải lên tất cả**
4. Pipeline tự động:
   - File được push lên GitHub → CF Pages trigger build → Python convert → deploy (~2-3 phút)
5. Truyện xuất hiện trên site và trong tab **Thư viện**

### Thêm truyện mới (manual qua git)

```bash
cp my-novel.epub books/
git add books/my-novel.epub
git commit -m "add: my-novel"
git push
```

### Xóa truyện

Admin UI → **Thư viện** → nút **Xóa** → xác nhận → tự động rebuild.

---

## Giới hạn cần biết

| Giới hạn | Giá trị | Ghi chú |
|---|---|---|
| File EPUB tối đa | ~25 MB | Giới hạn GitHub Contents API |
| CF Pages builds/tháng | 500 | Free tier |
| Worker requests/ngày | 100,000 | Thừa cho personal use |
| Build time | ~2-3 phút | Phụ thuộc số lượng chương |

---

## Cấu hình nâng cao

### Custom domain

CF Pages Dashboard → **Custom domains** → thêm domain của bạn. Miễn phí với CF nameservers.

### Nhiều người dùng admin

Hiện tại chỉ có 1 admin secret. Để multi-user, cần mở rộng Worker với JWT hoặc nhiều secrets.

### Tắt public admin (bảo mật)

Worker đã yêu cầu Bearer token cho tất cả requests, nhưng file `admin/index.html` vẫn public. 
Để ẩn, thêm CF Pages rule redirect `/admin/*` về 404, hoặc dùng CF Access (có free tier).

