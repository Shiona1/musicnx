# MusicNX development commands (Linux/macOS/POSIX shell, Windows via Git Bash/msys).
# Python project lives in backend/ (uv), frontend in frontend/ (pnpm).

PY := uv run --directory backend
WEB := pnpm --dir frontend
# Use the repository config even though uv runs the app from backend/.
CONFIG := $(CURDIR)/config/config.yaml

.PHONY: help install dev mock test lint check build dev-web requirements clean

help:
	@echo "MusicNX targets:"
	@echo "  install   backend uv sync + frontend pnpm install"
	@echo "  mock      run backend on :8080 in mock mode (no mpv, no token)"
	@echo "  dev       run backend with autoreload in mock mode"
	@echo "  dev-web   Vite dev server on :5173 with API/WS proxy to :8080"
	@echo "  test      pytest (backend, no network/Pi/mpv)"
	@echo "  lint      ruff + svelte-check"
	@echo "  check     lint + test"
	@echo "  build     build frontend into backend/app/static"
	@echo "  requirements  regenerate backend/requirements.txt from uv.lock"
	@echo "  clean     remove caches and build output"

install:
	uv sync --directory backend
	$(WEB) install

mock:
	MUSICNX_MODE=mock MUSICNX_CONFIG=$(CONFIG) $(PY) uvicorn app.main:app --host 0.0.0.0 --port 8080

dev:
	MUSICNX_MODE=mock MUSICNX_CONFIG=$(CONFIG) $(PY) uvicorn app.main:app --host 127.0.0.1 --port 8080 --reload

dev-web:
	$(WEB) dev

test:
	$(PY) pytest -q

lint:
	$(PY) ruff check .
	$(WEB) run check

check: lint test

build:
	$(WEB) run build

requirements:
	cd backend && uv export --format requirements-txt --no-dev --no-emit-project --no-hashes -o requirements.txt

clean:
	rm -rf backend/.pytest_cache backend/.ruff_cache frontend/dist
	rm -rf backend/app/static/*
