# 后续优化方案调研（2026-11-17）

> 状态：**调研 + P0 关键 probe 已完成（2026-11-17，消耗 52,907 EC）**。
> 成本模型已从"缓存波动"修正为**确定性计费公式**（见第二节 probe 结果），
> P0-1 增量架构因此改道（区间查询方案已证伪弃用）。
> 所有锚点标注来源（实测/SDK 文档/推断）。延续 README 的诚实契约：
> 每条优化都写清"证据是什么、还没验证什么"，不把推断写成结论。

## 一、现状与瓶颈（一句话版）

M0–M5 闭环已验证，管道正确性由四道数据门看守；真正卡脖子的是两件事：

1. **EC 成本结构**：单票历史 ~4,275 EC 中估值序列占 ~2,848 EC（4 × ~712，
   实测 2026-11-17）；全市场截面 `screener.query` 4 轻列 ~204,430 EC/天，
   日频不可持续。
2. **`qfq_const factor=1.0` 的增量一致性缺陷**：qfq 锚点随每次除权整体重算，
   不同日期拉的旧片段互不一致（manifest 的 params 守卫只挡参数不同，
   挡不住同参数下的值漂移）。

## 二、关键发现：SDK 三个尚未利用的能力

读自 wmshare SDK `src/wmshare/api/__init__.py`（2026-11-17，本地安装 1.20.2）。
适配器目前只用了 SDK 表面的一部分，以下能力直接解锁三条优化线：

| SDK 能力 | 证据 | 适配器现状 | 解锁的优化 |
|---|---|---|---|
| `kline.bars(begin, end, adj)` 日 K **区间查询**（单次 ≤5 年 / 1 万根上限）且 `adj=None/qfq/hfq` | `KlineApi.bars` docstring："日 K 覆盖自上市首日起的全部交易日（最早 1991 年）… 区间上限 5 年" | 只按 `n` 根数拉取，只用 `qfq` | 增量更新、真实 factor、深历史分块 |
| `moneyflow.bars(begin, end)` 区间查询 | `MoneyflowApi.bars` docstring："区间查询时默认取 max_limit（上限 10000）" | 只按 `n` 拉取 | 资金流增量更新 |
| `kline.snapshot` 批量：**23 字段/票，批量 ≈58 EC/票**（逐票首算 10,250 EC/票） | `KlineApi.snapshot` docstring：含 `close/percent/volume/amount/chg_5d/chg_10d/chg_20d/chg_90d/roi_1y/ma20/ma60/ma120/ma60_up/vol_ratio_5/mv/mv_free/symbol/name/industry/is_st/is_suspend/kline_date/kline_sas` | 未使用 | 截面层降本、股票池卫生过滤 |

另确认两个**服务端硬边界**（不是我们能优化的）：

- `valuation.hist_stats` 窗口封顶 **3y**（`_WINDOWS = ("6m","1y","2y","3y")`，
  注释"实测 6m 起可用；上限 3 年"）→ 估值深历史只有 accumulate 一条诚实路径；
- `kline.bars` 区间单次上限 **5 年** → 深历史需分块（可做，EC 线性增长）。

## 二点五、live probe 结果（2026-11-17 实测，总消耗 52,907 EC）

对 600519（及批量路径 5/3 票）发 9 次请求，得到**确定性计费公式**——
推翻了 README 里"EC 随缓存状态波动"的旧解释（波动来自取数根数不同，不是缓存）：

| 调用 | 实测 EC | 拟合 |
|---|---|---|
| `kline.bars n=10, qfq` | 826 | **kline = 156 + 67 × 服务根数** |
| `kline.bars n=60, qfq` | 4,176（重复两次完全一致） | （n=60: 156+4020=4176 ✓） |
| `kline.bars n=60, hfq / adj=None` | 4,176 / 4,175 | 复权口径**不影响**计费 |
| `kline.bars begin/end（~63 交易日窗口）` | **33,656** | **区间查询强付 max_limit（默认 500）根**：156+33500=33656 ✓；返回 70 行 > 窗口交易日数也印证"先取满再客户端过滤" |
| `moneyflow.bars n=15` | 396 | moneyflow = 156 + 16 × 根数 |
| `kline.batch_live ×5 票` | 226（45.2/票） | **当日 OHLCV 批量，最便宜的单票日频路径** |
| `kline.snapshot ×3 票` | 300（100/票） | 23 字段 ≈ 4.3 EC/字段（docstring 说 ≈58/票，N=3 实测 100，小批量有开销） |

