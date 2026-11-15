"""TLS-сертификат балансировщика: загрузка, проверка и подмена без перезапуска.

Главная идея: новый сертификат сначала полностью проверяется и загружается
в НОВЫЙ ssl.SSLContext, и только потом одним присваиванием становится активным.
Если что-то не так, активным остаётся прежний, а клиенты ничего не замечают.
"""
import hashlib
import os
import re
import ssl
import threading
from datetime import datetime, timezone

from .logs import log
from .state import PersistError

_PEM_CERT = re.compile(r"-----BEGIN CERTIFICATE-----.+?-----END CERTIFICATE-----", re.S)
_SHORT = {"commonName": "CN", "organizationName": "O", "organizationalUnitName": "OU",
          "countryName": "C", "stateOrProvinceName": "ST", "localityName": "L"}


class CertError(Exception):
    """Сертификат или ключ не годятся. code — машинный код причины для admin API."""

    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


def _no_password():
    # Без этого OpenSSL, встретив зашифрованный ключ, спросил бы пароль в терминале
    # и повесил бы поток навсегда.
    raise CertError("key_encrypted", "Ключ защищён паролем, такие ключи не поддерживаются.")


def _dn(rdns) -> str:
    """(( ('commonName', 'localhost'), ),) -> 'CN=localhost'"""
    return ", ".join(f"{_SHORT.get(k, k)}={v}" for rdn in rdns for k, v in rdn)


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class ActiveCert:
    """Загруженная пара "сертификат + ключ": TLS-контекст и сведения о сертификате."""

    def __init__(self, ctx, decoded: dict, fingerprint: str, source: str):
        self.ctx = ctx
        self.subject = _dn(decoded.get("subject", ()))
        self.issuer = _dn(decoded.get("issuer", ()))
        self.sans = [value for _kind, value in decoded.get("subjectAltName", ())]
        self.not_before = ssl.cert_time_to_seconds(decoded["notBefore"])
        self.not_after = ssl.cert_time_to_seconds(decoded["notAfter"])
        self.fingerprint = fingerprint
        self.source = source
        self.loaded_at = datetime.now(timezone.utc).timestamp()

    def warnings(self) -> list:
        now = datetime.now(timezone.utc).timestamp()
        if now > self.not_after:
            return ["expired"]
        if now < self.not_before:
            return ["not_yet_valid"]
        return []

    def to_dict(self) -> dict:
        """То, что admin API отдаёт наружу. Приватного ключа здесь нет и быть не может."""
        return {
            "subject": self.subject,
            "issuer": self.issuer,
            "sans": self.sans,
            "not_before": _iso(self.not_before),
            "not_after": _iso(self.not_after),
            "fingerprint_sha256": self.fingerprint,
            "loaded_at": _iso(self.loaded_at),
            "source": self.source,
            "warnings": self.warnings(),
        }


def load_pair(cert_path: str, key_path: str, source: str) -> ActiveCert:
    """Проверить пару файлов и собрать из неё TLS-контекст. Ошибка -> CertError."""
    try:
        with open(cert_path, encoding="ascii", errors="replace") as f:
            cert_pem = f.read()
    except OSError as e:
        raise CertError("cert_file_missing", f"Файл сертификата {cert_path} не читается: {e.strerror}.") from e
    try:
        with open(key_path, encoding="ascii", errors="replace") as f:
            key_pem = f.read()
    except OSError as e:
        raise CertError("cert_file_missing", f"Файл ключа {key_path} не читается: {e.strerror}.") from e

    # 1. Сертификат читается? Берём первый блок в файле: это серверный сертификат,
    #    за ним может идти цепочка промежуточных.
    block = _PEM_CERT.search(cert_pem)
    if not block:
        raise CertError("cert_invalid", "В сертификате нет блока BEGIN CERTIFICATE.")
    try:
        fingerprint = hashlib.sha256(ssl.PEM_cert_to_DER_cert(block.group(0))).hexdigest()
        # В публичном API модуля ssl нет функции "разобрать сертификат из файла",
        # поэтому берём внутреннюю (ею пользуются тесты самого CPython). Для проекта
        # "только стандартная библиотека" это меньшее зло, чем вызывать openssl
        # как внешнюю программу. С библиотекой cryptography это одна строчка.
        decoded = ssl._ssl._test_decode_cert(cert_path)
        _ = decoded["notBefore"], decoded["notAfter"]
    except (ssl.SSLError, ValueError, KeyError) as e:
        raise CertError("cert_invalid", "Сертификат не читается: файл повреждён или это не PEM.") from e

    # 2. Ключ без пароля?
    if "ENCRYPTED" in key_pem:
        _no_password()

    # 3. Ключ читается и подходит к сертификату? Это проверяет сам OpenSSL при загрузке.
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3  # клиенты со старой версией получат отказ на рукопожатии
    ctx.set_alpn_protocols(["http/1.1"])  # HTTP/2 этот сервер не умеет
    try:
        ctx.load_cert_chain(cert_path, key_path, password=_no_password)
    except ssl.SSLError as e:
        text = str(e).upper()
        if "MISMATCH" in text or "NO_CERTIFICATE_ASSIGNED" in text:
            raise CertError("cert_key_mismatch", "Сертификат и ключ не совпадают.") from e
        raise CertError("key_invalid", "Ключ не читается: файл повреждён или это не PEM.") from e
    except OSError as e:
        raise CertError("cert_file_missing", f"Файлы сертификата не читаются: {e.strerror}.") from e
    return ActiveCert(ctx, decoded, fingerprint, source)


