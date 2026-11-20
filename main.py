"""Florin testnet bot.

  python main.py status                  # балансы и позиции всех аккаунтов
  python main.py faucet                  # клейм крана для всех (у кого прошло 2ч)
  python main.py once                    # одна сессия (кран + 2-4 действия) на каждый аккаунт
  python main.py run                     # бесконечный фарм: у каждого аккаунта свой таймер, THREADS потоков
  python main.py once --only 1-3,7       # выбрать аккаунты (номера строк accounts.txt)
  python main.py once --action open_borrow --n 1   # принудительно одно действие
"""
import argparse
import os
import random
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

os.chdir(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.getcwd())

import config as cfg  # noqa: E402
from florin.chain import fw  # noqa: E402
from florin.strategy import Session  # noqa: E402
from florin.utils import State, load_accounts, setup_logging, sleep_left  # noqa: E402

log = setup_logging(cfg.LOG_DIR)


def parse_only(s: str | None):
    if not s:
        return None
    res = set()
    for part in s.split(","):
        if "-" in part:
            a, b = part.split("-")
            res.update(range(int(a), int(b) + 1))
        else:
            res.add(int(part))
    return res


def run_session(acc, state, args):
    try:
        s = Session(acc, state)
        if args.cmd == "faucet":
            s.faucet(force=args.force)
        else:
            s.run(actions=args.n, only=args.action)
    except Exception as e:
        log.error(f"[{acc.idx:02d}] {acc.address} | сессия упала: {type(e).__name__}: {e}", exc_info=True)


def run_all(accs, state, args):
    if cfg.SHUFFLE_ACCOUNTS:
        random.shuffle(accs)
    if cfg.THREADS > 1:
        with ThreadPoolExecutor(cfg.THREADS, thread_name_prefix="worker") as ex:
            for a in accs:
                ex.submit(run_session, a, state, args)
                time.sleep(random.uniform(5, 30))
        return
    for i, a in enumerate(accs):
        run_session(a, state, args)
        if i < len(accs) - 1 and args.cmd != "faucet":
            d = random.uniform(*cfg.DELAY_BETWEEN_ACCOUNTS)
            log.info(f"пауза {d:.0f}с до следующего аккаунта")
            time.sleep(d)
        elif args.cmd == "faucet":
            time.sleep(random.uniform(3, 10))


