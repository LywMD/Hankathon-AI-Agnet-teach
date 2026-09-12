"""驗證 OCR 擷取之決算數字，過濾內部邏輯矛盾之列（例如人事費超過支出合計），
避免將明顯錯誤的自動辨識結果誤植入風險評分系統。

判定為可疑並剔除的條件（任一成立即剔除）：
* 支出合計 <= 0 或 收入合計 <= 0（欄位辨識失敗的典型徵兆）
* 人事費 > 支出合計（不可能發生於真實決算，代表某一欄位被誤讀）
* 支出合計 或 收入合計 未達 100 萬元（同類機構決算規模通常為千萬級，
  過小代表金額被截斷）
* |支出合計 - 已知細項加總| 占支出合計比例 > 60%（代表細項擷取嚴重失準，
  expense_other 殘差不具參考意義）
"""
from __future__ import annotations

import csv
from pathlib import Path

SRC = Path(r"C:\Users\lyw01\Desktop\work2\data\financials.csv")
OUT_OK = Path(r"C:\Users\lyw01\Desktop\work2\data\financials.csv")
OUT_REJECTED = Path(r"C:\Users\lyw01\Desktop\work2\.tmp\financials_rejected.csv")


def to_f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def is_suspect(row: dict) -> str | None:
    rev = to_f(row.get("total_revenue"))
    exp = to_f(row.get("total_expense"))
    per = to_f(row.get("expense_personnel"))
    if rev is None or exp is None:
        return "缺少收入或支出合計"
    if rev <= 0 or exp <= 0:
        return "收入或支出合計為零或負值"
    if rev < 1_000_000 or exp < 1_000_000:
        return "金額規模過小，疑似欄位截斷"
    if per is not None and per > exp:
        return "人事費超過支出合計，數值矛盾"
    # 部分來源（如市立幼兒園決算）僅有收入／支出合計，無細項科目，
    # 此時跳過細項一致性檢查（沒有細項可比對，並非資料錯誤）。
    detail_fields = ("expense_personnel", "expense_teaching", "expense_facility",
                     "expense_admin")
    if any(row.get(k) not in (None, "") for k in detail_fields):
        known = sum(to_f(row.get(k)) or 0 for k in detail_fields)
        other = to_f(row.get("expense_other")) or 0
        if abs(exp - (known + other)) / exp > 0.05:
            return "細項加總與支出合計不吻合"
        if other < 0 and abs(other) / exp > 0.15:
            return "其他支出殘差為顯著負值，細項擷取失準"
    surplus = to_f(row.get("surplus"))
    if surplus is not None and abs(surplus - (rev - exp)) / rev > 0.05:
        return "本期餘絀與收入減支出不吻合，疑似擷取錯誤"
    return None


def main() -> None:
    with SRC.open("r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    fieldnames = list(rows[0].keys()) if rows else []

    kept, rejected = [], []
    for row in rows:
        reason = is_suspect(row)
        if reason:
            row["_reject_reason"] = reason
            rejected.append(row)
        else:
            kept.append(row)

    with OUT_OK.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in kept:
            w.writerow(r)

    with OUT_REJECTED.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames + ["_reject_reason"])
        w.writeheader()
        for r in rejected:
            w.writerow(r)

    print(f"total={len(rows)} kept={len(kept)} rejected={len(rejected)}")
    print(f"rejected list written to {OUT_REJECTED}")


if __name__ == "__main__":
    main()
