# listingmerge · 多家中介二手房源重复挂牌归并

> 客户：安家数据 · 房源中台（7 人）。聚合 9 家中介的二手房挂牌，供 3 家银行做抵押估价参考。
> 现状：爬虫把 9 家挂牌原样入库，同一套房出现 3-5 次，估价师手动挑一条；
> 面积/楼层/朝向写法不一（`89.5㎡` / `89.5平米` / `建面89.5`），已成交未下架的僵尸挂牌还在拉低均价。

本系统把「采集 → 采集分诊 → 字段解析归一 → blocking → 相似度 → 并查集 → golden record → 风险分诊 → 复核 → 导出」串成一条可追溯链路：
同一套房只留一份主档（价格中位 / 面积众数 / 中介去重计数），
价格离群（钓鱼价）与超期无带看（僵尸挂牌）单独进复核队列，导出清单带全部算法读数。
采集环节按 **完整（complete）/ 待补（incomplete）/ 不合规（invalid）** 三档分诊，
原文全量留档；只有完整且未撤回的挂牌进入房源簇，待补补正后可重新入列，银行清单不含未完成或已撤回房源。

## 1. 运行

```bash
cd batch-20261114/projects/gb-614
docker compose up -d --build          # 5 个服务全部 healthy（约 20-40s）
docker compose exec -T backend python -m scripts.seed_demo     # 幂等，可重复执行
docker compose exec -T backend python -m pytest tests -q
BASE_URL=http://localhost:18203 node ../../test-artifacts/gb-614/tests/smoke.mjs
BASE_URL=http://localhost:18203 node ../../test-artifacts/gb-614/tests/screenshots.cjs
docker compose down -v --remove-orphans
```

| 服务 | 端口 | 说明 |
| --- | --- | --- |
| frontend（Nginx 静态站） | 18203 | 原生 HTML/CSS/ES Modules，`/api` 反代到 backend 并剥掉前缀 |
| backend（FastAPI） | 19203 | 全部计算/校验/持久化 |
| db（MySQL 8.0） | 17703 | utf8mb4，启动时 `create_all` 建表 |
| redis | 内部 | Celery broker + worker 心跳 |
| worker（Celery） | 内部 | 10s 心跳 + 每日 07:40 全量重算（`service_healthy` 校验心跳） |

演示账号（密码 `Demo@2026`）：`admin@listingmerge.io` / `analyst@listingmerge.io` / `viewer@listingmerge.io`。

## 2. 领域实体

| 实体 | 表 | 作用 | 自然键 |
| --- | --- | --- | --- |
| Agency 中介渠道 | `agencies` | 9 家挂牌来源、采集入口与口径 | `code` |
| Listing 原始挂牌 | `listings` | 采集原文 + 原文快照 + 归一结果 + 采集分诊 + 所属簇 | `(agency_id, lid)` |
| IngestBatch 采集批次 | `ingest_batches` | 批次台账：三类计数、新增/更新/未变化、重试链、失败原因 | `batch_no` |
| ListingCorrection 补正留痕 | `listing_corrections` | 字段级补正 applied/rejected 全留痕，补正失败不丢记录 | 自增 |
| Property 房源簇 | `properties` | golden record：面积众数/价格中位/中介数/风险分 | `cluster_key`（成员挂牌哈希） |
| MatchRun 归并重算 | `match_runs` | 每次算法 A+B 的输入输出与耗时（含拦截/重入计数） | `run_no` / `seed_key` |
| Review 复核单 | `reviews` | 风险簇的人工结论（待复核/确认/驳回） | `seed_key` |

每个实体都贯穿：表 → `models/` → `schemas/` → `services/` → `controllers/` → `routes/` → 前端 `api.js` + 页面。

### 采集分诊三档（`services/intake_service.py`，纯函数）

| 档位 | 触发条件 | 去向 |
| --- | --- | --- |
| `complete` 完整 | 小区/户型/面积/价格均可解析且面积在合理区间 | 可进入房源簇（还需未撤回、未下架） |
| `incomplete` 待补 | `area_missing`（面积待核实）/ `price_missing`（价格面议） | 原文留档、不入簇；补正后重算自动重新入列 |
| `invalid` 不合规 | 小区/户型无法解析、面积越界（`area_out_of_range` 等） | 原文留档、不入簇，退回渠道整改 |

