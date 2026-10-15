#!/usr/bin/env bash

set -Eeuo pipefail
IFS=$'\n\t'

APP_DIR="${APP_DIR:-/opt/service-api}"
REPOSITORY_URL="${REPOSITORY_URL:-https://github.com/1nteractme/Service-API.git}"
BRANCH="${BRANCH:-main}"
DOMAIN="${DOMAIN:-}"
SERVICE_BASE_URL="${SERVICE_BASE_URL:-}"
SERVICE_SESSION_ENCRYPTION_KEY="${SERVICE_SESSION_ENCRYPTION_KEY:-}"
ENABLE_TLS="${ENABLE_TLS:-0}"
CERTBOT_EMAIL="${CERTBOT_EMAIL:-}"

log() {
    printf '\n[%s] %s\n' "$(date '+%H:%M:%S')" "$*"
}

fail() {
    printf '\nОшибка: %s\n' "$*" >&2
    exit 1
}

require_root() {
    [[ "${EUID}" -eq 0 ]] || fail "запустите скрипт от root"
}

validate_input() {
    [[ -n "$SERVICE_BASE_URL" ]] || fail \
        "укажите SERVICE_BASE_URL, например SERVICE_BASE_URL=https://external.example"

    [[ "$SERVICE_BASE_URL" =~ ^https?://[^[:space:]]+$ ]] || fail \
        "SERVICE_BASE_URL должен начинаться с http:// или https://"

    if [[ -n "$DOMAIN" && ! "$DOMAIN" =~ ^[A-Za-z0-9.-]+$ ]]; then
        fail "DOMAIN содержит недопустимые символы"
    fi

    if [[ "$ENABLE_TLS" == "1" ]]; then
        [[ -n "$DOMAIN" ]] || fail "для ENABLE_TLS=1 укажите DOMAIN"
        [[ -n "$CERTBOT_EMAIL" ]] || fail "для ENABLE_TLS=1 укажите CERTBOT_EMAIL"
    fi
}

install_packages() {
    log "Устанавливаем системные пакеты"
    export DEBIAN_FRONTEND=noninteractive

    apt-get update
    apt-get install -y \
        ca-certificates \
        curl \
        docker.io \
        docker-compose-v2 \
        git \
        nginx \
        openssl \
        python3 \
        ufw

    if [[ "$ENABLE_TLS" == "1" ]]; then
        apt-get install -y certbot python3-certbot-nginx
    fi

    systemctl enable --now docker
    systemctl enable --now nginx
}

prepare_repository() {
    log "Подготавливаем репозиторий: $APP_DIR"

    if [[ -d "$APP_DIR/.git" ]]; then
        git -C "$APP_DIR" fetch --prune origin
        git -C "$APP_DIR" checkout "$BRANCH"
        git -C "$APP_DIR" pull --ff-only origin "$BRANCH"
    elif [[ -e "$APP_DIR" ]]; then
        fail "$APP_DIR уже существует, но это не Git-репозиторий"
    else
        mkdir -p "$(dirname "$APP_DIR")"
        git clone --branch "$BRANCH" --single-branch "$REPOSITORY_URL" "$APP_DIR"
    fi

    mkdir -p "$APP_DIR/data"
}

generate_fernet_key() {
    python3 -c \
        'import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())'
}

prepare_environment() {
    log "Проверяем production-конфигурацию"
    local env_file="$APP_DIR/.env"

    umask 077

    if [[ ! -f "$env_file" ]]; then
        cp "$APP_DIR/.env.example" "$env_file"

        local session_key="$SERVICE_SESSION_ENCRYPTION_KEY"
        if [[ -z "$session_key" ]]; then
            session_key="$(generate_fernet_key)"
            log "Сгенерирован новый Fernet-ключ. Сохраните его в безопасном месте."
        fi

        sed -i \
            -e 's|^SERVICE_DB_PATH=.*|SERVICE_DB_PATH=/app/data/service.sqlite3|' \
            -e 's|^SERVICE_ENV=.*|SERVICE_ENV=production|' \
            -e "s|^SERVICE_BASE_URL=.*|SERVICE_BASE_URL=$SERVICE_BASE_URL|" \
            -e "s|^SERVICE_SESSION_ENCRYPTION_KEY=.*|SERVICE_SESSION_ENCRYPTION_KEY=$session_key|" \
            "$env_file"
    fi

    chmod 600 "$env_file"

    grep -q '^SERVICE_BASE_URL=https\?://' "$env_file" || \
        fail "в $env_file отсутствует корректный SERVICE_BASE_URL"

    if grep -q '^SERVICE_BASE_URL=https://service\.example\.com$' "$env_file"; then
        fail "в $env_file остался шаблонный SERVICE_BASE_URL"
    fi

    if grep -q '^SERVICE_SESSION_ENCRYPTION_KEY=replace-with' "$env_file"; then
        fail "в $env_file остался шаблонный Fernet-ключ"
    fi
}

configure_nginx() {
    log "Настраиваем Nginx"
    local server_name="$DOMAIN"
    local nginx_file="/etc/nginx/sites-available/service-api"

    if [[ -z "$server_name" ]]; then
        server_name="$(hostname -I | awk '{print $1}')"
    fi

    cat > "$nginx_file" <<EOF
server {
    listen 80;
    server_name $server_name;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 360s;
    }
}
EOF

    ln -sfn "$nginx_file" /etc/nginx/sites-enabled/service-api
    nginx -t
    systemctl reload nginx
}

configure_firewall() {
    log "Настраиваем firewall"
    ufw allow OpenSSH
    ufw allow 'Nginx Full'
    ufw --force enable
}

start_application() {
    log "Собираем и запускаем API"
    cd "$APP_DIR"
    docker compose up -d --build
    docker compose ps

    log "Проверяем health endpoint"
    if ! curl --fail --silent --show-error --retry 10 --retry-delay 2 \
        http://127.0.0.1:8000/health; then
        printf '\nПоследние логи контейнера:\n' >&2
        docker compose logs --tail=100 api >&2
        exit 1
    fi
    printf '\n'
}

configure_tls() {
    [[ "$ENABLE_TLS" == "1" ]] || return 0

    log "Выпускаем TLS-сертификат для $DOMAIN"
    certbot --nginx \
        --non-interactive \
        --agree-tos \
        --no-eff-email \
        --redirect \
        --email "$CERTBOT_EMAIL" \
        -d "$DOMAIN"
}

show_result() {
    local server_ip
    server_ip="$(hostname -I | awk '{print $1}')"

    log "Развёртывание завершено"
    if [[ "$ENABLE_TLS" == "1" ]]; then
        printf 'API: https://%s\n' "$DOMAIN"
        printf 'Проверка: curl -fsS https://%s/health\n' "$DOMAIN"
    elif [[ -n "$DOMAIN" ]]; then
        printf 'API: http://%s\n' "$DOMAIN"
        printf 'Проверка: curl -fsS http://%s/health\n' "$DOMAIN"
    else
        printf 'API: http://%s\n' "$server_ip"
        printf 'Проверка: curl -fsS http://%s/health\n' "$server_ip"
    fi

    printf 'Логи: cd %s && docker compose logs -f api\n' "$APP_DIR"
}

main() {
    require_root
    validate_input
    install_packages
    prepare_repository
    prepare_environment
    configure_nginx
    configure_firewall
    start_application
    configure_tls
    show_result
}

main "$@"
