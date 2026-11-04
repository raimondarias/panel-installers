"""Shared constants, errors, validators and safe file helpers. Standard library only."""
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import tarfile
import tempfile
import urllib.parse
import urllib.request
import uuid


PANEL_VERSION = "v2.0.0-beta.2"
WINGS_VERSION = "v1.13.4"
PROJECT = "ptero-installer"
SCHEMA = 1


class InstallerError(Exception):
    pass


class Cancelled(InstallerError):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise InstallerError("La API redirige la petición. Introduce la URL final del Panel.")


def node_configuration(payload, url):
    """Mismos campos remotos permitidos por Wings PanelConfiguration, sin indirection."""
    try:
        if not isinstance(payload, dict):
            raise ValueError("objeto esperado")
        uuid.UUID(payload["uuid"])
        for field in ("token", "token_id"):
            value = payload[field]
            if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
                raise ValueError("token no válido o con indirection")
        api = payload["api"]
        port = api["port"]
        sftp = payload["system"]["sftp"]["bind_port"]
        if any(type(value) is not int or not 1 <= value <= 65535 for value in (port, sftp)):
            raise ValueError("puerto no válido")
        host = api.get("host", "0.0.0.0")
        if not valid_host(host):
            raise ValueError("host de escucha no válido")
        ssl = api["ssl"]
        if type(ssl["enabled"]) is not bool:
            raise ValueError("ssl.enabled debe ser booleano")
        for field in ("cert", "key"):
            if not isinstance(ssl.get(field, ""), str):
                raise ValueError("ruta SSL no válida")
            if ssl["enabled"] and not Path(ssl.get(field, "")).is_absolute():
                raise ValueError("SSL necesita rutas absolutas de certificado y clave")
        upload = api.get("upload_limit", 100)
        if type(upload) is not int or upload < 0:
            raise ValueError("upload_limit no válido")
        return {"debug": False, "uuid": payload["uuid"], "token_id": payload["token_id"],
                "token": payload["token"], "api": {"host": host, "port": port,
                "ssl": {key: ssl.get(key, "") for key in ("enabled", "cert", "key")}, "upload_limit": upload},
                "system": {"sftp": {"bind_port": sftp}}, "remote": url}
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise InstallerError("La API no devolvió una configuración de nodo válida.") from exc


def file_sha256(path):
    """Stream a file through SHA-256 without loading it into memory."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def write_checksum(path):
    """Write the ``<file>.sha256`` companion used by every installer archive."""
    value = file_sha256(path)
    atomic_write(Path(str(path) + ".sha256"), value + "\n")
    return value


def download(url, target, limit, timeout=90):
    """Stream an HTTPS download to ``target`` with a size cap; return its SHA-256."""
    digest, size = hashlib.sha256(), 0
    with urllib.request.urlopen(url, timeout=timeout) as response, Path(target).open("wb") as stream:
        while chunk := response.read(1024 * 1024):
            size += len(chunk)
            if size > limit:
                raise InstallerError("The download exceeds its size limit.")
            digest.update(chunk)
            stream.write(chunk)
    return digest.hexdigest()


def atomic_write(path, content, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise InstallerError(f"No se escribe sobre un enlace simbólico: {path}")
    fd, name = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def write_json(path, data):
    atomic_write(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def read_json(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError) as exc:
        raise InstallerError(f"No se puede leer JSON: {path}: {exc}") from exc


def valid_email(value):
    return bool(re.fullmatch(r"[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+", value))


def valid_host(value):
    if len(value) > 253 or not value:
        return False
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return all(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", part)
                   for part in value.split("."))


def valid_domain(value):
    try:
        ipaddress.ip_address(value)
        return False
    except ValueError:
        return valid_host(value) and "." in value


def valid_url(value):
    try:
        url = urllib.parse.urlsplit(value)
        return (url.scheme in ("https", "http") and valid_host(url.hostname or "")
                and not url.username and not url.password and not url.query
                and not url.fragment and url.path in ("", "/")
                and (url.port is None or 0 < url.port <= 65535))
    except ValueError:
        return False


def valid_version(value, component):
    major = "2" if component == "panel" else "1"
    return bool(re.fullmatch(r"v" + major + r"\.\d+\.\d+(?:-(?:alpha|beta|rc)\.\d+)?", value))


def version_key(tag):
    nums = tuple(map(int, tag[1:].split("-")[0].split(".")))
    suffix = tag.split("-", 1)[1] if "-" in tag else ""
    rank = 3 if not suffix else {"alpha": 0, "beta": 1, "rc": 2}[suffix.split(".")[0]]
    return (*nums, rank, int(suffix.split(".")[1]) if suffix else 0)


def secure_extract(archive, destination, preserve_ownership=True):
    """Extrae archivos y symlinks internos; rechaza rutas externas/dispositivos."""
    destination = Path(destination).resolve()
    with tarfile.open(archive, "r:gz") as tar:
        members = [member for member in tar.getmembers() if not (member.isdir() and Path(member.name) == Path("."))]
        seen = set()
        previous = {}
        links = {Path(m.name) for m in members if m.issym()}
        for member in members:
            name = Path(member.name)
            target = (destination / name).resolve()
            if (name.is_absolute() or ".." in name.parts or not name.parts
                    or not target.is_relative_to(destination)
                    or not (member.isfile() or member.isdir() or member.issym() or member.islnk()) or member.name in seen
                    or any(parent in links for parent in name.parents)):
                raise InstallerError(f"Backup con entrada no permitida: {member.name}")
            if member.issym():
                link = Path(member.linkname)
                if link.is_absolute() or not (target.parent / link).resolve().is_relative_to(destination):
                    raise InstallerError(f"Backup con enlace externo: {member.name}")
            if member.islnk():
                source = previous.get(member.linkname)
                if source is None or not (source.isfile() or source.islnk()):
                    raise InstallerError("Backup con hardlink sin archivo original anterior.")
            seen.add(member.name)
            previous[member.name] = member
        # Los links se crean al final: nunca escribir a través de un link del tar.
        for member in members:
            target = destination / member.name
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True, mode=0o700)
            elif member.isfile() or member.islnk():
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                with tar.extractfile(member) as source, target.open("xb") as output:
                    shutil.copyfileobj(source, output)
        for member in members:
            if member.issym():
                target = destination / member.name
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                target.symlink_to(member.linkname)
        for member in members:
            target = destination / member.name
            if member.issym():
                try:
                    internal = target.resolve().is_relative_to(destination)
                except RuntimeError as exc:
                    raise InstallerError("Backup con ciclo de enlaces.") from exc
                if not internal:
                    raise InstallerError("Backup con enlace que escapa del destino.")
            else:
                target.chmod(member.mode & 0o777 & ~0o002)
                if preserve_ownership and os.geteuid() == 0:
                    # .env debe seguir siendo legible para nginx dentro del contenedor.
                    os.chown(target, member.uid, member.gid)
