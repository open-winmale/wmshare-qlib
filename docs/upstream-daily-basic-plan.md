# 立项方案：wmshare 服务端 `valuation.daily_basic`（每股每交易日估值宽表）

> 状态：**方案 v2，待评审立项**（2026-11-17；v2 修正：物化写入路径改走
> airflow 四段式——「XS 直写 Mongo store」无先例，见 §3/§4；底层触碰面
> 收敛为「storage 1 行 Go 注册数据（选型 A）或零 Go（选型 B）」，kernel /
> winmale 零改动，见 §9）。
> 本文档是 [upstream-requests.md](upstream-requests.md) 请求 1 的正式设计。
> 事实锚点全部来自本地服务端 4 仓链的源码实读（winmale / kernel / script /
> storage / airflow），标注 文件:行号，可回溯。延续诚实契约：写清
> "证据是什么、还没验证什么"，不把推断写成结论。

## 1. 目标与价值

**要什么**：每股每交易日物化的估值/指标宽表 + 带日期区间的查询端点，对标
tushare `daily_basic`。首发字段分期见 §2。

**为什么值得做**（消费方 = wmshare-qlib 及一切回测用户）：
- 层① `.bin` 直接纳真历史字段（现在是 kline/moneyflow/PE≤3y 封顶）；
- 层③「每日攒 PIT」获得官方历史回填对账锚——攒的真值与回填真值互证；
- 全市场日频基本面截面从「204k EC/月」（screener 实测，roadmap §2.5）变为
  近似免费的缓存读——日频基本面组合首次可持续；
- tushare 能给历史不是魔法：它的服务端存了每日物化宽表；wmshare 已有同构
  先例（`indices.trend` 按周物化约 14 年，script/web_echo/v2index/valuation_trend.xs），
  本方案是把周频密度扩展为日频 + 下沉到个股。

## 2. 字段分期

| 期 | 字段 | 计算源 | 证据 |
|---|---|---|---|
| **P0** | `pe_ttm` / `pb` / `ps` / `pr` | **复用 HIST_STATS 计算核心**（kernel/analysis/vfunc_hist_pct.go：读 `kline_normal` 不复权 K 线 + 财报 store + `bssaa_equity` 股本，公告日 TTM、PIT 无未来函数，vfunc_hist_pct.go:176-186） | 现算先例就是它，口径零分歧 |
| **P1** | `total_mv` / `circ_mv` | close × 股本（总股本源 = `bssaa_equity`，HIST_STATS 已用；**流通股本 store 待 probe**——script 侧有 capital-change 通道但日线序列未证实） | 需先 probe |
| **P1** | `turnover_rate` | volume / 流通股本 | 同上，依赖流通股本源 |
| **P1** | `dv_ttm` | TTM 分红 / 市值（`bssaa_bonus`，HIST_STATS 的 bonus 通道同源） | bssaa_bonus 预注册见 storage/collection_registry.go:44-48 |

单位口径：**对齐 SDK 2.0 基础单位**（率=小数、额=元、量=股，wmshare
src/wmshare/_catalog/units.yaml:1-15）——物化时落基础单位，消费端零换算。

## 3. 存储选型：store 表（两个子选型），否决 memo 裸键

**先决事实（写入路径）**：本地证据中 XS 的写面只有 memo 缓存键（indices.trend
写 `m_valtrend_series:*`）与 INDEX Redis 截面（query_index 的
`INDEX_FIELD_MATERIALIZE`）——**「XS 直写 Mongo store」无先例**。store 层的
现成写入轨道是 **airflow 四段式**（buyback 同款：拉数 → raw 文档 →
`do_clean_data` 入库 → guard 对账）。因此无论选哪个子选型，**写入都走
airflow**（见 §4），XS 只做读侧。

**选型 A（推荐）：V2 catalog 新表 `valuation_daily`**——per-symbol 时序文档
（每 symbol 每日一行、ts 降序），与 `kline_normal` 同构：
- 注册：`storage/v2/mapping.go:33-60` `catalogMappings` **加一行**（注释明说
  「新增数据类型时在此处添加一行即可」）+ `collection_registry.go:24-58`
  按 `UniqueKeySymbolSamplingTs` 注册。
