# Cloudflare Zero Trust で公開する(日本語)

手元の Linux マシン(自宅サーバー、VPS、余っている PC など)で jupyter-rmcp が
動いている状態から、**スマホの Claude アプリから使えるようにする**までの手順です。

Cloudflare Zero Trust(以下 CFZT)を触ったことがなくても進められるように書いています。
実際の作業はスクリプト 1 本(`scripts/setup_cfzt.py`)がやるので、あなたがやるのは
**ブラウザでしかできない 2〜3 個の準備**と、設定ファイルに値を入れることだけです。

- ゼロから VM も含めて作りたい → [GCE 無料枠の手順書](../deploy/gce-cloudflare.md)
  (アカウント準備は [setup.md](setup.md))
- API を 1 つずつ理解しながら手で設定したい → [IAP-OAUTH.md](../IAP-OAUTH.md)
- 英語版 → [cloudflare-zero-trust.md](../deploy/cloudflare-zero-trust.md)

## そもそも何をしているのか

このサーバーは **「任意のコードを実行できる口」** です。認証なしでインターネットから
届く状態は、そのまま「誰でも入れるリモートシェル」を意味します。だから公開するときは
必ず前に認証を置きます。

CFZT でやるのは次の 2 つです。

**1. トンネル(Cloudflare Tunnel)** — あなたのマシンで `cloudflared` という常駐
プロセスが動き、Cloudflare 側へ**外向きに**接続を張ります。ポートを開けません。
ルーターのポート開放も、固定 IP も要りません。外からポートスキャンしても、
そもそも待ち受けているものが無いので何も見つかりません。

**2. Access(アクセスポリシー)** — 「このホスト名に来たリクエストは、
**このメールアドレスでログインした人だけ**通す」という判定を、**Cloudflare 側で**
やります。許可されないリクエストはあなたのマシンに 1 バイトも届きません。

```
スマホの Claude ──ログイン──┐
Claude Code ──サービストークン┤
                             ▼
        ┌── Cloudflare のエッジ:あなたのメールか、さもなくば拒否 ──┐
   その他全員 ────────► 401 / ログイン画面(ここで止まる)          │
        └──────────────────┬────────────────────────────────────┘
                           │ 許可された場合のみ
                           ▼
                手元のマシンから張った外向きトンネル(開放ポート無し)
                           ▼
                 127.0.0.1:7130 → カーネル(コードを実行)
```

この「**手前で止まる**」が全てです。逆に言うと、Access のポリシーが緩ければ、
その範囲の全員があなたのマシンでコードを実行できます。

## 必要なもの・費用

| 必要なもの | 費用 | 備考 |
|---|---|---|
| Cloudflare アカウント | 無料(Zero Trust は 50 ユーザーまで無料) | クレカ登録も不要 |
| 独自ドメイン | 年 1,500 円程度 | **Cloudflare でネームサーバーを管理していること**が必須 |
| Linux マシン | — | すでに jupyter-rmcp が動いていること |

ドメインは必須です。CFZT は「Cloudflare が DNS を持っているホスト名」しか守れないので、
ここだけは回避できません。Cloudflare Registrar で新規に取るのが一番手っ取り早いです
(原価売りなので安い)。既存ドメインでも、ネームサーバーを Cloudflare に向ければ使えます。

## 準備(ブラウザでの作業)

### 準備 1 — ドメインを Cloudflare に載せる

https://dash.cloudflare.com でアカウントを作り、ドメインを追加します。
既存ドメインの場合は、指示されたネームサーバー 2 つを、そのドメインを買った業者
(お名前.com など)の管理画面で設定します。反映まで数十分〜数時間かかることがあります。

ダッシュボードでそのドメインの状態が **Active** になっていれば OK です。

### 準備 2 — Zero Trust を有効化する

ダッシュボード左メニューの **Zero Trust** を開きます。初回は**チーム名**を聞かれるので
決めてください(`<チーム名>.cloudflareaccess.com` があなたのログイン用ドメインになります)。
プランは **Free** を選びます。支払い方法の入力を求められることがありますが、Free プランなら
課金されません。

### 準備 3 — API トークンを作る