由此得出的**三个改道级结论**：

1. **区间查询（`begin/end`）不能用于增量**——比等量 n 根数贵 8 倍
   （窗口外根数照付）。原 P0-1 的"区间增量"方案证伪弃用。
2. **demo 客户端不支持 `begin/end` 与 `adj`**（显式 UsageError："demo 快照只覆盖
   不复权、不带日期区间的 kline.bars(n<=120)"）→ 增量与真实 factor 均 live-only，
   demo 侧需诚实降级说明（假设 2 已回答）。
3. ~~**日频化的正确路径是 `batch_live`**~~ **（2026-11-17 实现时修正）**：
   batch_live 的盘中价是 **raw 基准**（成交价必然未复权），其 `percent`
   字段在除权日的口径未经实测且无法按需 probe（除权日不可预约）——直接
   用它追加 hfq 面板是未验证捷径，P0-1 v1 改用**双拉 n 尾巴**（bars(n=k)
   同时给出 hfq/raw/factor，确定性计价）。batch_live 保留为后续优化候选，
   前提是除权日 percent 口径得到验证。速算保持有效：csi300 日频
   batch_live ≈ 13.6k EC/天 vs 双拉尾巴 k=6 ≈ 134k EC/天（6.5× vs 全量）。

> ⚠️ README"EC transparency"节的两条旧锚点与本模型冲突，待一次全 bundle
> re-probe 后修订：①"kline itself is cheap"——实测 n=60 单独就是 4,176 EC，
> 比 4 支估值（~712×4）还贵；②"EC volatile 322–9763"——确定性公式下，
> 波动应来自取数根数/字段数差异，非缓存状态。

## 三、优化方向与优先级

### P0-1 增量历史模式（probe 后改道：batch_live 日频 + n 尾巴修复）

> **状态：v1 已实现（2026-11-17，Tier-1 60 passed / Tier-2 61 passed）。
> v1 = 双拉 n 尾巴（`update-history` 命令），batch_live 路径推迟**——
> 实现中发现 batch_live 的盘中价是 raw 基准，其 `percent` 字段在除权日的
> 口径（价格涨幅 vs 全收益）无法按需 probe（除权日不可预约），直接用它
> 追加 hfq 面板是未验证捷径。v1 每票 `bars(n=k, adj=hfq) + adj=None` 双拉
> 直接给出 factor，正确性现在即可验证。

~~原方案"区间查询 begin/end 增量"已证伪弃用（见第二节 probe 结论 1）。~~

**已实现的架构（v1）：**

- `resume.py`：tail 片段类（`tail/<sym>.parquet`）+ manifest 记录
  `bulk_date/last_date/tail{last_date,valuation_last_date}` +
  `load_panel_with_tail` 重叠行 reconciliation 门（不可变历史必须一致，
  否则响亮拒绝——上游修订绝不静默重写）；
- `fetch.py`：通用逐票执行器提取（`_execute_per_symbol`，EC 天花板/并发
  纪律单点化）+ `_fetch_tail_one_symbol`（kline 双拉 + moneyflow 尾巴 +
  估值按节奏）+ `fetch_history_tail`（计划阶段零 EC：gap≤0 跳过、
  k>tail_max_n 跳过、估值到期判断）；
- `cli.py`：`update-history`（live-only、hfq_factor-only、--max-ec 必填、
  前置 convention 守卫拒绝旧 store，合并后全量重 dump——dump 是本地 IO
  零 EC，增量的只在抓取端）；
- **诚实成本重算**：日常运行 k≈6（gap 1 天 + overlap 5）≈
  2×(156+67×6) ≈ **1.1k EC/票/天**（原 roadmap 的 45 EC/票 是 batch_live
  路径的数字，v1 未采用；7.4k 全量 → 6.5×，非 9.4×）。聚合口径：csi300
  的估值周刷 ~854k EC/周是主导项——`--valuation-days` 是预算杠杆，
  组合规模按预算定，工具在发射前用 --max-ec 强制声明。
- **幂等**：同日重跑 gap=0 → 全部跳过，0 EC。
- 待办：live 真实链路一次验证（与 P0-2 的待办合并执行）。