- **Go 触碰面 = 这 1 行注册表数据，无任何新 Go 逻辑**；代价是 storage 库
  重编译部署（kernel 的 go.work include ../storage，联动发布）。
- 读路径：XS `store.query("valuation_daily", ...)`（tag 字符串路由，先例：
  StatementsApi 的 `store.query("fin.indicator", ...)`）→ 经
  `v2.TagToCatalog`（kernel/analysis/market_reader.go:132）与 HIST_STATS 的
  ts 区间查询（vfunc_hist_pct.go:751-765）同构，任意 `begin/end` 天然支持。

**选型 B（零 Go）：复用通用 `cn.bssaa` 集合**——`name_prefix`
（如 `valuationdaily.LIST`）+ `DIM_DATE`（=trade_date）+ `Sampling.YEAR_DAY`，
buyback 的 `cn.gphghts.LIST.<code>` 就是这么进 `cn.bssaa` 的
（airflow/bssaa/config.py:1050-1068 的 DIM_DATE 分区键实现）：
- 零 storage/Go 改动；XS 读侧走 `store.find` 按 prefix 取（script 仓
  buyback.store_find_ts_plans 同款，script/sys/buyback/store.xs:22-26）。
- 代价：非专用 catalog（无 v2 路由语义），读函数要在 XS 层多做 prefix 约定；
  长期多数据集共享一个集合，键管理纪律要求高。

**评审决策点**：A = 1 行 Go 数据 + 干净路由；B = 零 Go + prefix 纪律。
两者写入侧（airflow）与读侧形态完全一致，后续可从 B 平迁 A。

**否决**：memo 裸键方案（indices.trend 模式）。理由：该模式为「56 键 ×
~730 个周点」设计（读侧 script/sys/index/query.xs:291-303），5.7k 票 ×
全历史日频是 ~4 个数量级更重的行数，memo 热存扛不住；且 memo 键无 ts
区间索引，任意窗口查询要全量回读。

**留用 memo 之处**：只在「最新一日截面」用 memo/指针（现成 `qi_serving_index`
promote 指针机制，script/sys/dview.xs:93-99）做热路径，历史一律走 store。

## 4. 物化：airflow 轨道（写入）+ XS/SDK（读取）

**写入走 airflow 四段式**（buyback_tushare 同款机器，全部复用已验证组件）：

1. **DAG 声明**：`airflow/dag_registry.yaml` 加一条
   `pipeline.cn.valuation_daily`（daily `schedule: "30 16 * * 1-5"`，EOD 后；
   manual dry_run 默认——照 cn_buyback_tushare_dags.py:31-38 的 manual/daily
   双 DAG 循环生成模式）；
2. **拉数**：`airflow/tasks/cn_valuation_daily.py` — 以内部 openapi app 调
   `analysis.xs.eval` 执行 `HIST_STATS(window="6m", metric)`（HIST_STATS 是
   全局 vfunc，kernel/analysis/vfunc_global.go:458——SDK 自己就是拼一行
   `return HIST_STATS(...)` 调它的，api/__init__.py:1666；eval 走
   `POST /v1/analysis/xs/eval`，winmale/biz/openapi/register.go:11）。
   symbol 分组拉取，取序列末点=当日值；
3. **raw 文档**：写 spider 信封 raw（`RAW_DB/RAW_COLLECTION` 模式，
   cn_buyback_tushare.py:33-34，「一票一文档，upsert=整票替换（幂等重建）」）；
4. **clean 入库**：复用 `do_clean_data`（DIM_DATE=trade_date 分区键实现：
   airflow/bssaa/config.py:1050-1068 的 `dt_name` 映射），只重清洗受影响
   symbol（`clean_type="radd"`，cn_buyback_tushare_dags.py:121-133）。

**计算量与优化**：HIST_STATS 最小窗口 6m（`_WINDOWS` 实测 6m 起可用），
日频逐日调用有 ~120× 冗余——**周批量优化**：每周五收盘后一次 6m 调用
产出本周 5 个交易日行，调用量降 ~5×（4 字段 × 5.7k 票 ÷ 5 ≈ 4.6k 调用/周）。
**未验证项**：单次 `HIST_STATS(6m)` 实际 fuel——首个试点周实测后再定
日调 vs 周批量的分组粒度（进 §10 清单）。

