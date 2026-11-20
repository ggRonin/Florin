"""Логи, аккаунты, state, faucet."""
import json
import logging
import os
import re
import sys
import threading
import time
from dataclasses import dataclass

import requests
from eth_account import Account

from .contracts import FAUCET_URL

Account.enable_unaudited_hdwallet_features()

# ---------------- logging ----------------
SUCCESS = 25
logging.addLevelName(SUCCESS, "OK")


LOG_DIR = "logs"
_FMT = logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s", "%Y-%m-%d %H:%M:%S")
_FMT_DBG = logging.Formatter("%(asctime)s.%(msecs)03d | %(levelname)-7s | %(threadName)-12s | "
                             "%(module)s:%(lineno)d | %(message)s", "%Y-%m-%d %H:%M:%S")


class _ShortFormatter(logging.Formatter):
    """Для консоли и florin.log: без трейсбеков (они в debug.log / errors.log)."""

    def format(self, record):
        saved = record.exc_info, record.exc_text
        record.exc_info, record.exc_text = None, None
        try:
            return super().format(record)
        finally:
            record.exc_info, record.exc_text = saved


def _rot(path, level, fmt, mb=10, backups=5):
    from logging.handlers import RotatingFileHandler
    h = RotatingFileHandler(path, maxBytes=mb * 1024 * 1024, backupCount=backups, encoding="utf-8")
    h.setLevel(level)
    h.setFormatter(fmt)
    return h


def setup_logging(log_dir: str):
    """logs/florin.log  — основной лог (INFO+)
       logs/debug.log   — всё подряд: tx, газ, nonce, ответы крана, полные трейсбеки
       logs/errors.log  — только предупреждения и ошибки с трейсбеками
       logs/accounts/NN_0xADDR.log — отдельная история каждого аккаунта (DEBUG)"""
    global LOG_DIR
    LOG_DIR = log_dir
    os.makedirs(os.path.join(log_dir, "accounts"), exist_ok=True)
    root = logging.getLogger("florin")
    root.setLevel(logging.DEBUG)
    root.handlers.clear()
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sh = logging.StreamHandler(sys.stdout)
    sh.setLevel(logging.INFO)
    sh.setFormatter(_ShortFormatter(_FMT._fmt, _FMT.datefmt))
    root.addHandler(sh)
    root.addHandler(_rot(os.path.join(log_dir, "florin.log"), logging.INFO,
                         _ShortFormatter(_FMT._fmt, _FMT.datefmt)))
    root.addHandler(_rot(os.path.join(log_dir, "debug.log"), logging.DEBUG, _FMT_DBG, mb=20))
    root.addHandler(_rot(os.path.join(log_dir, "errors.log"), logging.WARNING, _FMT_DBG))
    # web3/urllib3 сетевые ошибки тоже в debug
    for name in ("web3", "urllib3"):
        lg = logging.getLogger(name)
        lg.setLevel(logging.WARNING)
        lg.addHandler(root.handlers[2])
    return root


_acc_loggers: dict[str, logging.Logger] = {}
_acc_lock = threading.Lock()


def _acc_logger(idx: int, address: str) -> logging.Logger:
    with _acc_lock:
        if address in _acc_loggers:
            return _acc_loggers[address]
        lg = logging.getLogger(f"florin.acc{idx:02d}")
        lg.setLevel(logging.DEBUG)
        lg.handlers.clear()
        lg.addHandler(_rot(os.path.join(LOG_DIR, "accounts", f"{idx:02d}_{address}.log"),
                           logging.DEBUG, _FMT_DBG, mb=5, backups=3))
        lg.propagate = True  # дублируется в общие логи
        _acc_loggers[address] = lg
        return lg


