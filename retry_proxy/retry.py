import asyncio
import contextvars
import json
import random
import time
from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime

import httpx

from .config import logger, settings
from .key_pool import headers_with_key


_client_ip = contextvars.ContextVar("client_ip", default="")


def set_client_ip(value):
    return _client_ip.set(value)


def reset_client_ip(token):
    _client_ip.reset(token)


def parse_model(body):
    if not body: return ""
    try:
        value = json.loads(body).get("model")
        return value if isinstance(value, str) and value else ""
    except Exception: return ""


def parse_retry_after(value):
    if not value: return None
    try: return max(float(value.strip()), 0.0)
    except ValueError: pass
    try:
        dt = parsedate_to_datetime(value); now = datetime.now(tz=dt.tzinfo) if dt.tzinfo else datetime.now()
        return max((dt - now).total_seconds(), 0.0)
    except (TypeError, ValueError, OverflowError): return None


def is_host_level_error(exc): return isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout))
def filter_headers(headers, skip): return {k: v for k, v in headers.items() if k.lower() not in skip}


def calc_backoff_wait(consecutive, base, cap, enabled, ra_wait=None):
    if enabled and consecutive > 0:
        value = min(base * (2 ** (consecutive - 1)), cap)
        value = min(value * random.uniform(0.8, 1.2), cap)
        if ra_wait is not None: return (value, "RA+EB") if value > ra_wait else (ra_wait, "RA")
        return value, "EB"
    return (ra_wait, "RA") if ra_wait is not None else (base, "")


def _should_retry(status):
    return status >= 500 or status in (429, 401, 403) if settings.retry_broad else status in settings.retry_status_codes


def _tag(method, path, provider, model, client_ip=""):
    name = f"{provider}/{model}" if model else (provider or "?")
    ip = client_ip or _client_ip.get()
    ip_tag = f"[{ip}] " if ip else ""
    return f"{ip_tag}[{method} /{path}] [\033[36m{name}\033[0m]"


def _sc(status):
    if status == 0: return "\033[91mERR\033[0m"
    if status < 300: return f"\033[32m{status}\033[0m"
    if status < 400: return f"\033[34m{status}\033[0m"
    if status < 500: return f"\033[33m{status}\033[0m"
    return f"\033[31m{status}\033[0m"


@dataclass
class RetryResult:
    response: object
    winner_attempt: int
    total_sent: int
    last_status: int
    retry_codes: list
    first_ok: bool
    key_id: str
    started_at: float


