#!/usr/bin/env bash
# Online bootstrap: ask first, update this bootstrap when a newer release exists,
# then fetch only the selected language package from this version's GitHub Release.
set -Eeuo pipefail
REPOSITORY="raimondarias/pterodactyl-installer"
VERSION="1.3.0"
original_args=("$@")
self_update=1
if [[ -n "${PTERO_NO_SELF_UPDATE:-}" ]]; then self_update=0; fi
language=""
plain_mode=0
animations=1
preview_mode=0
bootstrap_active=0
download_pid=""
WORK_DIR=""
forwarded=()
restore_bootstrap() {
    if [[ "$bootstrap_active" == 1 ]]; then
        printf '\033[?1000l\033[?1006l\033[0m\033[?25h\033[?1049l'
        bootstrap_active=0
    fi
}
cleanup() {
    restore_bootstrap
    if [[ -n "$download_pid" ]]; then
        kill "$download_pid" 2>/dev/null || true
        printf '\r\033[K\033[?25h'
    fi
    if [[ -n "$WORK_DIR" ]]; then rm -rf -- "$WORK_DIR"; fi
    # A self-updated bootstrap removes the private directory it was started from.
    if [[ -n "${PTERO_UPDATE_DIR:-}" && "$PTERO_UPDATE_DIR" == */ptero-update.* && -d "$PTERO_UPDATE_DIR" ]]; then
        rm -rf -- "$PTERO_UPDATE_DIR"
    fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP
while (($#)); do
    case "$1" in
        --language)
            [[ $# -ge 2 ]] || { printf '%s\n' 'Missing --language value.' >&2; exit 2; }
            language="$2"; shift 2 ;;
        --plain) plain_mode=1; forwarded+=("$1"); shift ;;
        --no-animation) animations=0; forwarded+=("$1"); shift ;;
        --preview) preview_mode=1; forwarded+=("$1"); shift ;;
        --no-self-update) self_update=0; shift ;;
        --version) printf 'Pterodactyl Installer %s\n' "$VERSION"; exit 0 ;;
        --help|-h)
            printf '%s\n' 'Pterodactyl Installer — online language bootstrap' \
                'Usage: bash install.sh [--language en|es|pt-BR] [--preview] [--plain] [--no-animation] [--no-self-update] [action]' \
                'Without --language, the first question selects a language before downloading runtime code.' \
                'A newer published release replaces this bootstrap automatically; --no-self-update keeps this version.' \
                'Offline source checkout: bash scripts/run_local.sh [options]'
            exit 0 ;;
        *) forwarded+=("$1"); shift ;;
    esac
