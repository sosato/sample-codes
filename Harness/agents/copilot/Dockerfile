# B) GitHub Copilot SDK 版のコンテナデプロイ用 Dockerfile。
#
# ZIP (--deploy-mode code) デプロイで Copilot CLI ランタイムの取得に失敗する場合
# (R2: egress 制限 / 読み取り専用 FS) のフォールバック。
# ランタイムバイナリを **ビルド時に** 焼き込み、実行時ダウンロードを完全に無効化する。
#
# ビルド:
#   docker build -t copilot-3step:local .
# ローカル実行:
#   docker run --rm -p 8088:8088 -e FOUNDRY_PROJECT_ENDPOINT=... copilot-3step:local

FROM mcr.microsoft.com/devcontainers/python:3.13-bookworm

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# --- Copilot CLI ランタイムをビルド時に取得して固定パスへ配置 (R2 対策) -------
ENV COPILOT_CLI_EXTRACT_DIR=/opt/copilot-runtime
RUN mkdir -p "$COPILOT_CLI_EXTRACT_DIR" \
    && python -m copilot download-runtime \
    && find "$COPILOT_CLI_EXTRACT_DIR" -maxdepth 2 -type f -name 'copilot' -exec chmod +x {} \;
ENV COPILOT_CLI_PATH=/opt/copilot-runtime/copilot \
    COPILOT_SKIP_CLI_DOWNLOAD=1

# CLI の状態書き込み先。Hosted Agent では $HOME がターン間で永続化される。
ENV COPILOT_HOME=/home/vscode/.copilot
RUN mkdir -p "$COPILOT_HOME" && chmod -R 0777 "$COPILOT_HOME"

COPY . .

EXPOSE 8088
CMD ["python", "main.py"]
