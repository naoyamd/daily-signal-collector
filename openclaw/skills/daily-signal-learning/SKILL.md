---
name: daily-signal-learning
description: Daily Signalの記事やVault項目について、利用者が明示した評価だけを収集側へ返す。
---

# Daily Signal feedback

利用者が明示的に「役立った」「不要」「今後増やして」「減らして」などと評価した場合だけ、
`/home/node/.openclaw/workspace/daily-signal-collector/.collector/feedback-inbox/`へ1イベント1 JSONで保存する。
推測した評価や通常会話は保存しない。

```json
{
  "type": "feedback",
  "event_id": "重複しない安定ID",
  "article_id": "対象記事または項目",
  "item_id": "分かる場合のVault item ID",
  "rating": "up|down|0から1の数値",
  "note": "利用者が明示した理由",
  "signals": {"source": "分かる場合", "keywords": ["明示された関心語"]}
}
```

秘密情報、記事本文、Web本文は書かない。