done
choose_language_tui() {
    local rows columns selected=1 previous="" key sequence frame=0 left index row label style reset="" mouse part
    read -r rows columns < <(stty size)
    if (( rows < 18 || columns < 60 )); then return 1; fi
    bootstrap_active=1
    printf '\033[?1049h\033[?25l\033[?1000h\033[?1006h\033[2J'
    while true; do
        read -r rows columns < <(stty size)
        left=$(( (columns - 54) / 2 + 1 ))
        if (( left < 1 )); then left=1; fi
        if [[ "$previous" != "$selected:$rows:$columns" ]]; then
            printf '\033[2J'
            if [[ -z "${NO_COLOR+x}" ]]; then printf '\033[1;36m'; fi
            printf '\033[2;%sHPterodactyl Installer  %s' "$left" "$VERSION"
            printf '\033[0m\033[4;%sHWhich language would you like to use?' "$left"
            if (( rows < 18 || columns < 60 )); then
                printf '\033[6;1HEnlarge the terminal (60 columns, 18 rows). Esc cancels.'
            else
                printf '\033[6;%sHChoose your setup language before downloading.' "$left"
                for index in 1 2 3; do
                    case "$index" in 1) label='English' ;; 2) label='Español' ;; 3) label='Português (Brasil)' ;; esac
                    row=$(( 7 + index * 2 ))
                    style=""; reset=""
                    if [[ "$selected" == "$index" ]]; then
                        style=$'\033[7m'; reset=$'\033[0m'
                    fi
                    printf '\033[%s;%sH%s  %s. %-30s  %s' "$row" "$left" "$style" "$index" "$label" "$reset"
                done
                printf '\033[%s;%sHDownloads only the selected language package.' "$((rows - 3))" "$left"
                printf '\033[%s;%sH↑↓ / 1–3 Select · Click / Enter Continue · Esc Cancel' "$((rows - 1))" "$left"
            fi
            previous="$selected:$rows:$columns"
        fi
        if (( animations && rows >= 18 && columns >= 60 )); then
            case "$((frame % 4))" in 0) label='○' ;; 1) label='◔' ;; 2) label='●' ;; 3) label='◕' ;; esac
            printf '\033[2;%sH%s' "$((left + 50))" "$label"
            frame=$((frame + 1))
        fi
        key=""
        if ! IFS= read -rsn1 -t 0.12 key; then continue; fi
        case "$key" in
            $'\033')
                sequence=""
                IFS= read -rsn2 -t 0.15 sequence || true
                case "$sequence" in
                    '[A'|'OA') selected=$(( (selected + 1) % 3 + 1 )) ;;
                    '[B'|'OB') selected=$(( selected % 3 + 1 )) ;;
                    '[<')
                        mouse=""
                        while (( ${#mouse} < 32 )); do
                            part=""
                            IFS= read -rsn1 -t 0.15 part || break
                            mouse+="$part"
                            [[ "$part" == M || "$part" == m ]] && break
                        done
                        if [[ "$mouse" =~ ^0\;([0-9]+)\;([0-9]+)m$ ]] && (( rows >= 18 && columns >= 60 )); then
                            if (( BASH_REMATCH[1] >= left && BASH_REMATCH[1] < left + 38 )); then
                                case "${BASH_REMATCH[2]}" in 9) language='en' ;; 11) language='es' ;; 13) language='pt-BR' ;; *) continue ;; esac
                                restore_bootstrap
                                return 0
                            fi
                        fi ;;
                    *) exit 130 ;;
                esac ;;
            1|2|3) selected="$key" ;;
            q|Q) exit 130 ;;
            '')
                if (( rows < 18 || columns < 60 )); then continue; fi
                case "$selected" in 1) language='en' ;; 2) language='es' ;; 3) language='pt-BR' ;; esac
                restore_bootstrap
                return 0 ;;
        esac
    done
}
if [[ -z "$language" ]]; then
    if [[ "$plain_mode" == 0 && -t 0 && -t 1 && "${TERM:-dumb}" != dumb ]]; then
        choose_language_tui || true
    fi
fi
if [[ -z "$language" ]]; then
    printf '\n  Pterodactyl Installer\n\n'
    printf '%s\n' '  Which language would you like to use?' '' \
        '  1. English' '  2. Español' '  3. Português (Brasil)' ''
    while true; do
        printf '  Select [1]: '
        if ! IFS= read -r answer; then
            printf '\n%s\n' 'Input closed. No language package was downloaded.' >&2
            exit 130
        fi
        case "$answer" in
            1|'') language="en"; break ;;
            2) language="es"; break ;;
            3) language="pt-BR"; break ;;
            *) printf '%s\n' '  Invalid selection. Enter 1, 2 or 3.' ;;
        esac
    done
fi
case "$language" in en|es|pt-BR) ;; *) printf '%s\n' 'Invalid language. Use en, es or pt-BR.' >&2; exit 2 ;; esac

