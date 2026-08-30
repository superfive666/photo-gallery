#!/usr/bin/env bash
#
# 生产部署：就地构建 → 迁移 → 滚动重启 → 健康检查 → 失败回滚 → 清理旧镜像。
#
# 在生产机上、于本仓库的一份 clone 里执行（构建需要源码）：
#
#     git pull && ./scripts/deploy.sh
#
# 这个脚本是原 .github/workflows/deploy.yml 的移植（见 plans/0013）。
# 里面每一步的顺序都是踩出来的，改动前先读 docs/deployment.md 的「部署」一节：
#
#   · 迁移必须在换代码之前 —— DDL 只追加，旧代码在新 schema 上能正常工作；
#     反过来会有一段新代码访问不存在列的窗口。
#   · 失败时先抓日志再回滚 —— 回滚会重建容器，抹掉崩溃现场。
#   · 只在动过运行中的容器之后才回滚 —— 构建/迁移失败时旧版本还在正常服务。
#   · 镜像按数量保留，不能用 prune --filter until=…（它只删悬空镜像）。
#
# 环境变量：
#   DEPLOY_DIR  生产状态目录（.env、compose 文件、.deployed-tag 所在），默认 /opt/photo-gallery
#   IMAGE_TAG   镜像 tag，默认 sha-<当前 commit 前 7 位>

set -euo pipefail

DEPLOY_DIR="${DEPLOY_DIR:-/opt/photo-gallery}"
IMAGE_PREFIX="${IMAGE_PREFIX:-photo-gallery}"
IMAGE_TAG="${IMAGE_TAG:-sha-$(git rev-parse --short=7 HEAD)}"
# 生产机有 GPU：把已锁定的 CPU 版 onnxruntime 换成同版本的 GPU 版
export EMBEDDING_GPU="${EMBEDDING_GPU:-true}"
export IMAGE_PREFIX IMAGE_TAG

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

log()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
fail() { printf '\n\033[31m错误：%s\033[0m\n' "$*" >&2; exit 1; }

# --- ① 校验生产目录与 .env -------------------------------------------------
# 生产 .env 只存在于这台机器上，从不进 git。缺失就中止，别用默认密码起服务。
log "校验 $DEPLOY_DIR"
[ -d "$DEPLOY_DIR" ] || fail "$DEPLOY_DIR 不存在，先按 docs/deployment.md 初始化生产机"
[ -f "$DEPLOY_DIR/.env" ] || fail "缺少 $DEPLOY_DIR/.env"
perms="$(stat -c '%a' "$DEPLOY_DIR/.env")"
[ "$perms" = "600" ] || fail ".env 权限是 $perms，应为 600"
echo "将部署 $IMAGE_TAG"

# --- ② 构建 ----------------------------------------------------------------
# 构建在仓库工作区里做（build context 需要源码），但读的是生产 .env ——
# 否则 compose 插值会把一堆变量解析成空串，只给一串 warning。
# tools profile 把一次性容器 jobs 也带进来，迁移要用它。
log "构建镜像"
cd "$REPO_DIR"
docker compose --env-file "$DEPLOY_DIR/.env" --profile tools build

# --- ③ 同步 compose 与 schema ----------------------------------------------
# .env 与数据卷留在原地不动。
log "同步 compose 与 schema 到 $DEPLOY_DIR"
install -m 644 docker-compose.yml docker-compose.gpu.yml "$DEPLOY_DIR/"
mkdir -p "$DEPLOY_DIR/docs"
rm -rf "$DEPLOY_DIR/docs/schema"
cp -r docs/schema "$DEPLOY_DIR/docs/schema"

cd "$DEPLOY_DIR"

# --- ④ 迁移（必须在新代码启动之前）-----------------------------------------
log "执行数据库迁移"
docker compose run --rm jobs python -m jobs migrate

# --- ⑤ 滚动重启 + 健康检查 --------------------------------------------------
# 从这里开始已经动了运行中的容器，失败要回滚。
rollback_needed=0

capture_logs() {
    echo "================ embedding 容器日志 ================"
    docker compose logs --no-color --tail=120 embedding || true
    echo "================ api 容器日志 ================"
    docker compose logs --no-color --tail=40 api || true
}