def status(accs, state):
    tot = {"eth": 0, "spy": 0, "fusd": 0, "debt": 0, "sp": 0, "tr": 0}
    print(f"{'#':>3} {'address':42} {'ETH':>9} {'SPY':>9} {'FUSD':>10} {'pos':>3} {'debt':>10} {'SP':>10}")
    for a in accs:
        try:
            s = Session(a, state)
            s.refresh()
            debt = sum(t["debt"] for t in s.troves)
            print(f"{a.idx:>3} {a.address} {fw(s.bal['eth'], 5):>9} {fw(s.bal['spy'], 2):>9} "
                  f"{fw(s.bal['fusd'], 2):>10} {len(s.troves):>3} {fw(debt, 0):>10} {fw(s.sp['deposit'], 2):>10}")
            tot["eth"] += s.bal["eth"]; tot["spy"] += s.bal["spy"]; tot["fusd"] += s.bal["fusd"]
            tot["debt"] += debt; tot["sp"] += s.sp["deposit"]; tot["tr"] += len(s.troves)
        except Exception as e:
            print(f"{a.idx:>3} {a.address} ошибка: {str(e)[:120]}")
    print(f"{'':>3} {'ИТОГО':42} {fw(tot['eth'], 5):>9} {fw(tot['spy'], 2):>9} {fw(tot['fusd'], 2):>10} "
          f"{tot['tr']:>3} {fw(tot['debt'], 0):>10} {fw(tot['sp'], 2):>10}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("cmd", choices=["status", "faucet", "once", "run"])
    p.add_argument("--only", help="номера аккаунтов: 1-5,8")
    p.add_argument("--action", help="принудительное действие (open_borrow, sp_deposit, ...)")
    p.add_argument("--n", type=int, help="сколько действий за сессию")
    p.add_argument("--force", action="store_true", help="кран без проверки таймера")
    args = p.parse_args()

    accs = load_accounts(cfg.ACCOUNTS_FILE)
    only = parse_only(args.only)
    if only:
        accs = [a for a in accs if a.idx in only]
    state = State(cfg.STATE_FILE)
    log.info(f"аккаунтов: {len(accs)}")

    if args.cmd == "status":
        status(accs, state)
    elif args.cmd in ("faucet", "once"):
        run_all(accs, state, args)
    else:
        scheduler(accs, state, args)


def scheduler(accs, state, args):
    """Каждый аккаунт по своему таймеру: сессия стартует, когда подошёл его следующий клейм крана
    (последний клейм + 2ч + 5..45 мин). Параллельно работает до THREADS аккаунтов."""
    lock = threading.Lock()
    running = set()
    t0 = int(time.time())
    for a in accs:  # первый старт — вразнобой
        st = state.acc(a.address)
        if not st.get("faucet_next"):
            st["faucet_next"] = t0 + int(random.uniform(*cfg.START_SPREAD_MIN) * 60)
    state.save()

    def job(a):
        try:
            run_session(a, state, args)
        finally:
            st = state.acc(a.address)
            if int(st.get("faucet_next", 0)) <= int(time.time()):
                # сессия упала до клейма крана (RPC/сеть) — не долбим каждые 15с, ретрай через 10-25 мин
                st["faucet_next"] = int(time.time()) + int(random.uniform(10, 25) * 60)
                log.warning(f"[{a.idx:02d}] сессия не дошла до крана, повтор через ~"
                            f"{(st['faucet_next'] - time.time()) / 60:.0f} мин")
                state.save()
            with lock:
                running.discard(a.idx)

    log.info(f"планировщик: {len(accs)} аккаунтов, потоков {cfg.THREADS}, "
             f"сон {'-'.join(cfg.SLEEP_TIME) if cfg.SLEEP_TIME else 'выключен'}")
    last_report = 0
    sleeping = False
    with ThreadPoolExecutor(cfg.THREADS, thread_name_prefix="worker") as ex:
        while True:
            left = sleep_left(cfg.SLEEP_TIME)
            if left:
                if not sleeping:
                    sleeping = True
                    wake = time.strftime("%H:%M", time.localtime(time.time() + left))
                    log.info(f"💤 время сна — новые сессии не запускаю до {wake} "
                             f"(текущие доработают и остановятся)")
                time.sleep(30)
                continue
            if sleeping:  # проснулись: просроченные аккаунты стартуют вразнобой, а не все сразу
                sleeping = False
                t = int(time.time())
                for a in accs:
                    st = state.acc(a.address)
                    if int(st.get("faucet_next", 0)) <= t:
                        st["faucet_next"] = t + int(random.uniform(*cfg.SLEEP_WAKE_SPREAD_MIN) * 60)
                state.save()
                log.info("☀ сон окончен, аккаунты стартуют вразнобой в течение часа")
            now_ = int(time.time())
            due = [a for a in accs if a.idx not in running
                   and int(state.acc(a.address).get("faucet_next", 0)) <= now_]
            random.shuffle(due)
            for a in due:
                with lock:
                    if len(running) >= cfg.THREADS:
                        break
                    running.add(a.idx)
                ex.submit(job, a)
                time.sleep(random.uniform(3, 20))  # не стартуем пачкой
            if now_ - last_report > 900:
                last_report = now_
                nxt = sorted((int(state.acc(a.address).get("faucet_next", 0)), a.idx) for a in accs
                             if a.idx not in running)
                if nxt:
                    log.info(f"в работе: {len(running)} | ближайший: #{nxt[0][1]} в "
                             f"{time.strftime('%H:%M', time.localtime(nxt[0][0]))}")
            time.sleep(15)


if __name__ == "__main__":
    main()
