# 立项方案（v3 定稿）：wmshare 服务端 `valuation.daily_basic`

> 状态：**已实施（2026-11-18 全链落地，待部署上线）**。v3 按最终方案定稿：
> 存储选型一锤定音（V2 catalog 专用表）、**8 字段一次到位**（流通股本源已
> 查实，无源项为零）、**抽样先行**（hs300 样本组全字段×全历史）、
> **时效 SLA 定稿**（当日 17:00 前）。本文档是
> [upstream-requests.md](upstream-requests.md) 请求 1 的正式设计。
> 事实锚点全部来自本地服务端 4 仓链源码实读（winmale/kernel/script/storage/
> airflow），标注 文件:行号，可回溯。延续诚实契约：写清"证据是什么、
> 还没验证什么"，不把推断写成结论。

## 1. 目标与价值

每股每交易日物化的估值宽表 + 带日期区间查询端点，对标 tushare `daily_basic`：
- 层① `.bin` 直接纳真历史字段（现状：kline/moneyflow + PE≤3y 封顶）；
- 层③「每日攒 PIT」获得官方历史回填对账锚——攒的真值与回填真值互证；
- 全市场日频基本面截面从 ~204k EC/月（screener 实测）变为近似免费的缓存读；
- tushare 能给历史是因为其服务端存了每日物化宽表；wmshare 已有同构先例
  （`indices.trend` 按周物化约 14 年，script/web_echo/v2index/valuation_trend.xs），
  本方案 = 周频密度扩展为日频 + 下沉到个股。

## 2. 字段定稿：8 个一次到位（全部有现成服务端源，无源项为零）

单位：全部落 **2.0 基础单位**（率=小数、额=元、量=股；wmshare
`_catalog/units.yaml:1-15`），物化即终态，消费端零换算。

| 字段 | 计算公式 | 数据源 store | 现成先例（文件:行号） |
|---|---|---|---|
| `pe_ttm` | MV ÷ NPTS_TTM（MV = kline_normal.close × equity_change.总股份） | `kline_normal` + 财报 income + `{mk}.bssaa equity_change.PROPS` | HIST_STATS "pe"：kernel/analysis/vfunc_hist_pct.go:455-458, 1596-1604 |
| `pb` | MV ÷ (SE − OEIR)，公告日时点前向填充 | 同上 + balance | HIST_STATS "pb"：vfunc_hist_pct.go:463-466, 1607 |
| `ps` | MV ÷ TR_TTM | 同上 + income(TR) | HIST_STATS "ps"：vfunc_hist_pct.go:476-478 |
| `pr` | PE_TTM ÷ ROE_TTM ÷ 100 | 同上 + indicator(CurIr.ROE) | HIST "pr_base/pr_correct"：vfunc_hist_pct.go:499-500, 1665-1712 |
| `total_mv` | close(不复权) × equity_change.**总股份** | `kline_normal` + equity_change | `$MV`/`$MV_LAST`：script/sys/metric/shared/kline.xs:11, 170, 182 |
| `circ_mv` | close(不复权) × equity_change.**流通股份** | 同上（**CN 有流通股份**；HK/US 无此键 → 诚实 nil，shares.xs:40 明示） | `$MV_FREE`/`$MV_FREE_LAST`：kline.xs:12, 171, 183 |
| `turnover_rate` | volume(股) ÷ 流通股份(股)，小数 | equity_change + kline.volume | `$TURNOVER_RATE`：script/xs/iwencai_strategy/metrics/iwencai_technical.xs:98-100 |
| `dv_ttm` | DPS_TTM ÷ CLOSE（fiscal：公告日 as-of 财报期凑 LTM） | `alt.bonus bonus.PROPS`（每笔分红事件，ts=除权日） | `$BONUS_RATE_TTM(_LAST)`：script/sys/metric/shared/bonus.xs:87-101；kernel 同构 histCalcBonus：vfunc_hist_pct.go:1860-1899 |

**股本时点语义（定稿）**：equity_change 是事件表（YEAR_DAY 日分辨率，字段
「总股份/流通股份/变更原因」原文见 airflow/bssaa/config.py:6135-6165）；
**变更日（含当日）生效**，日频序列按 bar 承接、非交易日顺延到下一根 K 线
（HIST_STATS 前向填充同款：vfunc_hist_pct.go:2437
`sharePoints[si].ts <= klines[i].ts`）。股本事件滞后由现网双兜底补偿：
`equity_change_weekly_recon` 周日 20:00 diff（airflow/equity_change_weekly_recon_dag.py:231）
+ `daily_data_guard` 盘前对 tushare daily_basic 的 total_share/float_share
逐日 diff 0.1% 触发补拉（airflow/daily_data_guard_dag.py:356-457）。