class AccLog:
    def __init__(self, idx: int, address: str):
        self.p = f"[{idx:02d}] {address[:6]}…{address[-4:]} | "
        self.l = _acc_logger(idx, address)

    def debug(self, m): self.l.debug(self.p + m, stacklevel=2)
    def info(self, m): self.l.info(self.p + m, stacklevel=2)
    def warning(self, m): self.l.warning(self.p + m, stacklevel=2)
    def error(self, m): self.l.error(self.p + m, stacklevel=2)
    def success(self, m): self.l.log(SUCCESS, self.p + m, stacklevel=2)

    def exception(self, m):
        """Ошибка в консоль коротко, полный трейсбек — в файлы."""
        self.l.error(self.p + m, exc_info=True, stacklevel=2)


# ---------------- accounts ----------------
@dataclass
class Acc:
    idx: int
    key: str
    address: str
    proxy: str | None


def _norm_proxy(p: str) -> str | None:
    p = p.strip()
    if not p:
        return None
    if "://" not in p:
        p = "http://" + p
    return p


def load_accounts(path: str) -> list[Acc]:
    """Строка: `мнемоника или приватник[,login:pass@host:port]`"""
    res = []
    with open(path, encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            secret, _, proxy = line.partition(",")
            secret = secret.strip()
            if re.fullmatch(r"(0x)?[0-9a-fA-F]{64}", secret):
                acct = Account.from_key(secret)
            else:
                acct = Account.from_mnemonic(" ".join(secret.split()))
            res.append(Acc(n, acct.key.hex() if acct.key.hex().startswith("0x") else "0x" + acct.key.hex(),
                           acct.address, _norm_proxy(proxy)))
    return res


# ---------------- proxy manager ----------------
INTERNET_CHECK_URLS = ["https://www.google.com/generate_204", "https://1.1.1.1", "https://api.ipify.org"]
PROXY_CHECK_URL = "https://faucet.florin.so/"


def has_internet() -> bool:
    """Проверка интернета БЕЗ прокси — чтобы не менять прокси, когда упала своя сеть."""
    for u in INTERNET_CHECK_URLS:
        try:
            requests.get(u, timeout=10)
            return True
        except Exception:
            continue
    return False


def proxy_alive(proxy: str | None) -> bool:
    if not proxy:
        return True
    for _ in range(2):
        try:
            r = requests.get(PROXY_CHECK_URL, proxies={"http": proxy, "https": proxy},
                             headers={"User-Agent": UA}, timeout=20)
            if r.status_code < 500:
                return True
        except Exception:
            pass
        time.sleep(2)
    return False


class ProxyManager:
    """Если прокси аккаунта не работает: берёт первую рабочую из Proxy.txt, удаляет её оттуда
    (чтобы не выдать повторно) и записывает в accounts.txt вместо старой."""

    def __init__(self, accounts_file: str, pool_file: str):
        self.accounts_file = accounts_file
        self.pool_file = pool_file
        self.lock = threading.Lock()
        self.log = logging.getLogger("florin")

    def _wait_internet(self):
        warned = False
        while not has_internet():
            if not warned:
                self.log.warning("нет интернета — жду восстановления, прокси НЕ меняю")
                warned = True
            time.sleep(30)
        if warned:
            self.log.info("интернет восстановлен")

    def _pop_pool(self) -> str | None:
        try:
            with open(self.pool_file, encoding="utf-8") as f:
                lines = [l.strip() for l in f if l.strip()]
        except FileNotFoundError:
            return None
        if not lines:
            return None
        first, rest = lines[0], lines[1:]
        with open(self.pool_file, "w", encoding="utf-8") as f:
            f.write("\n".join(rest) + ("\n" if rest else ""))
        return first

    def _write_account(self, acc: "Acc", raw: str):
        with open(self.accounts_file, encoding="utf-8") as f:
            lines = f.read().splitlines()
        i = acc.idx - 1
        secret = lines[i].partition(",")[0].rstrip()
        lines[i] = f"{secret},{raw}"
        tmp = self.accounts_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        os.replace(tmp, self.accounts_file)

    def ensure(self, acc: "Acc", log=None) -> str | None:
        """Возвращает рабочий прокси аккаунта (при необходимости заменяет)."""
        log = log or AccLog(acc.idx, acc.address)
        if not acc.proxy or proxy_alive(acc.proxy):
            return acc.proxy
        with self.lock:
            # прокси не отвечает — сначала убеждаемся, что дело не в нашем интернете
            self._wait_internet()
            if proxy_alive(acc.proxy):
                return acc.proxy
            while True:
                raw = self._pop_pool()
                if raw is None:
                    log.error("Proxy.txt пуст — заменить прокси нечем")
                    return acc.proxy
                new = _norm_proxy(raw)
                if proxy_alive(new):
                    self._write_account(acc, raw)
                    acc.proxy = new
                    host = raw.rsplit("@", 1)[-1]
                    log.success(f"прокси заменён на …@{host} (удалён из Proxy.txt)")
                    return new
                if not has_internet():  # сеть упала во время перебора — возвращаем прокси в пул
                    self._push_front(raw)
                    self._wait_internet()
                    continue
                log.warning(f"прокси из пула …@{raw.rsplit('@', 1)[-1]} тоже не работает, беру следующий")

    def _push_front(self, raw: str):
        try:
            with open(self.pool_file, encoding="utf-8") as f:
                rest = f.read()
        except FileNotFoundError:
            rest = ""
        with open(self.pool_file, "w", encoding="utf-8") as f:
            f.write(raw + "\n" + rest)


# ---------------- state ----------------
class State:
    def __init__(self, path: str):
        self.path = path
        self.lock = threading.Lock()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        try:
            with open(path, encoding="utf-8") as f:
                self.data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            self.data = {}

    def acc(self, address: str) -> dict:
        with self.lock:
            return self.data.setdefault(address.lower(), {})

    def save(self):
        with self.lock:
            for _ in range(10):  # другие потоки могут менять dict во время сериализации
                try:
                    s = json.dumps(self.data, indent=1, ensure_ascii=False)
                    break
                except RuntimeError:
                    time.sleep(0.05)
            else:
                return
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(s)
            os.replace(tmp, self.path)


# ---------------- faucet ----------------
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/131.0.0.0 Safari/537.36")


def claim_faucet(address: str, proxy: str | None) -> tuple[bool, str]:
    proxies = {"http": proxy, "https": proxy} if proxy else None
    try:
        r = requests.post(
            FAUCET_URL,
            json={"recipientAddress": address},
            headers={"User-Agent": UA, "Origin": "https://faucet.florin.so",
                     "Referer": "https://faucet.florin.so/", "Content-Type": "application/json"},
            proxies=proxies, timeout=60)
    except Exception as e:
        return False, f"сеть/прокси: {e}"
    try:
        j = r.json()
    except Exception:
        return False, f"HTTP {r.status_code}: {r.text[:200]}"
    errs = j.get("errors") or []
    msg = next((e.get("message") for e in errs if isinstance(e, dict) and e.get("message")), None)
    if msg:
        return False, msg
    if (j.get("data") or {}).get("requestToken") is True:
        return True, "ok"
    return False, j.get("error") or f"HTTP {r.status_code}: {str(j)[:200]}"


def now() -> int:
    return int(time.time())


def sleep_left(window) -> int:
    """Сколько секунд осталось спать (0 — сейчас не время сна). window = ("01:00", "08:00") или None."""
    if not window:
        return 0
    lt = time.localtime()
    cur = lt.tm_hour * 60 + lt.tm_min
    s_h, s_m = map(int, window[0].split(":"))
    e_h, e_m = map(int, window[1].split(":"))
    start, end = s_h * 60 + s_m, e_h * 60 + e_m
    if start == end:
        return 0
    inside = start <= cur < end if start < end else (cur >= start or cur < end)
    if not inside:
        return 0
    return (((end - cur) % 1440) * 60) - lt.tm_sec
