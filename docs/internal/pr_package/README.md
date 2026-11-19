# 上游 collector PR 素材包（未提交）

状态：**素材已备，未提 PR、未签 CLA**。所有对外动作由仓库所有者在确认后本人执行。

## 一、提 PR 前置缺口（2026-11-17 实测）

| 前置 | 实测结果（2026-11-17） | 结论 |
|---|---|---|
| 可 review 的公开仓 | ✅ 已就绪：`open-winmale/wmshare-qlib`（public，2026-11-17 创建） | PR 需要 reviewer 能访问源，此前置已满足 |
| 第三方数据依赖 | collector 依赖 `wmshare` SDK | 上游需先决定是否接受外部数据商依赖，不应假设会被接受 |
| CLA | 微软 CLA 是法律签署 | 只能由账号所有人在 PR 触发后本人完成，代理无效 |

**因此本目录只提供"可提 PR 的内容"，不代替任何对外动作。**

## 二、规范依据（均来自官方仓实读，不臆测）

读自 `microsoft/qlib` 官方仓（GitHub API 只读，2026-11-17）：

- `scripts/data_collector/README.md`：自定义采集需在 `scripts/data_collector/<name>/` 下建 `collector.py`，
  定义 `XxxCollector(BaseCollector)` / `XxxNormalize(BaseNormalize)` / `Run(BaseRun)` 三个类；
  `sys.path.append(str(CUR_DIR.parent.parent))` 以解析 `data_collector.base` 导入。
- 同 README：**Features[Price/Volume]、Calendar、Instruments 必需；未复权则 factor=1**。
  本项目 wmshare 路径用后复权价 + 真 `factor = hfq/raw` 列（live 默认
  `hfq_factor`，与 Qlib「factor = adjusted/raw」自身约定一致）；`factor=1`
  规则仅适用于无复权源（demo 仍 qfq_const）。
- `scripts/data_collector/base.py`：`BaseRun` 抽象方法为
  `collector_class_name` / `normalize_class_name` / `default_base_dir`。
- `scripts/data_collector/yahoo/collector.py`：真实 CLI 入口是 `fire.Fire(Run)`。

> 诚实边界：wheel 分发的 pyqlib **不含** `scripts/`，所以上述规范来自官方仓源码而非本地 vendor；
> 本地无法对 collector 骨架做集成运行验证（缺少 `data_collector.base` 模块）。骨架的规范符合性靠逐条对照官方文档，
> 不声称"已跑通"。

## 三、素材清单

| 文件 | 内容 |
|---|---|
| `collector.py` | 三段式骨架：`WmshareCollector` / `WmshareNormalize` / `Run`，方法签名按官方基类 |
| `README.md` | 本说明（前置缺口、规范依据、提交步骤、诚实边界） |

## 四、提交步骤（由仓库所有者执行，代理无效）

1. ~~先将 `wmshare-qlib` 公开为可 review 的仓库~~ ✅ 已完成（2026-11-17 创建，见第一节）。
2. 与上游维护者沟通是否接受外部数据依赖（issue 先行，避免直接 PR 被拒）。
3. 将 `collector.py` 按 Qlib 目录约定放入 `scripts/data_collector/wmshare/`，
   并补齐 `BaseCollector` 所需的 `instrument_list` / 路径配置等运行时细节（骨架已标注 TODO 语义处）。
4. Fork 后开 PR；按机器人提示签署微软 CLA（本人）。
5. PR 描述模板：

```text
Title: [Data Collector] Add wmshare A-share/HK collector

Summary:
- Adds a data_collector module sourcing CN/HK OHLCV + valuation from wmshare SDK.
- Emits a Qlib-native .bin panel with the REAL ``factor = hfq/raw`` column
  (back-adjusted prices, Qlib's own adjusted-price convention).
- Follows the 3-class structure in scripts/data_collector/README.md.

Motivation:
- Precomputed A-share metrics as Qlib factors; reproducible panel dump.

Testing:
- Offline dump + Qlib D.features readback verified in the linked repo.
- NOTE: CLA to be signed by author.
```

## 五、明确不做

- ✗ 不 push 仓 / 不建仓 / 不改 GitHub 任何设置。
- ✗ 不代为签署 CLA（法律行为）。
- ✗ 不声称"已贡献/已收录"——STAR 传导是推断非实测，上游排期不受我们控制。
- ✗ 不把含第三方数据商名的文件放进 wmshare 主仓 git 跟踪面（禁词门禁）。