rollback() {
    # 只回滚镜像，不回滚数据库 —— DDL 是向后兼容的新增，旧代码在新 schema 上能正常工作。
    [ -f .deployed-tag ] || fail "没有上一次成功部署的记录（首次部署？），需要人工介入"
    prev="$(cat .deployed-tag)"
    # 上一个版本的镜像还在本机（没有仓库可拉，所以回滚能力全靠下面的保留策略）
    docker image inspect "$IMAGE_PREFIX-api:$prev" >/dev/null 2>&1 \
        || fail "本机已经没有 $prev 的镜像了（被清掉？），需要人工介入"
    printf '\n\033[33m健康检查未通过，回滚到 %s\033[0m\n' "$prev"
    IMAGE_TAG="$prev" docker compose up -d --no-deps --force-recreate embedding api web worker
}

log "滚动重启"
if ! docker compose up -d --no-deps embedding api web worker; then
    rollback_needed=1
fi

if [ "$rollback_needed" -eq 0 ]; then
    log "健康检查"
    healthy=0
    for attempt in $(seq 1 30); do
        if docker compose exec -T api python -c "
import sys, urllib.request
try:
    r = urllib.request.urlopen('http://localhost:8000/readyz', timeout=5)
    sys.exit(0 if r.status == 200 else 1)
except Exception:
    sys.exit(1)
" 2>/dev/null; then
            echo "就绪检查通过（第 $attempt 次）"
            healthy=1
            break
        fi
        sleep 5
    done
    [ "$healthy" -eq 1 ] || rollback_needed=1
fi

# --- ⑥ 失败路径：先抓日志，再回滚 -------------------------------------------
if [ "$rollback_needed" -eq 1 ]; then
    # ⚠️ 必须在回滚**之前**抓：回滚重建容器会抹掉崩溃日志，
    # 不抓的话「新版本为什么起不来」就永远无从知晓。
    capture_logs
    rollback
    fail "部署失败，已回滚"
fi

# --- ⑦ 记录本次已知良好的 tag -----------------------------------------------
log "记录版本"
# 上一个在线版本是回滚目标，记下来供清理步骤保留
if [ -f .deployed-tag ] && [ "$(cat .deployed-tag)" != "$IMAGE_TAG" ]; then
    cp .deployed-tag .previous-tag
fi
echo "$IMAGE_TAG" > .deployed-tag
# 同时写进 .env：让人工手敲的 docker compose 命令（up / run / ps）默认指向当前在线版本。
# 不写的话 .env 里残留的 IMAGE_TAG=dev 会让 compose 去找不存在的 :dev 镜像，
# 进而在没有源码的生产目录里试图构建。
if grep -q '^IMAGE_TAG=' .env; then
    sed -i "s|^IMAGE_TAG=.*|IMAGE_TAG=$IMAGE_TAG|" .env
else
    echo "IMAGE_TAG=$IMAGE_TAG" >> .env
fi

# --- ⑧ 清理旧镜像 -----------------------------------------------------------
# ⚠️ 不能用 `docker image prune --filter until=…`：它只删「悬空（无 tag）」镜像，
# 带 sha- tag 的旧版本一个都不会碰 —— embedding 单个 5.5GB，堆几次磁盘就满了。
# 改为按数量保留：当前在线（.deployed-tag）+ 上一个（.previous-tag，回滚目标）。
log "清理旧版本镜像"
keep1="$(cat .deployed-tag 2>/dev/null || true)"
keep2="$(cat .previous-tag 2>/dev/null || true)"
echo "保留: $keep1 $keep2"
for svc in api jobs web embedding; do
    docker images --format '{{.Repository}}:{{.Tag}}' "$IMAGE_PREFIX-$svc" \
    | while read -r img; do
        tag="${img##*:}"
        case "$tag" in "<none>"|"$keep1"|"$keep2") continue ;; esac
        # 正被容器使用的镜像 rmi 会拒绝删除，跳过即可
        docker rmi "$img" >/dev/null 2>&1 && echo "已删除 $img" || echo "跳过 $img（使用中）"
      done
done
# 重建同名 tag 时旧层会变成悬空镜像，一并清掉
docker image prune -f >/dev/null
# buildkit 构建缓存单独设上限。别清太狠：CUDA 轮子与模型权重的缓存层
# 正是「改业务代码时构建只要几十秒而不是二十分钟」的原因。
docker builder prune -f --keep-storage 20g >/dev/null 2>&1 || true
docker system df

log "已部署 $IMAGE_TAG"
