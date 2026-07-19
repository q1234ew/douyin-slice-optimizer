"""Fail-closed policy primitives for public model providers.

This module deliberately contains no provider SDK imports.  It only decides
whether a caller is allowed to cross the local/public boundary.  Secret values
are resolved transiently from the process environment and are never included
in policy objects, decisions, logs, caches, or database records.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import os
import re


_ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")


class UploadLevel(str, Enum):
    """The most sensitive representation that may leave the local machine."""

    STRUCTURED_SUMMARY = "structured_summary"
    REPRESENTATIVE_FRAMES = "representative_frames"
    SHORT_AUDIO_WINDOW = "short_audio_window"
    SHORT_VIDEO_WINDOW = "short_video_window"
    FULL_MEDIA = "full_media"


class PolicyDenied(RuntimeError):
    """A public-model request was rejected before any network call."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class SecretEnvRef:
    """Reference to a secret environment variable, never the secret itself."""

    env_var: str

    def __post_init__(self) -> None:
        if not _ENV_NAME.fullmatch(self.env_var):
            raise ValueError("secret env_var must be an uppercase environment variable name")

    @property
    def is_configured(self) -> bool:
        return bool(os.environ.get(self.env_var))

    def resolve(self) -> str:
        """Return the secret for immediate SDK use without retaining it here."""

        value = os.environ.get(self.env_var)
        if not value:
            raise PolicyDenied(
                "secret_not_configured",
                f"required secret environment variable {self.env_var} is not configured",
            )
        return value


@dataclass(frozen=True, slots=True)
class DataPermission:
    """Explicit authorization for data to leave the local environment."""

    may_leave_local: bool = False
    authorization_basis: str = ""
    allowed_upload_levels: frozenset[UploadLevel] = field(default_factory=frozenset)
    redaction_strategy: str = ""
    retention_days: int | None = None
    retention_policy_reference: str = ""

    def __post_init__(self) -> None:
        levels = frozenset(UploadLevel(level) for level in self.allowed_upload_levels)
        object.__setattr__(self, "allowed_upload_levels", levels)
        if self.retention_days is not None and (
            isinstance(self.retention_days, bool)
            or not isinstance(self.retention_days, int)
            or self.retention_days < 0
        ):
            raise ValueError("retention_days must be a non-negative integer or None")
        if not self.may_leave_local:
            if levels:
                raise ValueError("upload levels cannot be allowed while may_leave_local is false")
            return
        if not self.authorization_basis.strip():
            raise ValueError("authorization_basis is required when data may leave local")
        if not levels:
            raise ValueError("at least one explicit upload level is required")
        if not self.redaction_strategy.strip():
            raise ValueError("redaction_strategy is required when data may leave local")
        if not self.retention_policy_reference.strip():
            raise ValueError(
                "retention_policy_reference is required when data may leave local"
            )

    def permits(self, upload_level: UploadLevel) -> bool:
        return self.may_leave_local and UploadLevel(upload_level) in self.allowed_upload_levels


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    allowed: bool
    code: str
    provider: str
    upload_level: UploadLevel


@dataclass(frozen=True, slots=True)
class PublicModelPolicy:
    """Provider policy that remains disabled unless every gate is explicit."""

    provider: str = ""
    enabled: bool = False
    secret: SecretEnvRef | None = None
    budget_configured: bool = False
    data_permission: DataPermission = field(default_factory=DataPermission)

    def __post_init__(self) -> None:
        if self.enabled and not self.provider.strip():
            raise ValueError("provider is required when public models are enabled")

    def decision(self, upload_level: UploadLevel) -> PolicyDecision:
        level = UploadLevel(upload_level)
        code = "allowed"
        allowed = True
        if not self.enabled:
            code, allowed = "public_models_disabled", False
        elif self.secret is None or not self.secret.is_configured:
            code, allowed = "secret_not_configured", False
        elif not self.budget_configured:
            code, allowed = "budget_not_configured", False
        elif not self.data_permission.may_leave_local:
            code, allowed = "data_export_not_permitted", False
        elif not self.data_permission.permits(level):
            code, allowed = "upload_level_not_permitted", False
        return PolicyDecision(
            allowed=allowed,
            code=code,
            provider=self.provider,
            upload_level=level,
        )

    def authorize(self, upload_level: UploadLevel) -> PolicyDecision:
        decision = self.decision(upload_level)
        if not decision.allowed:
            raise PolicyDenied(decision.code, f"public model request denied: {decision.code}")
        return decision
