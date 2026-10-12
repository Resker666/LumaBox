from dataclasses import dataclass, field


@dataclass(frozen=True)
class InputLink:
    url: str = field(repr=False)
    work_id: str | None
    kind: str | None


@dataclass(frozen=True)
class MediaResource:
    url: str = field(repr=False)
    order: int


@dataclass(frozen=True)
class Work:
    work_id: str
    kind: str
    resources: tuple[MediaResource, ...]


@dataclass(frozen=True)
class VerifiedFile:
    name: str
    bytes: int
    sha256: str
    width: int
    height: int
    duration: float | None
