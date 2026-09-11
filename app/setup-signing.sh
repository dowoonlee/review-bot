#!/bin/bash
# 로컬 코드서명 신원을 만든다 (자체 서명, 10년).
#
# 서명이 붙으면 앱의 신원이 빌드마다 바뀌지 않아, macOS 가 준 권한(파일 접근 등)이
# 다시 빌드해도 유지된다. 다만 **알림 센터는 이것으로 열리지 않는다** — Apple 이
# 발급한 인증서(무료 Apple Development 포함)여야 한다. README 의 "알림" 절 참고.
set -euo pipefail

NAME="review-bot-local"
KEYCHAIN="$HOME/Library/Keychains/login.keychain-db"

if security find-identity -v -p codesigning | grep -q "$NAME"; then
    echo "이미 있습니다: $NAME"
    exit 0
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

cat > "$TMP/cs.cnf" <<'CNF'
[req]
distinguished_name = dn
x509_extensions    = v3
prompt             = no
[dn]
CN = review-bot-local
O  = review-bot (local development)
[v3]
basicConstraints     = critical,CA:false
keyUsage             = critical,digitalSignature
extendedKeyUsage     = critical,codeSigning
subjectKeyIdentifier = hash
CNF

echo "▸ 키와 인증서 생성"
openssl req -x509 -newkey rsa:2048 -sha256 -days 3650 -nodes \
    -keyout "$TMP/key.pem" -out "$TMP/cert.pem" -config "$TMP/cs.cnf" 2>/dev/null

# OpenSSL 3 기본 알고리즘은 Apple Security 가 읽지 못한다 — 구식 PBE 로 내보낸다.
openssl pkcs12 -export -out "$TMP/id.p12" -inkey "$TMP/key.pem" -in "$TMP/cert.pem" \
    -name "$NAME" -passout pass:temp \
    -macalg sha1 -keypbe PBE-SHA1-3DES -certpbe PBE-SHA1-3DES 2>/dev/null

echo "▸ 키체인에 넣기 (키는 codesign 만 쓸 수 있게 제한)"
security import "$TMP/id.p12" -k "$KEYCHAIN" -P temp -T /usr/bin/codesign

echo "▸ 코드서명 용도로 신뢰 (암호를 물어볼 수 있습니다)"
security add-trusted-cert -r trustRoot -p codeSign -k "$KEYCHAIN" "$TMP/cert.pem"

security find-identity -v -p codesigning | grep "$NAME" || {
    echo "❌ 신원이 잡히지 않습니다"; exit 1; }
echo "▸ 완료 — 이제 build.sh 가 이 신원으로 서명합니다"
echo "  되돌리기: security delete-certificate -c $NAME"
