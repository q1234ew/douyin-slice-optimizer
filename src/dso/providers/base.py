from __future__ import annotations

from typing import Protocol, runtime_checkable

from dso.providers.contracts import ProviderDescriptor, ProviderRequest, ProviderResult


@runtime_checkable
class PublicModelProvider(Protocol):
    @property
    def descriptor(self) -> ProviderDescriptor: ...

    def invoke(self, request: ProviderRequest) -> ProviderResult: ...


Provider = PublicModelProvider