### P0-2 真实 factor 列，替换 `qfq_const=1.0`

> **状态：已实现（2026-11-17，Tier-1 49 passed / Tier-2 50 passed）。**
> 采用方案 b：live 默认 `hfq_factor`（双拉 hfq+raw，`factor = hfq/raw`，
> volume ÷ factor——probe 实测上游 volume 不随复权），demo 强制 `qfq_const`。
> Qlib 约定已对照上游 `YahooNormalize1d.adjusted_price` 源码确认
> （factor = adjusted/raw，raw = close/factor，volume_adj = raw/factor）。
> 落点：`config.adjustment_mode` / `normalize.merge_adjusted_klines` /
> `fetch._fetch_one_symbol` 双拉分支 / `panel` factor 单调门 /
> `resume` 新参数键守卫 / `cli --adjust`。

README 声明 qfq 已复权所以 factor≡1，自洽但隐藏了一个正确性问题：
**qfq 历史值随每次除权整体重算**，跨除权事件的两个 fragment 互不一致，
现有任何守卫都不检测这一点。方案（二选一，推荐 b）：

- a) 同票双拉 `qfq` + 原始价（`adj=None`），`factor = qfq/raw`；
  满足 Qlib 不变式 `raw = close / factor`；qfq 片段仍会过期（方案 a 不解决）。
- b) **改用 hfq 为存储基准**：hfq 历史值不随后续除权变化 → 追加安全，
  fragment 永不过期；`factor = hfq/raw` 单调不减，可在 dump 门里校验
  （除权日跳变、其余日期恒定）。回测收益在 qfq/hfq 下一致（同为全复权口径）。
- 代价：每次多一支 kline 调用（kline 便宜，量级可接受）。
- 风险：会改变已 dump 的值 → 现有 verify 锚点全部重建，需迁移说明 +
  重跑 Tier-2 读回；`panel.assert_panel_integrity` 增加因子单调/跳变门。

### P0-3 截面层降本：节奏分层 + snapshot 批量替代

> **状态：v1 已实现（2026-11-17，Tier-1 69 passed / Tier-2 70 passed）。
> v1 = `--via snapshot`（kline.snapshot 批量技术合成），选型依据**：服务端
> `--idx` 不支持子集（`index.hs300` not loadable），且 screener 按**扫描面**
> 计费、无法客户端缩小扫描面——snapshot 批量天然接受票列表，是**唯一
> 操作者可控扫描面的路径**。screener 保留为全市场月频口径。
> 待 probe：N=300 大批量单价（docstring 称 ~58/票，N=3 实测 ~100/票）；
> live 真实批量一次验证。

全市场 4 轻列 ~204k EC/天不可持续。已实现的分层：

- **`--via snapshot` 日频（hs300 口径）**：`config.SNAPSHOT_METRICS` 技术合成
  （chg_20d / roi_1y / ma60_up higher_better，mv lower_better——声明式方向，
  z-score 合成；与 screener 基本面族**永不混装**，metering note 标注族别）；
  23 字段 ≈ 2.5-4.3 EC/字段，比 screener 轻列（~9 EC/行/列）便宜；
  ~300 票 ≈ 17-30k EC/天；
- **卫生门**：universe 与 stocks.list 求交；ST / 停牌行剔除并告警
  （永不计分）；任一指标缺值的行剔除（anti-pitfall #5，永不补 0）；
- **守卫**：≥3 票（少了会静默回退逐票计费 ~10,250 EC/票）；`--max-ec` 必填、
  `client.last_ec_cost` 后验；
- **指数成分文件由操作者提供**（真实 csi300 成分源是 P1-1 的 universe
  文件项，不静默猜）；
- 注意点（已实测 2026-11-17，见第二节）：
  - `kline.snapshot` N=3 实测 **100 EC/票**（docstring 说 ≈58/票，小批量有
    开销），大批量（N≥300）单价待 probe；
  - 批量路径要求 ≥3 票且同市场（`_assert_same_market`）；
  - ETF/索引外标的**回退逐票计费（单票 10,250 EC）** → 必须先用
    `stocks.list`（66 EC，5,693 名）过滤非股票 universe；
  - `kline_date/kline_sas` 在批量路径为 None（当日索引未物化）。
