"""打包成可在評審端電腦直接執行的 exe，並驗證產出結果。

用法：
    python scripts/build_exe.py            # 完整打包 + 驗證
    python scripts/build_exe.py --skip-build   # 只驗證現有 dist 產出

流程
----
1. 檢查 data/ 內主程式實際需要的資料表是否齊備（缺哪張表會導致對應
   風險構面在評審端完全沒有分數，必須事先擋下來）
2. 呼叫 PyInstaller 依 spec 打包
3. 啟動產出的 exe（headless），透過本機 API 確認它真的讀到內建資料
   並完成分析——只確認「檔案有生成」是不夠的，評審端要的是「打開有結果」
4. 整理出 release/ 交付資料夾

為什麼要驗證到「分析完成」
------------------------
打包最常見的失敗不是建置失敗，而是建置成功但執行期缺少資源（資料沒被
納入、hiddenimports 漏宣告）。這類問題只有真的把 exe 跑起來、查詢它的
分析結果才會現形，而競賽現場沒有第二次機會。
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA = BASE_DIR / "data"
DIST = BASE_DIR / "dist"
RELEASE = BASE_DIR / "release"
SPEC = BASE_DIR / "教保機構風險預警系統.spec"
EXE_NAME = "教保機構風險預警系統.exe"

# 主程式會載入的資料表 →（是否為必要, 對應風險構面說明）
REQUIRED_TABLES = {
    "institutions.csv": (True, "機構母體，缺少則無法分析"),
    "penalties.csv": (False, "法遵構面與監督式模型標籤"),
    "evaluations.csv": (False, "評鑑構面"),
    "posts.csv": (False, "輿情構面"),
    "financials.csv": (False, "財務構面比率分析"),
    "ledger.csv": (False, "鑑識檢定（末兩位數／班佛定律）"),
}


def count_rows(path: Path) -> int:
    if not path.exists():
        return -1
    with path.open("r", encoding="utf-8-sig") as f:
        return max(0, sum(1 for _ in f) - 1)


def check_data() -> bool:
    print("=" * 66)
    print("1. 檢查資料集")
    print("=" * 66)
    ok = True
    for name, (required, note) in REQUIRED_TABLES.items():
        n = count_rows(DATA / name)
        if n < 0:
            mark = "✗ 缺少" if required else "－ 未提供"
            print(f"  {mark:<8} {name:<20} {note}")
            if required:
                ok = False
        elif n == 0:
            print(f"  ! 空檔    {name:<20} {note}")
            if required:
                ok = False
        else:
            print(f"  ✓ {n:>6} 列 {name:<20} {note}")
    if not ok:
        print("\n必要資料表缺漏，請先執行：")
        print("  python scripts/collect_ece.py")
        print("  python scripts/build_dataset.py")
    return ok


def run_build() -> bool:
    print()
    print("=" * 66)
    print("2. PyInstaller 打包")
    print("=" * 66)
    if not SPEC.exists():
        print(f"  ✗ 找不到 spec：{SPEC}")
        return False
    # 清除舊產出，避免殘留檔案混入交付內容
    for d in (DIST, BASE_DIR / "build"):
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)

    log_path = BASE_DIR / ".tmp" / "build_exe.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    print(f"  執行中…（完整輸出：{log_path}）")
    with log_path.open("w", encoding="utf-8") as log:
        proc = subprocess.run(
            [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
             str(SPEC)],
            cwd=str(BASE_DIR), stdout=log, stderr=subprocess.STDOUT,
            text=True, timeout=1800,
        )
    if proc.returncode != 0:
        print(f"  ✗ 打包失敗（返回碼 {proc.returncode}），請查看 {log_path}")
        return False

    exe = DIST / EXE_NAME
    if not exe.exists():
        print(f"  ✗ 未產出 {EXE_NAME}")
        return False
    print(f"  ✓ 產出 {exe.name}（{exe.stat().st_size / 1024 / 1024:.1f} MB）")
    return True


def verify_exe(port: int = 8791, timeout: int = 600) -> bool:
    """啟動 exe 並確認它真的完成分析。"""
    print()
    print("=" * 66)
    print("3. 實際執行驗證")
    print("=" * 66)
    exe = DIST / EXE_NAME
    if not exe.exists():
        print("  ✗ 找不到 exe")
        return False

    # 用獨立的 settings.json 指定測試埠，避免與開發環境衝突
    (DIST / "settings.json").write_text(
        json.dumps({"port": port}, ensure_ascii=False), encoding="utf-8")

    proc = subprocess.Popen([str(exe), "--no-gui"], cwd=str(DIST),
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.time() + timeout
        last = ""
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(base + "/api/status",
                                            timeout=10) as r:
                    st = json.loads(r.read().decode("utf-8"))
                msg = f"{st.get('progress')}% {st.get('status')}"
                if msg != last:
                    print(f"    {msg}")
                    last = msg
                if st.get("error"):
                    print(f"  ✗ 分析錯誤：{str(st['error'])[:300]}")
                    return False
                if st.get("ready"):
                    break
            except (urllib.error.URLError, TimeoutError, ConnectionError):
                pass
            time.sleep(2)
        else:
            print("  ✗ 逾時未完成分析")
            return False

        with urllib.request.urlopen(base + "/api/summary", timeout=30) as r:
            s = json.loads(r.read().decode("utf-8"))
        n = s.get("n_institutions") or 0
        counts = ((s.get("data") or {}).get("counts") or {})
        print(f"  ✓ 分析完成：{n} 所機構、高風險 {s.get('n_high_risk')} 所")
        print(f"    載入資料：" + "、".join(
            f"{k}={v}" for k, v in counts.items() if v))
        dims = s.get("dimension_avg") or []
        zero = [d["label"] for d in dims if not d.get("avg")]
        if zero:
            print(f"    ! 下列構面平均分數為 0（該資料表可能未納入打包）："
                  f"{'、'.join(zero)}")
        return n > 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
        # 清掉驗證過程產生的檔案，保持交付內容乾淨
        for junk in ("settings.json", "krews.log"):
            (DIST / junk).unlink(missing_ok=True)
        for d in ("output", "sample_data", "data"):
            shutil.rmtree(DIST / d, ignore_errors=True)


def make_release() -> None:
    print()
    print("=" * 66)
    print("4. 整理交付資料夾")
    print("=" * 66)
    shutil.rmtree(RELEASE, ignore_errors=True)
    RELEASE.mkdir(parents=True, exist_ok=True)
    shutil.copy2(DIST / EXE_NAME, RELEASE / EXE_NAME)

    # 同時附上一份外置 data/：評審若要檢視原始資料可直接開啟，
    # 且此資料夾會優先於 exe 內建版本被載入，便於後續更新。
    out_data = RELEASE / "data"
    out_data.mkdir(exist_ok=True)
    for name in REQUIRED_TABLES:
        src = DATA / name
        if src.exists():
            shutil.copy2(src, out_data / name)

    readme = RELEASE / "使用說明.txt"
    readme.write_text(
        "教保機構風險預警系統\n"
        "====================\n\n"
        "【執行方式】\n"
        "  雙擊「教保機構風險預警系統.exe」即可。\n"
        "  程式會自動完成分析並開啟瀏覽器顯示儀表板。\n\n"
        "【系統需求】\n"
        "  Windows 10 或以上，不需安裝 Python 或任何套件。\n"
        "  分析過程完全在本機執行，不需要網路連線。\n\n"
        "【首次開啟的提醒】\n"
        "  本程式未經數位簽章，Windows 可能顯示「已保護您的電腦」警告。\n"
        "  請點選「其他資訊」→「仍要執行」。\n"
        "  程式僅在本機 127.0.0.1 開啟服務，不對外開放連線。\n\n"
        "【資料說明】\n"
        "  data 資料夾內為分析所使用的公開資料集（CSV，可用 Excel 開啟）：\n"
        "    institutions.csv  機構基本資料（全國教保資訊網）\n"
        "    penalties.csv     裁罰紀錄（全國教保資訊網）\n"
        "    evaluations.csv   基礎評鑑結果（全國教保資訊網）\n"
        "    posts.csv         新聞與社群輿情\n"
        "    financials.csv    決算收支彙總（非營利園財務報告）\n"
        "    ledger.csv        決算科目明細（鑑識檢定用）\n"
        "  此資料夾若存在會優先被載入；刪除後程式會使用內建的同一份資料。\n\n"
        "【無法開啟時】\n"
        "  請查看與 exe 同層的 krews.log，內含啟動與錯誤紀錄。\n",
        encoding="utf-8")

    total = sum(f.stat().st_size for f in RELEASE.rglob("*") if f.is_file())
    print(f"  ✓ {RELEASE}")
    for f in sorted(RELEASE.rglob("*")):
        if f.is_file():
            print(f"      {f.relative_to(RELEASE)}"
                  f"（{f.stat().st_size / 1024:.0f} KB）")
    print(f"  合計 {total / 1024 / 1024:.1f} MB")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-build", action="store_true")
    ap.add_argument("--skip-verify", action="store_true")
    ap.add_argument("--port", type=int, default=8791)
    args = ap.parse_args()

    if not check_data():
        return 1
    if not args.skip_build and not run_build():
        return 1
    if not args.skip_verify and not verify_exe(port=args.port):
        print("\n驗證未通過：exe 可以產生，但在乾淨環境下無法完成分析。")
        print("請勿以此版本交付。")
        return 1
    make_release()
    print()
    print("=" * 66)
    print("完成。請把 release 資料夾整個交付，或只交付其中的 exe。")
    print("=" * 66)
    return 0


if __name__ == "__main__":
    sys.exit(main())
