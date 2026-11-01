#!/usr/bin/env python3
"""Package only allowlisted public assets. Never include local patient artifacts."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ("index.html", "styles.css", "app.js", "demo-data.js")


def main() -> None:
    output = ROOT / "artifacts" / "releases" / "HRV-EarlyWarning-demo.zip"
    output.parent.mkdir(parents=True, exist_ok=True)
    contents = {}
    for name in ASSETS:
        path = ROOT / "web" / name
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"Expected regular public asset: {name}")
        contents[name] = path.read_bytes()
    contents["README.txt"] = (
        "HRV Early Warning — 循環風險研究原型\n\n"
        "解壓縮整個資料夾，再以 Chrome、Edge 或 Firefox 開啟 index.html。\n"
        "無須安裝、不需網路。三個情境均為合成資料，分數僅供介面展示。\n"
        "操作：選病例 → 播放或拖動時間 → 檢視品質及模型 → 研究證據。\n"
        "可匯入本機 hrv-replay-v1 JSON；資料只留在瀏覽器記憶體。\n"
        "此壓縮檔不含患者資料、模型權重或私有設定。\n"
        "研究結果尚未證實 HRV 額外效益；不可用於臨床決策。\n\n"
        "原始碼與完整方法：\n"
        "https://github.com/zxc29051617/Biomedical-Innovation-2026_HRV-EarlyWarning\n"
    ).encode("utf-8")
    hashes = {name: hashlib.sha256(data).hexdigest() for name, data in contents.items()}
    contents["manifest.json"] = (json.dumps(hashes, indent=2) + "\n").encode()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(contents.items()):
            # Fixed metadata keeps repeated builds of the same assets identical.
            info = zipfile.ZipInfo("HRV-EarlyWarning-demo/" + name, (2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, data)
    print(json.dumps({"path": str(output.relative_to(ROOT)), "assets": len(contents),
                      "sha256": hashlib.sha256(output.read_bytes()).hexdigest()}))


if __name__ == "__main__":
    main()