class RetryProxy:
    def __init__(self, config=settings, client=None, logger_=logger, pools=None, log_store=None):
        self.config, self.client, self.logger = config, client, logger_
        self.pools, self.log_store = pools or {}, log_store

    async def _send(self, method, url, headers, body):
        assert self.client is not None
        req = self.client.build_request(method, url, headers=headers, content=body if body else None)
        return await self.client.send(req, stream=True)

    async def _race(self, method, url, req_headers, body, path, t0, provider, model, pool):
        total_sent = last_status = round_num = 0; retry_codes = []; c429 = cother = 0; last_key_id = ""
        while True:
            round_num += 1
            to_fire = min(self.config.max_concurrent, self.config.max_retries - total_sent) if self.config.max_retries > 0 else self.config.max_concurrent
            if to_fire <= 0: break
            entry = pool.pick() if pool else None
            hdrs = headers_with_key(req_headers, entry.key) if entry else req_headers
            if entry: last_key_id = entry.key_id
            async def send(n):
                try: return "ok", await self._send(method, url, hdrs, body), n
                except asyncio.CancelledError: raise
                except Exception as exc:
                    return "error", exc, n
            start = total_sent; tasks = set()
            for _ in range(to_fire):
                total_sent += 1; tasks.add(asyncio.create_task(send(total_sent)))
            key_tag = f"[{last_key_id}]" if pool and last_key_id else ""
            self.logger.info(f"{_tag(method, path, provider, model)}{key_tag} R{round_num} {to_fire}发(#{start + 1}-#{total_sent}) {time.time() - t0:.1f}s")
            winner = None; winner_attempt = 0; close = []; saw429 = False; ra_max = 0.0; host_error = False; remaining = tasks
            while remaining and winner is None:
                done, remaining = await asyncio.wait(remaining, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    if task.cancelled(): continue
                    kind, result, attempt = task.result()
                    if kind == "error":
                        last_status = 0; retry_codes.append(0); host_error |= is_host_level_error(result)
                        self.logger.warning(f"{_tag(method, path, provider, model)}{key_tag} ERR #{attempt}({time.time() - t0:.1f}s): {result!r}")
                    elif _should_retry(result.status_code):
                        last_status = result.status_code; retry_codes.append(result.status_code); close.append(result)
                        if result.status_code == 429:
                            saw429 = True; wait = parse_retry_after(result.headers.get("retry-after")); ra_max = max(ra_max, wait or 0)
                        self.logger.warning(f"{_tag(method, path, provider, model)}{key_tag} {_sc(result.status_code)} #{attempt}({time.time() - t0:.1f}s)")
                    else: winner, winner_attempt, last_status = result, attempt, result.status_code
            for task in remaining: task.cancel()
            if remaining: await asyncio.gather(*remaining, return_exceptions=True)
            for task in remaining:
                if task.done() and not task.cancelled():
                    try:
                        kind, result, _ = task.result()
                        if kind == "ok": close.append(result)
                    except Exception: pass
            for response in close:
                if response is winner: continue
                try: await response.aread()
                except Exception: pass
                try: await response.aclose()
                except Exception: pass
            if winner is not None:
                self.logger.info(f"{_tag(method, path, provider, model)}{key_tag} -> {_sc(winner.status_code)} #{winner_attempt}胜出(R{round_num},{total_sent}发) {time.time() - t0:.2f}s")
                return RetryResult(winner, winner_attempt, total_sent, last_status, retry_codes, round_num == 1, last_key_id, t0)
            if self.config.max_retries > 0 and total_sent >= self.config.max_retries: break
            if pool and entry and not host_error:
                pool.mark_cooldown(entry, self.config.key_cooldown, ra_max or None)
                if pool.has_fresh():
                    self.logger.info(f"{_tag(method, path, provider, model)}{key_tag} R{round_num}全败 换key {time.time() - t0:.1f}s")
                    continue
            if saw429:
                c429 += 1; cother = 0; wait, src = calc_backoff_wait(c429, self.config.retry_interval_429, self.config.retry_backoff_max_429, self.config.retry_backoff_429, ra_max or None)
            else:
                cother += 1; c429 = 0; wait, src = calc_backoff_wait(cother, self.config.retry_interval, self.config.retry_backoff_max, self.config.retry_backoff)
            self.logger.info(f"{_tag(method, path, provider, model)}{key_tag} R{round_num}全败 {wait:.1f}s后{f'({src})' if src else ''} {time.time() - t0:.1f}s")
            await asyncio.sleep(wait)
        return RetryResult(None, 0, total_sent, last_status, retry_codes, False, last_key_id, t0)

    async def _stagger(self, method, url, req_headers, body, path, t0, provider, model, pool):
        total_sent = last_status = 0; retry_codes = []; in_flight = {}; winner = None; winner_attempt = 0
        next_allowed = 0.0; c429 = cother = 0; last_key_id = ""; all_tasks = set()
        async def send(n):
            entry = pool.pick() if pool else None; hdrs = headers_with_key(req_headers, entry.key) if entry else req_headers
            if entry: nonlocal last_key_id; last_key_id = entry.key_id
            try: return "ok", await self._send(method, url, hdrs, body), n, entry
            except asyncio.CancelledError: raise
            except Exception as exc: return "error", exc, n, entry
        def can_fire(now): return winner is None and (self.config.max_retries == 0 or total_sent < self.config.max_retries) and len(in_flight) < self.config.max_concurrent and now >= next_allowed
        total_sent = 1; task = asyncio.create_task(send(total_sent)); in_flight[task] = time.time(); all_tasks.add(task)
        while True:
            key_tag = f"[{last_key_id}]" if pool and last_key_id else ""
            if not in_flight:
                if self.config.max_retries > 0 and total_sent >= self.config.max_retries: break
                wait = max(next_allowed - time.time(), 0)
                if wait > 0: self.logger.info(f"{_tag(method, path, provider, model)}{key_tag} 退避 {wait:.1f}s {time.time() - t0:.1f}s")
                await asyncio.sleep(wait)
                if can_fire(time.time()):
                    total_sent += 1; task = asyncio.create_task(send(total_sent)); in_flight[task] = time.time(); all_tasks.add(task)
                continue
            now = time.time(); delay = max(min(in_flight.values()) + self.config.retry_interval - now, 0)
            done, _ = await asyncio.wait(set(in_flight), timeout=delay, return_when=asyncio.FIRST_COMPLETED)
            now = time.time()
            if not done:
                if can_fire(now):
                    total_sent += 1; task = asyncio.create_task(send(total_sent)); in_flight[task] = now; all_tasks.add(task)
                    self.logger.info(f"{_tag(method, path, provider, model)}{key_tag} 补发#{total_sent}(在飞{len(in_flight)}) {now - t0:.1f}s")
                continue
            for task in done:
                in_flight.pop(task, None)
                if task.cancelled(): continue
                kind, result, attempt, entry = task.result()
                key_tag = f"[{entry.key_id}]" if pool and entry is not None else ""
                if kind == "error":
                    last_status = 0; retry_codes.append(0)
                    self.logger.warning(f"{_tag(method, path, provider, model)}{key_tag} ERR #{attempt}({now - t0:.1f}s) {result!r} 立即补发")
                    if pool and entry and not is_host_level_error(result): pool.mark_cooldown(entry, self.config.key_cooldown)
                    if can_fire(now):
                        total_sent += 1; new_task = asyncio.create_task(send(total_sent)); in_flight[new_task] = now; all_tasks.add(new_task)
                elif _should_retry(result.status_code):
                    last_status = result.status_code; retry_codes.append(result.status_code)
                    self.logger.warning(f"{_tag(method, path, provider, model)}{key_tag} {_sc(result.status_code)} #{attempt} 在飞{len(in_flight)}")
                    ra = parse_retry_after(result.headers.get("retry-after")) if result.status_code == 429 else None
                    if pool and entry: pool.mark_cooldown(entry, self.config.key_cooldown, ra)
                    if result.status_code == 429:
                        c429 += 1; cother = 0; wait, _ = calc_backoff_wait(c429, self.config.retry_interval_429, self.config.retry_backoff_max_429, self.config.retry_backoff_429, ra); next_allowed = max(next_allowed, now + wait)
                        self.logger.warning(f"{_tag(method, path, provider, model)}{key_tag} {_sc(429)} #{attempt} {wait:.1f}s 在飞{len(in_flight)} {now - t0:.1f}s")
                    else:
                        cother += 1; c429 = 0
                        if self.config.retry_backoff:
                            wait, _ = calc_backoff_wait(cother, self.config.retry_interval, self.config.retry_backoff_max, True); next_allowed = max(next_allowed, now + wait)
                            self.logger.warning(f"{_tag(method, path, provider, model)}{key_tag} {_sc(result.status_code)} #{attempt} {wait:.1f}s 在飞{len(in_flight)} {now - t0:.1f}s")
                        elif can_fire(now):
                            total_sent += 1; new_task = asyncio.create_task(send(total_sent)); in_flight[new_task] = now; all_tasks.add(new_task)
                            self.logger.warning(f"{_tag(method, path, provider, model)}{key_tag} {_sc(result.status_code)} #{attempt} 立即补发 在飞{len(in_flight)} {now - t0:.1f}s")
                    try: await result.aread()
                    except Exception: pass
                    await result.aclose()
                else:
                    winner, winner_attempt, last_status = result, attempt, result.status_code
                    last_key_id = entry.key_id if entry is not None else ""
                    self.logger.info(f"{_tag(method, path, provider, model)}{key_tag} -> {_sc(result.status_code)} #{attempt}胜出({total_sent}发) {now - t0:.2f}s")
                    break
            if winner:
                for task in all_tasks:
                    if not task.done(): task.cancel()
                await asyncio.gather(*all_tasks, return_exceptions=True)
                for task in all_tasks:
                    if task.cancelled(): continue
                    try:
                        kind, result, _, _ = task.result()
                        if kind == "ok" and result is not winner: await result.aclose()
                    except Exception: pass
                in_flight.clear(); break
            if in_flight and can_fire(time.time()) and any(time.time() - stamp >= self.config.retry_interval for stamp in in_flight.values()):
                total_sent += 1; task = asyncio.create_task(send(total_sent)); in_flight[task] = time.time(); all_tasks.add(task)
                self.logger.info(f"{_tag(method, path, provider, model)}{key_tag} 补发#{total_sent}(在飞{len(in_flight)}) {time.time() - t0:.1f}s")
        return RetryResult(winner, winner_attempt, total_sent, last_status, retry_codes, bool(winner and winner_attempt == 1), last_key_id, t0)

    async def request(self, method, url, headers, body, path, provider, model, pool=None):
        start = time.time()
        if self.config.hedge_mode == "race":
            return await self._race(method, url, headers, body, path, start, provider, model, pool)
        if self.config.hedge_mode == "stagger":
            return await self._stagger(method, url, headers, body, path, start, provider, model, pool)
        attempt = 0; last_status = 0; retry_codes = []; c429 = cother = 0; last_key_id = ""
        while True:
            attempt += 1; entry = pool.pick() if pool else None; send_headers = headers_with_key(headers, entry.key) if entry else headers
            if entry: last_key_id = entry.key_id
            key_tag = f"[{last_key_id}]" if pool and last_key_id else ""
            if self.config.max_retries > 0 and attempt > self.config.max_retries:
                self.logger.error(f"{_tag(method, path, provider, model)}{key_tag} 放弃({self.config.max_retries}次) {time.time() - start:.1f}s")
                break
            cycle = time.time()
            try: response = await self._send(method, url, send_headers, body)
            except (httpx.RequestError, httpx.HTTPError) as exc:
                last_status = 0; retry_codes.append(0); elapsed = time.time() - cycle
                key_tag = f"[{last_key_id}]" if pool and last_key_id else ""
                sleep_for = max(self.config.retry_interval - elapsed, 0)
                self.logger.warning(f"{_tag(method, path, provider, model)}{key_tag} ERR #{attempt}({elapsed:.2f}s) {exc!r} {sleep_for:.2f}s后重试")
                if pool and entry and not is_host_level_error(exc):
                    pool.mark_cooldown(entry, self.config.key_cooldown)
                    if pool.has_fresh():
                        self.logger.warning(f"{_tag(method, path, provider, model)}{key_tag} ERR #{attempt} 换key 总{time.time() - start:.1f}s")
                        continue
                await asyncio.sleep(sleep_for); continue
            if _should_retry(response.status_code):
                last_status = response.status_code; retry_codes.append(response.status_code)
                ra = parse_retry_after(response.headers.get("retry-after")) if response.status_code == 429 else None
                if pool and entry: pool.mark_cooldown(entry, self.config.key_cooldown, ra)
                try: await response.aread()
                except Exception: pass
                await response.aclose()
                key_tag = f"[{last_key_id}]" if pool and last_key_id else ""
                if pool and pool.has_fresh():
                    self.logger.warning(f"{_tag(method, path, provider, model)}{key_tag} {_sc(response.status_code)} #{attempt} 换key 总{time.time() - start:.1f}s")
                    continue
                if response.status_code == 429: c429 += 1; cother = 0; wait, src = calc_backoff_wait(c429, self.config.retry_interval_429, self.config.retry_backoff_max_429, self.config.retry_backoff_429, ra)
                else: cother += 1; c429 = 0; wait, src = calc_backoff_wait(cother, self.config.retry_interval, self.config.retry_backoff_max, self.config.retry_backoff)
                sleep_for = wait if src.startswith("RA") else max(wait - (time.time() - cycle), 0)
                self.logger.warning(f"{_tag(method, path, provider, model)}{key_tag} {_sc(response.status_code)} #{attempt} {sleep_for:.2f}s后重试{f'({src})' if src else ''} 总{time.time() - start:.1f}s")
                await asyncio.sleep(sleep_for); continue
            key_tag = f"[{last_key_id}]" if pool and last_key_id else ""
            self.logger.info(f"{_tag(method, path, provider, model)}{key_tag} -> {_sc(response.status_code)} #{attempt} {time.time() - start:.2f}s")
            return RetryResult(response, attempt, attempt, response.status_code, retry_codes, attempt == 1, last_key_id, start)
        return RetryResult(None, 0, attempt - 1, last_status, retry_codes, False, last_key_id, start)
