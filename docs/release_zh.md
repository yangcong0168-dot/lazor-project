# v1.0.1 发布包与运行说明

v1.0.0 的运行 ZIP 漏装了 Z3 及其原生库，之前将它称为完整运行包不准确。新电脑解压后，默认 SMT 和编辑器的约束求解因此可能无法运行。v1.0.1 修正这项打包问题：六个平台分别提供带 `.deps` 原生依赖和数据库的运行包，无需再执行 `pip install`。

请从[最新 Release](https://github.com/yangcong0168-dot/lazor-project/releases/latest)选择适合自己的包。本机仍需 Python 3.10+，推荐 Python 3.12；图形编辑器还需要本机 Python 的 Tkinter/Tcl/Tk 支持。

最终优化的是精确搜索 `search`。命令行必须显式指定 `--algorithm search`；省略该参数时，`--solve` 仍使用 SMT，桌面编辑器也仍使用 SMT。算法默认值没有改变。

## 选择下载内容

每包都包含代码、`.deps` 中的 Z3 Python 模块及相应原生库、`data/search_tables.sqlite3`。按**运行程序的 Python 进程架构**选择，不能只看电脑的 CPU：

| 平台运行包 | 所需解释器与系统 |
| --- | --- |
| [windows-x64](https://github.com/yangcong0168-dot/lazor-project/releases/download/v1.0.1/lazor-project-v1.0.1-windows-x64.zip) | Windows，64 位 x86 Python |
| [windows-arm64](https://github.com/yangcong0168-dot/lazor-project/releases/download/v1.0.1/lazor-project-v1.0.1-windows-arm64.zip) | Windows，ARM64 Python |
| [macos-x64](https://github.com/yangcong0168-dot/lazor-project/releases/download/v1.0.1/lazor-project-v1.0.1-macos-x64.zip) | macOS 13+，x86-64 Python |
| [macos-arm64](https://github.com/yangcong0168-dot/lazor-project/releases/download/v1.0.1/lazor-project-v1.0.1-macos-arm64.zip) | macOS 13+，ARM64 Python |
| [linux-x64](https://github.com/yangcong0168-dot/lazor-project/releases/download/v1.0.1/lazor-project-v1.0.1-linux-x64.zip) | Linux，x86-64 Python，glibc 2.27+ |
| [linux-arm64](https://github.com/yangcong0168-dot/lazor-project/releases/download/v1.0.1/lazor-project-v1.0.1-linux-arm64.zip) | Linux，ARM64 Python，glibc 2.38+ |

例如 Apple Silicon 上通过 Rosetta 运行的 x86-64 Python 应使用 `macos-x64`；使用 ARM64 Python 时才选 `macos-arm64`。Windows ARM 电脑上运行 x64 Python 也应选 `windows-x64`。原生 Z3 的架构必须与 Python 进程一致。

兼容名称 [lazor-project-v1.0.1.zip](https://github.com/yangcong0168-dot/lazor-project/releases/download/v1.0.1/lazor-project-v1.0.1.zip) **只对应 Windows x64 包**，不是跨平台通用包。[最新版本页面](https://github.com/yangcong0168-dot/lazor-project/releases/latest)和 [v1.0.1 SHA256 校验文件](https://github.com/yangcong0168-dot/lazor-project/releases/download/v1.0.1/SHA256SUMS.txt)可用于确认下载。

GitHub 自动生成的 **Source code (zip)**、**Source code (tar.gz)** 以及克隆的仓库均为源码，不包含 `.deps` 原生依赖或数据库，不能当作上述运行包。源码运行 SMT/编辑器求解需要按下文安装依赖。

源码用户可以另取未改变的[数据库附件](https://github.com/yangcong0168-dot/lazor-project/releases/download/v1.0.0/lazor-search-tables.zip)；[v1.0.0 验收附件](https://github.com/yangcong0168-dot/lazor-project/releases/download/v1.0.0/lazor-validation.zip)保存的是当时的算法测试记录。源码不包含开发机历史 `tmp/`、`outputs/`，历史文档引用这些目录不表示它们随源码一起提供。

## 启动最终优化搜索

完整解压所选平台运行包，保留 `.deps` 目录。在包含 `README.md`、`lazor/` 和 `boards/` 的项目根目录执行；若本机命令叫 `python3`，请在所有命令中一致替换：

```powershell
python --version
python -m lazor boards/custom_test.bff --solve --algorithm search --timeout 10
```

`search`、模拟和 SVG 输出只使用 Python 标准库；平台运行包还带有 SMT 所需的 Z3。导出结果示例：

```powershell
python -m lazor boards/custom_test.bff --solve --algorithm search --timeout 10 --output outputs/result.txt --svg outputs/result.svg --save-board outputs/result.bff
```

输出文件会自动创建所需目录。若同名文件已经存在，应换一个输出路径；确认需要替换时再加 `--force`。

## 数据库是可选加速项

v1.0.1 平台运行包已经包含数据库，无需另行下载。源码用户也可解压 `lazor-search-tables.zip` 到项目根目录。平台运行包的结构包含：

```text
项目根目录/
├── README.md
├── .deps/
│   └── z3/
│       └── lib/  （与所选平台匹配的原生库）
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

**v1.0.1 平台运行包：无需安装依赖，直接运行。** Python 和 Tk 由本机提供，Z3 从包内 `.deps` 加载：

```powershell
python -m lazor boards/mad_1.bff --solve --algorithm smt --timeout 110
python -m lazor.editor
```

**源码下载或克隆：先给同一个 Python 解释器安装依赖。**

```powershell
python -m pip install -r requirements.txt
python -m lazor boards/mad_1.bff --solve --algorithm smt --timeout 110
```

也可以使用 `python -m pip install --only-binary=:all: --no-deps --target .deps -r requirements.txt` 将依赖放在项目内。安装和启动应使用相同的 Python 与架构。

Windows 可双击 `Launch Editor.cmd`；所有受支持平台均可运行 `python -m lazor.editor`。启动器不会安装 Python、Tk 或依赖。若同时安装了多种架构的 Python，请明确使用与下载包匹配的解释器。操作说明见[编辑器指南](editor_zh.md)。

v1.0.1 的解压包隔离验证、SMT 与编辑器求解子进程验证，应以本版 Release 实际列出的执行记录为准。下节算法成绩属于此前版本，不替代本次打包验收。

Windows x64 本地候选包已在两个全新的 `python -I -S` 进程中通过验收：Z3 原生 SAT/UNSAT、真实 SMT 关卡及独立模拟复核、编辑器后台 spawn 子进程求解均通过，并确认实际加载的 DLL 位于解压包内。最终 261 项本地回归测试全部通过，包含依赖选择与发布门禁的错误拦截测试。

GitHub 发布流程要求六个平台分别对最终 ZIP 完成相同隔离验收；所有平台通过后才允许发布。Release 附件中的 `verification-<platform>.json` 记录各包的 SHA256、实际运行平台和验收结果，`SHA256SUMS.txt` 对应最终上传文件。

## 验收结果与本地复现

**以下是 v1.0.0 优化算法的历史验收数据，没有宣称 v1.0.1 重新运行了完整 5,997 例。** 当时完整矩阵和冻结后生成的 164 例新种子测试均为零错误、零超时；总算法耗时分别从 57.8085 秒降至 36.2246 秒、从 2.0098 秒降至 1.3599 秒。计时条件、逐组结果、退化案例及验证范围见[优化报告](adaptive_search_zh.md)。这些是合成测试集上的同机测量，不代表任意题目的耗时保证。

仅持有源码时，可以生成新的测试矩阵并测试当前版本：

```powershell
python -m lazor.scaling generate --all-sizes --output-dir outputs/scaling_full
python -m lazor.full_benchmark --manifest outputs/scaling_full/manifest.json --root . --output outputs/my_full_run.jsonl --method search --timeout 1
```

历史报告中的旧版对比命令依赖当时冻结的源码和输入，不能直接假定源码下载中存在这些文件。阅读旧文档时按优化阶段区分基线；算法性能以 v1.0.0 优化报告为准，v1.0.1 的依赖打包与可运行性则看本版实际验收记录。
