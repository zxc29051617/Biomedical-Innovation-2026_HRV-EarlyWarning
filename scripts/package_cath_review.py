#!/usr/bin/env python3
"""Package allowlisted offline ECG review assets and synthetic practice only."""
import hashlib
import json
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ("index.html", "styles.css", "core.js", "app.js")


def main():
    output = ROOT / "artifacts/releases/Cath-ECG-Review.zip"
    output.parent.mkdir(parents=True, exist_ok=True)
    contents = {}
    for name in ASSETS:
        path = ROOT / "web/cath-review" / name
        if path.is_symlink() or not path.is_file():
            raise ValueError("Expected regular allowlisted review asset")
        contents[name] = path.read_bytes()
    contents["README.txt"] = (
        "十二導程 ECG 盲性複核工具\n\n"
        "解壓縮整個資料夾，再用 Chrome、Edge 或 Firefox 開啟 index.html。\n"
        "載入合成練習即可操作；沒有真實患者資料或模型分數。\n"
        "可匯入 cath-ecg-review-v1 JSON，波形僅留頁面記憶體，沒有上傳或自動保存。\n"
        "依團隊經核定的判讀手冊填寫，匯出 cath-ecg-annotation-v1 JSON。\n"
        "切換病例會清空判讀，請先匯出。工具不會自動量測ST或判斷OMI。\n"
        "圖格以秒/mV表示，螢幕毫米不等於紙張毫米。尚未完成臨床量測驗證。\n\n"
        "https://github.com/zxc29051617/Biomedical-Innovation-2026_HRV-EarlyWarning\n"
    ).encode()
    contents["manifest.json"] = (json.dumps(
        {name: hashlib.sha256(data).hexdigest() for name, data in contents.items()}, indent=2) + "\n").encode()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(contents.items()):
            info = zipfile.ZipInfo("Cath-ECG-Review/" + name, (2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, data)
    print(json.dumps({"path": str(output.relative_to(ROOT)), "files": len(contents),
                      "sha256": hashlib.sha256(output.read_bytes()).hexdigest()}))


if __name__ == "__main__":
    main()
