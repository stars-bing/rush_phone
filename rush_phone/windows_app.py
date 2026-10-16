"""Windows 桌面入口。Mac 继续使用 python -m rush_phone。"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from queue import Empty, Queue

from .windows_runtime import (
    LauncherController, LauncherLock, initialize_data, load_preset, user_data_dir,
)


class LauncherWindow:
    def __init__(self, root, data_dir: Path, events: Queue, controller: LauncherController):
        import tkinter as tk
        from tkinter import ttk
        from tkinter.scrolledtext import ScrolledText

        self.root, self.data_dir, self.events, self.controller = root, data_dir, events, controller
        self.closing = False
        root.title("RushPhone · iPhone 购买助手")
        root.geometry("820x610")
        root.minsize(690, 520)
        root.protocol("WM_DELETE_WINDOW", self._close)
        frame = ttk.Frame(root, padding=18)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="iPhone 购买助手", font=("Microsoft YaHei UI", 18, "bold")).pack(anchor="w")
        ttk.Label(frame, text="先登录并保留 Chrome 窗口，再选择一种机型开始等待开售。", wraplength=740).pack(anchor="w", pady=(8, 12))
        self.specs = tk.StringVar()
        ttk.Label(frame, textvariable=self.specs, wraplength=740).pack(anchor="w", pady=(0, 12))
        actions = ttk.Frame(frame)
        actions.pack(fill="x")
        self.buttons = {}
        for column, (action, label) in enumerate((("login", "登录"), ("duo", "购买 Duo"), ("pro-max", "购买 Pro Max"))):
            actions.columnconfigure(column, weight=1)
            button = ttk.Button(actions, text=label, command=lambda key=action: self._start(key))
            button.grid(row=0, column=column, sticky="ew", padx=(0, 8) if column < 2 else 0)
            self.buttons[action] = button
        self.allow_submit = tk.BooleanVar(value=False)
        self.submit_checkbox = ttk.Checkbutton(frame, variable=self.allow_submit,
            text="允许提交订单（勾选后会真实下单；付款由你在 Chrome 中手动完成）")
        self.submit_checkbox.pack(anchor="w", pady=(14, 4))
        ttk.Label(frame, text="未勾选时也会持续监控，但会停在最终提交前。登录和验证仍需手动完成。",
                  wraplength=740).pack(anchor="w")
        controls = ttk.Frame(frame)
        controls.pack(fill="x", pady=12)
        self.stop_button = ttk.Button(controls, text="停止", command=self._stop, state="disabled")
        self.stop_button.pack(side="left")
        ttk.Button(controls, text="打开配置和日志目录", command=self._open_folder).pack(side="left", padx=8)
        self.status = tk.StringVar(value="就绪：请先点击登录，并在专用 Chrome 中确认登录成功。")
        ttk.Label(frame, textvariable=self.status, wraplength=740).pack(anchor="w", pady=(0, 8))
        self.log = ScrolledText(frame, height=14, wrap="word", state="disabled", font=("Microsoft YaHei UI", 10))
        self.log.pack(fill="both", expand=True)
        self._refresh_specs()
        self.root.after(100, self._poll)

    def _refresh_specs(self):
        lines = []
        for key, label in (("duo", "Duo"), ("pro-max", "Pro Max")):
            config, _ = load_preset(self.data_dir, key)
            lines.append(f"{label}：{config.variables.get('model', '')} / {config.variables.get('capacity', '')} / {config.variables.get('color', '')}")
        self.specs.set("\n".join(lines))

    def _start(self, action):
        from tkinter import messagebox

        try:
            self._refresh_specs()
            self.controller.start(action, submit_order=self.allow_submit.get())
        except Exception as exc:
            messagebox.showerror("无法启动", str(exc), parent=self.root)
            return
        self.status.set("正在打开专用 Chrome，请稍候……" if action == "login" else "正在连接已登录 Chrome，准备监控……")
        self._buttons()

    def _stop(self):
        self.controller.stop()
        self.status.set("正在停止，等待当前网页操作结束；Chrome 保持打开，请核对页面和订单。")
        self.stop_button.configure(state="disabled")

    def _open_folder(self):
        from tkinter import messagebox

        try:
            os.startfile(str(self.data_dir))
        except OSError:
            messagebox.showerror("无法打开目录", f"请手动打开：{self.data_dir}", parent=self.root)

    def _append(self, message):
        self.log.configure(state="normal")
        self.log.insert("end", message + "\n")
        if int(self.log.index("end-1c").split(".")[0]) > 1500:
            self.log.delete("1.0", "300.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _buttons(self):
        busy = self.controller.busy
        for action, button in self.buttons.items():
            disabled = busy or self.closing or (self.controller.review_required and action != "login")
            button.configure(state="disabled" if disabled else "normal")
        self.submit_checkbox.configure(state="disabled" if busy or self.closing else "normal")
        self.stop_button.configure(state="normal" if busy and not self.controller.stop_event.is_set() else "disabled")

    def _poll(self):
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "finished":
                    self.status.set(payload.message)
                    if payload.state == "unknown":
                        self._append("请先在原 Chrome 核对订单；核对后再重新启动本程序，避免重复下单。")
                else:
                    self._append(payload)
                    if not self.controller.stop_event.is_set():
                        if "购买按钮尚不可操作" in payload or "持续检查购买按钮" in payload:
                            self.status.set("等待开售：持续检查购买按钮，定时刷新相同规格。")
                        elif "需要人工登录或验证" in payload or "正在核对商店登录" in payload:
                            self.status.set("等待人工登录或验证，请在原 Chrome 窗口完成。")
                        elif "继续结账" in payload:
                            self.status.set("正在结账，请留意原 Chrome 中的配送、地址和付款方式。")
        except Empty:
            pass
        if self.closing and not self.controller.busy:
            self.root.destroy()
            return
        self._buttons()
        self.root.after(100, self._poll)

    def _close(self):
        if self.controller.busy:
            self.closing = True
            self._stop()
            self._buttons()
        else:
            self.root.destroy()


def self_test(report: Path) -> int:
    """仅供构建验证：临时配置、离线浏览器、Tk 窗口，不访问 Apple。"""
    checks = []
    try:
        import tkinter as tk
        from playwright.sync_api import sync_playwright

        with tempfile.TemporaryDirectory(prefix="rush-phone-smoke-") as directory:
            data_dir = Path(directory) / "中文配置 with spaces"
            initialize_data(data_dir)
            lock = LauncherLock(data_dir)
            lock.acquire()
            try:
                controller = LauncherController(data_dir, Queue())
                try:
                    root = tk.Tk()
                    root.withdraw()
                    try:
                        window = LauncherWindow(root, data_dir, controller.events, controller)
                        root.update_idletasks()
                        if window.allow_submit.get():
                            raise AssertionError("启动时不应默认允许最终提交")
                        checks.append("Tk 窗口、默认配置、中文及空格路径、单实例锁、文件权限")
                    finally:
                        root.destroy()
                finally:
                    controller.close()
            finally:
                lock.close()
            with sync_playwright() as playwright:
                # 使用临时测试资料和本地空白页；与用户的 Chrome 登录资料无关。
                profile = data_dir / "offline-browser"
                owner = playwright.chromium.launch_persistent_context(
                    str(profile), channel="chrome", headless=True, args=["--remote-debugging-port=0"])
                try:
                    owner.set_offline(True)
                    owner.pages[0].set_content('<button onclick="this.textContent=\'完成\'">离线测试</button>')
                    port = (profile / "DevToolsActivePort").read_text().splitlines()[0]
                    browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}", no_defaults=True)
                    page = browser.contexts[0].pages[0]
                    page.get_by_role("button", name="离线测试", exact=True).click()
                    if page.get_by_role("button", name="完成", exact=True).count() != 1:
                        raise AssertionError("本地 CDP 操作未完成")
                    checks.append("Playwright 驱动、系统 Chrome、同一标签页 CDP 操作（离线）")
                finally:
                    owner.close()
        result = {"ok": True, "checks": checks}
    except Exception as exc:
        result = {"ok": False, "checks": checks, "error": type(exc).__name__}
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if result["ok"] else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Windows 一键启动器")
    parser.add_argument("--self-test", type=Path, metavar="REPORT", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if sys.platform != "win32":
        if sys.stderr is not None:
            print("此入口仅用于 Windows；Mac 请继续使用 python -m rush_phone。", file=sys.stderr)
        return 2
    if args.self_test:
        return self_test(args.self_test.resolve())
    import tkinter as tk
    from tkinter import messagebox

    root = tk.Tk()
    root.withdraw()
    lock = None
    controller = None
    try:
        data_dir = user_data_dir()
        initialize_data(data_dir)
        lock = LauncherLock(data_dir)
        lock.acquire()
        controller = LauncherController(data_dir, Queue())
        LauncherWindow(root, data_dir, controller.events, controller)
        root.deiconify()
        root.mainloop()
        return 0
    except Exception as exc:
        messagebox.showerror("启动器未能启动", str(exc), parent=root)
        return 1
    finally:
        if controller is not None:
            controller.stop()
            if controller.thread is not None:
                controller.thread.join()
            controller.close()
        if lock is not None:
            lock.close()
        try:
            root.destroy()
        except tk.TclError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
