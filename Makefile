.DEFAULT_GOAL := help
COMPOSE := docker compose
ALBUM ?=

.PHONY: help
help: ## 显示所有命令
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

# ---------------------------------------------------------------------------
# 环境
# ---------------------------------------------------------------------------
.PHONY: up
up: ## 起 db + embedding + api + web
	$(COMPOSE) up -d db embedding api web

.PHONY: down
down: ## 停掉全部容器（保留数据卷）
	$(COMPOSE) down

.PHONY: logs
logs: ## 跟随日志
	$(COMPOSE) logs -f --tail=100

.PHONY: ps
ps: ## 容器状态
	$(COMPOSE) ps

.PHONY: build
build: ## 重建全部镜像
	$(COMPOSE) build

# ---------------------------------------------------------------------------
# 数据库
# ---------------------------------------------------------------------------
.PHONY: migrate
migrate: ## 顺序执行 docs/schema/*.sql（跳过已应用的版本）
	$(COMPOSE) run --rm jobs python -m jobs migrate

.PHONY: psql
psql: ## 进 psql
	$(COMPOSE) exec db psql -U $${POSTGRES_USER:-gallery} -d $${POSTGRES_DB:-photo_gallery}

.PHONY: backup
backup: ## 导出数据库到 ./backups/
	@mkdir -p backups
	$(COMPOSE) exec -T db pg_dump -U $${POSTGRES_USER:-gallery} -Fc $${POSTGRES_DB:-photo_gallery} \
		> backups/photo_gallery-$$(date +%Y%m%d-%H%M%S).dump
	@echo "备份完成。提醒：未演练过恢复的备份等于没有备份。"

# ---------------------------------------------------------------------------
# 离线任务
# ---------------------------------------------------------------------------
.PHONY: probe
probe: ## 探查源站页面结构，不写库。用法：make probe ALBUM=2026-08-10
	$(COMPOSE) run --rm jobs python -m jobs probe $(if $(ALBUM),--album $(ALBUM),)

.PHONY: ingest
ingest: ## 拉取并建库（批量）。用法：make ingest ALBUM=2026-08-10（省略则全部）
	$(COMPOSE) run --rm jobs python -m jobs ingest $(if $(ALBUM),--album $(ALBUM),)

.PHONY: ingest-full
ingest-full: ## 全量重跑（忽略已入库记录）
	$(COMPOSE) run --rm jobs python -m jobs ingest --full $(if $(ALBUM),--album $(ALBUM),)

.PHONY: block
block: ## opt-out：屏蔽某人全部人脸。用法：make block SELFIE=/data/eval/me.jpg
	@test -n "$(SELFIE)" || { echo "用法：make block SELFIE=<自拍路径>"; exit 2; }
	$(COMPOSE) run --rm jobs python -m jobs block --selfie "$(SELFIE)"

.PHONY: eval
eval: ## 跑评估集，输出 precision/recall 与漏检归因
	$(COMPOSE) run --rm jobs python -m jobs eval $(if $(SWEEP),--sweep,)

# ---------------------------------------------------------------------------
# 质量
# ---------------------------------------------------------------------------
.PHONY: test
test: test-py test-web ## 全部测试

.PHONY: test-py
test-py: ## pytest（api + jobs + libs）
	$(COMPOSE) run --rm jobs pytest -q

.PHONY: test-web
test-web: ## vitest
	cd web && npm run test -- --run

.PHONY: lint
lint: ## ruff + mypy + eslint + prettier
	ruff check api jobs libs embedding
	ruff format --check api jobs libs embedding
	mypy api jobs libs embedding
	cd web && npm run lint && npx tsc --noEmit

.PHONY: fmt
fmt: ## 自动格式化
	ruff format api jobs libs embedding
	ruff check --fix api jobs libs embedding
	cd web && npm run format