- 节奏分层建议：hs300 日频（snapshot 或轻列）+ 全市场周频（snapshot 批量）
  + 已测列的离线重建（README 已有此口子）。

### P1-1 股票池卫生与 universe 文件

> **状态：已实现（2026-11-17，Tier-1 88 passed / Tier-2 89 passed）。
> `write-universe` 命令**：`--index <key>`（live，真实成分源
> `indices.members`——SDK 里确实有这个端点，与 screener 的 not-loadable
> 服务端索引是不同域；成本待 probe）或 `--codes-file`（qlib 式直通、
> 裸码经 stocks.list 显式映射，永不数字段位猜后缀）；
> `--exclude-st/--exclude-suspended` 卫生前置（live snapshot ~100 EC/票，
> 在昂贵的历史抓取**之前**剔除）；范围从 .bin 头推导 = dump 实际包含的
> 覆盖（停牌/晚上市成员永不伪造交易日）；面板外成员丢弃并告警。
> example 脚本的手写 `backtest_universe.txt` 已被取代。

- 用 snapshot 的 `is_st/is_suspend` 做 `--exclude-st/--exclude-suspended`；
- 按 Qlib 惯例落 `instruments/csi300.txt` 等多 universe 文件（现状只有 `all.txt` + 例子脚本现场写的临时 `backtest_universe.txt`，见 `examples/alpha158_lgbm.py` 的 `build_handler`）；
  让 `D.instruments("csi300")` 直接可用。

- 用 snapshot 的 `is_st/is_suspend` 做 `--exclude-st/--exclude-suspended`；
- 按 Qlib 惯例落 `instruments/csi300.txt` 等多 universe 文件
  （现状只有 `all.txt` + 例子脚本现场写的临时 `backtest_universe.txt`，
  见 `examples/alpha158_lgbm.py` 的 `build_handler`）；
  让 `D.instruments("csi300")` 直接可用。

### P1-2 深历史（opt-in，默认不开；probe 后改用 n 根数路径）

> **状态：已实现（2026-11-17，Tier-1 92 passed / Tier-2 93 passed）。
> `dump-history --years N`**：n = 250×N 根（n 模式无向后翻页——每次调用都
> 返回最近 n 根——所以深历史必须是单次调用 + 放大 served-bar cap，
> SDK 硬顶 10,000 根 ≈ 1991 年以来全部 A 股历史）；与 `--n` 互斥；
> dry-run 对 n>500 显式标注 `EXTRAPOLATED`（>500 根计价是线性外推，
> probe 待办）。实测口径：5y（n=1250）估 ~190.8k EC/票（hfq 双拉）——
> 深历史只适合小 universe 一次性建仓，csi300 口径明确劝退；
> 估值 3y 服务端硬顶不动，诚实路径只有 accumulate。

- ~~区间分块~~已弃用（区间查询强付 500 根起步）。n 根数路径：n=1200（5y）
  ≈ 80.6k EC/票/次——**只适合小 universe 的一次性建仓**，csi300 全建仓
  ≈ 24M EC，不可行，明确写进文档劝退；
- 估值 3y 是服务端硬顶 → 诚实路径只有 accumulate 累积，不做任何回填；
- 形态：`--years N`（内部按 n=250×N 根数取，不碰 begin/end），仅 live，
  发射前打印 `--dry-run` 预算并要求确认。

### P1-3 工程信誉件（CI/重试/dry-run 等社区可见性基础件）

> **状态：已实现（2026-11-17，Tier-1 79 passed / Tier-2 80 passed /
> ruff 清洁）。** CI（`.github/workflows/ci.yml`：py3.10/3.12 矩阵，
> wmshare 从上游仓安装，ruff 0.14.4 锚定版本，Tier-1 only——Tier-2 仍是
> 本地可选交叉验证）；README badge 已挂。

- **GitHub Actions CI** ✅：Tier-1 测试零 Qlib、零凭证、零 EC；
- **live 拉取重试/退避** ✅：`fetch.call_with_retry`——范围刻意收窄：
  只重试逐票历史调用（kline adj/raw、moneyflow、估值），瞬态 =
  TransportError/ServerError/限流 429（尊重 `retry_after`）；`QuotaError`
  （LimitError 子类！）显式排除，Auth/Usage/Api/Data 永不重试；
  **screener.query 与 kline.snapshot 刻意不重试**——物化中途超时重发是
  双重计费，`--max-ec` 是它们唯一的守卫；计费逐次记账（失败的一次可能
  已计费），重试作为 warning 上报、绝不静默；
