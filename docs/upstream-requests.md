# 对 wmshare 服务端的需求清单（upstream requests）

> 状态：需求论证完成（2026-11-17，基于 wmshare SDK v1.22.2 源码穷举 +
> `../script` 平台仓架构对照）。提需求/排期由仓库所有者在 wmshare 侧执行；
> 本文件只固化「要什么、为什么客户端造不出来、上游做了解锁什么」。
> 延续诚实契约：每项写清证据与边界，不把推断写成结论。

## 背景一句话

wmshare 的 2,752 个预计算指标是**服务端 cron 预热的当日截面物化**
（GUIDE.md:303：单键全量重算 ~3.2M fuel / 39 秒，故不在请求时现算），
SDK 指标面（`screener.query` / `indicator.*`）无任何日期参数、无历史序列端点
（`api/__init__.py:1060-1062,1121,1601-1604`；官方边界声明
METRIC_ADAPTER.md:149-156）。个股日频序列存储服务端只有 4 类：
日K（全历史）、moneyflow（≤5y 区间）、margin（limit 制）、cbond 转债 K；
估值 PE/PB/PS/PR 有 ≤3y 日频（`valuation.hist_stats`，PIT）。

对照：tushare `daily_basic` 能给每股每交易日 PE/PB/市值/换手历史，
是因为其服务端存了**按日物化的派生指标宽表**。wmshare 已有同架构先例：
`indices.trend` 就是按周物化约 14 年的指数/行业估值序列（56 键 × ~730 期，
服务端预热缓存，GUIDE.md:298-304）——按日物化是现成能力的密度扩展，
不是新架构。

## 为什么不在客户端硬凑（红线依据）

客户端用原始数据重算派生指标有两条堵死的路：

1. **公式保真无法验证**：服务端精确口径（窗口日历、复权基准、ST/停牌处理、
   分位的分母 universe）不公开。只有「今天的值」一个锚点，自建序列在其他日期
   会**静默漂移**——这比复制粘贴造假更隐蔽，违反本仓「不伪造历史」红线
   （README "What we deliberately do NOT do"）。
2. **底稿与成本不够**：分位/排名类指标需要 10 年全市场底稿；服务端估值序列
   3y 硬顶、kline 区间 5y 硬顶；区间查询强付 `max_limit` 根
   （63 天窗口实测 33,656 EC）。深历史重算已被 roadmap P1-2 定为
   「写进文档劝退」。

所以正确路径是**上游加数据集**，客户端等映射。

## 请求 1（最高优先）：daily_basic 等价端点

**要什么**：每股每交易日物化的估值/指标宽表 + 带日期区间的查询端点，
对标 tushare `daily_basic`。首发字段建议（SDK 已有当前值码、需求最旺）：

| 字段 | 现状（v1.22.2） | 用途 |
|---|---|---|
| `pe_ttm` / `pb` / `ps` | hist_stats ≤3y | >3y 回测、全市场横截面 |
| `total_mv` / `circ_mv` | 仅当前值（snapshot/indicator） | 规模因子、流动性 |
| `turnover_rate` | 仅当前值 | 流动性因子（tushare 最常用字段之一） |
| `dv_ttm` | 仅当前值（官方明示 DV 无 series） | 红利因子 |

形态参照现有 `valuation.hist_stats`：`symbol, metric, begin, end` →
`[[date, value], ...]`；或全市场宽表按日取（`as_of=date`）。

**架构依据**：服务端已有 cron 预热（GUIDE.md:303）+ 按周物化 14 年估值
（indices.trend）。按日物化全市场 4-7 列 ≈ 现有周日物化量的 7 倍写放大，
在现有架构内可评估。

**解锁后本仓动作**（届时 roadmap 立项）：
- 层① `.bin` 直接纳新字段（真历史，无需改契约，`config.HISTORY_FIELDS` +
  `FIELD_UNITS` 加声明即可）；
- 层③ 已积累的 PIT 序列与官方真历史做**一次性对账**（重叠区间逐值核对，
  复用 `resume.load_panel_with_tail` 的 reconciliation 纪律）；
- 截面层成本下降：日频改走本端点，`screener.query` 204k EC/月退居周频校验。

## 请求 2：screener 服务端索引子集（`--idx` 修好）

**现状**：`screener.query --idx index.hs300` 返回 "not loadable"
（服务端运行时索引只物化了全市场日频索引，probe 2026-11-17）。
**要什么**：指数域索引（hs300/zz500 等）可用于 screener 查询。
**解锁**：csi300 口径基本面截面从 ~204k EC（全市场）降到 ~11k EC（300 票），
日频基本面合成成为可能。当前绕行方案（`kline.snapshot` 显式票单，技术合成族）
只覆盖技术因子，覆盖不了 `$PE_TTM_LAST` 等基本面族。

## 请求 3（可选）：margin 计价与口径 probe

**现状**：`MarginApi.query` 是个股日频真历史（7 列），是层① `.bin` 的天然
扩容候选；但 EC 计价未 probe、demo 数据无 margin 键（2026-11-17 核实，
live-only）。**要什么**：一次官方口径确认（计价公式 + limit 制分页语义）。
**解锁**：roadmap P2-2「两融进 .bin」可落地。

## 非请求（明确不做）

- ✗ 不要求上游开放「任意指标码 × 任意日期」的现算端点——3.2M fuel/次的
  请求时现算正是服务端明确拒绝的形态（GUIDE.md:303），按日物化才是正解；
- ✗ 不在客户端用「close × 最新股本 × 报告期财务」回溯合成市值/估值历史
  冒充官方值（股本变动日失真 + 公式无法验证）。
