.PHONY: install dev start test lint clean lock upgrade help

# 默认目标：显示帮助
help:
	@echo "Git LFS Server - 常用命令"
	@echo ""
	@echo "  make install   安装依赖（首次或更新后）"
	@echo "  make dev       开发模式（热重载）"
	@echo "  make start     生产模式启动"
	@echo "  make test      跑 smoke test"
	@echo "  make lock      更新 uv.lock"
	@echo "  make upgrade   升级所有依赖到最新版本"
	@echo "  make clean     清理 .venv、缓存和临时文件"

# 安装/同步依赖
install:
	uv sync

# 开发模式（热重载）
dev:
	uv run uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

# 生产模式
start:
	uv run python run.py

# 跑 smoke test
test:
	uv run python -m tests.test_smoke

# 重新生成 lockfile（依赖未变时同步）
lock:
	uv lock

# 升级所有依赖到最新兼容版本
upgrade:
	uv lock --upgrade
	uv sync

# 清理：venv、缓存、临时文件
clean:
	rm -rf .venv
	rm -rf .pytest_cache .ruff_cache .mypy_cache
	rm -f lfs_metadata.db lfs_metadata.db-*
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name '*.pyc' -delete
