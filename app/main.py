"""程式入口：啟動本機服務、顯示控制視窗、自動開啟儀表板。

打包成單一 exe 後，評審端只需雙擊即可執行：
* 不需安裝 Python
* 服務僅綁定 127.0.0.1，不對外開放連線
* 僅分析 data/ 資料夾內的真實資料，不內建任何模擬／虛構資料
"""
from __future__ import annotations

import sys
import threading
import time
import traceback
import webbrowser

from . import config
from .engine import ENGINE
from .server import start


def _log(text: str) -> None:
    try:
        config.base_dir().mkdir(parents=True, exist_ok=True)
        with config.LOG_FILE.open("a", encoding="utf-8") as fh:
            fh.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {text}\n")
    except Exception:  # noqa: BLE001
        pass


def run_gui(url: str) -> None:
    import tkinter as tk
    from tkinter import messagebox, ttk

    root = tk.Tk()
    root.title(f"{config.APP_NAME} v{config.APP_VERSION}")
    root.geometry("620x330")
    root.minsize(560, 300)
    try:
        root.configure(bg="#0f172a")
    except tk.TclError:
        pass

    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    # 打包成 exe 後，部分環境的 Tcl/Tk 佈景資源未隨附完整，若以自訂樣式名稱
    # （如 "K.TProgressbar"）建立元件，可能因找不到對應 layout 而丟出
    # TclError（"Layout ... not found"），導致整個 GUI 啟動失敗並降級為
    # 主控台模式（使用者將看不到任何視窗）。改為直接覆寫內建樣式名稱，
    # 一定已有現成 layout 可用，不受佈景資源缺漏影響。
    progressbar_style = "Horizontal.TProgressbar"
    try:
        style.configure(progressbar_style, troughcolor="#1e293b", background="#38bdf8",
                        bordercolor="#1e293b", lightcolor="#38bdf8", darkcolor="#0284c7")
    except tk.TclError:
        pass

    tk.Label(root, text=config.APP_NAME, fg="#e2e8f0", bg="#0f172a",
             font=("Microsoft JhengHei UI", 17, "bold")).pack(pady=(20, 2))
    tk.Label(root, text=config.APP_SUBTITLE, fg="#7dd3fc", bg="#0f172a",
             font=("Microsoft JhengHei UI", 10)).pack()

    status_var = tk.StringVar(value="正在啟動服務…")
    tk.Label(root, textvariable=status_var, fg="#cbd5e1", bg="#0f172a",
             font=("Microsoft JhengHei UI", 10)).pack(pady=(18, 6))

    bar = ttk.Progressbar(root, style=progressbar_style, length=460, maximum=100)
    bar.pack()

    url_var = tk.StringVar(value=url)
    tk.Label(root, textvariable=url_var, fg="#64748b", bg="#0f172a",
             font=("Consolas", 9)).pack(pady=(8, 0))

    btns = tk.Frame(root, bg="#0f172a")
    btns.pack(pady=16)

    def open_dashboard():
        webbrowser.open(url)

    def open_folder():
        import os
        import subprocess
        config.ensure_dirs()
        try:
            os.startfile(str(config.base_dir()))  # noqa: S606
        except Exception:  # noqa: BLE001
            subprocess.Popen(["explorer", str(config.base_dir())])  # noqa: S603,S607

    def quit_app():
        if messagebox.askokcancel("結束", "確定要關閉系統？儀表板頁面將無法繼續使用。"):
            root.destroy()
            sys.exit(0)

    open_btn = tk.Button(btns, text="開啟儀表板", command=open_dashboard,
                         bg="#0284c7", fg="white", relief="flat", padx=18, pady=7,
                         font=("Microsoft JhengHei UI", 10, "bold"), state="disabled")
    open_btn.pack(side="left", padx=6)
    tk.Button(btns, text="開啟資料夾", command=open_folder, bg="#334155", fg="#e2e8f0",
              relief="flat", padx=14, pady=7,
              font=("Microsoft JhengHei UI", 10)).pack(side="left", padx=6)
    tk.Button(btns, text="結束", command=quit_app, bg="#475569", fg="#e2e8f0",
              relief="flat", padx=14, pady=7,
              font=("Microsoft JhengHei UI", 10)).pack(side="left", padx=6)

    hint = tk.Label(root, text="提示：分析完成後會自動開啟瀏覽器；若未開啟請點「開啟儀表板」。",
                    fg="#475569", bg="#0f172a", font=("Microsoft JhengHei UI", 9))
    hint.pack(side="bottom", pady=8)

    state = {"opened": False}

    def tick():
        bar["value"] = ENGINE.progress
        if ENGINE.error:
            status_var.set("分析發生錯誤，詳見 krews.log")
            open_btn.config(state="normal")
        elif ENGINE.ready:
            status_var.set(f"分析完成：共 {ENGINE.summary.get('n_institutions', 0)} 所機構，"
                           f"高風險 {ENGINE.summary.get('n_high_risk', 0)} 所")
            open_btn.config(state="normal")
            if not state["opened"]:
                state["opened"] = True
                root.after(400, open_dashboard)
        else:
            status_var.set(f"{ENGINE.status}…（{ENGINE.progress}%）")
        root.after(300, tick)

    tick()
    root.mainloop()


def run_console(url: str) -> None:
    print(f"\n{config.APP_NAME} v{config.APP_VERSION}")
    print(config.APP_SUBTITLE)
    print(f"儀表板網址：{url}\n")
    last = -1
    while not ENGINE.ready and not ENGINE.error:
        if ENGINE.progress != last:
            last = ENGINE.progress
            print(f"  [{ENGINE.progress:3d}%] {ENGINE.status}")
        time.sleep(0.4)
    if ENGINE.error:
        print("分析失敗：", ENGINE.error)
    else:
        print(f"  [100%] 完成，共 {ENGINE.summary.get('n_institutions', 0)} 所機構")
        webbrowser.open(url)
    print("\n按 Ctrl+C 結束服務。")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass


def main() -> None:
    config.ensure_dirs()
    _log(f"啟動 {config.APP_NAME} v{config.APP_VERSION}（frozen={getattr(sys,'frozen',False)}）")
    port_pref = 0
    try:
        port_pref = int(ENGINE.settings.get("port") or 0)
    except (TypeError, ValueError):
        port_pref = 0
    try:
        httpd, port, _ = start(port_pref)
    except OSError:
        httpd, port, _ = start(0)
    url = f"http://127.0.0.1:{port}/"
    _log(f"服務啟動於 {url}")

    headless = "--no-gui" in sys.argv
    if headless:
        return run_console(url)
    try:
        run_gui(url)
    except Exception:  # noqa: BLE001
        _log("GUI 啟動失敗，改用主控台模式：\n" + traceback.format_exc())
        run_console(url)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:  # noqa: BLE001
        _log("未預期錯誤：\n" + traceback.format_exc())
        try:
            import tkinter.messagebox as mb
            mb.showerror(config.APP_NAME, "啟動失敗，請查看 krews.log")
        except Exception:  # noqa: BLE001
            print(traceback.format_exc())
        sys.exit(1)
