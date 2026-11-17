# 立项方案：wmshare 服务端 `valuation.daily_basic`（每股每交易日估值宽表）

> 状态：**方案完成，待评审立项**（2026-11-17）。本文档是
> [upstream-requests.md](upstream-requests.md) 请求 1 的正式设计。
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

## 3. 存储选型：storage V2 catalog 新表（否决 memo 裸键）

**选**：V2 catalog 新表 `valuation_daily`——per-symbol 时序文档（每 symbol
每日一行、ts 降序存储），与 `kline_normal` 同构：
- 注册：`storage/v2/mapping.go:33-60` `catalogMappings` **加一行**（注释明说
  「新增数据类型时在此处添加一行即可」）+ `collection_registry.go:24-58`
  按 `UniqueKeySymbolSamplingTs` 注册（分红/公告类同款唯一键）。
- 读路径：与 HIST_STATS 的 `getStoreDocs`（kernel/analysis/vfunc_hist_pct.go:751-765，
  `WithStartTs/WithEndTs` ts 区间查询）完全同构——任意 `begin/end` 窗口天然支持。

**否决**：memo 裸键方案（indices.trend 模式，`m_valtrend_series:v1:<key>` +
`__cachettl_forever`）。理由：该模式为「56 键 × ~730 个周点」设计（读侧
script/sys/index/query.xs:291-303），5.7k 票 × 全历史日频是 ~4 个数量级更重的
行数，memo 热存扛不住；且 memo 键无 ts 区间索引，任意窗口查询要全量回读。

**留用 memo 之处**：只在「最新一日截面」用 memo/指针（现成 `qi_serving_index`
promote 指针机制，script/sys/dview.xs:93-99）做热路径，历史一律走 store。

## 4. 物化 job：XS cron 轨道（照 valuation_trend 的纪律）

新文件两个，零 Go 改动：
1. `script/xs/cron/materialize_valuation_daily.xs` — 计算今日行并 append：
   - **增量优先**：照 `valuation_trend.xs:9-18` 的 append_key 模式——只算
     `cached.asof` 之后的缺口日，秒级完成；asof 未变空转跳过；结构异常/
     缺口超阈值自动 fallback 全量重算该 symbol。
   - **计算口径**：P0 直接调 HIST_STATS 同核（或抽其内核为共享宿主函数），
     保证物化值 ≡ 现算值（这是 §5 对账门的成立前提）。
2. `script/config/xs_cron_valuation_daily.json` — job 声明（schema 照
   `xs_cron_valuation_trend.json`）：
   - EOD 增量：`20 16 * * 1-5` 起、symbol 分组错峰（每 3 分钟一组，照
     valuation_trend 的错峰表）；
   - 周六全量校验重算：`vt_full` 同款（重算近窗逐值比对，漂移告警）；
   - **预算红线**：照 `xs_cron_valuation_trend.json` 头部 11-14 死锁事故复盘
     ——分组 + `job_timeout` + `on_overlap: skip` 三件套不可省。
- 导入：winmale admin 后台 selective import（`winmale/internal/xscron/xs_cron_import.go:13`
  「selective import from config/xs_cron*.json」，预览勾选导入）。

计算量级估算（P0）：4 字段 × 5.7k 票日行。单票单日 = HIST_STATS 同核的
增量边际（读当日 kline_normal + 最近 4 个财报期），远轻于现算 3y 序列
（GUIDE.md:303 的 3.2M fuel 是全历史重算，非日增量）。**未验证项**：单票日增
实际 fuel 需首个试点 job 实测后再定分组粒度。

## 5. 历史回填：回填即真值（与客户端重算的本质区别）

**为什么回填是诚实的**：输入 store（kline_normal、财报、bssaa_equity）不可变
且计算确定性 → 同一公式在历史日期上算出的值 = 当天现算出的值。这正是
wmshare-qlib 否决「客户端用原始数据重算」的原因（公式不可验证），而服务端
**就是公式本体**——回填不存在口径漂移问题。

**分期**：
1. 回填试点：全市场近 **5 年**（约 7M 行），先核单票成本与对账通过率；
2. 扩展至**上市首日全历史**（1991 起，~28M 行）——按 symbol 分组分批，
   每批跑完即对账，不追求单 job 完成。

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

| 仓 | 文件 | 动作 |
|---|---|---|
| storage | `storage/v2/mapping.go:33-60` | catalogMappings 加 `valuation_daily` 一行 |
| storage | `storage/collection_registry.go:24-58` | 注册 UniqueKeySymbolSamplingTs |
| kernel | `kernel/analysis/`（新或扩展 vfunc） | 物化读函数：按 symbol+窗口读 `valuation_daily`（照 vfunc_hist_pct.go:751-765 模式） |
| script | `script/xs/cron/materialize_valuation_daily.xs` | 新增：EOD 增量 append + 周六全量校验 |
| script | `script/config/xs_cron_valuation_daily.json` | 新增：错峰分组 job 声明（含预算注释） |
| script | `script/sys/valuation/query.xs` | 新增 `valuation.daily_basic` xs:catalog 函数 |
| wmshare | `_catalog/domains/valuation.yaml` + `api/__init__.py` + `client.py` | 函数目录 + 门面方法 + estimate_cost 分支 |
| winmale | （无代码改动） | admin 后台导入 cron json；可选 ECBillingPolicy 配置 |
| wmshare-qlib | `config.py`/`fetch.py`/docs | 上游上线后另立实施计划 |

## 10. 风险与红线

- ✗ **不做请求时现算**：单键全量重算 3.2M fuel/39s（script 仓实测，GUIDE.md:303）
  正是服务端明确拒绝的形态——一切走物化；
- ✗ 物化核与现算核不分叉（门 A 强制）；
- ✗ 预算三件套不可省（分组 / job_timeout / on_overlap skip，11-14 事故教训）；
- ✗ 流通股本源未 probe 前，P1 字段不承诺；
- ✗ 单位落库即基础单位，消费端零换算（2.0 units.yaml 语义）；
- 未验证清单：单票日增 fuel、流通股本 store 有无、回填全历史总成本
  （P0 试点实测后再钉分组粒度）。
