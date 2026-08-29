## 改动目标
让每日选片更偏向高分照片、并消除连拍占多席的问题。你已确认：**幂次加权（weight = final^8，"适中"）+ 60 秒连拍去重窗口**。

## 涉及文件
- `render_daily_photo.py` — 主逻辑（3 处改动）
- `config.py` — 新增 2 个配置项

---

## 改动 1：`config.py` ③评分段（第 60 行 HIGH_SCORE_THRESHOLD 之后）新增 2 个配置项

```python
# 精英池加权抽样：weight = final^SCORE_WEIGHT_POWER。
# 1.0=均匀随机(原行为)；8.0=适中(高分约2.7倍更易中，低分精英仍有机会)。
SCORE_WEIGHT_POWER: float = 8.0

# 连拍去重：拍摄时间间隔(秒)≤此值的视为同一组连拍，
# 组内只保留 final_score 最高的一张进入抽样池。0=关闭去重。
BURST_DEDUP_WINDOW_SEC: int = 60
```

放 HIGH_SCORE_THRESHOLD 后面，因为这俩都是选片阶段的可调参数，注释里说明默认值对应的体感（避免日后回来调参时忘记）。

## 改动 2：`render_daily_photo.py` load_sim_rows — 读出 exif_datetime 供去重使用

**位置**：`load_sim_rows`（第 121-179 行）

- SELECT 字段表（第 123-136 行）增加 `exif_datetime`
- for 解包元组（第 144 行）增加 `exif_dt`
- item dict（第 158-178 行）增加字段：
  ```python
  "shot_dt": _parse_exif_datetime(exif_dt),  # datetime | None，去重用
  ```
- 文件顶部加一个辅助函数 `_parse_exif_datetime`，把 `"2024:08:11 11:59:07"` 这类 EXIF 字符串解析成 `datetime`（覆盖库里出现的两种格式：`YYYY:MM:DD HH:MM:SS` 和 `YYYY-MM-DD HH:MM:SS`），失败返回 None。

这样 item 里的 `date`/`md`（日级，用于按月日分组）保持不变，新增的 `shot_dt`（秒级）专供去重。

## 改动 3：`render_daily_photo.py` choose_photos_for_today — 去重 + 加权抽样

**位置**：第 476-509 行（`# 计算 final_score 并排序` 到 `selection_mode = "topn_pool_random"`）

在 `candidates.sort(...)` 之后、`elite = [...]` 之前，**插入去重步骤**：

```python
# 连拍去重：shot_dt 间隔 ≤ BURST_DEDUP_WINDOW_SEC 秒的视为同组，保留组内 final 最高
if BURST_DEDUP_WINDOW_SEC > 0:
    candidates = dedup_bursts(candidates, BURST_DEDUP_WINDOW_SEC)
```

`dedup_bursts` 逻辑：按 shot_dt 升序排序，相邻两张差 ≤ 窗口秒则归为同组（传递合并：A-B-C 若 A-B、B-C 都 ≤ 窗口，则三者同组），每组保留 `_final_score` 最高的；shot_dt 为 None 的照片视为"时间未知"，不参与合并、单独保留（避免误杀）。

**elite_random 分支**（第 484-487 行）从均匀随机改为加权无放回抽样：

```python
if len(elite) >= count:
    chosen_list = weighted_sample_without_replacement(elite, count, SCORE_WEIGHT_POWER)
    selection_mode = "elite_random"
```

**topn_pool_random 分支**（第 494-497 行）同样从均匀改为加权：

```python
if len(pool) >= count:
    chosen_list = weighted_sample_without_replacement(pool, count, SCORE_WEIGHT_POWER)
```

新增辅助函数 `weighted_sample_without_replacement(items, k, power)`：
- weight_i = items[i]["_final_score"] ** power
- 逐张抽取：按权重归一化后抽 1 张 → 从池里移除 → 重新归一化 → 抽下一张，直到 k 张
- 这样保证无重复、且高分照片每轮都被加权（不只是一次性加权）
- `power=0` 时退化为 `random.sample`（与原均匀随机行为一致，便于校验）

elite_only 分支（精英不足 count、不补齐）和兜底分支（全局 Top-N）**不动**——前者没有随机、后者本就取最高分。

## 改动 4：日志增强（main 函数，第 1582 行附近）

`[SELECT]` 日志行不变（已含 final、display、各项权重）。新增一行 `[INFO]` 在选片信息块里打印去重效果，方便日后复盘：

```python
print(f"[INFO] 去重后精英数:", info.get("post_dedup_elite_count", "N/A"))
```

choose_photos_for_today 返回的 info dict 里增加 `post_dedup_elite_count` 和 `dedup_removed` 两个字段。

---

## 不改动 / 显式保留
- `choose_photo_for_today` 单张版（第 344 行）**不动**——它用 `elite_top3_random` 模式，当前未被 main 调用，避免回归。
- `compute_final_score` 公式、所有权重配置、portrait_boost 重排序逻辑**不动**。
- `mark_photo_used` / used_at 近期降权逻辑**不动**。
- 兜底分支（global Top-N）逻辑不动。

## 验证方式
改完用今天的真实数据（08-11 的 37 张精英）跑一次 `load_sim_rows + choose_photos_for_today`，打印：
1. 去重前/后精英数对比（预期 37 → 约 30~33）
2. 跑 1000 次抽样，统计每张照片的入选频率，确认高分（如 final=102.6 的两张）频率 ≈ 21%、低分精英 ≈ 6%，与你选的"适中"档一致
3. 跑一次完整 `python3 render_daily_photo.py`，确认日志格式正常、5 张照片正常生成 BIN

如果 1000 次抽样的实测频率和预期偏差大，再回头微调 power 值。