from .base import PoolSyncAdapter, PoolSyncError
from .sub2api import Sub2APIAdapter


ADAPTERS = {
    Sub2APIAdapter.name: Sub2APIAdapter(),
}


__all__ = ["ADAPTERS", "PoolSyncAdapter", "PoolSyncError"]