スクリプトが Cloudflare を操作するための鍵です。
**My Profile → API Tokens → Create Token → Create Custom Token** と進み、
Permissions に次の 4 行を追加します。

| 種別 | 権限 | レベル |
|---|---|---|
| Account | Cloudflare Tunnel | Edit |
| Account | Access: Apps and Policies | Edit |
| Account | Access: Service Tokens | Edit(別マシンの Claude Code を使う場合のみ) |
| Zone | DNS | Edit |

Account Resources は自分のアカウント、Zone Resources は上のドメインを指定します。

**トークンは作成時に 1 回しか表示されません。** その場でコピーしてください。
権限が足りないトークンでも、スクリプトが「どの権限が足りないか」を名前で教えるので、
足りなければ作り直せば大丈夫です(既存トークンの権限追加は UI が渋いので、
作り直すほうが早いことが多いです)。

アカウント ID は、ダッシュボードの URL の `/accounts/` の後ろにある文字列です。
省略しても自動検出されます。

## 1 つだけ必ず引っかかる設定

**`.env` の `MCP_BEARER` を空にしてください。**

Access の OAuth を有効にすると、Cloudflare が**自分のトークンを** `Authorization`
ヘッダに入れて転送してきます。すると `MCP_BEARER` と一致しなくなり、MCP サーバーが
全リクエストに 401 を返します。**認証の担当は Cloudflare に移ったので、こちらは空にする**、
が正しい状態です。

```bash
sed -i 's/^MCP_BEARER=.*/MCP_BEARER=/' .env && docker compose up -d mcp
```

スクリプトは `MCP_BEARER` が入ったままだと `apply` を拒否します。
(後から「なぜか繋がらない」と悩むより、ここで止まるほうが安いので)

## 実行する

```bash
# 1. 設定ファイルの雛形を作る(deploy.local/cfzt.env、git 管理外)
python3 scripts/setup_cfzt.py init

# 2. 4 つの値を埋める
$EDITOR deploy.local/cfzt.env
#    CF_API_TOKEN   準備 3 で作ったトークン
#    CF_ACCOUNT_ID  空でよい(自動検出)
#    MCP_HOSTNAME   使いたいホスト名。例: mcp.yourdomain.com
#    ACCESS_EMAIL   Cloudflare にログインする自分のメールアドレス

# 3. 検証だけする(まだ何も作らない)
python3 scripts/setup_cfzt.py check

# 4. Cloudflare 側を作る(ポリシー → アプリ → OAuth → トンネル → DNS)
python3 scripts/setup_cfzt.py apply

# 5. このマシンで cloudflared を入れて起動する(sudo が必要)
python3 scripts/setup_cfzt.py connector

# 6. 検証(スマホから繋がるかどうかが、ここでほぼ決まる)
python3 scripts/setup_cfzt.py verify
```

`check` は何も変更せず、トークンの有効性・アカウント・ゾーン・権限を確認して、
「これから何を作るか」を表示します。先に読んでください。

### 順番には意味があります

Cloudflare の公式ガイドは**トンネルを先に**作らせます。その手順どおりにやると、
**ポリシーを作るまでの数分間、あなたのホスト名は誰でもアクセスできる状態**になります。
その間に URL を知られれば、任意のコードを実行されます。

このスクリプトは逆で、**門(Access アプリとポリシー)を先に作り、扉(DNS とトンネル)を
後に作ります**。DNS が引けるようになった時点で、既に認証が効いています。
無防備な時間帯が存在しません。

`apply` と `connector` が別コマンドなのもそのためです。**まとめないでください。**

### 許可リストはメールアドレス 1 つ

`ACCESS_EMAIL` がこのシステムのセキュリティの全部です。ここに書いた人は、
あなたのマシンで**任意のコードを実行できます**(サンドボックスはありません。
[SECURITY.md](../SECURITY.md))。自分のアドレス 1 つにしてください。
グループや「認証済みの全ユーザー」は選ばないでください。

## Claude に登録する

`verify` が `VERIFY OK` を出したら、