- **`--dry-run` EC 预算器** ✅：`estimate.py` 纯函数（锚点常量收进
  `config.EC_*` 单一事实源），四命令发射前报价，超天花板告警，
  不建 store、不发请求。

### P2-1 上游 PR 跟进（⏸ 搁置，2026-11-17 用户决定）

`pr_package/` 素材已备、公开仓已建（commit e5bda6b）。qlib collector
**暂不做**（用户 2026-11-17 拍板）；素材保留，重启时下一步仍是：
issue 先行问 microsoft/qlib 维护者是否接受第三方数据商依赖
（pr_package/README 已识别这是最大不确定项）。PR 与 CLA 只能由仓库所有者本人执行。

### P2-2 其余

- **HK 支持**：kline 已覆盖港股 5 位码，但适配器只有 sh/sz/bj 映射
  （`config.EXCHANGE_SUFFIX`），README 标题写了 CN/HK —— 补齐映射或改口径，二选一；
- ~~**score 层 60 天自动晋升**~~ ✅ **已实现（2026-11-17）**：`accumulate`
  追加后自动检查守卫、通过即晋升；`--min-days` 默认接线
  `config.history_days_threshold()`（消除 CLI 独立硬编码的双事实源）；
  meta 增 `available_from` 字段，表述从「攒够 N 才能回测」改为
  **PIT 自建序列：自采集日起可用**（门槛只是晋升守卫，数据从第一天起就是真值）；
- **两融（margin）进 .bin**：`MarginApi.query` 是个股日频真历史序列
  （trade_date + 融资 3 列 + 融券 3 列），是层①的天然扩容候选；
  但 EC 计价未 probe、demo 数据无 margin 键（2026-11-17 核实，live-only）
  → 实现前需一次 live probe 定价与口径；
- **基准卡**：csi300 × 2y 跑一次 Alpha158-lite，发布诚实数字 + 总 EC 成本，
  逐步取代"demo 10 票 IC≈0.05"作为对外表述的主锚点。

## 四、建议执行顺序

```
P0-2 hfq 基准 + 真实 factor ✅ 已实现（2026-11-17）
        ↓
P0-1 增量模式（update-history：双拉 n 尾巴 + reconciliation 门）✅ 已实现（2026-11-17）
        ↓
P0-3 截面 hs300 日频（--via snapshot 技术合成）✅ 已实现（2026-11-17）
        ↓
P1-3 CI + 重试 + --dry-run 预算器 ✅ 已实现（2026-11-17）
        ↓
P1-1 universe 文件（write-universe：indices.members 真实源 + 卫生前置）✅ 已实现（2026-11-17）
        ↓
P1-2 深历史（--years N，单次调用放大 cap，外推标注）✅ 已实现（2026-11-17）
        ↓
daily_basic 上游落地 + 消费端 ✅（2026-11-18 生产上线：--daily-basic 门控 +
Gate B reconcile-daily-basic；读 EC 实测 2,038/18,317 EC 已入 README EC 表）
        ↓
P2 上游 PR / HK / 基准卡 ← 下一步（所有 P0/P1 实现项已落地；
        live 验证批：hfq 链路 ~7.5k + snapshot N=300 ~30k + indices probe，待所有者点头）
```

### 2026-11-18 daily_basic 上线纪要（上游六层全通）

storage v1.0.163 → kernel/analysis v1.5.440 → winmale 20261118110034-7233802
→ script 9eb2cb9d → wmshare 2.1.0（PyPI+MCP registry）→ 本仓 0.3.0。
生产验证：600519 门 A 值 22.179009201538978@2026-11-17 经 SDK 全链复现；
样本组 hs300=300 票 dry_run 绿。读 EC 实测（非 cached）已入 README EC 表与
方案 §9。卫生窗待办（非阻塞）：meta/less/legends pin 拉齐 + less 二进制
重部署 + dbtool 白名单补 valuation_daily + SDK estimate_cost daily_basic
档注记同步实测 EC。

## 五、probe 假设跟踪（2026-11-17 更新）

