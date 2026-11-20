"""Низкоуровневая работа с сетью: вызовы контрактов по сигнатуре, отправка tx, permit."""
import time
from decimal import Decimal

from eth_abi import decode, encode
from eth_account import Account
from eth_account.messages import encode_typed_data
from web3 import Web3

from .contracts import CHAIN_ID, ERRORS

Account.enable_unaudited_hdwallet_features()

E18 = 10**18


def to_wei(x) -> int:
    return int(Decimal(str(x)) * E18)


def fw(x: int, digits: int = 4) -> str:
    return f"{x / E18:,.{digits}f}"


def cs(a: str) -> str:
    return Web3.to_checksum_address(a)


def _split_types(s: str) -> list[str]:
    """'uint256,(address,uint256),bool' -> ['uint256', '(address,uint256)', 'bool']"""
    out, depth, cur = [], 0, ""
    for ch in s:
        if ch == "," and depth == 0:
            out.append(cur)
            cur = ""
            continue
        depth += ch == "("
        depth -= ch == ")"
        cur += ch
    if cur:
        out.append(cur)
    return out


def calldata(sig: str, args=()) -> str:
    types = _split_types(sig[sig.index("(") + 1 : -1])
    data = Web3.keccak(text=sig)[:4] + (encode(types, list(args)) if types else b"")
    return "0x" + data.hex()


def _short(v, n=400) -> str:
    def conv(x):
        if isinstance(x, bytes):
            return "0x" + x.hex()[:16] + "…"
        if isinstance(x, (list, tuple)):
            return [conv(i) for i in x]
        return x
    s = str(conv(v))
    return s if len(s) <= n else s[:n] + "…"


class TxError(Exception):
    pass


def decode_revert(err: Exception) -> str:
    data = getattr(err, "data", None)
    if isinstance(data, dict):
        data = data.get("data")
    if not isinstance(data, str):
        # иногда revert-data лежит в args
        for a in getattr(err, "args", ()):
            if isinstance(a, str) and a.startswith("0x") and len(a) >= 10:
                data = a
                break
            if isinstance(a, dict) and isinstance(a.get("data"), str):
                data = a["data"]
                break
    if isinstance(data, str) and data.startswith("0x") and len(data) >= 10:
        sel = data[:10].lower()
        if sel == "0x08c379a0":
            try:
                return "revert: " + decode(["string"], bytes.fromhex(data[10:]))[0]
            except Exception:
                pass
        if sel in ERRORS:
            return f"revert: {ERRORS[sel]}"
        return f"revert: {data[:74]}"
    return str(err)[:300]


