"""Independent Tk desktop editor for designing and running BFF test cases."""

import argparse
import math
import multiprocessing
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .editor_model import EditorDocument, save_document
from .editor_runner import EditorRunner
from .output import write_bff, write_report, write_svg

ROOT = Path(__file__).resolve().parents[1]
INK = "#15243b"
MUTED = "#60738d"
BACKGROUND = "#edf2f8"
BLUE = "#2563eb"
TOOLS = (
    ("o", "空格 · 可放方块"), ("x", "禁放区域 x"),
    ("A", "A  反射方块"), ("B", "B  遮挡方块"),
    ("C", "C  折射方块"), ("L", "激光起点"),
    ("P", "目标点"), ("erase", "橡皮擦"),
)
DIRECTIONS = {"右下 ↘": (1, 1), "右上 ↗": (1, -1),
              "左下 ↙": (-1, 1), "左上 ↖": (-1, -1)}


class EditorApp:
    """Keep draft editing separate from background runs and solved overlays."""

    def __init__(self, root: tk.Tk, path: Path | None = None):
        self.root = root
        self.document = EditorDocument.new()
        self.filepath = None
        self.saved_text = self.document.to_bff()
        self.history = []
        self.redo_history = []
        self.result = self.search = None
        self.runner = EditorRunner()
        self.busy = False
        self.updating = False
        self.inventory_error = False
        self.controls = []
        self.geometry = (40.0, 40.0, 80.0)
        self.tool = tk.StringVar(value="A")
        self.direction = tk.StringVar(value="右下 ↘")
        self.width_var = tk.StringVar(value="4")
        self.height_var = tk.StringVar(value="4")
        self.count_vars = {kind: tk.StringVar(value="0") for kind in "ABC"}
        self.timeout_var = tk.StringVar(value="15")
        self.status_var = tk.StringVar(value="准备就绪：选择工具，点击棋盘。")
        self.summary_var = tk.StringVar(value="尚未运行\n先设置激光和目标。")
        self.hover_var = tk.StringVar(value="激光和目标自动吸附到格子边中点")
        self._build_ui()
        for variable in self.count_vars.values():
            variable.trace_add("write", self._inventory_changed)
        self.root.bind("<Control-s>", lambda event: self.save())
        self.root.bind("<Control-o>", lambda event: self.open_file())
        self.root.bind("<Control-z>", lambda event: self.undo())
        self.root.bind("<Control-y>", lambda event: self.redo())
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        if path is not None:
            self._load(path)
        else:
            self.refresh()

    def _button(self, parent, text, command, **kwargs):
        widget = ttk.Button(parent, text=text, command=command, **kwargs)
        self.controls.append(widget)
        return widget

    def _build_ui(self):
        self.root.title("Lazor Lab — 关卡编辑器")
        self.root.geometry("1200x820")
        self.root.minsize(1080, 740)
        self.root.configure(bg=BACKGROUND)
        self.root.option_add("*Font", ("Microsoft YaHei UI", 10))
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure(
            "TButton", padding=(10, 7), font=("Microsoft YaHei UI", 10)
        )
        style.configure("Accent.TButton", background=BLUE, foreground="white")
        style.map("Accent.TButton", background=[("active", "#1d4ed8")])
        style.configure(
            "Tool.TRadiobutton", padding=(9, 9), background="white"
        )
        style.map("Tool.TRadiobutton", background=[("selected", "#dbeafe")])
        self.root.rowconfigure(1, weight=1)
        self.root.columnconfigure(0, weight=1)

        header = tk.Frame(self.root, bg=INK, padx=24, pady=14)
        header.grid(row=0, column=0, sticky="ew")
        tk.Label(header, text="LAZOR LAB", bg=INK, fg="white",
                 font=("Segoe UI", 21, "bold")).pack(side="left")
        tk.Label(header, text="  测试关卡编辑器", bg=INK, fg="#b9c9e2",
                 font=("Microsoft YaHei UI", 11)).pack(side="left")
        self._button(header, "另存为…", lambda: self.save(True)).pack(
            side="right", padx=4)
        self._button(header, "保存 .bff", self.save).pack(side="right", padx=4)
        self._button(header, "打开…", self.open_file).pack(side="right", padx=4)

        body = tk.Frame(self.root, bg=BACKGROUND, padx=16, pady=16)
        body.grid(row=1, column=0, sticky="nsew")
        body.rowconfigure(0, weight=1)
        body.columnconfigure(1, weight=1)
        left = tk.Frame(body, bg="white", padx=14, pady=14, width=204)
        left.grid(row=0, column=0, sticky="ns", padx=(0, 12))
        tk.Label(left, text="01  绘制棋盘", bg="white", fg=INK,
                 font=("Microsoft YaHei UI", 12, "bold")).pack(anchor="w")
        for value, label in TOOLS:
            button = ttk.Radiobutton(
                left, text=label, variable=self.tool, value=value,
                style="Tool.TRadiobutton", command=self._tool_changed,
            )
            button.pack(fill="x", pady=2)
            self.controls.append(button)
        tk.Label(left, text="激光方向", bg="white", fg=MUTED).pack(
            anchor="w", pady=(12, 5))
        direction = ttk.Combobox(
            left, textvariable=self.direction, values=list(DIRECTIONS),
            state="readonly", width=17,
        )
        direction.pack(fill="x")
        self.direction_box = direction
        self.controls.append(direction)
        tk.Label(left, text="重复点击目标可取消\n右键：擦除点或方块",
                 bg="white", fg=MUTED, justify="left").pack(
                     anchor="w", pady=(12, 8))
        history = tk.Frame(left, bg="white")
        history.pack(fill="x")
        self._button(history, "撤销", self.undo).pack(side="left", expand=True)
        self._button(history, "重做", self.redo).pack(side="right", expand=True)
        self._button(left, "载入 Mad 1 示例", self.load_example).pack(
            fill="x", pady=(16, 0))

        center = tk.Frame(body, bg="white", padx=8, pady=8)
        center.grid(row=0, column=1, sticky="nsew")
        center.rowconfigure(1, weight=1)
        center.columnconfigure(0, weight=1)
        self.board_label = tk.Label(center, bg="white", fg=INK, anchor="w",
                                    font=("Microsoft YaHei UI", 11, "bold"))
        self.board_label.grid(row=0, column=0, sticky="ew", padx=12, pady=6)
        self.canvas = tk.Canvas(center, bg="white", highlightthickness=0)
        self.canvas.grid(row=1, column=0, sticky="nsew")
        self.canvas.bind("<Configure>", lambda event: self.draw())
        self.canvas.bind("<Button-1>", self.click_canvas)
        self.canvas.bind("<Button-3>", self.erase_canvas)
        self.canvas.bind("<Motion>", self.hover)
        self.canvas.bind("<Leave>", lambda event: self.canvas.delete("hover"))
        tk.Label(center, textvariable=self.hover_var, bg="white", fg=MUTED,
                 wraplength=520).grid(row=2, column=0, sticky="ew", pady=8)

        right = tk.Frame(body, bg="white", padx=14, pady=14, width=288)
        right.grid(row=0, column=2, sticky="ns", padx=(12, 0))
        tk.Label(right, text="02  测试设置", bg="white", fg=INK,
                 font=("Microsoft YaHei UI", 12, "bold")).pack(anchor="w")
        size = tk.Frame(right, bg="white")
        size.pack(fill="x", pady=(12, 5))
        for label, variable in (("列", self.width_var), ("行", self.height_var)):
            tk.Label(size, text=label, bg="white").pack(side="left", padx=3)
            spin = ttk.Spinbox(
                size, from_=1, to=16, width=4, textvariable=variable
            )
            spin.pack(side="left")
            self.controls.append(spin)
        self._button(size, "新建", self.new_board).pack(side="right")
        tk.Label(right, text="待放库存（不是棋盘上的固定块）", bg="white",
                 fg=MUTED, wraplength=260, justify="left").pack(
                     anchor="w", pady=(10, 5))
        stock = tk.Frame(right, bg="white")
        stock.pack(fill="x")
        for kind, variable in self.count_vars.items():
            tk.Label(stock, text=kind, bg="white", fg=INK).pack(
                side="left", padx=(4, 3))
            spin = ttk.Spinbox(stock, from_=0, to=256, width=4,
                               textvariable=variable)
            spin.pack(side="left", padx=(0, 5))
            self.controls.append(spin)
        tk.Label(right, text="画出的 A / B / C 都会保存为固定块。", bg="white",
                 fg=MUTED, wraplength=260, justify="left").pack(
                     anchor="w", pady=(8, 10))
        self._button(right, "模拟当前光路", lambda: self.run("simulate"),
                     style="Accent.TButton").pack(fill="x", pady=3)
        run_row = tk.Frame(right, bg="white")
        run_row.pack(fill="x", pady=3)
        self._button(run_row, "约束规划求解", lambda: self.run("solve")).pack(
            side="left", fill="x", expand=True)
        timeout = ttk.Entry(run_row, textvariable=self.timeout_var, width=4)
        timeout.pack(side="left", padx=(5, 2))
        self.controls.append(timeout)
        tk.Label(run_row, text="秒", bg="white").pack(side="left")
        self.cancel_button = ttk.Button(
            right, text="取消运行", command=self.cancel, state="disabled"
        )
        self.cancel_button.pack(fill="x", pady=3)
        self._button(right, "导出结果图 / 报告…", self.export_result).pack(
            fill="x", pady=3)
        tk.Label(right, textvariable=self.summary_var, bg="#edf4ff", fg=INK,
                 justify="left", anchor="w", padx=10, pady=10,
                 wraplength=248).pack(fill="x", pady=(12, 10))
        tk.Label(right, text="03  .bff 文件预览", bg="white", fg=INK,
                 font=("Microsoft YaHei UI", 11, "bold")).pack(anchor="w")
        self.preview = tk.Text(right, width=30, height=10, wrap="none",
                               font=("Consolas", 9), bg="#f5f7fb", fg=INK,
                               relief="flat", padx=8, pady=8)
        self.preview.pack(fill="both", expand=True, pady=(8, 0))
        tk.Label(
            self.root, textvariable=self.status_var, bg=INK, fg="white",
            anchor="w", padx=20, pady=9,
        ).grid(row=2, column=0, sticky="ew")

    @property
    def dirty(self):
        return (
            self.inventory_error or self.document.to_bff() != self.saved_text
        )

    def _checkpoint(self):
        self.history.append(self.document.clone())
        self.history = self.history[-100:]
        self.redo_history.clear()

    def _clear_result(self):
        self.result = self.search = None
        self.summary_var.set("棋盘已编辑\n重新运行以查看结果。")

    def refresh(self):
        self.updating = True
        for kind, variable in self.count_vars.items():
            variable.set(str(self.document.block_counts[kind]))
        self.width_var.set(str(self.document.width))
        self.height_var.set(str(self.document.height))
        self.inventory_error = False
        self.updating = False
        name = self.filepath.name if self.filepath else "未命名关卡"
        self.root.title(
            f"Lazor Lab — 关卡编辑器 — {name}{' *' if self.dirty else ''}"
        )
        self.board_label.config(text=(
            f"{name}   ·   {self.document.width} × {self.document.height}"
            f"   ·   {len(self.document.lasers)} 激光 / "
            f"{len(self.document.targets)} 目标"
        ))
        self.preview.config(state="normal")
        self.preview.delete("1.0", "end")
        self.preview.insert("1.0", self.document.to_bff())
        self.preview.config(state="disabled")
        self.draw()

    def _inventory_changed(self, *args):
        if self.updating or self.busy:
            return
        try:
            counts = {
                kind: int(var.get()) for kind, var in self.count_vars.items()
            }
            if any(value < 0 for value in counts.values()):
                raise ValueError
        except ValueError:
            self.inventory_error = True
            self.status_var.set("库存请输入 0 或正整数；修正后才能保存或运行。")
            return
        self.inventory_error = False
        if counts != self.document.block_counts:
            self._checkpoint()
            self.document.block_counts = counts
            self._clear_result()
            self.refresh()

    def _validated_text(self):
        if self.inventory_error:
            raise ValueError("库存请输入 0 或正整数。")
        return self.document.to_bff(validate=True)

    def _tool_changed(self):
        self.status_var.set("选择好工具后点击棋盘；激光方向可在左侧切换。")
        self.canvas.delete("hover")

    def _point_at(self, px, py):
        left, top, cell = self.geometry
        x, y = (px - left) * 2 / cell, (py - top) * 2 / cell
        width, height = 2 * self.document.width, 2 * self.document.height
        if not (0 <= x <= width and 0 <= y <= height):
            return None
        candidates = [
            (a, b) for a in range(
                max(0, math.floor(x)), min(width, math.ceil(x)) + 1
            )
            for b in range(
                max(0, math.floor(y)), min(height, math.ceil(y)) + 1
            )
            if (a + b) % 2 == 1
        ]
        if not candidates:
            candidates = [
                (a, b) for a in range(width + 1) for b in range(height + 1)
                if (a + b) % 2 == 1
            ]
        return min(
            candidates,
            key=lambda point: (point[0] - x)**2 + (point[1] - y)**2,
        )

    def _cell_at(self, px, py):
        left, top, cell = self.geometry
        column, row = (
            math.floor((px - left) / cell), math.floor((py - top) / cell)
        )
        if (
            0 <= column < self.document.width
            and 0 <= row < self.document.height
        ):
            return column, row
        return None

    def _pixel(self, point):
        left, top, cell = self.geometry
        return left + point[0] * cell / 2, top + point[1] * cell / 2

    def hover(self, event):
        self.canvas.delete("hover")
        if self.busy:
            return
        point = self._point_at(event.x, event.y)
        cell = self._cell_at(event.x, event.y)
        self.hover_var.set(f"格子 (列, 行)：{cell}    半格坐标 (x, y)：{point}")
        if point and self.tool.get() in ("L", "P", "erase"):
            x, y = self._pixel(point)
            self.canvas.create_oval(x-8, y-8, x+8, y+8, outline=BLUE,
                                    width=2, tags="hover")

    def click_canvas(self, event):
        if self.busy or self.inventory_error:
            return
        tool = self.tool.get()
        if tool == "erase":
            return self.erase_canvas(event)
        point = self._point_at(event.x, event.y)
        cell = self._cell_at(event.x, event.y)
        if (tool in ("L", "P") and point is None) or (
            tool in "oxABC" and cell is None
        ):
            return
        self._checkpoint()
        if tool == "L":
            self.document.add_laser(*point, *DIRECTIONS[self.direction.get()])
        elif tool == "P":
            self.document.toggle_target(*point)
        else:
            self.document.set_cell(*cell, tool)
        self._clear_result()
        self.refresh()
        self.status_var.set("已更新草稿。Ctrl+S 保存为 .bff；可以随时模拟光路。")

    def erase_canvas(self, event):
        if self.busy or self.inventory_error:
            return
        cell = self._cell_at(event.x, event.y)
        # Imported files can contain targets at corners/centers, or invalid
        # source positions needing repair. Hit-test existing points directly
        # instead of restricting the eraser to new-source snap positions.
        existing = set(self.document.targets)
        existing.update((ray.x, ray.y) for ray in self.document.lasers)
        point = None
        if existing:
            nearest = min(existing, key=lambda value: math.dist(
                self._pixel(value), (event.x, event.y)
            ))
            distance = math.dist(self._pixel(nearest), (event.x, event.y))
            if distance <= max(10, self.geometry[2] * .3):
                point = nearest
        if point is None and cell is None:
            return
        self._checkpoint()
        if point is not None:
            self.document.remove_point(*point)
        elif cell is not None:
            self.document.set_cell(*cell, "o")
        self._clear_result()
        self.refresh()

    def draw(self):
        canvas = self.canvas
        canvas.delete("all")
        width, height = (
            max(canvas.winfo_width(), 200), max(canvas.winfo_height(), 200)
        )
        columns, rows = self.document.width, self.document.height
        cell = min((width - 82) / columns, (height - 82) / rows, 110)
        left, top = (width - columns * cell) / 2, (height - rows * cell) / 2
        self.geometry = (left, top, cell)
        colors = {"o": "#f8fafc", "x": "#e2e8f0", "A": "#bfdbfe",
                  "B": "#475569", "C": "#ddd6fe"}
        placements = self.result.layout.placements if self.result else {}
        for row, values in enumerate(self.document.grid):
            for column, original in enumerate(values):
                symbol = placements.get((column, row), original)
                x, y = left + column * cell, top + row * cell
                canvas.create_rectangle(
                    x+2, y+2, x+cell-2, y+cell-2, fill=colors[symbol],
                    outline="#94a3b8", width=2 if symbol in "ABC" else 1,
                    dash=(5, 3) if (column, row) in placements else (),
                )
                if symbol != "o":
                    canvas.create_text(
                        x+cell/2, y+cell/2, text=symbol,
                        fill="white" if symbol == "B" else INK,
                        font=("Segoe UI", max(10, int(cell * .23)), "bold"),
                    )
        for column in range(columns):
            canvas.create_text(
                left+(column+.5)*cell, top-22, text=column, fill=MUTED
            )
        for row in range(rows):
            canvas.create_text(
                left-23, top+(row+.5)*cell, text=row, fill=MUTED
            )
        for x in range(2 * columns + 1):
            for y in range(2 * rows + 1):
                if (x + y) % 2:
                    px, py = self._pixel((x, y))
                    canvas.create_oval(px-2, py-2, px+2, py+2,
                                        fill="#c6d2e2", outline="")
        if self.result:
            for start, end in self.result.segments:
                canvas.create_line(*self._pixel(start), *self._pixel(end),
                                   fill="#ef4444", width=2.5)
        for index, point in enumerate(sorted(self.document.targets), 1):
            x, y = self._pixel(point)
            hit = self.result and point in self.result.hit_targets
            color = "#16a34a" if hit else "#ea580c"
            canvas.create_oval(
                x-8, y-8, x+8, y+8, fill="white", outline=color, width=3
            )
            canvas.create_text(
                x+11, y-12, text=f"P{index}", fill=color, anchor="w"
            )
        for index, laser in enumerate(self.document.lasers, 1):
            x, y = self._pixel((laser.x, laser.y))
            length = min(23, cell * .28)
            canvas.create_line(x, y, x+laser.vx*length, y+laser.vy*length,
                               fill=BLUE, width=3, arrow="last")
            canvas.create_oval(x-5, y-5, x+5, y+5, fill=BLUE, outline="white")
            canvas.create_text(
                x+8, y+14, text=f"L{index}", fill=BLUE, anchor="w"
            )

    def undo(self):
        if self.busy or not self.history:
            return
        self.redo_history.append(self.document.clone())
        self.document = self.history.pop()
        self._clear_result()
        self.refresh()

    def redo(self):
        if self.busy or not self.redo_history:
            return
        self.history.append(self.document.clone())
        self.document = self.redo_history.pop()
        self._clear_result()
        self.refresh()

    def _may_replace(self):
        if not self.dirty:
            return True
        choice = messagebox.askyesnocancel(
            "保存修改", "当前关卡有未保存的修改，先保存吗？", parent=self.root)
        return self.save() if choice is True else choice is False

    def new_board(self):
        if self.busy:
            return
        try:
            width, height = (
                int(self.width_var.get()), int(self.height_var.get())
            )
            if not (1 <= width <= 16 and 1 <= height <= 16):
                raise ValueError
        except ValueError:
            messagebox.showerror("尺寸错误", "行列数应为 1 到 16 的整数。", parent=self.root)
            return
        if self._may_replace():
            self.document = EditorDocument.new(width, height)
            self.filepath = None
            self.history.clear()
            self.redo_history.clear()
            self.saved_text = self.document.to_bff()
            self._clear_result()
            self.refresh()

    def _load(self, path):
        try:
            document = EditorDocument.from_text(
                Path(path).read_text(encoding="utf-8-sig")
            )
            if document.width > 16 or document.height > 16:
                raise ValueError("图形编辑器支持最大 16×16；更大的关卡请用命令行。")
        except (OSError, ValueError) as exc:
            messagebox.showerror("打开失败", str(exc), parent=self.root)
            return
        self.document = document
        self.filepath = Path(path)
        self.saved_text = document.to_bff()
        self.history.clear()
        self.redo_history.clear()
        self._clear_result()
        self.refresh()
        self.status_var.set(f"已载入 {self.filepath.name}；编辑不会自动覆盖原文件。")

    def open_file(self):
        if self.busy or not self._may_replace():
            return
        path = filedialog.askopenfilename(
            parent=self.root, title="打开 BFF 关卡", initialdir=ROOT / "boards",
            filetypes=[("Lazor 关卡", "*.bff"), ("所有文件", "*")],
        )
        if path:
            self._load(path)

    def load_example(self):
        if not self.busy and self._may_replace():
            self._load(ROOT / "boards" / "mad_1.bff")

    def save(self, save_as=False):
        if self.busy:
            return False
        try:
            self._validated_text()
            path = self.filepath
            if save_as or path is None:
                selected = filedialog.asksaveasfilename(
                    parent=self.root, title="保存测试关卡为 .bff",
                    initialdir=ROOT / "boards", initialfile="custom_test.bff",
                    defaultextension=".bff", filetypes=[("Lazor 关卡", "*.bff")],
                )
                if not selected:
                    return False
                path = Path(selected)
            save_document(self.document, path)
            self.filepath = path
            self.saved_text = self.document.to_bff()
            self.refresh()
            self.status_var.set(f"已保存：{path}")
            return True
        except (OSError, ValueError) as exc:
            messagebox.showerror("无法保存关卡", str(exc), parent=self.root)
            return False

    def _set_busy(self, busy):
        self.busy = busy
        for control in self.controls:
            control.configure(state="disabled" if busy else "normal")
        if not busy:
            self.direction_box.configure(state="readonly")
        self.cancel_button.configure(state="normal" if busy else "disabled")

    def run(self, mode):
        if self.busy:
            return
        try:
            text = self._validated_text()
            timeout = float(self.timeout_var.get()) if mode == "solve" else 15
            if not math.isfinite(timeout) or timeout <= 0 or timeout > 110:
                raise ValueError("运行预算请输入 0 到 110 之间的秒数（不含 0）。")
            self.runner.start(
                mode, text, timeout=timeout
            )
        except (OSError, ValueError, RuntimeError) as exc:
            messagebox.showerror("无法运行", str(exc), parent=self.root)
            return
        self.result = self.search = None
        self._set_busy(True)
        self.summary_var.set("正在运行…\n编辑已暂停，可点击取消。")
        self.status_var.set(
            "正在后台模拟光路…" if mode == "simulate" else "正在求解状态转移约束…"
        )
        self.draw()
        self.root.after(100, self._poll)

    def _poll(self):
        if not self.busy:
            return
        outcome = self.runner.poll()
        if outcome is None:
            self.root.after(100, self._poll)
            return
        self._set_busy(False)
        kind, payload = outcome
        if kind == "error":
            self.summary_var.set(f"运行失败\n{payload}")
            self.status_var.set("运行失败，请查看上方错误说明。")
            return
        if kind == "solve":
            self.search = payload
            if not payload.solved:
                reasons = {"no_solution": "约束不可满足：当前关卡无解",
                           "timeout": "已到时间预算，尚未确定有无解",
                           "unknown": "求解器暂未确定有无解"}
                self.summary_var.set(
                    f"{reasons[payload.status]}\n"
                    f"{payload.state_count} 个状态 · "
                    f"{payload.constraint_count} 条约束"
                )
                self.status_var.set(reasons[payload.status])
                return
            self.result = payload.solution
        else:
            self.result = payload
        result = self.result
        total = len(result.layout.puzzle.targets)
        title = (
            "有效解" if result.is_solution else
            "目标已命中，库存未放完" if result.all_targets_hit else "仍有目标未命中"
        )
        detail = f"{title}\n命中 {len(result.hit_targets)} / {total} 个目标"
        if self.search:
            detail += (
                f"\n约束规划 {self.search.elapsed_seconds:.3f} 秒\n"
                f"{self.search.state_count} 个状态 · "
                f"{self.search.constraint_count} 条约束"
            )
        else:
            detail += f"\n跳过 {result.repeated_states} 个重复光线状态"
        self.summary_var.set(detail)
        self.status_var.set("运行完成。虚线块是规划得到的摆法；原始关卡未改变。"
                            if self.search else "模拟完成；绿色目标已命中，橙色目标未命中。")
        self.draw()

    def cancel(self):
        if self.busy:
            self.runner.cancel()
            self._set_busy(False)
            self.summary_var.set("已取消运行\n原始关卡保持不变。")
            self.status_var.set("运行已取消，可继续编辑。")

    def export_result(self):
        if self.busy:
            return
        if self.result is None:
            messagebox.showinfo("先运行", "先模拟或求解，才能导出结果图。", parent=self.root)
            return
        directory = filedialog.askdirectory(
            parent=self.root, title="选择结果保存文件夹",
            initialdir=ROOT / "outputs",
        )
        if not directory:
            return
        stem = (
            self.filepath.stem if self.filepath else "custom_test"
        ) + "_result"
        paths = [
            Path(directory) / (stem + suffix)
            for suffix in (".svg", ".txt", ".bff")
        ]
        if self.filepath and any(
            path.resolve() == self.filepath.resolve() for path in paths
        ):
            messagebox.showerror("路径冲突", "结果文件不能覆盖原关卡。", parent=self.root)
            return
        if any(path.exists() for path in paths) and not messagebox.askyesno(
            "覆盖结果", "同名结果文件已存在，是否覆盖？", parent=self.root
        ):
            return
        try:
            for path, writer in zip(
                paths, (write_svg, write_report, write_bff)
            ):
                writer(self.result, path, search=self.search)
        except (OSError, ValueError) as exc:
            messagebox.showerror("导出失败", str(exc), parent=self.root)
            return
        self.status_var.set(f"已导出 SVG 图、文本报告和摆法快照：{directory}")

    def close(self):
        if self.busy:
            if not messagebox.askyesno(
                "运行中", "停止当前运行并关闭编辑器？", parent=self.root
            ):
                return
            self.cancel()
        if self._may_replace():
            self.runner.cancel()
            self.root.destroy()


def main(argv=None):
    """Launch the separate desktop editor, optionally opening a BFF file."""
    parser = argparse.ArgumentParser(description="Lazor graphical BFF editor")
    parser.add_argument("board", nargs="?", type=Path)
    args = parser.parse_args(argv)
    root = tk.Tk()
    EditorApp(root, args.board)
    root.mainloop()


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