**求值配方**：pe/pb/ps/pr 四件一次 `HIST_STATS` 序列调用直出（公告日 PIT）；
mv/mv_free/turnover/dv_ttm 按 metric 层同式对单日求值（SAS 日频序列
`m_mv_day`/`m_mv_free_day`：kline.xs:11-12；换手：iwencai_technical.xs:98-100；
股息：bonus.xs:87-101）。**不新增任何 kernel 宿主函数**——HIST_STATS 是
全局 vfunc（kernel/analysis/vfunc_global.go:458），SDK 自己就是拼一行
`return HIST_STATS(...)` 调它的（api/__init__.py:1666）。

## 3. 存储定稿：V2 catalog 专用表 `valuation_daily`

per-symbol 时序文档（每 symbol 每日一行、ts 降序），与 `kline_normal` 同构：
- 注册：`storage/v2/mapping.go:33-60` `catalogMappings` 加一行（注释原文
  「新增数据类型时在此处添加一行即可」）+ `storage/collection_registry.go:24-58`
  按 `UniqueKeySymbolSamplingTs` 注册（分红/公告类同款唯一键）。
- **Go 触碰面 = 这 1 行注册表数据，无任何新逻辑**；storage 库随 go.work
  重编译部署（kernel go.work include ../storage 联动）。
- 读路径：XS `store.query("valuation_daily", ...)`（tag 字符串路由，先例：
  StatementsApi 的 `store.query("fin.indicator", ...)`）→ `v2.TagToCatalog`
  （kernel/analysis/market_reader.go:132）→ ts 区间查询与 HIST_STATS 的
  `getStoreDocs`（vfunc_hist_pct.go:751-765，`WithStartTs/WithEndTs`）同构，
  任意 `begin/end` 天然支持。
- 热路径：最新一日截面用 memo/指针（现成 `qi_serving_index` promote 机制，
  script/sys/dview.xs:93-99）；历史一律走 store。
- **否决 memo 裸键**（indices.trend 模式为 56 键 × ~730 周点设计，
  script/sys/index/query.xs:291-303；5.7k 票 × 全历史日频超 4 个数量级，
  且 memo 无 ts 区间索引）。

**写入路径（定稿）**：airflow 四段式——本地证据中 XS 写面只有 memo 与
INDEX Redis 截面，「XS 直写 Mongo store」无先例；store 的现成写入轨道就是
airflow（buyback 同款：拉数 → raw 文档 → `do_clean_data` 入库 → guard）。

## 4. 物化与更新时效（SLA 定稿：当日 17:00 前）

**时序链（CN，北京时间，全部有源码证据）**：

```
15:00 收盘
  → 16:00  kline 入库 DAG 启动（airflow/dag_registry.yaml:329 "0 16 * * 1-5"；
           kline_normal 同 DAG 写入，tasks/kline_daily.py:51-55）
  → 16:10  kline SLA 验收（kline_update_check.xs:14「约 10 分钟，SLA 16:10」；
           源未就绪重试 ≤15 分钟：tasks/kline_daily.py:44-45）
  → 16:25  收窗检查（xs_cron.json:616-626 "10,25 16 * * 1-5"）
  → 16:25  【本方案 daily DAG 启动】"25 16 * * 1-5"
  → 17:00  【SLA】当日行全部物化完成
```

- **DAG**：`airflow/dag_registry.yaml` 加 `pipeline.cn.valuation_daily`
  （daily `"25 16 * * 1-5"` + manual 双 DAG，照 cn_buyback_tushare_dags.py:31-38
  循环生成模式；manual 默认 dry_run）。
- **拉数**：`airflow/tasks/cn_valuation_daily.py` 以内部 openapi app 调
  `analysis.xs.eval` 按 symbol 分组求值（HIST_STATS 序列 + §2 配方单日码），
  写 spider 信封 raw（`RAW_DB/RAW_COLLECTION` 模式，cn_buyback_tushare.py:33-34，
  「一票一文档，upsert=整票替换」幂等）。
- **入库**：复用 `do_clean_data`（DIM_DATE=trade_date 分区键实现：
  airflow/bssaa/config.py 的 `dt_name` 映射），`clean_type="radd"` 只重清洗
  受影响 symbol（cn_buyback_tushare_dags.py:121-133）。
