# 参与开发

## 提交前必须跑 `make check`

本仓库**没有 CI**（没有 GitHub Actions，见
[`docs/plans/0013`](docs/plans/0013-drop-cicd-go-public.md)），
所以门禁在你自己机器上：

```bash
make check      # lint（ruff + mypy + eslint + prettier）+ 全部测试
```

它跑的就是原先 CI 跑的那几项，逐条对齐。`make check` 不绿的分支请不要提 PR。

需要真实 Postgres 的那部分测试在没有 `DATABASE_URL` 时会自动跳过 ——
本地想完整跑一遍就先起个库：

```bash
make up                                    # 本地叠加层会带一个容器化 pg
export DATABASE_URL=postgresql+asyncpg://gallery:change-me@localhost:5432/photo_gallery
make migrate && make check
```

## 分支模型

```
main  ──●────────●────────●──────▶   始终可部署
         \      /  \    /
          ●──●─    ●──●            feature/*、fix/* 短生命周期分支
```

- `main` 始终可部署。不直推，走 PR。
- 分支命名：`feature/<slug>`、`fix/<slug>`、`chore/<slug>`。
- 提交信息用中文或英文均可，但要说清**为什么**而不只是「做了什么」。

## 迭代流程

1. 在 `docs/plans/NNNN-<slug>.md` 写计划（目标 / 范围 / 非范围 / 验收标准 / 风险）。
2. 需要改库就加 `docs/schema/NNN_*.sql`，并同步更新 `docs/schema/README.md` 的表清单。
   **已发布的迁移文件绝不原地修改**，只能新增。
3. 实现 + 测试。
4. 更新 README 的「已知局限」与相关文档。

## 依赖

Python 用 uv workspace 管理，四个成员共用一份 `uv.lock`：

```bash
# 改对应成员的 pyproject.toml（api / jobs / embedding / libs），然后
uv lock                  # 刷新 uv.lock —— 必须一起提交
uv sync --all-packages
```

不要写 `requirements.txt`，也不要在容器里 `pip install`。

## 这个仓库是公开的

- **内网地址、真实密钥、生产主机名一律不进仓库** —— 代码、文档、提交信息都算。
  `.env` 永不提交；`.env.example` 只放占位值。
- 隐私相关的硬约束见 [`CLAUDE.md`](CLAUDE.md)「不可违反的约束」，
  改到人脸、embedding、日志、自拍处理这些区域时请先读完那一节。
- 模型许可：InsightFace 的预训练权重**仅授权非商业研究用途**，见 README 末尾。
