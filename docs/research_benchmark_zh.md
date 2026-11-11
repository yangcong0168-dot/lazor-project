# 第二轮优化的对照实验

本轮对照使用冻结的上轮代码和当前候选代码，对同一份不可变测试面板逐题运行。每次试验使用新 Python 进程，串行执行，并在棋盘、重复次数之间交替版本顺序，减少导入缓存、热身顺序和并发争用影响。代码、输入、运行参数与 Python/平台信息都写入报告，代码或输入变化后不得向旧报告续写。

实验面板共 131 个合成用例：

- **参考集 82 个**：4×4—44×44 每个尺寸 2 个；轮转覆盖所有 49 个 A/C 独立数量组合。
- **留出集 49 个**：换用新的固定随机种子，每个 A/C 组合 1 个，并覆盖全部 41 个尺寸。生成后检查其输入字节与原 5,997 个用例都不相同。
- 三个类别近似均衡：开放有解、障碍/多光源有解、简单奇偶性无解控制。报告分集合、类别、预期结论分别汇总。

留出集与参考集共享生成机制，因此它只检查新布局的泛化，不代表真实题库或全新问题分布。一旦用留出集结果选择参数或算法，它就成为验证集；下一次最终评估需要另换种子。实验设计遵循独立评估实例和一致截止时间的原则，参见 [Eggensperger 等，JAIR 2019](https://arxiv.org/abs/1705.06058)。

生成面板（目标目录必须不存在）：

```powershell
python -m lazor.research_benchmark prepare outputs/scaling_full/manifest.json --output-dir outputs/research_optimization/panel
```

在候选实现冻结后对照搜索算法：

```powershell
python -m lazor.research_benchmark run outputs/research_optimization/panel/manifest.json --baseline-root tmp/research_baseline --output outputs/research_optimization/search_comparison.json --methods search --timeout 0.3
```

这会运行 131×2=262 个进程。`--methods smt search` 同时比较两个算法；`--split reference` 只运行参考集，适合调参；`--split holdout` 用于冻结后评估。`--repeats 3` 增加重复，`--limit 10` 可做接口检查。断点续跑需保持所有参数不变并加 `--resume`。大规模正式对照前应先运行全部正确性测试，确认机器没有其他高负载任务。

报告采用 PAR-2：在给定求解预算内得到正确结论的试验记实际算法时间，其余试验记为预算的两倍。公式为 `mean(t_i if timely_correct else 2*cutoff)`；超时、未知、强制终止都保留在分母中。[SAT Competition 2024 官方规则](https://github.com/satcompetition/2024/blob/main/README.md)使用这一指标惩罚未完成实例。错误答案和进程错误还会使对应版本的 `valid` 为假，不能仅凭惩罚后的分数声称该版本可用。

速度比只在**同一棋盘、同一次重复、双方都按时给出正确结论**的配对上计算；`baseline/current>1` 表示当前实现更快。报告同时保存配对数量、比值中位数和几何平均数。某版本超时而另一个完成时，不伪造“实际加速倍数”；该差别通过完成数和 PAR-2 表达。简单无解控制单独报告，不能让其快速返回掩盖有解难题的超时。

`elapsed_seconds` 是算法本身的预算范围时间，包含算法内部验证、建模/预处理、求解和验证；`worker_elapsed_seconds` 另外包含导入和读取。两版本返回的布局都由同一个当前模拟器再次校验，这段时间记为 `external_verification_seconds`。`process_wall_seconds` 记录子进程启动至退出的实际耗时。子进程超过求解预算加 2 秒的外部保护线时会被终止，记录 `hard_timeout`，不会被误报为无解。

这个面板强调尺寸与库存覆盖，不能替代 5,997 个组合的全量测试。奇偶性无解只属于快速拒绝控制；本轮没有据此证明困难无解题的性能。毫秒级结果容易受系统调度影响，应结合重复试验、工作量计数、正确结论数和超时数判断改动收益。