1. ~~**区间查询的 EC 定价是否与 n 根数一致**~~ → **已证伪并改道**：区间强付
   `max_limit`（默认 500）根，贵 8 倍；计费公式见第二节。增量架构已改为
   batch_live + n 尾巴（P0-1 修订版）。
2. ~~**demo 客户端是否支持 `begin/end`**~~ → **已确认不支持**（显式
   UsageError，`adj` 同样不支持）→ 增量/真实 factor 均 live-only，
   demo 诚实降级。
3. **`screener.query` 定价口径矛盾** —— 未重 probe（本次未发 screener 请求）。
   SDK docstring 声称"不计标的数（实测 70~4.8 万）"与本仓 2026-11-17 实测
   204,430 EC（随 universe 放大）矛盾；2.0 的 `estimate_cost` 模型
   （轻列 70~1,000 EC）延续乐观口径，与实测仍差两个量级。
   以实测为准，保留**每季度 re-probe**。
4. **README"EC transparency"两条旧锚点** —— 已在 README 加确定性公式更正注记
   （2026-11-17）；一次全 bundle re-probe（1 票 × kline+4 估值+moneyflow，
   估 ~7.4k EC）刷新旧锚点数字仍待执行；`snapshot` 大批量（N=300）单价待 probe。
5. ~~**P0-2/P0-1 live 全链路验证**~~ ✅ **已完成（2026-11-17，见
   benchmark-card.md）**：真实 dump → 尾巴追加 → 重叠核对 → Tier-2 读回
   全 PASS；真实 factor 除权跳变（工行 2.5546→2.6001）成立。实测口径：
   3 票 n=20 全 bundle 仅 9,270 EC（估值锚点对缓存路径偏保守）；
   服务端 EOD 最后 bar = 上一交易日，**盘中运行 update 会重复付核对成本
   （~1.2k/票/次）→ EOD 管道应收盘后运行**。`batch_live` percent 除权
   口径仍待某除权日机会性 probe。
6. ~~**snapshot 大批量单价**~~ ✅ **已完成**：N=81 实测 **66.2 EC/票**
   （N=3 时 ~100，向 docstring ~58 靠拢）；~81 票单批会挂 → 已实现客户端
   分批（SNAPSHOT_BATCH_CHUNK=30）+ 真实 81 票 score CSV 读回（5,364 EC）。
7. ~~**indices.members probe**~~ ✅ **已完成**：`index.hs300` 服务端实为
   **100 票口径**（含 19 个非 A 股码被 stocks.list 正确剔除）；
   全量拉取 12,754 EC（默认 top-20 也要 6,962 EC——probe 应直接带 top_n）；
   universe 文件机制离线+卫生路径均验证。
8. ~~**wmshare SDK 2.0.0 漂移**~~ ✅ **已适配（2026-11-17，v0.2.2）**：clean break
   到 `wmshare>=2.0.1,<3`（2.0.0 的 demo 快照旧且缺 cost_estimate）。
   2.0 破坏面四类全部落地：`_df` 全删（src 10 处 + examples 1 处）、
   序列升序（守卫翻转，管道本体不动——ascending_frame 本就统一升序输出）、
   hist_stats/screener.query 信封化（data.series DF / data.rows；1.x 平铺形状
   响亮拒绝而非静默空帧）、demo 快照重拍 2026-11-17（锚值 1453.9 @2026-11-16）。
   异常体系零变化 → 重试策略零改动。本地 87 passed 与 CI（@v2.0.1）同版本。
   单位台账换算（2.0 强制基础单位）与适配器既有单位声明一致，无需改。
9. ~~**wmshare 2.2.0 适配（`_fields` / `truncated`）**~~ ✅ **已闭环（2026-11-18，v0.3.2）**：
   floor 提到 `wmshare>=2.2.0,<3`。采纳两条新面：`_fields` 客户端列投影
   （签名探测 + 旧版回退；投影**不降 EC**——只收窄返回体）与
   `BatchResult.truncated` 静默缺票告警（截面 chunk + 卫生检查两路径）。
   验证：真实 SDK 契约测试 ×2（锁定 `_fields`/`truncated` 存在性 + 按 key
   读取容忍宽度）；**live 闭环实测**（2026-11-18，3 票）：探测器 True、
   批量投影生效（返回键恰为请求集）、truncated=False、适配器全路径出分、
   EC=300/3 票（≈100 EC/票，与既有锚一致）。
   上游已知小缺口（已记录，不影响本仓）：demo 客户端**批量**路径忽略
   `_fields`（单票路径正常投影），live 批量正常——签名对齐、语义未对齐；
   本仓按 key 读取故无感。
