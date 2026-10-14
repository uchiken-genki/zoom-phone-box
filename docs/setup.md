# セットアップ手順

この手順は、公開用に認証情報や PC 固有のパスを除いた状態から始めるためのものです。値はすべて、自分の Zoom アカウントと Box アカウントで発行したものを使います。

## 1. 用意するもの

- Python 3.12 以上が PATH に入っていること
- ngrok のアカウントと実行ファイル。PATH に `ngrok` があるか、環境変数 `NGROK_EXE` に実行ファイルのフルパスを設定する
- Zoom アカウントの管理者権限（Server-to-Server OAuth アプリを作るため）
- Box の管理者権限（Platform アプリの承認と Enterprise ID の確認のため）

## 2. アプリを起動できる状態にする

```powershell
cd zoom-phone-box
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env
```

`.env` の次の 3 つだけ、Zoom App Marketplace の Server-to-Server OAuth アプリからコピーします。

- `ZOOM_ACCOUNT_ID`
- `ZOOM_CLIENT_ID`
- `ZOOM_CLIENT_SECRET`

他の項目は空のままで構いません。Box と Webhook の秘密情報は、起動後の設定画面で保存します。`.env` は Git に含めません。

起動は次です。ngrok の公開 URL を `data/` に書いてからアプリを起動します。

```powershell
.\start.ps1
```

ブラウザで http://127.0.0.1:8787/ を開きます。待受ポートを変えるときは、起動前に環境変数 `APP_PORT` を設定します。初期値は `8787` です。

## 3. Zoom 側

Zoom App Marketplace の Develop → Manage から、この連携用の Server-to-Server OAuth アプリを開きます。

1. Scopes に次を追加する。
   - `phone:read:call_recording:admin`
   - `phone:read:recording_transcript:admin`
   - `phone:read:ai_call_summary:admin`
   - `phone:read:fax_log:admin`
   - `meeting:read:summary:admin`
2. Feature に表示される Secret Token をコピーする。
3. アプリの「設定」画面の「Zoom Webhook」に Secret Token を保存する。Validate より先に保存する。
4. 同じ Feature で Event Subscriptions をオンにし、方法は Webhook を選ぶ。
5. Event notification endpoint URL に、ホーム画面の Webhook URL を入れる。形は `https://（ngrok のホスト）/zoom/webhook` です。
6. Validate が成功したら、Add Events で次の 6 件を追加する。受信対象は All users in the account にして保存する。
   - `phone.recording_completed`
   - `phone.recording_transcript_completed`
   - `phone.ai_call_summary_changed`
   - `phone.fax_received`
   - `meeting.summary_completed`
   - `meeting.summary_updated`
7. Activation でアプリを有効化する。
8. ホームの「Zoom を確認」で、トークンが取得できることを確かめる。

フリー版の ngrok は起動のたびにホスト名が変わります。変わったら、Zoom の Endpoint URL を新しい Webhook URL に差し替え、もう一度 Validate します。アプリ側の URL は自動では Zoom に登録されません。

## 4. Box 側

Developer Console で Platform アプリ（Client Credentials Grant）を作ります。

1. 構成のアプリアクセスレベルは「アプリアクセスのみ」にする。
2. アプリケーションスコープの「コンテンツ操作」で、ファイルとフォルダの読み書きを有効にして保存する。
3. Client ID と Client Secret をコピーする。
4. Enterprise ID は、Developer Console 右上のアカウントアイコンからコピーする。
5. アプリの「設定」画面に 3 つを入力して「Box の設定を保存」を押す。保存先の親フォルダ ID は、サービスアカウントのルートなら `0` のままにする。
6. 「サービスアカウントで接続」を押す。
7. 管理コンソールで、このアプリの承認が有効になっていることを確かめる。スコープやアクセスを変えたあとは、再承認が必要です。
8. ホームの「フォルダを作成」を押す。サービスアカウントの中に `Zoom` と、その下の `録音`、`文字起こし`、`議事録`、`FAX` ができます。あわせて社内共有リンクが作られ、ホームの「保存先」に表示されます。

アプリアクセスのみのサービスアカウントが作ったフォルダは、人間のユーザーの「すべてのファイル」にはそのまま出ません。出したいユーザーは、設定画面の「Box の共有ユーザー」に Box のログインメールアドレスを入れ、「保存して共有」を押します。権限は閲覧者または編集者です。状態が「表示中」になれば、そのユーザーの「すべてのファイル」に `Zoom` が出ます。「招待の承認待ち」のときは、Box から届く招待メールで承認します。

## 5. 受信 FAX

ホームの「FAX を確認」を開きます。

- 受信済みで未保存の FAX を、設定した間隔で Box の `FAX` フォルダへ転送します。初期値は 30 秒です。
- 「更新」は、間隔を待たずにその場で確認して転送します。
- 「間隔を保存」は 10 秒から 3600 秒まで指定できます。次の確認から反映され、アプリの再起動は不要です。
- Zoom から届く受信メールは、このアプリの保存完了ではありません。アプリへ届くのは Webhook か、この定期確認です。

## 6. 動作確認

1. ホームの「Zoom を確認」が成功する。
2. ホームの「Box を確認」でサービスアカウント名が表示される。
3. 「フォルダを作成」のあと、保存先に 4 つのフォルダが出る。
4. Zoom の Validate が成功する。
5. テスト通話の録音、または受信 FAX のあと、Box の対応するフォルダにファイルが増える。
6. 確認画面で、保存済みの FAX が「保存済み」になり、もう一度「更新」しても再転送されない。

失敗した Webhook は、ホームの「失敗分を再実行」でやり直せます。

## 7. コンテナで動かすとき

この手順の標準は、PC 上で `start.ps1` を実行する方法です。コンテナで動かす場合は、このディレクトリをマウントし、作業ディレクトリをアプリのルートにして `python app.py` を起動します。`APP_HOST` は `0.0.0.0`、`APP_PORT` は待受ポートです。

アプリは起動時と 15 秒ごとに ngrok のローカル API（`http://127.0.0.1:4040/api/tunnels`）を読み、公開 URL を更新します。コンテナからその API が見えないときは、環境変数 `NGROK_API_URL` に、同じ JSON を返す URL を設定します。ホストの待受ポートを PC のブラウザから開く方法は、使う環境に合わせて別途用意します。

認証情報はイメージに焼き込まず、`.env` と `data/` をマウントするか、起動後の設定画面で保存します。

## 8. 公開してはいけないもの

次のファイルにはトークン、秘密情報、共有したメールアドレス、処理した FAX の識別子が入ります。リポジトリへ入れないでください。

- `.env`
- `data/box_settings.json`
- `data/box_token.json`
- `data/box_share.json`
- `data/zoom_webhook_secret.txt`
- `data/public_base_url.txt`
- `data/ngrok.json`
- `data/state.json`
- `data/folders.json`
- `data/ui_token`
- `data/inbox/`