- 原文快照存 `listings.raw_payload`，补正只写 `listing_corrections`，永不覆盖原文；
- 批次按 `(agency_id, lid)` 自然键 upsert：同批重试不新增，未变化记 `unchanged`、有变化记 `updated`；
- 撤回挂牌置 `withdrawn_at` + `off_market`，记录保留但下次重算出簇，且不进银行清单。

## 3. 业务模块表（7 个）

| 模块 | 页面 | services + controller | 主要 API |
| --- | --- | --- | --- |
| ① 中介与采集 | `#/agencies` 中介与采集 | `agency_service` + `controllers/agency_controller.py` | `GET/POST /api/agencies`、`GET/PATCH /api/agencies/{id}` |
| ② 采集分诊与补正 | `#/intake` 采集批次 | `intake_service` + `controllers/intake_controller.py` | `POST/GET /api/intake/batches`、`POST /api/intake/batches/{no}/retry`、`POST /api/listings/{id}/corrections`、`POST /api/listings/{id}/withdraw` |
| ③ 字段解析归一 | `#/listings` 挂牌明细（原文 vs 归一 vs 分诊） | `normalizer` + `listing_service` + `listing_controller.py` | `GET /api/listings`（含 `stage` 过滤）、`GET /api/listings/{id}` |
| ④ 分块与相似度 | `#/clusters` 归并簇（成员对照） | `entity_resolution`（blocking / similarity / UnionFind） | 由 `POST /api/runs` 驱动，读数见 `GET /api/runs/{id}` |
| ⑤ 聚类与 golden record | `#/clusters` 归并簇 | `entity_resolution.resolve` + `pipeline_service` + `property_service` + `property_controller.py` | `GET /api/properties`、`GET /api/properties/{id}`、`POST /api/runs` |
| ⑥ 风险与复核 | `#/reviews` 复核与导出 | `risk_rules` + `review_service` + `review_controller.py` | `GET /api/reviews`、`PATCH /api/reviews/{id}`、`GET /api/overview` |
| ⑦ 导出（含银行清单） | `#/reviews` 复核与导出 | `export_service` + `export_controller.py` | `GET /api/exports/{clusters,listings,reviews,bank-book}.csv` |

横切能力（不计入业务模块）：登录 JWT（`auth_service` / `routes/auth.py`）、审计日志（`middlewares/audit_logger.py` + `audit_service` + `routes/audit.py`）、限流（`middlewares/rate_limit.py`）。

## 4. 两个算法（纯 Python，结果进页面与导出）

### 算法 A｜实体解析（`backend/src/services/entity_resolution.py`）

- **输入闸口**：流水线只把 `ingest_stage=complete` 且未撤回/下架的挂牌送入解析；待补、不合规记录不参与 blocking，从源头避免缺项把相邻户型串成一套；
- **blocking**：同 `community_norm` + 同 `layout_norm` + 面积桶（5㎡ 一档，取同桶与相邻桶）且 `|Δ面积| ≤ 5㎡`；
- **相似度**：`0.35*area_close + 0.25*price_close + 0.20*floor_match + 0.10*orientation + 0.10*title_token`
  - `area_close = 1 − |Δa|/6`，`price_close = 1 − |Δp|/均值/0.30`（30% 价差处归零，跨中介报价天然差 1-3%），
    `floor_match` 同层 1.0 / 同区间 0.6，`orientation` 归一后相等 1.0，`title_token` = 标题字符二元组 Jaccard；
- **阈值 0.80 连边**，并查集 + 传递闭包；簇置信 = 簇内最大成对分（`max_pair_score`）；
- **golden record**：价格取簇内中位、面积取众数（并列取小）、中介数去重计数、价差 = p90 − p10。

### 算法 B｜僵尸与钓鱼挂牌识别（`backend/src/services/risk_rules.py`）

