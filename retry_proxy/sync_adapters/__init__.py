from .base import PoolSyncAdapter, PoolSyncError
from .new_api import NewAPIAdapter
from .sub2api import Sub2APIAdapter


ADAPTERS = {
    Sub2APIAdapter.name: Sub2APIAdapter(),
    NewAPIAdapter.name: NewAPIAdapter(),
}


__all__ = ["ADAPTERS", "PoolSyncAdapter", "PoolSyncError"]