**预算与守护**：airflow 侧照 buyback 的 daily_data_guard / crossval 体系挂
新表检测（airflow/daily_data_guard_dag.py、data_health/）；分组与超时纪律
照 `xs_cron_valuation_trend.json` 的 11-14 死锁事故复盘执行（DAG 级
execution_timeout + 分组 + 重试上限）。

**XS cron 只做可选的读侧预热**（如最新一日截面的 memo 热路径），不做写入。

**回填 job**：同一 DAG 的 manual 模式 + 分块参数（按 symbol 组 × 3y 窗口
分块回走 HIST_STATS——3y 上限不阻碍全历史，见 §5），逐块跑逐块对账。

## 5. 历史回填：回填即真值（与客户端重算的本质区别）

**为什么回填是诚实的**：输入 store（kline_normal、财报、bssaa_equity）不可变
且计算确定性 → 同一公式在历史日期上算出的值 = 当天现算出的值。这正是
wmshare-qlib 否决「客户端用原始数据重算」的原因（公式不可验证），而服务端
**就是公式本体**——回填不存在口径漂移问题。

**分期**：
1. 回填试点：全市场近 **5 年**（约 7M 行），先核单票成本与对账通过率；
2. 扩展至**上市首日全历史**（1991 起，~28M 行）——按 symbol 分组分批，
   每批跑完即对账，不追求单 job 完成。

**机制**：HIST_STATS 窗口上限 3y 不阻碍全历史——按 **3y 分块回走**
（end=今天 → end=3 年前 → …，每块一次调用物化 ~750 行），块间重叠区间
顺便做逐值自洽校验；配合 §4 的周批量（每块可拆周粒度调用）。

**正确性双门**（缺一不可）：
- **门 A（服务端内）**：回填序列与 `valuation.hist_stats` 3y 现算逐值对账
  （重叠区间必须 100% 一致；不一致 = 物化核与现算核分叉，阻断上线）；
- **门 B（消费端）**：回填序列与 wmshare-qlib 层③已攒的每日真值快照对账
  （复用 `resume.load_panel_with_tail` 的 reconciliation 纪律）。

## 6. 读端点：`valuation.daily_basic(symbol, begin, end, fields)`

**服务端**：`script/sys/valuation/query.xs` 新增 `xs:catalog` 函数（照
`sys/` 现有域函数注解规范），返回统一信封 `{ok, as_of, data, meta}`（2.x
门面契约，CHANGELOG 2.0.0），`data` 为长表 DF `[date, symbol, field, value]`
或宽表（建议长表，与 qlib 层①管道同构）。**不需要新 Go 代码/新 HTTP 路由**
——SDK 经 `POST /v1/analysis/xs/eval` 按名调用（winmale/biz/openapi/register.go:11）。
内部读取按 §3 选型：A = `store.query("valuation_daily", ...)`（tag 路由），
B = `store.find` 按 name_prefix 取（buyback 同款）。

**SDK 侧**（wmshare 仓，上游接受后）：
- `_catalog/domains/valuation.yaml` 加函数行 + `tools/extract_from_scripts.py`
  从 script 仓同步 xs:catalog 生成 inventory；
- `api/__init__.py` ValuationApi 加 `daily_basic(symbol, begin, end, fields=None)`
  （信封直通，与 `hist_stats` 同款 `_envelope` 风格）；
- `client.py` `estimate_cost` 加分支：`cached_eval` 模型（先例：
  hist_stats「序列走缓存实测整次 ≈4 EC」，client.py:320-323）；
- **demo 策略**：罐头少量真实值（如 600519 一年）或显式 `demo_unsupported`
  ——不伪造（2.0 demo 纪律，CHANGELOG 2.0.0「不伪造数据」）。

**适配器侧**（wmshare-qlib，上游上线后另立实施计划）：
`config.HISTORY_FIELDS`/`FIELD_UNITS` 纳新字段 → fetch 直读 daily_basic 进
`.bin` → 层③ 与回填历史对账 job → screener 204k EC/月 退居周频校验。

