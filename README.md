# Zoom Phone → Box

Zoom Phone の録音、文字起こし、AI 要約、Zoom Meetings の AI コンパニオン議事録、受信 FAX を Box に保存するアプリです。

Zoom はこれらのファイルを Box の指定フォルダへ直接書き込みません。このアプリが Zoom からファイルを受け取り、Box API でアップロードします。

保存先は次のフォルダです。親フォルダは設定画面の「保存先の親フォルダ ID」（初期値 `0` はサービスアカウントのルート）です。

```
Zoom/
  録音/
  文字起こし/
  議事録/
  FAX/
```

電話の AI 要約と会議の議事録は、どちらも「議事録」に入ります。ファイル名の先頭付近が `電話` か `会議` かで区別します。

初回の設定手順は [docs/setup.md](docs/setup.md) です。

## 動作

- Zoom の Webhook（`/zoom/webhook`）で、録音、文字起こし、AI 要約、議事録、受信 FAX を受け取ります。
- 受信 FAX は、Webhook が届かなくても Zoom の FAX 履歴を定期確認して Box へ転送します。間隔の初期値は 30 秒で、確認画面から 10 秒から 3600 秒の間で変更できます。
- Box とは Client Credentials Grant（サービスアカウント）で接続します。ブラウザで Box にログインする OAuth は使いません。
- 同じ資料が再送された場合は保存済みとしてスキップします。同名ファイルは Box 上の新しいバージョンとして更新します。20MB 以上の録音は分割アップロードします。

## 必要なもの

- Python 3.12 以上
- [ngrok](https://ngrok.com/)（Zoom からこの PC へ Webhook を届けるため）
- Zoom の Server-to-Server OAuth アプリ
- Box の Platform アプリ（Client Credentials Grant、アプリアクセスのみ）

## 起動

```powershell
copy .env.example .env
# .env に Zoom の Account ID / Client ID / Client Secret を書く
.\start.ps1
```

ブラウザで http://127.0.0.1:8787/ を開きます。Box の Client Secret、Enterprise ID、Zoom の Webhook Secret Token は、起動後の「設定」画面で入力します。これらは `data/` に保存され、リポジトリには含めません。

ngrok だけ起動し直すときは `.\start-ngrok.ps1` です。実行ファイルは PATH 上の `ngrok`、または環境変数 `NGROK_EXE` のパスを使います。

## 画面

| パス | 内容 |
|---|---|
| `/` | 接続状態、保存先、Zoom と Box の設定手順 |
| `/settings` | Box の認証情報、共有ユーザー、Webhook Secret Token |
| `/check` | FAX 履歴、自動転送の間隔、「更新」ボタン |

## テスト

```powershell
python -m unittest tests.test_sync
```

## リポジトリに含めないもの

`.gitignore` で次を除外しています。

- `.env`（Zoom の認証情報）
- `data/`（Box のトークン、Client Secret、Webhook Secret Token、共有ユーザー、処理済みの FAX、ngrok の公開 URL）

これらをコミットしないでください。
