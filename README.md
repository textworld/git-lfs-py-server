# Git LFS Server (FastAPI + Qiniu)

一个轻量的 Git LFS server，使用 FastAPI 实现，把对象存到七牛云对象存储。

## 特性

- ✅ 完整实现 [Git LFS Batch API v1](https://github.com/git-lfs/git-lfs/blob/main/docs/api/batch.md)
- ✅ 对象存到七牛云（兼容私有/公开 bucket）
- ✅ HTTP Basic Auth（Git 客户端 URL 嵌入凭据）
- ✅ 流式上传/下载（不把大文件装进内存）
- ✅ 上传时校验 SHA256，防止传输损坏
- ✅ 已上传对象自动跳过（Git 客户端 `batch` 接口会无 actions）
- ✅ 本地 SQLite 记录元数据，加速查询
- ✅ CORS、详细错误处理、健康检查

## 项目结构

```
git-lfs-py-server/
├── app/
│   ├── __init__.py
│   ├── main.py           # FastAPI app
│   ├── config.py         # pydantic-settings 配置
│   ├── auth.py           # HTTP Basic Auth
│   ├── models.py         # Git LFS Pydantic 模型
│   ├── metadata.py       # SQLite 元数据
│   ├── qiniu_client.py   # 七牛云封装
│   └── lfs_routes.py     # LFS 路由
├── tests/
│   └── test_smoke.py     # 19 个 smoke test
├── pyproject.toml        # 项目元数据 + 依赖
├── uv.lock               # 依赖锁文件（提交到仓库）
├── Makefile              # make install / dev / test / ...
├── .env.example
├── Dockerfile
├── run.py
└── README.md
```

## 快速开始

本项目使用 [uv](https://github.com/astral-sh/uv) 管理依赖。
请先 `brew install uv` 或 `pip install uv`。

### 1. 安装依赖

```bash
uv sync                # 生产依赖 + 开发依赖（httpx）
# 或只装生产依赖：
uv sync --no-group dev
```

这会在本地创建 `.venv/` 并安装 `pyproject.toml` 里声明的所有依赖。

### 2. 配置七牛云

1. 登录 [七牛云](https://portal.qiniu.com/) → 对象存储 → 创建 bucket
2. 控制台 → 个人中心 → 密钥管理，拿到 `AccessKey` / `SecretKey`
3. 域名管理：拿到 bucket 域名（如 `mybucket.qiniucdn.com`）

### 3. 复制环境变量

```bash
cp .env.example .env
# 编辑 .env 填入七牛云配置 + LFS 凭据
```

```dotenv
QINIU_ACCESS_KEY=xxxxxxxxxxxx
QINIU_SECRET_KEY=yyyyyyyyyyyy
QINIU_BUCKET=my-git-lfs
QINIU_BUCKET_DOMAIN=my-git-lfs.qiniucdn.com
QINIU_BUCKET_PRIVATE=true
LFS_USER=lfs
LFS_PASSWORD=强密码
SERVER_BASE_URL=https://lfs.example.com
```

### 4. 启动服务

```bash
# 方式 1：makefile
make dev          # 开发模式（热重载）
make start        # 生产模式
make test         # 跑 smoke test

# 方式 2：uv run
uv run uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
uv run python run.py
uv run python -m tests.test_smoke

# 方式 3：直接用 venv 的 python
source .venv/bin/activate
python run.py
```

Docker：

```bash
docker build -t git-lfs-py-server .
docker run -d --name git-lfs -p 8000:8000 --env-file .env -v $(pwd)/data:/data git-lfs-py-server
```

### 5. 在客户端仓库配置

每个需要用 LFS 的仓库，编辑 `.gitconfig` 或运行：

```bash
git config lfs.url https://lfs_user:lfs_password@lfs.example.com/api/v1
```

或者一次性全局配置：

```bash
git config --global lfs.url https://lfs_user:lfs_password@lfs.example.com/api/v1
```

### 6. 使用

```bash
git lfs install                       # 在仓库里初始化 LFS
git lfs track "*.psd"                 # 跟踪大文件
git add .gitattributes
git add files.psd
git commit -m "add large files"
git push origin main                   # 大文件走 LFS
```

## API 端点

| 方法   | 路径                                | 说明                                 |
|--------|-------------------------------------|--------------------------------------|
| POST   | `/api/v1/objects/batch`             | 批量协商 upload/download URL         |
| PUT    | `/api/v1/storage/{oid}`             | 上传对象                             |
| GET    | `/api/v1/storage/{oid}`             | 下载对象                             |
| HEAD   | `/api/v1/storage/{oid}`             | 探测对象是否存在                     |
| DELETE | `/api/v1/storage/{oid}`             | 删除对象（管理员）                   |
| GET    | `/api/v1/info/lfs/objects/basic`    | LFS spec 探测端点                    |
| GET    | `/healthz`                          | 健康检查                             |
| GET    | `/api/v1/stats`                     | 本地元数据统计                       |
| GET    | `/docs`                             | Swagger UI                           |

## 验证

启动后可以用 curl 测试：

```bash
# 健康检查
curl http://localhost:8000/healthz

# 模拟 Git LFS 客户端调用 batch 接口
curl -u lfs:your-password \
  -X POST http://localhost:8000/api/v1/objects/batch \
  -H 'Content-Type: application/json' \
  -d '{
    "operation": "download",
    "transfers": ["basic"],
    "objects": [{"oid": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855", "size": 0}]
  }'
```

## 生产部署建议

1. **HTTPS**：在前面挂 Nginx / Caddy / Cloudflare 终止 TLS。Git LFS 客户端对非 HTTPS 会有警告。
2. **反向代理时**：`proxy_headers=True` 已开启，但要确保 nginx 把 `Authorization` 头透传过来：
   ```nginx
   client_max_body_size 0;       # 不限制 body，由 LFS server 控制
   proxy_set_header Host $host;
   proxy_set_header X-Real-IP $remote_addr;
   proxy_request_buffering off;   # 流式上传
   proxy_buffering off;
   ```
3. **多实例**：把 SQLite 替换成共享存储（Redis / MySQL / Postgres），或者用 `ncdu` 把 `/data` 挂到共享卷。
4. **签名 URL**：私有 bucket 的下载 URL 默认 1 小时过期，可按需调整 `expires` 参数。
5. **限流 / 防滥用**：在前面挂 nginx `limit_req` 或 API gateway。

## GitHub Actions / CI 中的 LFS

在 CI runner 里如果需要 `git lfs pull` 或 `git lfs fetch`，也要配同样的 `lfs.url`：

```yaml
- uses: actions/checkout@v4
  with:
    lfs: true
- run: |
    git config lfs.url https://user:pass@lfs.example.com/api/v1
    git lfs pull
```

## 已知限制

- 大文件上传使用临时文件 + `put_file`，单机可扛 ~5GB。如需更大，改成七牛云分片上传 / ResumableUploader。
- 没有实现 LFS locking API（按需添加）。
- 没有 transfer 自定义协议。

## CI/CD

GitHub Actions 已配置两个工作流（`.github/workflows/`）：

### Test (`.github/workflows/test.yml`)

- **触发**：push 到 main、PR 推送到 main
- **矩阵**：Python 3.10 / 3.11 / 3.12
- **步骤**：
  1. `uv sync --frozen --all-groups` —— 按 lockfile 安装依赖
  2. `uv run python -m tests.test_smoke` —— 跑 19 个 smoke test
  3. `uv lock --check` —— 验证 pyproject.toml 与 uv.lock 一致
- **额外**：`docker-build-check` job 验证 Dockerfile 能成功构建（不推送）

### Docker (`.github/workflows/docker.yml`)

- **触发**：push 到 main、打 `v*.*.*` tag、手动 `workflow_dispatch`
- **架构**：`linux/amd64` + `linux/arm64`
- **镜像仓库**：`ghcr.io/<owner>/<repo>`
- **Tag 策略**：
  - 主分支推送 → `:main`、`:main-<sha>`、`:latest`
  - PR → `:pr-<num>`（构建但不推送）
  - `v1.2.3` tag → `:1.2.3`、`:1.2`、`:sha-...`、`:latest`
- **额外**：
  - 启用 SLSA **provenance attestation**（供应链溯源）
  - 启用 **SBOM** 生成（依赖清单）
  - **Trivy 漏洞扫描** + 上传结果到 Security tab
- **镜像特征**：
  - 多阶段构建，运行时镜像不含 uv / 编译器
  - 依赖层缓存（uv.lock 不变则不重装）
  - 非 root 用户运行
  - 内置 `HEALTHCHECK`

### 本地验证（模拟 CI）

```bash
make test                  # 跑 smoke test
make lock                  # 同步 lockfile
uv lock --check            # CI 的检查项，本地跑一下
docker build -t lfs:test . # 验证 Dockerfile
```

## License

MIT