message() {
    case "$language:$1" in
        en:runtime) printf '%s\n' 'Preparing the Python runtime...' ;;
        es:runtime) printf '%s\n' 'Preparando el entorno de Python...' ;;
        pt-BR:runtime) printf '%s\n' 'Preparando o ambiente Python...' ;;
        en:python) printf '%s\n' 'Python 3 is required. Install python3, tzdata and ca-certificates, then retry.' ;;
        es:python) printf '%s\n' 'Se necesita Python 3. Instala python3, tzdata y ca-certificates y vuelve a intentarlo.' ;;
        pt-BR:python) printf '%s\n' 'É necessário Python 3. Instale python3, tzdata e ca-certificates e tente novamente.' ;;
        en:curl) printf '%s\n' 'curl is required to download the selected language package.' ;;
        es:curl) printf '%s\n' 'Se necesita curl para descargar el paquete del idioma seleccionado.' ;;
        pt-BR:curl) printf '%s\n' 'O curl é necessário para baixar o pacote do idioma selecionado.' ;;
        en:download) printf '%s\n' 'Downloading the English installer from GitHub...' ;;
        es:download) printf '%s\n' 'Descargando el instalador en español desde GitHub...' ;;
        pt-BR:download) printf '%s\n' 'Baixando o instalador em português brasileiro do GitHub...' ;;
        en:failed) printf '%s\n' 'The language package could not be verified or downloaded. No installer code was run.' ;;
        en:checking) printf '%s\n' 'Checking for a newer installer release...' ;;
        es:checking) printf '%s\n' 'Buscando una versión más reciente del instalador...' ;;
        pt-BR:checking) printf '%s\n' 'Procurando uma versão mais recente do instalador...' ;;
        en:updating) printf 'Updating the installer from %s to %s...\n' "$2" "$3" ;;
        es:updating) printf 'Actualizando el instalador de %s a %s...\n' "$2" "$3" ;;
        pt-BR:updating) printf 'Atualizando o instalador de %s para %s...\n' "$2" "$3" ;;
        en:saved) printf 'Saved the updated installer to %s.\n' "$2" ;;
        es:saved) printf 'Instalador actualizado guardado en %s.\n' "$2" ;;
        pt-BR:saved) printf 'Instalador atualizado salvo em %s.\n' "$2" ;;
        en:unchecked) printf 'Could not check for installer updates; continuing with version %s.\n' "$VERSION" ;;
        es:unchecked) printf 'No se pudo comprobar si hay actualizaciones; se continúa con la versión %s.\n' "$VERSION" ;;
        pt-BR:unchecked) printf 'Não foi possível verificar atualizações; continuando com a versão %s.\n' "$VERSION" ;;
        en:unverified) printf 'The installer update could not be verified; continuing with version %s.\n' "$VERSION" ;;
        es:unverified) printf 'No se pudo verificar la actualización del instalador; se continúa con la versión %s.\n' "$VERSION" ;;
        pt-BR:unverified) printf 'Não foi possível verificar a atualização do instalador; continuando com a versão %s.\n' "$VERSION" ;;
        es:failed) printf '%s\n' 'No se pudo verificar o descargar el paquete del idioma. No se ejecutó el código del instalador.' ;;
        pt-BR:failed) printf '%s\n' 'Não foi possível verificar ou baixar o pacote do idioma. O código do instalador não foi executado.' ;;
    esac
}
command -v curl >/dev/null 2>&1 || { message curl >&2; exit 1; }
if ! command -v python3 >/dev/null 2>&1; then
    if [[ "$preview_mode" == 1 || "$EUID" -ne 0 ]] || ! command -v apt-get >/dev/null 2>&1; then
        message python >&2; exit 1
    fi
    message runtime
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y python3 tzdata ca-certificates
fi
umask 077
WORK_DIR="$(mktemp -d -t ptero-bootstrap.XXXXXXXX)"
download() {
    if [[ -t 1 && "$animations" == 1 && "$plain_mode" == 0 ]]; then
        local tick=0 status=0 symbol
        printf '\033[?25l'
        curl --proto '=https' --tlsv1.2 --fail --location --silent --show-error \
            --retry 2 --connect-timeout 15 --max-time 180 --max-filesize 16777216 "$1" --output "$2" &
        download_pid=$!
        while kill -0 "$download_pid" 2>/dev/null; do
            case "$((tick % 4))" in 0) symbol='⠋' ;; 1) symbol='⠙' ;; 2) symbol='⠹' ;; 3) symbol='⠸' ;; esac
            printf '\r  %s  GitHub · %s' "$symbol" "$language"
            tick=$((tick + 1))
            sleep 0.1
        done
        wait "$download_pid" || status=$?
        download_pid=""
        printf '\r\033[K\033[?25h'
        return "$status"
    else
        curl --proto '=https' --tlsv1.2 --fail --location --silent --show-error \
            --retry 2 --connect-timeout 15 --max-time 180 --max-filesize 16777216 "$1" --output "$2"
    fi
}
# Replace this bootstrap with a newer published release, verified against that
# release's SHA256SUMS, then restart it with the language already chosen.
self_update_bootstrap() {
    [[ "$self_update" == 1 && -z "${PTERO_SELF_UPDATED:-}" ]] || return 0
    local latest expected actual updater
    message checking
    if ! download "https://api.github.com/repos/$REPOSITORY/releases/latest" "$WORK_DIR/latest.json" 2>/dev/null; then
        message unchecked >&2; return 0
    fi
    latest="$(sed -n 's/.*"tag_name"[[:space:]]*:[[:space:]]*"v\([0-9][0-9]*\.[0-9][0-9]*\.[0-9][0-9]*\)".*/\1/p' "$WORK_DIR/latest.json" | head -n 1)"
    if [[ -z "$latest" ]]; then message unchecked >&2; return 0; fi
    if [[ "$latest" == "$VERSION" || "$(printf '%s\n%s\n' "$VERSION" "$latest" | sort -V | tail -n 1)" != "$latest" ]]; then
        return 0
    fi
    message updating "$VERSION" "$latest"
    local base="https://github.com/$REPOSITORY/releases/download/v$latest"
    if ! download "$base/SHA256SUMS" "$WORK_DIR/update.sums" 2>/dev/null || ! download "$base/install.sh" "$WORK_DIR/update.sh" 2>/dev/null; then
        message unverified >&2; return 0
    fi
    expected="$(awk '$2 == "install.sh" {print $1}' "$WORK_DIR/update.sums")"
    actual="$(sha256sum "$WORK_DIR/update.sh" | awk '{print $1}')"
    if [[ ! "$expected" =~ ^[0-9a-f]{64}$ || "$expected" != "$actual" ]] || ! grep -qx "VERSION=\"$latest\"" "$WORK_DIR/update.sh"; then
        message unverified >&2; return 0
    fi
    # Keep the update for the next run. Rename never rewrites the file this
    # shell is still reading, so the running bootstrap is unaffected.
    if [[ -f "$0" && ! -L "$0" && -w "$0" && -w "$(dirname -- "$0")" ]]; then
        if cp -- "$WORK_DIR/update.sh" "$0.ptero-new" && chmod "$(stat -c %a -- "$0")" "$0.ptero-new" && mv -f -- "$0.ptero-new" "$0"; then
            message saved "$0"
        else
            rm -f -- "$0.ptero-new"
        fi
    fi
    updater="$(mktemp -d -t ptero-update.XXXXXXXX)"
    cp -- "$WORK_DIR/update.sh" "$updater/install.sh"
    rm -rf -- "$WORK_DIR"
    WORK_DIR=""
    trap - EXIT
    exec env PTERO_SELF_UPDATED=1 PTERO_UPDATE_DIR="$updater" bash "$updater/install.sh" "${original_args[@]}" --language "$language"
}
self_update_bootstrap
verified=0
trap 'if [[ "$verified" == 0 ]]; then message failed >&2; fi' ERR
message download
release_tag="v$VERSION"
base_url="https://github.com/$REPOSITORY/releases/download/$release_tag"
download "$base_url/SHA256SUMS" "$WORK_DIR/SHA256SUMS"
download "$base_url/installer-$language.tar.gz" "$WORK_DIR/package.tar.gz"
python3 - "$WORK_DIR" "$language" "$REPOSITORY" "$VERSION" <<'PY'
import hashlib, json, re, sys, tarfile
from pathlib import Path, PurePosixPath
work, language, repository, version = Path(sys.argv[1]), *sys.argv[2:]
expected = [line.split()[0] for line in (work / 'SHA256SUMS').read_text().splitlines()
            if len(line.split()) == 2 and line.split()[1] == f'installer-{language}.tar.gz']
