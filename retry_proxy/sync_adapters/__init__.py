from .aihub import AIHubAdapter
from .base import PoolSyncAdapter, PoolSyncError
from .manual import ManualAdapter
from .new_api import NewAPIAdapter
from .sub2api import Sub2APIAdapter


ADAPTERS = {
    ManualAdapter.name: ManualAdapter(),
    Sub2APIAdapter.name: Sub2APIAdapter(),
    AIHubAdapter.name: AIHubAdapter(),
    NewAPIAdapter.name: NewAPIAdapter(),
}


__all__ = ["ADAPTERS", "PoolSyncAdapter", "PoolSyncError"]
