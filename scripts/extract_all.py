"""統一資料萃取入口：整合非營利園財報與公立學校決算書的萃取流程。

執行順序：
1. 檢查環境依賴（Tesseract、PyMuPDF）
2. 萃取非營利園財報 → institutions.csv + financials.csv
3. 萃取公立學校決算書 → 追加至 institutions.csv + financials.csv
4. 驗證資料完整性與一致性
5. 產生執行報告

使用方法：
    python scripts/extract_all.py

環境變數（選用）：
    DATA_SOURCE_PATH    - 資料來源根目錄（預設：C:\Users\lyw01\Desktop\w）
    TESSERACT_PATH      - Tesseract 執行檔路徑
    TESSDATA_PREFIX     - Tesseract 訓練資料目錄
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_SOURCE = Path(os.getenv("DATA_SOURCE_PATH", r"C:\Users\lyw01\Desktop\w"))
OUT_DIR = BASE_DIR / "data"
LOG_DIR = BASE_DIR / ".tmp"


def check_environment() -> bool:
    """檢查執行環境是否就緒。"""
    print("=" * 60)
    print("環境檢查")
    print("=" * 60)
    
    # 檢查 Python 版本
    py_ver = sys.version_info
    print(f"✓ Python {py_ver.major}.{py_ver.minor}.{py_ver.micro}")
    
    # 檢查 PyMuPDF
    try:
        import fitz
        print(f"✓ PyMuPDF (fitz) {fitz.version[0]}")
    except ImportError:
        print("✗ PyMuPDF (fitz) 未安裝")
        print("  請執行: pip install PyMuPDF")
        return False
    
    # 檢查 Tesseract
    tess_path = os.getenv("TESSERACT_PATH", r"C:\Program Files\Tesseract-OCR\tesseract.exe")
    if not Path(tess_path).exists():
        print(f"✗ Tesseract 未找到: {tess_path}")
        print("  請安裝 Tesseract OCR:")
        print("  https://github.com/UB-Mannheim/tesseract/wiki")
        return False
    
    try:
        result = subprocess.run(
            [tess_path, "--version"],
            capture_output=True,
            text=True,
            timeout=5
        )
        ver_line = result.stdout.split("\n")[0] if result.stdout else "unknown"
        print(f"✓ Tesseract {ver_line}")
    except Exception as e:
        print(f"✗ Tesseract 執行失敗: {e}")
        return False
    
    # 檢查 tessdata
    tessdata = os.getenv("TESSDATA_PREFIX", r"C:\Users\lyw01\AppData\Local\Temp\tessdata")
    chi_tra = Path(tessdata) / "chi_tra.traineddata"
    if not chi_tra.exists():
        print(f"✗ 繁體中文訓練資料未找到: {chi_tra}")
        print("  請下載 chi_tra.traineddata 至 tessdata 目錄")
        print("  https://github.com/tesseract-ocr/tessdata")
        return False
    print(f"✓ 繁體中文訓練資料: {chi_tra}")
    
    # 檢查資料來源目錄
    if not DATA_SOURCE.exists():
        print(f"✗ 資料來源目錄不存在: {DATA_SOURCE}")
        print(f"  請設定環境變數 DATA_SOURCE_PATH 或將資料放至預設路徑")
        return False
    print(f"✓ 資料來源目錄: {DATA_SOURCE}")
    
    nonprofit_dir = DATA_SOURCE / "非營利園財報"
    public_dir = DATA_SOURCE / "公校"
    
    if not nonprofit_dir.exists():
        print(f"⚠ 非營利園財報目錄不存在: {nonprofit_dir}")
    else:
        pdf_count = len(list(nonprofit_dir.glob("**/*.pdf")))
        print(f"✓ 非營利園財報: {pdf_count} 份 PDF")
    
    if not public_dir.exists():
        print(f"⚠ 公立學校目錄不存在: {public_dir}")
    else:
        pdf_count = len(list(public_dir.glob("**/*.pdf")))
        print(f"✓ 公立學校決算書: {pdf_count} 份 PDF")
    
    print()
    return True


def run_script(script_name: str, description: str) -> bool:
    """執行指定的萃取腳本。"""
    print("=" * 60)
    print(description)
    print("=" * 60)
    
    script_path = BASE_DIR / "scripts" / script_name
    if not script_path.exists():
        print(f"✗ 腳本不存在: {script_path}")
        return False
    
    start = time.time()
    try:
        result = subprocess.run(
            [sys.executable, str(script_path)],
            capture_output=True,
            text=True,
            timeout=3600  # 1 小時超時
        )
        elapsed = time.time() - start
        
        # 顯示標準輸出
        if result.stdout:
            print(result.stdout)
        
        # 顯示標準錯誤
        if result.stderr:
            print("標準錯誤:", file=sys.stderr)
            print(result.stderr, file=sys.stderr)
        
        if result.returncode == 0:
            print(f"✓ 完成（耗時 {elapsed:.1f} 秒）")
            return True
        else:
            print(f"✗ 執行失敗（返回碼 {result.returncode}）")
            return False
            
    except subprocess.TimeoutExpired:
        print(f"✗ 執行超時（> 1 小時）")
        return False
    except Exception as e:
        print(f"✗ 執行錯誤: {e}")
        return False


def validate_output() -> None:
    """驗證輸出檔案。"""
    print("=" * 60)
    print("輸出驗證")
    print("=" * 60)
    
    inst_file = OUT_DIR / "institutions.csv"
    fin_file = OUT_DIR / "financials.csv"
    
    if not inst_file.exists():
        print(f"✗ institutions.csv 不存在: {inst_file}")
        return
    
    if not fin_file.exists():
        print(f"✗ financials.csv 不存在: {fin_file}")
        return
    
    try:
        import csv
        
        with inst_file.open("r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            inst_rows = list(reader)
            inst_fields = reader.fieldnames or []
        
        with fin_file.open("r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            fin_rows = list(reader)
            fin_fields = reader.fieldnames or []
        
        print(f"✓ institutions.csv: {len(inst_rows)} 筆資料, {len(inst_fields)} 個欄位")
        print(f"  欄位: {', '.join(inst_fields[:5])}...")
        
        print(f"✓ financials.csv: {len(fin_rows)} 筆資料, {len(fin_fields)} 個欄位")
        print(f"  欄位: {', '.join(fin_fields[:5])}...")
        
        # 統計機構類型
        org_types = {}
        for row in inst_rows:
            ot = row.get("org_type", "未分類")
            org_types[ot] = org_types.get(ot, 0) + 1
        
        print(f"\n機構類型分布:")
        for ot, count in sorted(org_types.items(), key=lambda x: -x[1]):
            print(f"  {ot}: {count} 所")
        
        # 檢查 inst_id 唯一性
        inst_ids = [r.get("inst_id") for r in inst_rows if r.get("inst_id")]
        if len(inst_ids) != len(set(inst_ids)):
            print(f"⚠ 警告: 發現重複的 inst_id")
        
        print()
        
    except Exception as e:
        print(f"✗ 驗證失敗: {e}")


def main() -> int:
    """主執行流程。"""
    print(f"\n教保機構風險預警系統 - 資料萃取工具")
    print(f"資料來源: {DATA_SOURCE}")
    print(f"輸出目錄: {OUT_DIR}\n")
    
    # 1. 環境檢查
    if not check_environment():
        print("\n環境檢查未通過，請修正後重試。")
        return 1
    
    # 確保輸出目錄存在
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    
    # 2. 萃取非營利園財報
    nonprofit_ok = run_script(
        "extract_nonprofit_financials.py",
        "步驟 1/2: 萃取非營利園財報（OCR）"
    )
    
    if not nonprofit_ok:
        print("\n⚠ 非營利園財報萃取失敗，但繼續執行公立學校萃取...")
    
    # 3. 萃取公立學校決算書
    public_ok = run_script(
        "extract_public_schools.py",
        "步驟 2/2: 萃取公立學校決算書（OCR）"
    )
    
    if not public_ok:
        print("\n⚠ 公立學校決算書萃取失敗")
    
    # 4. 驗證輸出
    validate_output()
    
    # 5. 總結
    print("=" * 60)
    print("萃取完成")
    print("=" * 60)
    
    if nonprofit_ok and public_ok:
        print("✓ 所有資料萃取成功")
        print(f"\n輸出檔案:")
        print(f"  - {OUT_DIR / 'institutions.csv'}")
        print(f"  - {OUT_DIR / 'financials.csv'}")
        print(f"\n日誌檔案:")
        print(f"  - {LOG_DIR / 'extract_log.txt'}")
        print(f"  - {LOG_DIR / 'extract_public_schools_log.txt'}")
        print(f"\n下一步: 執行 python run.py 啟動分析系統")
        return 0
    else:
        print("⚠ 部分萃取失敗，請檢查日誌檔案")
        return 1


if __name__ == "__main__":
    sys.exit(main())
