from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


class StorageRevisionConflictError(RuntimeError):
    """另一个写入者已经修改了集合，调用方的 expected_revision 已过期"""

    def __init__(self, collection: str, expected: str | None, current: str | None):
        super().__init__(f"{collection} revision conflict: expected {expected!r}, current {current!r}")
        self.collection = collection
        self.expected = expected
        self.current = current


@dataclass(frozen=True)
class StorageMutation:
    """一次增量写入：upserts 按身份字段覆盖，delete_keys 删除对应身份。

    expected_revision 为 None 时不做乐观并发检查。
    """

    upserts: tuple[dict[str, Any], ...] = ()
    delete_keys: tuple[str, ...] = ()
    expected_revision: str | None = None

    @property
    def empty(self) -> bool:
        return not self.upserts and not self.delete_keys


@dataclass(frozen=True)
class StorageMutationResult:
    revision: str | None
    inserted: int = 0
    updated: int = 0
    deleted: int = 0


@dataclass(frozen=True)
class StorageSnapshot:
    items: list[dict[str, Any]] = field(default_factory=list)
    revision: str | None = None


class StorageBackend(ABC):
    """抽象存储后端基类"""

    # 支持 mutate_accounts / load_accounts_revision 的后端置为 True；
    # 其余后端由调用方回退到 save_accounts 全量保存。
    supports_account_mutations = False

    @abstractmethod
    def load_accounts(self) -> list[dict[str, Any]]:
        """加载所有账号数据"""
        pass

    @abstractmethod
    def save_accounts(self, accounts: list[dict[str, Any]]) -> None:
        """保存所有账号数据"""
        pass

    def load_accounts_snapshot(self) -> StorageSnapshot:
        """加载账号数据及其修订号；不支持修订号的后端 revision 为 None"""
        return StorageSnapshot(items=self.load_accounts(), revision=None)

    def load_accounts_revision(self) -> str | None:
        """只读取账号集合的修订号，None 表示后端不支持"""
        return None

    def mutate_accounts(self, mutation: StorageMutation) -> StorageMutationResult:
        """增量写入账号；默认实现读出全量、合并后全量保存，不做修订号检查"""
        by_token: dict[str, dict[str, Any]] = {}
        for item in self.load_accounts():
            if isinstance(item, dict) and str(item.get("access_token") or "").strip():
                by_token[str(item["access_token"]).strip()] = item
        deleted = sum(by_token.pop(key, None) is not None for key in mutation.delete_keys)
        inserted = updated = 0
        for item in mutation.upserts:
            key = str(item.get("access_token") or "").strip()
            if not key:
                continue
            if key in by_token:
                updated += by_token[key] != item
            else:
                inserted += 1
            by_token[key] = item
        self.save_accounts(list(by_token.values()))
        return StorageMutationResult(revision=None, inserted=inserted, updated=updated, deleted=deleted)

    @abstractmethod
    def load_auth_keys(self) -> list[dict[str, Any]]:
        """加载所有鉴权密钥数据"""
        pass

    @abstractmethod
    def save_auth_keys(self, auth_keys: list[dict[str, Any]]) -> None:
        """保存所有鉴权密钥数据"""
        pass

    @abstractmethod
    def health_check(self) -> dict[str, Any]:
        """健康检查，返回存储后端状态"""
        pass

    @abstractmethod
    def get_backend_info(self) -> dict[str, Any]:
        """获取存储后端信息"""
        pass