def _write(path: str, text: str, mode: int) -> None:
    # os.open, а не open(): права задаём в момент создания файла, чтобы ключ
    # ни на мгновение не оказался читаемым для посторонних.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w", encoding="ascii", errors="replace") as f:
        if hasattr(os, "fchmod"):  # файл мог остаться от прошлой попытки с другими правами
            os.fchmod(f.fileno(), mode)
        f.write(text)
        f.flush()
        os.fsync(f.fileno())


def _unlink_quietly(*paths) -> None:
    for p in paths:
        try:
            os.unlink(p)
        except OSError:
            pass


class CertStore:
    """Хранит активный сертификат и умеет его заменять."""

    def __init__(self, cert_path: str, key_path: str):
        self.cert_path = cert_path
        self.key_path = key_path
        self._lock = threading.Lock()  # замены выполняются по одной
        self._active = load_pair(cert_path, key_path, "startup")  # при старте ошибка — это отказ запускаться
        self._report(self._active)

    @property
    def ctx(self):
        """TLS-контекст для новых соединений."""
        return self._active.ctx

    def info(self) -> dict:
        return self._active.to_dict()

    def reload(self, source: str = "manual") -> dict:
        """Перечитать файлы с диска. При ошибке активным остаётся прежний сертификат."""
        with self._lock:
            try:
                new = load_pair(self.cert_path, self.key_path, source)
            except CertError as e:
                log("error", "сертификат не перезагружен, работаем со старым", reason=e.code, err=e.detail)
                raise
            self._active = new  # одно присваивание: новые соединения сразу получают новый контекст
            self._report(new)
            return new.to_dict()

    def upload(self, cert_pem: str, key_pem: str) -> dict:
        """Принять новую пару через admin API: проверить, записать в файлы, включить."""
        with self._lock:
            tmp_cert, tmp_key = self.cert_path + ".new", self.key_path + ".new"
            try:
                # Сначала пишем во временные файлы рядом с настоящими и проверяем их.
                # Настоящие файлы трогаем, только когда уверены, что пара рабочая.
                try:
                    _write(tmp_cert, cert_pem, 0o644)
                    _write(tmp_key, key_pem, 0o600)  # ключ читает только владелец
                except OSError as e:
                    raise PersistError(
                        f"Не удалось записать файлы сертификата рядом с {self.cert_path}: {e.strerror or e}") from e
                try:
                    new = load_pair(tmp_cert, tmp_key, "upload")
                except CertError as e:
                    log("error", "загруженный сертификат отклонён, работаем со старым", reason=e.code, err=e.detail)
                    raise
                try:
                    os.replace(tmp_key, self.key_path)  # replace атомарен: файл либо старый, либо новый
                    os.replace(tmp_cert, self.cert_path)
                except OSError as e:
                    raise PersistError(f"Не удалось заменить файлы сертификата: {e.strerror or e}") from e
            finally:
                _unlink_quietly(tmp_cert, tmp_key)  # после успешного replace их уже нет — это нормально
            self._active = new
            self._report(new)
            return new.to_dict()

    def _report(self, cert: ActiveCert) -> None:
        log("info", "сертификат загружен", source=cert.source, subject=cert.subject,
            not_after=_iso(cert.not_after), fingerprint=cert.fingerprint[:16])
        for w in cert.warnings():
            log("warn", "сертификат вне срока действия", problem=w, not_before=_iso(cert.not_before),
                not_after=_iso(cert.not_after))
