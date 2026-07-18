from __future__ import annotations

from threading import RLock

from dso.providers.base import PublicModelProvider
from dso.providers.contracts import ProviderDescriptor


class ProviderRegistryError(LookupError):
    pass


class ProviderRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, PublicModelProvider] = {}
        self._lock = RLock()

    def register(self, provider: PublicModelProvider, *, replace: bool = False) -> None:
        if not isinstance(provider, PublicModelProvider):
            raise TypeError("provider must implement PublicModelProvider")
        provider_id = provider.descriptor.identity.provider_id
        with self._lock:
            if provider_id in self._providers and not replace:
                raise ProviderRegistryError(f"provider {provider_id!r} is already registered")
            self._providers[provider_id] = provider

    def resolve(self, provider_id: str) -> PublicModelProvider:
        with self._lock:
            try:
                return self._providers[provider_id]
            except KeyError as exc:
                raise ProviderRegistryError(f"provider {provider_id!r} is not registered") from exc

    def unregister(self, provider_id: str) -> PublicModelProvider:
        with self._lock:
            try:
                return self._providers.pop(provider_id)
            except KeyError as exc:
                raise ProviderRegistryError(f"provider {provider_id!r} is not registered") from exc

    def descriptors(self) -> tuple[ProviderDescriptor, ...]:
        with self._lock:
            return tuple(
                self._providers[provider_id].descriptor
                for provider_id in sorted(self._providers)
            )

    def __contains__(self, provider_id: object) -> bool:
        with self._lock:
            return provider_id in self._providers


provider_registry = ProviderRegistry()
