#!/usr/bin/env bash
# Sets up the bridge to run natively (no containers): creates a virtualenv,
# installs dependencies, and generates an initial config.yaml with fresh
# random AppService tokens.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_ROOT"

PYTHON_BIN="${PYTHON_BIN:-python3}"
VENV_DIR="$PROJECT_ROOT/.venv"

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
    echo "error: $PYTHON_BIN not found on PATH" >&2
    exit 1
fi

echo "==> Creating virtualenv at $VENV_DIR"
"$PYTHON_BIN" -m venv "$VENV_DIR"

echo "==> Installing dependencies"
"$VENV_DIR/bin/pip" install --upgrade pip >/dev/null
"$VENV_DIR/bin/pip" install -r requirements.txt

mkdir -p data

if [ -f config.yaml ]; then
    echo "==> config.yaml already exists, leaving it untouched"
else
    echo "==> Generating config.yaml from config.example.yaml"
    cp config.example.yaml config.yaml
    chmod 600 config.yaml

    AS_TOKEN="as_$("$VENV_DIR/bin/python" -c 'import secrets; print(secrets.token_hex(32))')"
    HS_TOKEN="hs_$("$VENV_DIR/bin/python" -c 'import secrets; print(secrets.token_hex(32))')"

    # Portable in-place sed (GNU sed vs BSD/macOS sed take -i differently).
    sed_inplace() {
        if sed --version >/dev/null 2>&1; then
            sed -i "$1" "$2"
        else
            sed -i '' "$1" "$2"
        fi
    }
    sed_inplace "s|as_token: \"as_replace_me\"|as_token: \"${AS_TOKEN}\"|" config.yaml
    sed_inplace "s|hs_token: \"hs_replace_me\"|hs_token: \"${HS_TOKEN}\"|" config.yaml

    echo "==> Generated config.yaml with fresh random AppService tokens."
fi

cat <<EOF2

==> Done. Next steps (INSTALL.md has the details):
    1. Set up DNS, a wildcard TLS certificate and nginx for *.<your domain>
       and the PDS hostname (deploy/nginx.conf.example).
    2. Edit config.yaml -- at minimum set:
         bridge.domain, bridge.public_base_url
         synapse.base_url, synapse.server_name
    3. Generate the AppService registration file:
         $VENV_DIR/bin/python -m bridge.appservice config.yaml appservice-registration.yaml
       Add the resulting path to Synapse's homeserver.yaml under
       app_service_config_files, then restart Synapse.
    4. Run the bridge:
         $VENV_DIR/bin/python main.py
       (or install deploy/matrix-appservice-atproto.service to run it under systemd)
EOF2
