# 准确率评估与阈值标定

没有评估集，这个项目最后会变成「看起来能跑，但没人知道准不准」。
这是本项目第二大风险（第一是隐私）。

## 为什么不能用文献里的阈值

ArcFace 在 LFW 上的同人 cosine 相似度通常落在 0.35~0.5，很多教程直接抄这个数。
但那是在**正脸、清晰、单人**的基准集上得到的分布。本项目的实际输入是：

- 活动合影，人脸只占画面很小一块
- 侧脸、低头、被遮挡、逆光、舞台灯
- 跨越数年的照片
- 查询端是手机自拍（近距离、广角畸变、美颜滤镜）

这两个分布差别很大，抄来的阈值必然要么大量漏检、要么严重串人。**阈值只能用自己的数据标定。**

## 评估集构建

规模不用大，20 个人就足够定出可用的阈值。

```
eval/
  gallery/                 # 模拟照片库：从真实 album 里挑
    <album>/<file>.jpg
  queries/                 # 模拟自拍：每人 1~3 张
    person_01/selfie_1.jpg
    ...
  labels.csv               # ground truth
```

`labels.csv`：

```csv
person_id,gallery_path
person_01,2026-08-10/IMG_0231.jpg
person_01,2026-07-20/IMG_0245.jpg
person_02,2026-08-10/IMG_0231.jpg
...
```

- 每人 **3~5 张**已知出现的照片，覆盖不同活动、不同角度、至少一张「困难样本」（侧脸或小脸）。
- 同一张照片里有多个被标注的人是正常的（合影），各占一行。
- 必须包含**长得像的人**（兄弟姐妹、相似发型），这是误报的主要来源，不含它们的评估集会给出
  虚高的 precision。
- 评估集里的人必须**明确同意**参与测试。评估集不进 git（含真人照片），
  放宿主机固定路径并在 `.env` 里配 `EVAL_DIR`。

## 指标

以「每个查询自拍」为单位：

| 指标 | 定义 | 目标 |
| --- | --- | --- |
| Recall@all | 命中的本人照片数 / 该人在库中的全部照片数 | 越高越好，先看这个 |
| Precision@all | 命中中确实是本人的比例 | ≥ 0.95（串人的体验比漏图差得多） |
| Recall@20 | 前 20 个结果里的召回 | 用户实际只会看前几屏 |
| MRR | 首个正确结果的排名倒数 | 衡量排序质量 |
| 漏检归因 | 漏掉的照片中，因小脸被丢弃 / 检测失败 / 相似度不足 各占多少 | **最有价值的诊断** |

**漏检归因**是最该看的一项：如果漏检主要来自「小脸被质量门控丢弃」，那调阈值是白费力气，
该做的是降低 `MIN_FACE_PX` 或对大图做分块检测；如果主要来自「相似度不足」，才轮到调阈值。

## 标定流程

评估**不会自己灌库** —— 它读的是当前数据库里已有的数据。所以要按顺序来：

```bash
# 1. 把评估集的 gallery 灌进库（指向一个独立的测试数据库，别污染生产库）
#    local_dir adapter 会把 gallery/ 下的一级子目录名当作 album slug
SOURCE_ADAPTER=local_dir SOURCE_LOCAL_DIR=/data/eval/gallery make ingest
make cluster

# 2. 评估
make eval                # 全量评估 + 漏检归因
make eval SWEEP=1        # 阈值网格扫描，给出建议阈值
```

`jobs/eval.py` 做的事：

1. 从 `labels.csv` 读 ground truth，从 `queries/<person>/` 读自拍。
2. 每个人的多张自拍合成单一查询向量（与线上完全一致的做法）。
3. 调用 **api 里那份真实的 `search_by_embedding`** 执行检索 ——
   不是另写一份近似实现，否则测出来的指标和线上行为无关。
4. 计算 recall / precision / recall@20 / MRR。
5. 对每一张漏掉的照片做归因（`small_face` / `detect_fail` / `low_similarity` /
   `not_ingested`）。
6. `--sweep` 时在 `PERSON_MATCH_THRESHOLD × FACE_MATCH_THRESHOLD` 网格上扫描，
   输出 precision ≥ 0.95 前提下 recall 最大的组合。

> `labels.csv` 里的 `gallery_path` 是相对 `gallery/` 的路径（形如
> `2026-08-10/IMG_0001.jpg`），评估用 `local_dir.photo_url_for()` 把它换算成库里的
> `photo_url` 来对行。URL 规则只在那一个函数里定义，有测试
> `jobs/tests/test_eval.py::test_photo_url_matches_local_dir_adapter` 钉住这一点 ——
> 两处各写一遍的话，评估会把每张照片都判成 not_ingested，指标全为 0。

## 当前默认值（未标定，仅为占位）

```
FACE_MATCH_THRESHOLD    = 0.42    # 单脸直接命中
PERSON_MATCH_THRESHOLD  = 0.38    # 簇心匹配，可略松（簇心更稳）
MIN_DET_SCORE           = 0.50
MIN_FACE_PX             = 40
CLUSTER_MIN_SAMPLES     = 3       # DBSCAN
CLUSTER_EPS             = 0.30
```

> ⚠️ 这些是文献经验值，**不是**本项目的标定结果。首次评估完成后必须回来更新本节，
> 并在 `docs/plans/` 里记录标定过程与结论。

## 回归防护

阈值和模型是「改一下好像更好了」最容易失控的地方。所以：

- 评估结果（指标 + 阈值 + 模型版本 + 日期）追加到 `docs/evaluation-history.md`，只增不改。
- 任何改动模型、预处理、阈值、聚类参数的 PR，都要贴上评估前后对比。
- CI 不跑评估（需要真人照片，不能进 runner），由人工在本地执行并把结果贴到 PR。
