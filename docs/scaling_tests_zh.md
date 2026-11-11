# 4×4—44×44 性能测试集

测试矩阵将边长 N、A 数量、C 数量分别变化：N=4…44，A=4…10，C=4…10，包含 A≠C 的交叉组合。共 2,009 个参数组合。4×4 中 A+C>16 的 10 个组合容量不合法，生成器会在清单中记录跳过原因，不会写成“无解测试”。其余 1,999 个组合各生成 3 类棋盘，共 **5,997 个用例**。

| 类别 | 生成方法 | 用途与局限 |
| --- | --- | --- |
| `open` | 开放棋盘，种子随机放置全部 A/C，内部光源保证初始分光，再从真实光路选择目标 | 已知有解；覆盖尺寸、稀疏度、反射与分光，不保证每题同等难度 |
| `obstacles` | 增加固定 A/B/C、禁止放置的 x 格，以及第二个光源 | 检查混合几何、多光源；满盘时无额外空位放障碍，实际障碍数会记录 |
| `parity_unsat` | 在同参数 `open` 的有解核心中加入 x+y 为偶数的目标 | 已知无解的快速拒绝控制，明确不是困难无解题 |

每个有解用例附独立模拟验证的隐藏布局文件。求解进程只收到 `.bff` 路径，不收到布局或目标生成信息。无解控制的布局仅证明去掉不可能目标后的核心有解。目标至少包括一个非光源点；尽量选取空置布局无法击中的点以及两侧的远端目标。清单记录目标数、无布局时漏掉的目标数、光路状态数、分支点、反射转移、重复状态、最远点步数，以及退化类别，方便将简单样本与困难样本分开分析。

生成全量矩阵（输出目录必须不存在）：

```powershell
python -m lazor.scaling generate --all-sizes --output-dir outputs/scaling_full
```

默认不加 `--all-sizes` 时生成 4,8,12,…,44 共 11 个代表尺寸，仍包含全部 49 个 A/C 交叉组合；也可以指定 `--sizes 4,12,44 --inventory 4,7,10`。随机种子默认为 20261110，可用 `--seed` 指定。相同参数生成完全相同的棋盘、证据和清单。

先运行短时冒烟测试：

```powershell
python -m lazor.scaling run outputs/scaling_full/manifest.json --output outputs/scaling_smoke.json --methods smt search --timeout 1 --profile smoke
```

`smoke` 选择最小、中间、最大尺寸，以及最小/最大同量 A/C 的有效组合，默认全量清单中为 15 个用例。它是回归检查，不能替代全交叉性能测试。可加 `--sizes 4,44 --limit 3` 进一步缩小一次执行的范围。

全量计时与中断续跑：

```powershell
python -m lazor.scaling run outputs/scaling_full/manifest.json --output outputs/scaling_all.json --methods smt search --timeout 5 --repeats 3 --profile full
# 中断后使用相同参数并加 --resume
python -m lazor.scaling run outputs/scaling_full/manifest.json --output outputs/scaling_all.json --methods smt search --timeout 5 --repeats 3 --profile full --resume
```

全量运行可能很长。例如 5,997×2×3=35,982 次实验，单次 5 秒上限时，仅求解预算就约 50 小时，另加启动和校验开销。先分尺寸/类别分析；不要把未运行的用例计入通过数。

每次试验串行启动新的 Python 进程，重复试验交替方法顺序，完成一项就原子保存报告。`elapsed_seconds` 包含输入读取、求解和独立重放，排除进程启动；`process_wall_seconds` 另外记录含启动的墙钟时间。SMT 另报建模、求解、验证时间和模型规模；搜索法另报候选数、节点数和剪枝统计。子进程超过求解预算加 2 秒时会被终止，并明确记录 `hard_timeout`，不会把超时解释为无解。

报告保存代码/输入 SHA-256、Python/平台版本、参数和全部原始结果。`--resume` 要求这些条件完全一致；代码或输入变化必须另建报告，避免把不同版本混在一起。汇总给出每种状态的数量、正确结论数、错误结论数、已正确完成用例的中位耗时、包含正常返回超时的全部计时中位数，以及含强制终止和启动时间的全部墙钟中位数；中位数必须结合超时数阅读。超时不代表算法错误，也不证明无解。清单的最远点步数是光路点图的最短步数摘要，不是方向状态图的搜索深度。

本矩阵是可复现的合成测试，不代表官方题库分布，不承诺 44×44 组合搜索都能在固定时间内求出。正确性还要结合小棋盘穷举对照、环路、相邻分光块、反射后方实体阻挡等单元测试。
