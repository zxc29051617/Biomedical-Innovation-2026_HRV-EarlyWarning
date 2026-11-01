# 研究原型交付與驗收

第一版提供可離線操作的病例回放介面，將模型分數、生命徵象、RR 變異、品質拒絕及研究證據放在同一個工作流程。這是研究檢視工具；v1 尚無清楚的歷史 RR 增益證據，也尚未完成臨床及保留測試驗證。

## 可操作的交付

- `web/index.html`：直接開啟，不需後端、帳號或套件。三個合成情境標示為示意分數。
- `scripts/serve_demo.py`：可選的 loopback 靜態伺服器，只提供公開資產，不提供患者 API。
- `scripts/export_replay.py`：驗證完整 M2 快取、來源指紋、患者分組與已存模型後，固定選取三個開發驗證病例供本機匯入。預測前保存私有選例 manifest，不讀保留測試特徵或重訓。
- `scripts/package_demo.py`：以白名單打包公開網頁及說明；不含資料、權重、設定或其他 repo 檔案。

真實本機匯出包含三例、224 個逐小時時點，113 個當下 RR 片段通過 v1 工程篩選。選中的事件病例最後六小時沒有可用 RR，所以配對模型不出分；保留此結果，不事後換成表現更好的病例。此數字只描述這次固定展示選例，不代表研究族群效能。

## 可重跑的檢查

```sh
PYTHONPATH=src python -m unittest discover -s tests -v
python -m pip install playwright==1.56.0
python -m playwright install chromium
python scripts/check_demo_browser.py
python scripts/package_demo.py
```

完整 Python 測試需要 `signal,model,report` 選用依賴；CI 以 `configs/requirements-ci.txt` 約束直接依賴版本。瀏覽器測試僅使用人工合成資料，驗證離線載入、播放、切換模型、缺值不補、品質拒絕、事件到時才顯示、未來數值不能改變當下圖表、JSON 匯入與惡意文字處理、摘要匯出、手機版及無外部請求／持久儲存。

GitHub Actions 使用標準 `ubuntu-24.04` 公開執行器，唯讀 repo 權限、無資料庫帳密、無 MIMIC 掛載。公開 ECG 工程試行與產品測試分開；只保留彙總結果、合成展示與畫面截圖，不上傳下載的原始 ECG。

## 交付狀態

本機 Python 與前端核心檢查、實際瀏覽器驗收、公開 ECG 試行結果及發布位置於執行後記錄，未執行者不列為通過。這個開發環境限制 socket bind 與外部 DNS，因此本機靜態伺服器、瀏覽器安裝及線上部署需透過可用的正常執行環境完成；HTML 離線模式不依賴這些功能。