- 簇内价格离散度 `dispersion = (p90 − p10) / median`；`dispersion > 0.10` 打 `spread`；
- 挂牌价相对簇中位**低于 10% 以上** → `bait`（钓鱼价），同时落 `bait_lids`；
- 挂牌 **> 180 天且带看数为 0** → `stale`（僵尸挂牌），落 `stale_lids`；
- 风险分 `0-100 = 45*钓鱼 + 35*僵尸 + min(20, dispersion*200)`；`≥65` 高风险、`≥35` 中风险、`>0` 低风险、否则正常；
- 阈值常量集中在 `backend/src/constants/limits.py`，前端不复制任何算法结果。

## 5. 共享枚举出现位置

| 枚举组 | 后端 | 前端 |
| --- | --- | --- |
| 风险等级 `high/medium/low/none` | `constants/enums.py::RiskLevel` + `RISK_LEVEL_LABELS` | `js/config.js::ENUMS.riskLevel`、`riskTone()` |
| 风险标签 `bait/stale/spread` | `constants/enums.py::RiskTag` + `RISK_TAG_LABELS` | `js/config.js::ENUMS.riskTag`、`tagTone()` |
| 挂牌状态 `active/sold/off_market` | `constants/enums.py::ListingStatus` | `js/config.js::ENUMS.listingStatus`、`statusTone()` |
| 采集分诊 `complete/incomplete/invalid` | `constants/enums.py::IngestStage` + `INGEST_STAGE_LABELS` | `js/config.js::ENUMS.ingestStage`、`stageTone()` |
| 批次/补正状态 | `enums.py::BatchStatus/CorrectionStatus` | `js/config.js::ENUMS.batchStatus/correctionStatus` |
| 渠道类型 `broker/platform/feed` | `constants/enums.py::AgencyKind` | `js/config.js::ENUMS.agencyKind` |
| 复核状态/原因、运行状态、触发方式 | `enums.py::ReviewStatus/ReviewReason/RunStatus/TriggerKind` | `js/config.js::ENUMS.reviewStatus/reviewReason/runStatus/trigger` |
| 角色与权限点 | `constants/roles.py::PERMISSIONS`（含 `intake:read/write`、`listing:write`） | `js/config.js::PERMISSIONS` + `can()` |

## 6. 页面（7 个）

1. `#/login` 登录 —— 橱窗房源贴：4 张挂牌便签（3 家原文 + 1 张已归并）。
2. `#/overview` 总览 —— 挂牌数 / 完整可入簇 / 归并簇 / 风险簇四张价签 + 中介覆盖（含待补、不合规计数）+ 风险台账 + 近 7 天重算。
3. `#/agencies` 中介与采集 —— 9 家渠道表 + 抽屉（采集口径、分诊计数、挂牌样例、暂停采集）+ 登记渠道。
4. `#/intake` 采集批次 —— 批次台账（完整/待补/不合规、新增/更新/未变化、重试链、失败原因）+ 整批重试。
5. `#/listings` 挂牌明细 —— 原文 → 归一 对照、采集分诊徽标与缺项、所属簇；抽屉内可补正重新分诊、撤回挂牌；按中介/分诊/状态/风险筛选。
6. `#/clusters` 归并簇 —— 簇列表（价差/成员/置信/风险）+ 抽屉（成员对照表、golden record、钓鱼/僵尸命中）+ 全量重算（显示入列/拦截/重入）+ 运行历史。
7. `#/reviews` 复核与导出 —— 复核队列 + 复核结论表单 + 四种 CSV 导出（含银行清单）+ 管理员审计日志。

共用组件：`ui.js` 的 `table` / `drawer` / `filter-bar` 在 3、4、5、6 四个页面复用；`badge`、`kv`、`emptyState` 全站复用。

## 7. seed 口径（幂等）

`backend/scripts/demo_config.py` 确定性生成：9 家中介 × 60 条 = **540 条挂牌**，覆盖 24 个小区、4 种户型；
原始写法随机混用 `㎡ / 平米 / 建面 / 建筑面积 / 平方米（套内…）`、`万 / 元 / 总价…万 / 万（可谈）`、`3/18 / 3层/共18层 / 低楼层(共18层)`；
含 5 条分诊演示脏数据（均在单成员簇，不影响多成员簇与风险口径）：