- **及时性保障**：
  - 抽样阶段样本小（~100 票 × 8 字段），逐日调用即可满足 SLA；周批量
    （一次 6m 窗口调用产 5 个交易日行）记为**扩全市场时的优化项**；
  - 16:25 若日 K 未齐：重试兜底至 20:59（照 query_index `*/10` 收盘窗轮询
    模式，xs_cron.json:182-194）；次日不补昨日（昨日行由当日运行时的
    asof 增量补齐——照 valuation_trend append_key 模式）；
  - **节假日/asof 门控**：数据未变空转跳过，幂等不写坏
    （valuation_trend.xs:464-501 同款）；法定节假日靠覆盖率门闩
    （`_kline_ready.xs:11-17`，ready_ratio 0.995）；
  - **freshness guard**：挂 `daily_data_guard` 体系（airflow/daily_data_guard_dag.py）
    ——D+1 盘前检查每 symbol 最新行日期 = 最近交易日，迟到告警；
  - 预算纪律：分组 + 超时 + 重试上限三件套照
    `xs_cron_valuation_trend.json` 的 11-14 死锁事故复盘执行。

## 5. 历史回填：抽样先行，样本票一次补齐全字段 × 全历史

**为什么回填是诚实的**：输入 store（kline_normal、财报、equity_change、bonus）
不可变且计算确定性 → 同一公式在历史日期上算出的值 = 当天现算值。服务端
**就是公式本体**，回填不存在客户端重算的口径漂移问题。

**Rollout（定稿）**：
1. **样本组**：`index.hs300` 成分 ∩ `stocks.list`（上游实测 100 票口径；
   indices.members 全量拉取 12,754 EC，roadmap §5.7）——大中盘、行业分布
   好、样本规模使逐日调用成本可忽略；
2. 样本票**一次补齐全字段 × 上市首日起全历史**（pe/pb/ps/pr 用 HIST_STATS
   3y 分块回走，块间重叠自洽校验；mv/turnover/dv_ttm 按 §2 配式对历史逐段
   求值；equity_change 从 epoch 拉全量前向填充——vfunc_hist_pct.go:2357-2366
   现成）；
3. **扩全市场判据（硬性，三条同时满足）**：样本组连续 **20 个交易日**链路绿
   ＋ 门 A 对账 **100%** ＋ 单票 fuel 实测入预算表（`HIST_STATS(6m)` 单次
   fuel 由样本 DAG 首跑直接产出，无需单独 probe）；
4. 扩全市场（~5.7k 票）复用同一 DAG 参数化（universe 参数），分组错峰。

**正确性双门（缺一不可）**：
- **门 A（服务端内）**：物化/回填序列与 `valuation.hist_stats` 现算逐值对账，
  重叠区间 100% 一致；不一致 = 物化核与现算核分叉，阻断上线；
- **门 B（消费端）**：回填序列与 wmshare-qlib 层③已攒每日真值对账
  （`resume.load_panel_with_tail` 的 reconciliation 纪律）；层③攒得越久，
  此门越强。

## 6. 读端点：`valuation.daily_basic(symbol, begin, end, fields)`

- **服务端**：`script/sys/valuation/query.xs` 新增 `xs:catalog` 函数（照 `sys/`
  现有域函数注解规范），返回统一信封 `{ok, as_of, data, meta}`（2.x 门面契约，
  CHANGELOG 2.0.0），`data` 为长表 `[date, symbol, field, value]`（与 qlib
  层①管道同构）。**不需要新 Go 代码/新 HTTP 路由**——SDK 经
  `POST /v1/analysis/xs/eval` 按名调用（winmale/biz/openapi/register.go:11），
  内部 `store.query("valuation_daily", ...)` ts 区间读。
- **SDK 侧**（wmshare 仓）：`_catalog/domains/valuation.yaml` 加函数行 +
  `tools/extract_from_scripts.py` 同步 xs:catalog；`api/__init__.py`
  ValuationApi 加 `daily_basic(symbol, begin, end, fields=None)`（信封直通，
  与 hist_stats 同款 `_envelope` 风格）；`client.py` `estimate_cost` 加
  `cached_eval` 分支（先例：hist_stats「序列走缓存实测整次 ≈4 EC」，
  client.py:320-323）。
- **demo 策略**：罐头样本组少量真实值（如 600519 一年），或显式
  `demo_unsupported`——不伪造（2.0 demo 纪律）。
- **定价**：cached_eval 模型（近似免费读）；如需独立计费用 app 级
  `ECBillingPolicy`（winmale/internal/openapigw/ec_billing.go:231, :79-85
  admin 旋钮）；不动 `kernel/meta/cost_cu.go` 单价表。

