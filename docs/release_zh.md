# v1.0.0 发布包与运行说明

最终优化的是精确搜索 `search`。命令行必须显式指定 `--algorithm search`；省略该参数时，`--solve` 仍使用 SMT，桌面编辑器也仍使用 SMT。算法默认值没有改变。

## 选择下载内容

从本仓库的 Release 附件中选择：

| 附件 | 用途 |
| --- | --- |
| [lazor-project-v1.0.0.zip](https://github.com/yangcong0168-dot/lazor-project/releases/download/v1.0.0/lazor-project-v1.0.0.zip) | 全套运行包，解压后在包含 `README.md`、`lazor/` 和 `boards/` 的项目目录中运行 |
| [lazor-search-tables.zip](https://github.com/yangcong0168-dot/lazor-project/releases/download/v1.0.0/lazor-search-tables.zip) | 可选预计算数据库，解压到项目根目录，得到 `data/search_tables.sqlite3` |
| [lazor-validation.zip](https://github.com/yangcong0168-dot/lazor-project/releases/download/v1.0.0/lazor-validation.zip) | 随版本附带的验收记录，用于查看测试证据，不是运行求解器的前置条件 |

[版本页面](https://github.com/yangcong0168-dot/lazor-project/releases/tag/v1.0.0) · [SHA256 校验文件](https://github.com/yangcong0168-dot/lazor-project/releases/download/v1.0.0/SHA256SUMS.txt)

如果只下载或克隆源码，也可以直接运行。源码目录不包含开发机历史 `tmp/`、`outputs/`；历史文档引用这些目录，不表示它们随源码一起提供。需要查看随版本发布的验收资料时，另取 `lazor-validation.zip`。

## 启动最终优化搜索

使用 Python 3.10 或更新版本，所有命令均在项目根目录执行：

```powershell
python --version
python -m lazor boards/custom_test.bff --solve --algorithm search --timeout 10
```

`search`、模拟和 SVG 输出只使用 Python 标准库，不需要安装 Z3。导出结果示例：

```powershell
python -m lazor boards/custom_test.bff --solve --algorithm search --timeout 10 --output outputs/result.txt --svg outputs/result.svg --save-board outputs/result.bff
```

输出文件会自动创建所需目录。若同名文件已经存在，应换一个输出路径；确认需要替换时再加 `--force`。

## 数据库是可选加速项

解压 `lazor-search-tables.zip` 后，项目结构应包含：

```text
项目根目录/
├── README.md
├── lazor/
├── boards/
└── data/
    └── search_tables.sqlite3
```

无需每次启动重新建库，也不会启动时把整个数据库载入内存。程序按棋盘开局选择缓存方式：小盘跳过数据库初始化，适合位集的棋盘按尺寸编译并缓存掩码，其他所需静态表按需读取。

数据库缺失、版本不兼容或记录损坏时，程序会回退到正常计算。可以显式关闭数据库，或指定其他路径：

```powershell
python -m lazor boards/custom_test.bff --solve --algorithm search --no-table-db
python -m lazor boards/custom_test.bff --solve --algorithm search --table-db data/search_tables.sqlite3
```

Python 调用和基准进程也可以使用 `LAZOR_TABLE_DB` 环境变量指定数据库路径，或设为 `off`。数据库构建、格式和读取成本见[数据库说明](table_database_zh.md)。

## 使用 SMT 或桌面编辑器

需要 SMT 时，先为当前 Python 环境安装依赖：

```powershell
python -m pip install -r requirements.txt
python -m lazor boards/mad_1.bff --solve --algorithm smt --timeout 110
```

桌面编辑器通过 `python -m lazor.editor` 或 `Launch Editor.cmd` 启动，并需要 Python 的 Tkinter/Tcl/Tk 支持。操作说明见[编辑器指南](editor_zh.md)。

## 验收结果与本地复现

最终版本的完整 5,997 例矩阵和冻结后生成的 164 例新种子测试均为零错误、零超时。完整矩阵总算法耗时从 57.8085 秒降至 36.2246 秒；新种子测试从 2.0098 秒降至 1.3599 秒。计时条件、逐组结果、退化案例及验证范围见[最终优化报告](adaptive_search_zh.md)。这些是合成测试集上的同机测量，不代表任意题目的耗时保证。

仅持有源码时，可以生成新的测试矩阵并测试当前版本：

```powershell
python -m lazor.scaling generate --all-sizes --output-dir outputs/scaling_full
python -m lazor.full_benchmark --manifest outputs/scaling_full/manifest.json --root . --output outputs/my_full_run.jsonl --method search --timeout 1
```

历史报告中的旧版对比命令依赖当时冻结的源码和输入，不能直接假定源码下载中存在这些文件。阅读旧文档时按优化阶段区分基线；当前版本应以最终优化报告为准。