class Wallet:
    def __init__(self, key: str, rpc: str, proxy: str | None = None, log=None):
        self.log = log
        kw = {"timeout": 60}
        if proxy:
            kw["proxies"] = {"http": proxy, "https": proxy}
        self.w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs=kw))
        self.acct = Account.from_key(key)
        self.address = self.acct.address

    # ---------- helpers ----------
    def _retry(self, fn, attempts=4):
        last = None
        for i in range(attempts):
            try:
                return fn()
            except TxError:
                raise
            except Exception as e:  # сеть / прокси
                if "revert" in str(e).lower() or "execution reverted" in str(e).lower():
                    raise TxError(decode_revert(e)) from e
                last = e
                self._dbg(f"RPC ошибка (попытка {i + 1}/{attempts}): {type(e).__name__}: {str(e)[:300]}")
                time.sleep(2 + 3 * i)
        raise last

    def eth_balance(self) -> int:
        return self._retry(lambda: self.w3.eth.get_balance(self.address))

    def block(self):
        return self._retry(lambda: self.w3.eth.get_block("latest"))

    def call(self, to: str, sig: str, args=(), out=(), value: int = 0):
        tx = {"to": cs(to), "from": self.address, "data": calldata(sig, args)}
        if value:
            tx["value"] = value

        def _do():
            try:
                return self.w3.eth.call(tx)
            except Exception as e:
                if "revert" in str(e).lower() or getattr(e, "data", None):
                    raise TxError(decode_revert(e)) from e
                raise

        raw = self._retry(_do)
        if not out:
            return None
        res = decode(list(out), raw)
        return res[0] if len(out) == 1 else res

    def send(self, to: str, sig: str, args=(), value: int = 0, gas_mult: float = 1.3):
        tx = {
            "chainId": CHAIN_ID,
            "from": self.address,
            "to": cs(to),
            "data": calldata(sig, args),
            "value": value,
        }

        def _est():
            try:
                return self.w3.eth.estimate_gas(tx)
            except Exception as e:
                msg = str(e).lower()
                if "revert" in msg or getattr(e, "data", None) or "insufficient funds" in msg:
                    raise TxError(decode_revert(e)) from e
                raise

        self._dbg(f"TX -> {to} {sig.split('(')[0]} args={_short(args)} value={value} data={tx['data'][:10]}…")
        try:
            gas = self._retry(_est)
        except TxError as e:
            self._dbg(f"TX estimate revert: {e} | calldata={tx['data']}")
            raise
        tx["gas"] = int(gas * gas_mult)
        tx["gasPrice"] = int(self._retry(lambda: self.w3.eth.gas_price) * 1.2)
        tx["nonce"] = self._retry(lambda: self.w3.eth.get_transaction_count(self.address, "pending"))
        signed = self.acct.sign_transaction(tx)
        t0 = time.time()
        h = self._retry(lambda: self.w3.eth.send_raw_transaction(signed.raw_transaction))
        hx = Web3.to_hex(h)
        self._dbg(f"TX sent {hx} gas={tx['gas']} gasPrice={tx['gasPrice']} nonce={tx['nonce']}")
        rcpt = self._retry(lambda: self.w3.eth.wait_for_transaction_receipt(h, timeout=180, poll_latency=2))
        self._dbg(f"TX mined {hx} status={rcpt['status']} block={rcpt['blockNumber']} "
                  f"gasUsed={rcpt['gasUsed']} за {time.time() - t0:.1f}с")
        if rcpt["status"] != 1:
            raise TxError(f"tx reverted {hx}")
        return rcpt, hx

    def _dbg(self, m):
        if self.log:
            self.log.l.debug(self.log.p + m, stacklevel=2)

    # ---------- ERC-2612 permit ----------
    def sign_permit(self, token: str, spender: str, value: int) -> tuple:
        dom = self.call(token, "eip712Domain()",
                        out=["bytes1", "string", "string", "uint256", "address", "bytes32", "uint256[]"])
        _, name, version, chain_id, verifying, _, _ = dom
        nonce = self.call(token, "nonces(address)", [self.address], ["uint256"])
        deadline = self.block()["timestamp"] + 1200
        typed = {
            "types": {
                "EIP712Domain": [
                    {"name": "name", "type": "string"},
                    {"name": "version", "type": "string"},
                    {"name": "chainId", "type": "uint256"},
                    {"name": "verifyingContract", "type": "address"},
                ],
                "Permit": [
                    {"name": "owner", "type": "address"},
                    {"name": "spender", "type": "address"},
                    {"name": "value", "type": "uint256"},
                    {"name": "nonce", "type": "uint256"},
                    {"name": "deadline", "type": "uint256"},
                ],
            },
            "primaryType": "Permit",
            "domain": {"name": name, "version": version, "chainId": chain_id,
                       "verifyingContract": cs(verifying)},
            "message": {"owner": self.address, "spender": cs(spender), "value": value,
                        "nonce": nonce, "deadline": deadline},
        }
        sm = self.acct.sign_message(encode_typed_data(full_message=typed))
        return (value, deadline, sm.v, sm.r.to_bytes(32, "big"), sm.s.to_bytes(32, "big"))