## 7. 分期与验收（定稿）

| 期 | 内容 | 验收 |
|---|---|---|
| P0 样本组 | hs300 样本 100 票：全字段物化 + 全历史回填 + 端点上线 | 门 A 100%；门 B（层③已有天数）通过；单票 fuel 入预算表；连续 10 个交易日 SLA 达成（17:00 前） |
| P1 扩全市场 | ~5.7k 票复用同一 DAG | P0 三条扩容判据全绿后执行；分组错峰 + freshness guard 全量覆盖 |
| P2（可选） | screener 历史索引 as-of 截面（按日期查历史物化索引） | — |

## 8. 各仓改动锚点清单（评审排期后执行）

> **落地记录（2026-11-18）**：下表全部行已实现并通过闭环验证。实现态与设计的
> 两处偏差（均为设计内演化）：① kernel `normalizeReadSymbol` keep-prefix 分支
> 加了 `CatAltValuationDaily` 一行（mapping.go 头部的 SYNC 纪律本就要求
> kernel/analysis 同步比对，属注册表数据行，非逻辑）；② SDK `fields` 参数定稿
> 为 ARR（合同测试样例值语义），XS/门面/inventory 三处一致。

| 仓 | 文件 | 动作 | 底层？ |
|---|---|---|---|
| storage | `storage/v2/mapping.go:33-60` + `collection_registry.go:24-58` | catalogMappings 加 `valuation_daily` 一行 + UniqueKeySymbolSamplingTs 注册（**唯一直接触碰的 Go：注册表数据行，无新逻辑**；storage 库重编译部署） | 是（1 行数据） |
| airflow | `airflow/dag_registry.yaml:205-213` 附近 | 加 `pipeline.cn.valuation_daily`（manual+daily） | 否 |
| airflow | `airflow/auto_pipeline/cn_valuation_daily_dags.py`（新） | 四段式 DAG（照 cn_buyback_tushare_dags.py） | 否 |
| airflow | `airflow/tasks/cn_valuation_daily.py`（新） | 内部 eval 拉数（HIST_STATS + §2 配式）→ raw 文档 | 否 |
| airflow | `airflow/bssaa/config.py` | 加清洗规则（DIM_DATE=trade_date、name_prefix） | 否 |
| airflow | `airflow/daily_data_guard_dag.py` | freshness guard 检测器 | 否 |
| script | `script/sys/valuation/query.xs` | 新增 `valuation.daily_basic` xs:catalog 读函数 | 否 |
| wmshare | `_catalog/domains/valuation.yaml` + `api/__init__.py` + `client.py` | 函数目录 + 门面 + estimate_cost 分支 | 否 |
| winmale | （无代码改动） | 内部 openapi app 配置；可选 ECBillingPolicy | 否 |
| wmshare-qlib | `config.py`/`fetch.py`/tests/docs | 上游上线后另立实施计划（HISTORY_FIELDS 纳字段 + 层③对账） | 否 |

## 9. 风险与红线

- ✗ **不做请求时现算**：单键全量重算 3.2M fuel/39s（GUIDE.md:303）是服务端
  明确拒绝的形态——一切走物化；
- ✗ **不新增 kernel 宿主函数 / 不抽 HIST_STATS 内核**：全局 vfunc 原样复用，
  kernel 零改动；
- ✗ 物化核与现算核不分叉（门 A 强制）；
- ✗ 预算纪律不可省（分组 / 超时 / 重试上限，11-14 事故教训）；
- ✗ 单位落库即基础单位，消费端零换算；
- ✗ SLA 违约可见：freshness guard D+1 盘前必检，迟到即告警；
- 实施首周实测点（非阻塞，是 P0 验收动作的一部分）：
  `HIST_STATS(6m)` 单次 fuel（样本 DAG 首跑直接产出）、样本组单票全历史
  回填成本、扩全市场分组粒度。
- **读 EC 实测（2026-11-18 生产，winmale 20261118110034 窗后）**：
  单票全量 8 字段无过滤读 = **18,317 EC/次**；begin/end+字段过滤读
  （13 交易日 × 2 字段）= **2,038 EC/次**——§6"cached_eval 近似免费
  （≈4 EC）"假设**证伪**（store 整 key 扫描后过滤，成本随行数走）。
  消费侧预算按此锚修订；读优化（窗口裁剪/列存投影/limit 联动）列为
  后续项。SDK `estimate_cost` 的 daily_basic 档注记待下次 SDK 发版同步。
