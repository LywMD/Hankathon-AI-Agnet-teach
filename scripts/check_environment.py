"""環境檢查工具：診斷系統執行所需的依賴與配置。

此腳本不需要任何外部依賴（除 Python 標準函式庫），可用於初次安裝時的環境診斷。

使用方法：
    python scripts/check_environment.py

檢查項目：
    - Python 版本
    - 必要套件（PyMuPDF、其他）
    - Tesseract OCR 安裝與配置
    - 繁體中文訓練資料
    - 資料來源目錄結構
    - 輸出目錄權限
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def print_section(title: str) -> None:
    """印出區段標題。"""
    print(f"\n{'=' * 70}")
    print(f"  {title}")
    print('=' * 70)


def check_python() -> bool:
    """檢查 Python 版本。"""
    print_section("Python 環境")
    
    ver = sys.version_info
    print(f"版本: {ver.major}.{ver.minor}.{ver.micro}")
    print(f"路徑: {sys.executable}")
    print(f"平台: {sys.platform}")
    
    if ver.major < 3 or (ver.major == 3 and ver.minor < 8):
        print("✗ 需要 Python 3.8 或更新版本")
        return False
    
    print("✓ Python 版本符合需求")
    return True


def check_packages() -> bool:
    """檢查必要套件。"""
    print_section("Python 套件")
    
    required = {
        "fitz": "PyMuPDF",
        "PIL": "Pillow (選用，用於圖片處理)",
    }
    
    all_ok = True
    for module, desc in required.items():
        try:
            __import__(module)
            print(f"✓ {desc}")
        except ImportError:
            print(f"✗ {desc} - 未安裝")
            if module == "fitz":
                print(f"  安裝方法: pip install PyMuPDF")
                all_ok = False
            else:
                print(f"  （選用套件，可跳過）")
    
    return all_ok


def check_tesseract() -> dict:
    """檢查 Tesseract OCR。"""
    print_section("Tesseract OCR")
    
    result = {"installed": False, "version": None, "path": None}
    
    # 檢查環境變數
    tess_path = os.getenv("TESSERACT_PATH")
    if tess_path:
        print(f"環境變數 TESSERACT_PATH: {tess_path}")
    else:
        tess_path = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
        print(f"使用預設路徑: {tess_path}")
    
    tess_file = Path(tess_path)
    if not tess_file.exists():
        print(f"✗ Tesseract 執行檔不存在: {tess_path}")
        print(f"\n安裝指南:")
        print(f"  1. 下載安裝檔:")
        print(f"     https://github.com/UB-Mannheim/tesseract/wiki")
        print(f"  2. 執行安裝（預設路徑即可）")
        print(f"  3. 安裝時勾選「Additional language data」中的「Chinese Traditional」")
        return result
    
    result["path"] = tess_path
    print(f"✓ 找到執行檔: {tess_path}")
    
    # 執行版本檢查
    try:
        proc = subprocess.run(
            [tess_path, "--version"],
            capture_output=True,
            text=True,
            timeout=5
        )
        
        if proc.returncode == 0:
            version_line = proc.stdout.split("\n")[0] if proc.stdout else ""
            result["version"] = version_line
            result["installed"] = True
            print(f"✓ {version_line}")
        else:
            print(f"✗ 執行失敗（返回碼 {proc.returncode}）")
            
    except subprocess.TimeoutExpired:
        print(f"✗ 執行超時")
    except Exception as e:
        print(f"✗ 執行錯誤: {e}")
    
    return result


def check_tessdata() -> bool:
    """檢查 Tesseract 訓練資料。"""
    print_section("Tesseract 訓練資料")
    
    tessdata = os.getenv("TESSDATA_PREFIX")
    if tessdata:
        print(f"環境變數 TESSDATA_PREFIX: {tessdata}")
    else:
        # 嘗試多個常見位置
        candidates = [
            r"C:\Users\lyw01\AppData\Local\Temp\tessdata",
            r"C:\Program Files\Tesseract-OCR\tessdata",
            Path(sys.executable).parent / "share" / "tessdata",
        ]
        
        for cand in candidates:
            if Path(cand).exists():
                tessdata = str(cand)
                print(f"找到訓練資料目錄: {tessdata}")
                break
        else:
            print(f"✗ 找不到 tessdata 目錄")
            print(f"  請設定環境變數 TESSDATA_PREFIX")
            return False
    
    tessdata_path = Path(tessdata)
    if not tessdata_path.exists():
        print(f"✗ 目錄不存在: {tessdata}")
        return False
    
    print(f"✓ 訓練資料目錄: {tessdata}")
    
    # 檢查繁體中文資料
    chi_tra = tessdata_path / "chi_tra.traineddata"
    if not chi_tra.exists():
        print(f"✗ 繁體中文訓練資料不存在: chi_tra.traineddata")
        print(f"\n下載方法:")
        print(f"  1. 前往: https://github.com/tesseract-ocr/tessdata")
        print(f"  2. 下載 chi_tra.traineddata")
        print(f"  3. 放至: {tessdata_path}")
        return False
    
    print(f"✓ 繁體中文訓練資料: {chi_tra}")
    
    # 列出所有可用語言
    traineddata_files = list(tessdata_path.glob("*.traineddata"))
    langs = [f.stem for f in traineddata_files]
    print(f"\n可用語言 ({len(langs)} 個): {', '.join(sorted(langs)[:10])}...")
    
    return True


def check_data_source() -> dict:
    """檢查資料來源目錄。"""
    print_section("資料來源")
    
    result = {"exists": False, "nonprofit": 0, "public": 0}
    
    data_source = os.getenv("DATA_SOURCE_PATH", r"C:\Users\lyw01\Desktop\w")
    print(f"資料來源路徑: {data_source}")
    if os.getenv("DATA_SOURCE_PATH"):
        print(f"  (由環境變數 DATA_SOURCE_PATH 設定)")
    else:
        print(f"  (使用預設路徑)")
    
    src_path = Path(data_source)
    if not src_path.exists():
        print(f"✗ 目錄不存在")
        print(f"\n請執行以下其中一項:")
        print(f"  1. 將資料放至: {src_path}")
        print(f"  2. 設定環境變數 DATA_SOURCE_PATH 指向實際資料目錄")
        return result
    
    result["exists"] = True
    print(f"✓ 目錄存在")
    
    # 檢查非營利園財報
    nonprofit_dir = src_path / "非營利園財報"
    if nonprofit_dir.exists():
        pdf_files = list(nonprofit_dir.glob("**/*.pdf"))
        result["nonprofit"] = len(pdf_files)
        print(f"✓ 非營利園財報: {len(pdf_files)} 份 PDF")
        
        # 統計學年度分布
        year_dist = {}
        for pdf in pdf_files:
            for part in pdf.parts:
                if "學年度" in part:
                    year_dist[part] = year_dist.get(part, 0) + 1
        
        if year_dist:
            print(f"  學年度分布:")
            for year, count in sorted(year_dist.items()):
                print(f"    {year}: {count} 份")
    else:
        print(f"⚠ 非營利園財報目錄不存在: {nonprofit_dir}")
    
    # 檢查公立學校
    public_dir = src_path / "公校"
    if public_dir.exists():
        pdf_files = list(public_dir.glob("**/*.pdf"))
        result["public"] = len(pdf_files)
        print(f"✓ 公立學校決算書: {len(pdf_files)} 份 PDF")
        
        # 統計年度分布
        year_dist = {}
        for pdf in pdf_files:
            for part in pdf.parts:
                if "年度決算書" in part:
                    year_dist[part] = year_dist.get(part, 0) + 1
        
        if year_dist:
            print(f"  年度分布:")
            for year, count in sorted(year_dist.items()):
                print(f"    {year}: {count} 份")
    else:
        print(f"⚠ 公立學校目錄不存在: {public_dir}")
    
    return result


def check_output_dir() -> bool:
    """檢查輸出目錄。"""
    print_section("輸出目錄")
    
    base_dir = Path(__file__).resolve().parent.parent
    out_dir = base_dir / "data"
    log_dir = base_dir / ".tmp"
    
    print(f"專案根目錄: {base_dir}")
    print(f"資料輸出: {out_dir}")
    print(f"日誌輸出: {log_dir}")
    
    # 嘗試建立目錄
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        log_dir.mkdir(parents=True, exist_ok=True)
        print(f"✓ 目錄可寫入")
        
        # 檢查現有檔案
        if (out_dir / "institutions.csv").exists():
            print(f"  ℹ 發現現有檔案: institutions.csv")
        if (out_dir / "financials.csv").exists():
            print(f"  ℹ 發現現有檔案: financials.csv")
        
        return True
        
    except Exception as e:
        print(f"✗ 無法建立目錄: {e}")
        return False


def print_summary(checks: dict) -> None:
    """印出檢查總結。"""
    print_section("檢查總結")
    
    all_ok = all(checks.values())
    
    if all_ok:
        print("✓ 所有檢查通過，環境已就緒")
        print(f"\n下一步:")
        print(f"  python scripts/extract_all.py    # 執行資料萃取")
        print(f"  python run.py                    # 啟動分析系統")
    else:
        print("⚠ 部分檢查未通過，請修正後重試")
        print(f"\n需要修正的項目:")
        for name, ok in checks.items():
            if not ok:
                print(f"  ✗ {name}")


def main() -> int:
    """主檢查流程。"""
    print(f"\n教保機構風險預警系統 - 環境檢查工具")
    print(f"=" * 70)
    
    checks = {}
    
    # 1. Python
    checks["Python 版本"] = check_python()
    
    # 2. 套件
    checks["Python 套件"] = check_packages()
    
    # 3. Tesseract
    tess_result = check_tesseract()
    checks["Tesseract OCR"] = tess_result["installed"]
    
    # 4. Tessdata
    if tess_result["installed"]:
        checks["Tesseract 訓練資料"] = check_tessdata()
    else:
        checks["Tesseract 訓練資料"] = False
    
    # 5. 資料來源
    data_result = check_data_source()
    checks["資料來源"] = data_result["exists"]
    
    # 6. 輸出目錄
    checks["輸出目錄"] = check_output_dir()
    
    # 總結
    print_summary(checks)
    
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
