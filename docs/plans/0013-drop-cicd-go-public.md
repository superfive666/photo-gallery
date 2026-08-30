# 0013 — 移除 CI/CD 自动化，仓库转为公开

## 背景 / 问题

仓库要设为 public。当前的 `.github/workflows/` 与之直接冲突：

1. **安全**：`deploy.yml` / `ingest.yml` 跑在自建 runner 上，而 runner 账户在 `docker`
   组里（等价于 root）。public 仓库 + self-hosted runner 是一个已知的严重问题 ——
   仓库自己的 `docs/cicd.md` 第 19 节就写着这件事。虽然两个 workflow 都有
   `if: github.repository == ...` 与「不接受 pull_request」的双保险，但这道防线
   依赖后来每一个改 workflow 的人都不写错；公开之后它的失效代价是家用机器被入侵。
   最稳妥的处置不是加固，是让自建 runner 不再参与这个仓库。
2. **信息暴露**：workflow 与运维文档里写着生产机内网 IP、runner label、部署目录、
   家庭网段的 ufw 规则、路由器端口转发。这些对公开仓库的读者毫无价值，
   对想找目标的人却是现成的地图。

## 目标

- 删掉 `.github/workflows/` 全部三个 workflow，以及 100% 描述它们的 `docs/cicd.md`。
- 把两个被删 workflow 承担的生产职责补上等价的手动路径 —— 否则公开出去的项目
  按自己的文档是**没法部署**的（这是本次最容易漏掉的一点）。
- 运维文档去掉内网地址与机器名，改用占位符；删掉 runner 安装与 GitHub 侧配置。
- 文档里所有指向已删文件的链接同步修掉，公开仓库里不留死链。

## 非范围

- **不重写 git 历史。** 历史里有 3 个提交的信息含内网 IP。清理需要
  `filter-repo` + 强推 main，会让已合并 PR 的引用错乱、协作者的 clone 全部作废。
  这是仓库所有者才能拍板的取舍，本计划只如实标注，不代为执行。
- 不引入替代的托管 CI（GitHub 托管 runner 上跑 lint/test 其实是安全的，
  日后想加回来只需恢复 `ci.yml`，它不依赖本次删掉的任何东西）。
- 不改任何业务代码与 DDL。

## 设计

### 被删掉的三个职责，各自的替代

| 原 workflow | 职责 | 替代 |
| --- | --- | --- |
| `ci.yml` | PR 上跑 lint/typecheck/test/docker build | `make check`（新增目标，与原 CI 逐条对齐），提交前本地跑 |
| `deploy.yml` | build → migrate → up → 健康检查 → 失败回滚 → 清理镜像 | `scripts/deploy.sh`，在生产机上执行 |
| `ingest.yml` | 每日定时增量建库 | 生产机 crontab 一行（文档给出） |

`deploy.sh` 是 `deploy.yml` 的忠实移植，不是简化版 —— 那些步骤顺序都是踩出来的：

- **迁移必须在换代码之前**（DDL 只追加，旧代码在新 schema 上能正常工作；
  反过来会有一段新代码访问不存在列的窗口）；
- **失败时先抓日志再回滚** —— 回滚会重建容器抹掉崩溃日志（真踩过，排查绕了三轮）；
- **只在动过运行中的容器之后才回滚** —— 构建/迁移阶段失败时旧版本还在正常服务，
  这时 force-recreate 只会白白中断一次；
- **按数量保留镜像**（当前在线 + 上一个），不能用 `prune --filter until=…`：
  那个只删悬空镜像，带 tag 的旧版本一个都不碰（生产上真堆出过 3 份 5.5GB）。

把这些写成脚本而不是文档里的一串命令，是因为健康检查轮询与回滚有二十多行逻辑，
让人每次部署照着粘贴迟早会漏掉一步。

### 文档处置

| 文件 | 处置 |
| --- | --- |
| `docs/cicd.md` | 删除（分支模型那一节的内容并入 `CONTRIBUTING.md`） |
| `docs/deployment.md` | 重写：删 runner 安装与 GitHub 侧配置两节、删 Pi 一节、内网 IP 换占位符、部署改指向 `deploy.sh` |
| `docs/plans/0004-two-host-cicd.md` | 保留为历史记录（迭代流程依赖 plans 的连续性），但加「已废弃」抬头并抹掉 IP/机器名 |
| `docs/plans/0001`、`0005` | 只抹 IP，正文不动 |
| `README.md` / `CLAUDE.md` / `docker/README.md` | 更新目录表、部署段、约束 9、死链 |

`CLAUDE.md` 的约束 9 原本是「自建 runner 上不得因 fork PR 执行不受信任代码」。
runner 不再参与本仓库后这条失去对象，替换成对公开仓库真正有约束力的一条：
内网地址、真实密钥、生产主机名一律不进仓库。

## 验收标准

- `.github/` 不复存在；全仓搜不到具体的内网主机地址与 runner 机器名。
  （`web/nginx.conf` 里的 `10.0.0.0/8` / `172.16.0.0/12` / `192.168.0.0/16` 保留 ——
  那是可信代理的 RFC1918 标准列表，不指向任何具体主机。）
- 全仓 Markdown 无指向 `docs/cicd.md` 或 `.github/workflows/` 的死链。
- `bash -n scripts/deploy.sh` 通过；脚本里的步骤顺序与原 `deploy.yml` 逐条对得上。
- `make check` 跑通，且覆盖原 `ci.yml` 的每一项检查。
- `.env.example` 仍只有占位值（本来就是，公开前复核一次）。

## 风险

- **失去自动门禁。** 以前 PR 必须过 CI 才能合，现在靠人自觉跑 `make check`。
  这是「拿掉 workflows」的直接代价，`CONTRIBUTING.md` 里写明了。
  想要回门禁：恢复 `ci.yml` 即可，它跑在 GitHub 托管 runner 上，public 仓库下同样安全。
- **部署从「合并即部署」变成手动执行脚本。** 少了自动性，但也去掉了
  「一次误合并直接打到生产」这个此前一直存在的风险。
- **git 历史仍含内网 IP**（见非范围）。公开前需要仓库所有者单独决定是否清理。
