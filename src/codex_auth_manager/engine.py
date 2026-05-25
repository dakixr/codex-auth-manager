from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

from .rpc import Snapshot, query_quota
from .storage import get_account, read_auth, sync_saved_and_active, write_auth

LIVE_WORKERS = 4


@dataclass(frozen=True)
class QuotaResult:
    name: str
    snapshot: Snapshot | None = None
    error: Exception | None = None


def fetch_quota(name: str, *, sync_active: bool = False) -> Snapshot:
    account = get_account(name)
    data = read_auth(account.auth_path)
    snapshot = query_quota(data)
    if sync_active:
        sync_saved_and_active(name, snapshot.updated_auth)
    else:
        write_auth(account.auth_path, snapshot.updated_auth)
    return snapshot


def fetch_quotas(names: list[str], *, workers: int | None = None) -> list[QuotaResult]:
    if len(names) <= 1:
        return [_quota_result(name) for name in names]

    results: list[QuotaResult | None] = [None] * len(names)
    worker_count = max(1, min(workers or LIVE_WORKERS, len(names)))
    with ThreadPoolExecutor(max_workers=worker_count) as pool:
        futures = {pool.submit(fetch_quota, name): (idx, name) for idx, name in enumerate(names)}
        for future in as_completed(futures):
            idx, name = futures[future]
            try:
                results[idx] = QuotaResult(name=name, snapshot=future.result())
            except Exception as exc:
                results[idx] = QuotaResult(name=name, error=exc)
    return [result for result in results if result is not None]


def _quota_result(name: str) -> QuotaResult:
    try:
        return QuotaResult(name=name, snapshot=fetch_quota(name))
    except Exception as exc:
        return QuotaResult(name=name, error=exc)
