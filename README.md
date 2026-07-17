# Daily Signal Collector

公開Web情報を収集・学習し、Obsidian互換Markdownへ蓄積する独立サービスです。
ブログ生成、Hugo、Git commit/push、GitHub Pages公開の機能と権限は持ちません。

## Responsibility boundary

```text
daily-signal-collector                         daily-signal (blog)
RSS + OpenClaw generic web research
        ↓
Obsidian Markdown Vault (all in-scope items)
        ↓
adaptive ranking
        ↓
/var/lib/daily-signal-exchange/
  candidates/{digest,deep-dive,market}.json ──→ validate → edit → publish
  archive/<batch-id>.json                    ←── editorial feedback JSON
  feedback/*.json
```

境界は次の2契約だけです。

- `daily-signal-candidates/v1`: collectorが原子的に書く、期限付き・版別の候補JSON
- `daily-signal-feedback/v1`: blogが記事採否と全候補評価を返すJSON

ブログ側はVault、学習SQLite、収集設定、Webスカウトへアクセスしません。collector側は
ブログのGit資格情報、Hugoコンテンツ、公開処理へアクセスしません。

## Collection policy

保守性を優先し、収集方法は次の2つに限定します。

- RSS / Atom
- OpenClawによる一般Web検索と公開ページの確認

サイト専用APIクライアント、サイト固有HTMLパーサー、ログイン回避は実装しません。
閲覧できないページはスキップし、本文・PDFは保存せず、公開メタデータ、短い要約、元URLだけを扱います。

重点枠はAIによる設計30%、大手の民間・軍用航空機25%、航空機エンジン25%、CAD / CAE 20%です。
ドローン、UAV、UAS、eVTOL、エアタクシー等はWebスカウトとcollectorの両方で除外し、Vaultへも入れません。

`config/sources.yaml`の49社は学習結果で脱落しない必須ウォッチリストです。7社ずつ確認し、
7日で全社を一巡します。新規公開情報がなければ古いページで水増ししません。

Webスカウトの受け渡しは`daily-signal-scout/v2`です。生成時刻、実行クエリ、当日担当企業ごとの
`found / no_new_finding / unreachable`を必須にし、研究計画とのcoverageと鮮度を実行後に厳格検証します。
不完全な場合は検証診断を渡して1回だけ修復し、なお不正ならその回はRSS / Atomだけで安全に継続します。
同日再実行でも新しいOpenClaw sessionを使うため、古い会話状態を探索結果へ混入させません。

RSS / Atomは一時障害を指数バックオフ付きで再試行し、レスポンスサイズとMIME typeを検査します。
ETag / Last-Modifiedによる条件付き取得は`.collector/feed-http-cache.json`へ保存します。公開日が不明または
未来すぎる情報を収集時刻で補完せず、鮮度加点を与えないため、古い常設ページが最新ニュースとして浮上しません。

## Obsidian pool

正本は既定で`/opt/openclaw/data/workspace/daily-signal-vault`です。1情報を1 Markdownとして保存し、
YAML front matterへURL、DOI、著者、公開日、収集日、情報種別、スコア、編集状態を記録します。
日次MarkdownはObsidian wikilink索引です。管理マーカー外の手書きメモは再収集でも保持します。

```bash
python -m scripts.knowledge_pool --vault /opt/openclaw/data/workspace/daily-signal-vault search "aircraft engine"
python -m scripts.knowledge_pool --vault /opt/openclaw/data/workspace/daily-signal-vault report
python -m scripts.knowledge_pool --vault /opt/openclaw/data/workspace/daily-signal-vault rebuild
```

SQLiteは`/var/lib/daily-signal-collector/learning.sqlite3`に置く再構築可能な学習索引だけです。
情報本文の正本にはしません。情報源、キーワード、検索クエリ、ドメイングループごとにBeta事後分布を更新し、
高評価領域を深掘りしながら上限付き探索枠を残します。

## Local development

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m unittest discover -s tests
python -m scripts.adaptive_learning --db .collector/learning.sqlite3 plan \
  --config config/sources.yaml --output .collector/research-plan.json
python -m scripts.web_scout validate .collector/scout.json \
  --research-plan .collector/research-plan.json --max-age-hours 6
python -m scripts.collector_pipeline --config config/sources.yaml \
  --vault .collector/vault --scout .collector/scout.json \
  --learning-db .collector/learning.sqlite3 --edition digest \
  --output .collector/candidates.json
```

## VPS installation

想定配置:

- collector: `/opt/openclaw/data/workspace/daily-signal-collector`
- blog: `/opt/openclaw/data/workspace/daily-signal`
- exchange: `/var/lib/daily-signal-exchange`

collectorを先に、blogを後に導入します。

```bash
cd /opt/openclaw/data/workspace/daily-signal-collector
python3 -m venv .venv
sudo bash ops/install-vps.sh

# timerを有効化する前にcollector単体を確認
sudo systemctl start daily-signal-collector@digest.service
sudo journalctl -u daily-signal-collector@digest.service -n 200 --no-pager

cd /opt/openclaw/data/workspace/daily-signal
python3 -m venv .venv
sudo bash ops/install-vps.sh
sudo systemctl enable --now \
  daily-signal-emma.timer \
  daily-signal-emma-deep-dive.timer \
  daily-signal-emma-market.timer
```

repo、Vault、state、JSON exchangeのパスと実行ユーザーはsystemd unitと同じ固定値を使用します。
異なるパスが必要な場合は、runnerだけを環境変数で変更せず、unitとinstallerを一組で変更してください。
Scoutの既定は本探索1800秒、修復探索900秒、最大2回、handoff鮮度6時間です。必要な場合だけ
`/etc/default/daily-signal-collector`で`DAILY_SIGNAL_SCOUT_TIMEOUT`、
`DAILY_SIGNAL_SCOUT_REPAIR_TIMEOUT`、`DAILY_SIGNAL_SCOUT_ATTEMPTS`を調整できます。
handoff鮮度と件数上限は版別YAMLの`openclaw_scout`を変更します。

blogの各systemd serviceは対応する`daily-signal-collector@<edition>.service`を`Requires`し、
候補JSONの生成完了後にだけ編集・公開を開始します。手動確認は次のとおりです。

```bash
sudo systemctl start daily-signal-collector@digest.service
sudo journalctl -u daily-signal-collector@digest.service -n 200 --no-pager
python -m scripts.knowledge_pool --vault /opt/openclaw/data/workspace/daily-signal-vault report
```

## License

MIT