| 挂牌情形 | 分诊档位 | 缺项标记 |
| --- | --- | --- |
| 楼层缺失（201） | complete（仍可入簇） | `floor_missing` |
| 价格面议（202） | incomplete 待补 | `price_missing` |
| 面积待核实（250） | incomplete 待补 | `area_missing` |
| 面积越界 9999㎡（260） | invalid 不合规 | `area_out_of_range` |
| 小区缺失（270） | invalid 不合规 | `community_missing` |

跑完采集分诊 + 算法 A/B 后：**536 条完整（508 条在售可入簇，其余为已成交未下架）、2 条待补、2 条不合规**，
**382 个房源簇**（41 个多成员簇）、**6 个风险簇**（1 个钓鱼价簇含 2 条 bait、5 条僵尸挂牌）、**6 张待复核单**。
待补/不合规 4 条不参与归并、不出现在银行清单；补正为完整后下次重算自动重新入列（运行指标 `readmitted`）。
特别注意：`翠湖名邸 3-2-1 89.5㎡` 在 4 家中介交叉命中，价格 598/602/606/610 万，价差 12 万，中位 604 万。

seed 结束打印一行（连续两次逐字一致）：

```
SEED_SUMMARY brand=listingmerge users=3 sources=9 runs=2 findings=382 pending=6
```

### 银行清单（`GET /api/exports/bank-book.csv`）

文件头注释显式标注覆盖范围（中介数与编码、原始挂牌数、合规在售数、房源簇数）与待补/不合规/撤回数量；
正文仅收录 **完整、在售（active）、未撤回且已入簇** 的房源，每个房源簇一行（含单价、在售成员数、中介编码）。
待补、不合规、已撤回/下架记录在任何情况下都不会出现在清单中。

## 8. 权限矩阵

| 能力 | admin | analyst | viewer |
| --- | --- | --- | --- |
| 读总览/挂牌/簇/复核/采集批次 | ✅ | ✅ | ✅ |
| 登记/改中介渠道 | ✅ | ❌ 403 | ❌ 403 |
| 采集入库/批次重试/挂牌补正/撤回 | ✅ | ✅ | ❌ 403 |
| 发起全量重算 | ✅ | ✅ | ❌ 403 |
| 提交复核结论 | ✅ | ✅ | ❌ 403 |
| 导出 CSV（含银行清单） | ✅ | ✅ | ❌ 403 |
| 审计日志 | ✅ | ❌ 403 | ❌ 403 |

无 token 一律 401；非法参数 422；重复编码 409。

## 9. 目录

```
backend/src/{config,constants,controllers,middlewares,models,routes,schemas,services,utils}
backend/scripts/{seed_demo.py,demo_config.py,worker_healthcheck.sh}
backend/migrations/001_core_tables.sql   # DDL 参考（运行时由 create_all + schema_bootstrap 补列）
frontend/site/{index.html,css/{tokens,app}.css,js/{config,api,store,ui,app}.js,js/pages/*.js}
docker-compose.yml  .env  README.md  docs/
```

## 10. 稳定性与数据安全口径

- **确定性**：归并算法未改，同一份完整记录输入得到相同簇集合、`cluster_key` 与 golden record；未变化数据重复重算结果稳定（`test_repeated_pipeline_is_stable`）。
- **同批重试不新增**：批次与挂牌均按自然键 upsert，重复提交只产生 `updated/unchanged` 计数。
- **补正失败不丢记录**：补正先试算，仍为待补/不合规则只写 `rejected` 留痕，原记录保持不变。
- **原文全部留档**：`raw_payload` 为采集时快照，补正与撤回都不覆盖；内部 `listings.csv` 全量导出，对外银行清单只出合规房源。
- **旧库平滑升级**：`config/schema_bootstrap.py` 用 inspector 判列后 `ALTER TABLE ADD COLUMN`，MySQL 8.0 / SQLite 通用、可重复执行。
