# Box metadata exporter

Box の Metadata Query API で、指定フォルダ配下にあるメタデータ条件に一致する
ファイルを検索し、1 ファイルにつき 1 JSON を出力します。

出力 JSON には次が含まれます。

- Metadata Query API の一致結果
- Box のファイル基本情報
- ファイルに適用されている全メタデータインスタンス

## 前提

- Python 3.10 以上
- Box Custom App で Metadata Query API とファイル情報を読める権限
- 対象フォルダをアプリの Service Account または認証ユーザーと共有
- 既に対象ファイルへ適用済みのメタデータテンプレート
- 対象テンプレートのクエリ可能なフィールド

Metadata Query は検索条件に一致し、テンプレートが適用されたファイルだけを返します。
フォルダ配下の全ファイルを対象にする場合は、メタデータ条件ではなくフォルダ一覧
API を使う必要があります。

## セットアップ

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
py -m pip install -r requirements.txt
```

Box の Developer Token、CCG、JWT などで取得したアクセストークンを環境変数へ
設定します。トークンはソースコードや JSON 出力へ保存しません。

```powershell
$env:BOX_ACCESS_TOKEN = "<BOX_ACCESS_TOKEN>"
```

## 実行

```powershell
py .\box_metadata_export.py `
  --folder-id "1234567890" `
  --from-template "enterprise_12345678.contract" `
  --query "status = :status" `
  --param "status=approved" `
  --output-dir ".\metadata-export"
```

複数条件の場合:

```powershell
py .\box_metadata_export.py `
  --folder-id "1234567890" `
  --from-template "enterprise_12345678.contract" `
  --query "status = :status AND department = :department" `
  --param "status=approved" `
  --param "department=legal"
```

`--param` の値は JSON として解釈可能なら、その型を使います。文字列をそのまま
渡す場合は `approved` のように記述します。数値は `--param "year=2026"`、
配列などは `--param 'categories=[\"A\",\"B\"]'` と指定できます。

成功すると、出力先にファイル ID を名前とする JSON が 1 件ずつ生成されます。

```text
metadata-export/
  12345.json
  67890.json
```

## 参考

- https://developer.box.com/guides/search/quick-start/metadata-query-api
- https://developer.box.com/reference/post-metadata-queries-execute-read/
- https://developer.box.com/reference/get-files-id/
- https://developer.box.com/reference/get-files-id-metadata/