10. ~~**2.2.0 锚点分页计费 re-probe（P1 成本重锚）**~~ ✅ **已实测（2026-11-18）**：
   探针总耗 39,900 EC（用户批准 25k + 追加 8k 补测）。关键结论：
   - **区间查询仍按锚点窗口计费，不按返回行数**：49 个交易日窗口返回 49 行，
     实付 **33,688 EC**（= 156 + 67×500，与旧锚 33,656 一致）。2.2.0 分页解决
     的是**完整性**（任意跨度不静默截断），不是短窗口成本——故**放弃**
     「stale gap 用 begin/end 补拉」方案（33.7k vs n=60 的 4.2k，反向优化）；
   - `margin.query(limit=20)` = **2,472 EC**（≈124 EC/行），`margin.latest` =
     **182 EC**（SDK 此前对这两个接口无成本模型）；roadmap P2-2 两融进 .bin
     可按此定额；
   - `valuation.daily_basic` 读地板 = **1,707 EC**（1 日 × 1 字段），同日 8 字段
     仅 1,851 EC（字段几乎不加价、扫描才是成本），拟合 floor + ≈23 EC/doc；
     再次确认**非缓存**（SDK 的 `cache_hit 4~200` 档位错误，P3 补丁修正）。
   落地：`config.EC_*` 新增实测锚（区间窗口/margin/daily_basic floor+per-doc）；
   `estimate.daily_basic_read` / `margin_query` 新增，`--daily-basic --dry-run`
   现计入读数成本；README EC 表区间行改写为实测结论。

## 六、明确不做（延续 README 红线）

- ✗ 不用今天的估值回填历史（诚实契约红线不变）；
- ✗ 不为深历史绕过估值 3y 服务端硬顶；
- ✗ 不在 demo 路径伪装增量（demo 快照没有"新增 bar"概念）；
- ✗ 不在未 re-probe 定价前，按旧锚点承诺新的日频成本数字；
- ✗ 不触碰 wmshare 主仓（独立仓库纪律不变）。

## 十一、生产端自愈 + 消费端窗口化（2026-11-19，双完备批次）

**生产端（airflow，bf23a891 + f6f90d7c，已 release）**

11. **近窗自愈**：增量模式对近 5 交易日「缺行或 mv 缺失」的日补整行（pe 取
   6m 序列该日值 + mv AT(day)），基线读 mv 感知（残行可修）；V2 读失败退化
   只写前沿日。生产实证：缺日 11-12/13/16 × 300 自动补齐、11-17 残行
   mv 2→300 修复。raw 按 trade_date 合并（忠实累积日志，radd 重建安全）；
   mv 回走按交易日历（周末不再空转）；freshness 检测器分时段期望日 +
   近 5 日连续性（缺日 warn/error）；`dbtool valuation-daily-completeness`
   完备性审计（逐日行数 × 8 字段矩阵 + 缺日/逐票缺日清单）。

**消费端（wmshare-qlib，4df61d0）**

12. **tail daily_basic 窗口化**：resume 指针 `daily_basic_last_date` +
    `begin` 窗读——增量日更从 ≈18.3k EC/票（全史重读）降到 ≈2k EC/票；
    指针只随 RESULT 前进（空读不推进）。`estimate.tail_symbol` 补 daily_basic
    项；`reconcile-daily-basic --from-upstream --store` 门 B 自动化（官方侧
    按 accumulated 窗口拉取，消除手喂文件）；`daily_basic_to_frame` date
    落盘改 ISO 字符串（与 kline/moneyflow 一致，修掉混列 parquet 崩溃的
    潜在 bug）。
13. **服务端缺口登记**（wmshare 6fdf59e，Unreleased/Planned）：daily_basic
    分页+truncated（>1500 日静默缺席）、ts 下推、coverage API、多票批量、
    estimate_cost 再校准；扩全市场评审见
    [full-market-expansion-review.md](full-market-expansion-review.md)
    （≈177.6 万 EC/日，建议服务端批量 eval 先行）。