if len(expected) != 1 or not re.fullmatch(r'[0-9a-f]{64}', expected[0]):
    raise SystemExit('Invalid language package checksum file.')
archive = work / 'package.tar.gz'
if hashlib.sha256(archive.read_bytes()).hexdigest() != expected[0]:
    raise SystemExit('Language package checksum mismatch.')
destination = work / 'runtime'
destination.mkdir(mode=0o700)
with tarfile.open(archive, 'r:gz') as package:
    members = package.getmembers()
    if len(members) > 1000 or sum(item.size for item in members) > 64 * 1024 * 1024:
        raise SystemExit('Language package exceeds extraction limits.')
    seen = set()
    for item in members:
        path = PurePosixPath(item.name)
        if (path.is_absolute() or '..' in path.parts or not path.parts or
                not (item.isfile() or item.isdir()) or path in seen):
            raise SystemExit('Unsafe or duplicate language package member.')
        seen.add(path)
    # Packages contain regular files/directories only; never materialize links,
    # devices, archive ownership or setuid permissions from a remote archive.
    for item in members:
        target = destination / item.name
        if item.isdir():
            target.mkdir(mode=0o700, parents=True, exist_ok=True)
        else:
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with package.extractfile(item) as source, target.open('xb') as output:
                while block := source.read(1024 * 1024):
                    output.write(block)
            target.chmod(0o600)
metadata = json.loads((destination / 'ptero/distribution.json').read_text())
if (metadata.get('language') != language or metadata.get('repository') != repository or
        metadata.get('version') != version):
    raise SystemExit('The language package does not match this bootstrap. Download install.sh again.')
for filename in ('installer.py', 'ptero/cli.py', 'scripts/run_local.sh'):
    if not (destination / filename).is_file():
        raise SystemExit('Incomplete language package.')
PY
verified=1
PTERO_RELEASE_TAG="$release_tag" PTERO_SOURCE_REPOSITORY="$REPOSITORY" \
    bash "$WORK_DIR/runtime/scripts/run_local.sh" --language "$language" "${forwarded[@]}"
