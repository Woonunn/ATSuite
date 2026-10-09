from abc import ABC, abstractmethod
from typing import List

class StorageBase(ABC):
    @abstractmethod
    def upload(self, key: str, filepath: str):
        pass

    @abstractmethod
    def download(self, key: str, filepath: str):
        pass

    @abstractmethod
    def append(self, key: str, data) -> int:
        """Append data to object, return next position"""
        pass

    @abstractmethod
    def read(self, key: str) -> str:
        """Read whole object as text"""
        pass

    @abstractmethod
    def deleteobj(self, key: str) -> None:
        pass

    @abstractmethod
    def clearobj(self, key: str) -> None:
        pass

def create_storage(provider: str, **kwargs) -> StorageBase:
    if provider.startswith("ali"):
        try:
            from atsuite_sdk.oss import AliOSS
        except ModuleNotFoundError:
            from atsuite.ali.oss import AliOSS
        return AliOSS(**kwargs)
    if provider.startswith("aws"):
        try:
            from atsuite_sdk.s3 import AWSS3
        except ModuleNotFoundError:
            from atsuite.aws.s3 import AWSS3
        return AWSS3(**kwargs)
    if provider.startswith("gcp"):
        from atsuite_sdk.gcs import GCPStorage
        kwargs.pop("create_bucket", None)
        return GCPStorage(**kwargs)
    raise ValueError(f"Unknown storage provider: {provider}")