Claude → **設定 → コネクタ → カスタムコネクタを追加** →
`https://<あなたのホスト名>/mcp` を入力 → Cloudflare のログイン画面が出るので
ログイン → 完了です。スマホでも同じ URL を使います。

**別のマシンの Claude Code** から使いたい場合は、追加で:

```bash
python3 scripts/setup_cfzt.py service-token
```

サービストークンを作り、秘密情報を `secrets/claude-code.env`(600)に保存し、
`claude mcp add` のコマンドを表示します。**シークレットは作成時の 1 回しか取得できません**
のでパスワードマネージャなどに控えてください。

なお、この方式が使うヘッダ名(`CF-Access-Client-Id` など)は Claude アプリでは
送信できません。スマホ・Web からは OAuth の経路(上の手順)を使います。

## うまくいかないとき

**`verify` の 4/6 で `WWW-Authenticate` が無いと言われる** — 一番重要な項目です。
Claude アプリは「401 と一緒に返ってくる `WWW-Authenticate` ヘッダ」を読んで
認証方法を判断します。これが無いと、アプリ側で何をしても繋がりません。
厄介なのは **Claude Code からは繋がってしまう**ことで、「サーバーは正常なのに
アプリだけダメ」という状況になります。数分待って `verify` をやり直し、
それでも駄目ならダッシュボードで Access → 該当アプリ → Managed OAuth を確認して
`apply` をやり直してください。

**`verify` の 3/6 で「200 — OPEN」と出た** — 認証なしで応答しています。
今すぐ `sudo systemctl stop cloudflared` で止めて、`apply` をやり直し、
ポリシーが付いているか確認してください。

**急に全部 302 になった** — 誰か(あるいは別のツール)が Access アプリを作り直したか
編集して、ポリシーか OAuth 設定が消えた可能性が高いです。Cloudflare の API は
`PUT` で全体を置き換えるので、送り忘れた項目は消えます。`apply` をやり直せば直ります。

**`check` で権限が NOT readable と出る** — トークンにその権限がありません。
表に書いた名前がそのまま Cloudflare の UI の項目名なので、探して追加するか、
作り直してください。

**サービストークンがログイン画面にリダイレクトされる** — 通常の Allow ポリシーに
サービストークンを入れても**何も起きません**(無効です)。`non_identity` という種別の
ポリシーが必要で、`service-token` コマンドがそれを作ります。もう一度実行してください。

**`cloudflared` は動いているのに 502** — トンネルの転送先ポートに何もいません。
`cfzt.env` の `MCP_LOCAL_PORT` と `.env` の `MCP_HOST_PORT` が一致しているか、
`curl http://127.0.0.1:7130/health` が応答するかを確認してください。

## 運用するうえで

- **Cloudflare 側の設定を変えたら必ず `verify` を実行してください。** この仕組みの
  故障は「エラーが出る」ではなく「誰何しなくなる」という形で現れます。
- **やり直したいとき**は `python3 scripts/setup_cfzt.py destroy --yes` で
  アプリ・トンネル・DNS レコードを削除できます。ホスト側は
  `sudo cloudflared service uninstall` です。
- **JupyterLab(7131番)は意図的に公開していません。** 見たいときは SSH で
  `ssh -L 7131:127.0.0.1:7131 ユーザ@ホスト` してブラウザで開きます。Access 経由で
  公開することもでき、スマホからだと快適ですが、**2 つめのコード実行口**になるので、
  やるなら専用のアプリケーションを作り、メールのみのポリシーにしてください
  (サービストークンは絶対に付けないこと)。
- **通信は Cloudflare で復号されます。** この構成の性質上そうなります。
- **会社のデータを置かないでください。** これは個人用の実行環境です。

## Claude Code に任せる場合

準備 1〜3(ブラウザ作業)を済ませたら、あとはこう言えば進みます。

```
docs/deploy/cloudflare-zero-trust.md の手順で、このホストを Cloudflare Zero Trust
経由で公開して。ブラウザ操作が必要なところと、cfzt.env に入れる値が必要なところで
止めて聞いて。
```

手順書には、エージェントがやってはいけないこと(許可リストを広げない、秘密情報を
コミットしない、`apply` の前に `connector` を実行しない)が明記してあります。