## 7. 定价

- 读端点走 `cached_eval` 模型（近似免费读，先例 client.py:316-323）；
- 若需独立计费：app 级 `ECBillingPolicy` 覆盖（winmale/internal/openapigw/
  ec_billing.go:231 `ResolveECBill`，:79-85 admin escape hatch）；
- 单价表改动集中在 `kernel/meta/cost_cu.go:21-47`（需递增 `CostSchemaVersion`），
  并同步 docs/COST_ESTIMATION_SCHEME.md——本方案**不建议**动单价表。

## 8. 分期与验收

| 期 | 内容 | 验收 |
|---|---|---|
| P0 | 4 估值字段物化 + 端点，上线日起日更 | 门 A 100% 一致；试点 symbol 组连续 10 个交易日无缺行 |
| P0.5 | 历史回填（5 年试点 → 全历史） | 门 A + 门 B 双过；回填行数 = 预期交易日行数（停牌/晚上市诚实缺行） |
| P1 | mv/turnover/dv_ttm（流通股本 probe 后） | 新字段与平台 web 页面值抽检一致 |
| P2（可选） | screener 历史索引 as-of 截面（按日期查历史物化索引） | — |

## 9. 各仓改动锚点清单（评审通过后执行）

| 仓 | 文件 | 动作 | 底层？ |
|---|---|---|---|
| airflow | `airflow/dag_registry.yaml:205-213` | 加 `pipeline.cn.valuation_daily` 一条（manual + daily 双 DAG） | 否 |
| airflow | `airflow/auto_pipeline/cn_valuation_daily_dags.py`（新） | 四段式 DAG（照 cn_buyback_tushare_dags.py） | 否 |
| airflow | `airflow/tasks/cn_valuation_daily.py`（新） | 拉数（内部 eval 调 HIST_STATS）→ raw 文档 | 否 |
| airflow | `airflow/bssaa/config.py:1050-1068` 附近 | 加清洗规则（DIM_DATE=trade_date、name_prefix） | 否 |
| storage | **选型 A**：`storage/v2/mapping.go:33-60` + `collection_registry.go:24-58` | catalogMappings 加一行 + UniqueKeySymbolSamplingTs 注册（**唯一直接触碰的 Go：注册表数据行，无新逻辑；storage 库重编译部署**） | 是（1 行数据） |
| script | `script/sys/valuation/query.xs` | 新增 `valuation.daily_basic` xs:catalog 读函数 | 否 |
| script | （可选）读侧预热 memo | 最新截面热路径 | 否 |
| wmshare | `_catalog/domains/valuation.yaml` + `api/__init__.py` + `client.py` | 函数目录 + 门面方法 + estimate_cost 分支 | 否 |
| winmale | （无代码改动） | 内部 openapi app 配置；可选 ECBillingPolicy | 否 |
| wmshare-qlib | `config.py`/`fetch.py`/docs | 上游上线后另立实施计划 | 否 |

## 10. 风险与红线

- ✗ **不做请求时现算**：单键全量重算 3.2M fuel/39s（script 仓实测，GUIDE.md:303）
  正是服务端明确拒绝的形态——一切走物化；
- ✗ **不新增 kernel 宿主函数 / 不抽 HIST_STATS 内核**：复用全局 vfunc 原样调用
  （SDK 同款路径），kernel 零改动；
- ✗ 物化核与现算核不分叉（门 A 强制）；
- ✗ 预算纪律不可省（分组 / 超时 / 重试上限，11-14 事故教训；airflow 侧 =
  execution_timeout + 分组 + 重试上限）；
- ✗ 流通股本源未 probe 前，P1 字段不承诺；
- ✗ 单位落库即基础单位，消费端零换算（2.0 units.yaml 语义）；
- 未验证清单（P0 试点实测后收敛）：
  1. 单次 `HIST_STATS(6m)` 实际 fuel → 定日调 vs 周批量；
  2. `store.query` 的 tag 白名单机制（若 XS 域层另有 tag 枚举，选型 A 需在
     script 层同步加一行——仍是脚本层，非 Go）；
  3. 流通股本 store 有无（P1 前置）；
  4. 回填全历史总成本（5 年试点先核）。
