# 评估历史

**只增不改。** 每次跑 `make eval` 后把结果追加到本文件末尾。

阈值和模型是「改一下好像更好了」最容易失控的地方 —— 没有这份记录，半年后没人说得清
当前阈值是怎么定下来的、也没法判断某次改动到底是变好还是变坏。

任何改动模型、预处理、阈值或候选数的 PR，都要贴上改动前后的对比。

## 记录格式

```
## YYYY-MM-DD — <改动摘要>

- 模型：buffalo_l v1
- 阈值：face=0.42 min_face_px=40 min_det_score=0.50 candidates=500
- 评估集：N 人 / M 张 gallery 照片
- Recall: 0.xx  Precision: 0.xx  Recall@20: 0.xx  MRR: 0.xx
- 漏检归因：small_face=x detect_fail=x low_similarity=x
- 结论 / 下一步：
```

---

## 尚无记录

评估集还未建立（需要成员明确同意后才能采集）。当前 `.env.example` 与
`libs/gallery_core/config.py` 里的阈值是**文献经验值，不是标定结果**。

首次评估完成后：
1. 在此追加第一条记录；
2. 用标定出的值替换 `docs/evaluation.md`「当前默认值」一节与 `.env.example`；
3. 勾掉 `docs/deployment.md` 上线前检查清单里的「阈值已用评估集标定」。
