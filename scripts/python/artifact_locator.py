"""Controller-owned, flat registry fields locating an external deliverable.

The five fields are ``work_repo_root``, ``work_base_branch``, ``board_ref``,
``deliverable_relpath`` and ``deliverable_sha256``. Squad tasks use nulls;
external registration sets only the root/branch pair. Publication fills all
five. Missing fields in legacy entries mean null, never a guessed repository.

Durable-write contract for Stage 2b: after committing the deliverable to
board/<TASK-ID>, verify the committed blob's SHA-256, then validate and persist
the complete locator under the shared registry lock using an atomic, fsynced
write (including directory sync). Only AFTER that succeeds may the controller
publish the envelope: it is the watcher-visible commit marker. Ref/path/hash
come from controller-verified output, never worker-authored locator fields.
An already published locator must not be silently replaced on a retry.

This module validates shape and publication immutability. It does not open a
checkout, read a Git blob, verify its hash, acquire the registry lock, or publish
anything. Those operations
and enforcement of the write order belong to the later publisher, which must
call publish_over against the existing locator while holding that lock.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, fields
import re


MAX_FIELD_LENGTH = 4096


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{name} must be a non-empty, unpadded string")
    if len(value) > MAX_FIELD_LENGTH:
        raise ValueError(f"{name} must not exceed {MAX_FIELD_LENGTH} characters")
    # Includes the non-ASCII LINE_SPLIT_LOOKALIKES in send-task-input-guards.sh.
    if any(ord(char) < 32 or ord(char) == 127 or char in "\x85\u2028\u2029"
           for char in value):
        raise ValueError(f"{name} contains a control character")
    return value


def _branch(value: object, name: str) -> str:
    value = _text(value, name)
    # Git branch syntax, without invoking Git or resolving revision expressions.
    if (value.startswith(("-", "refs/")) or value == "HEAD" or value.endswith(".")
            or any(token in value for token in ("..", "@{"))
            or any(char in value for char in " ~^:?*[\\")
            or any(not part or part == "@" or part.startswith(".") or part.endswith(".lock")
                   for part in value.split("/"))):
        raise ValueError(f"{name} must be a literal Git branch name")
    return value


@dataclass(frozen=True)
class ArtifactLocator:
    work_repo_root: str | None = None
    work_base_branch: str | None = None
    board_ref: str | None = None
    deliverable_relpath: str | None = None
    deliverable_sha256: str | None = None

    def __post_init__(self) -> None:
        ownership = (self.work_repo_root, self.work_base_branch)
        deliverable = (self.board_ref, self.deliverable_relpath, self.deliverable_sha256)
        if any(value is not None for value in ownership):
            root = _text(self.work_repo_root, "work_repo_root")
            if (not root.startswith("/") or root.startswith("//") or "\\" in root
                    or (root != "/" and any(part in ("", ".", "..")
                                           for part in root[1:].split("/")))):
                raise ValueError("work_repo_root must be a normalized absolute POSIX path")
            _branch(self.work_base_branch, "work_base_branch")
        if any(value is not None for value in deliverable):
            if self.work_repo_root is None:
                raise ValueError("deliverable fields require external repository ownership")
            ref = _branch(self.board_ref, "board_ref")
            if not ref.startswith("board/TASK-") or ref.count("/") != 1 or ref == "board/TASK-":
                raise ValueError("board_ref must be board/<TASK-ID>")
            path = _text(self.deliverable_relpath, "deliverable_relpath")
            if (path.startswith("-") or "\\" in path or re.match(r"^[A-Za-z]:", path)
                    or any(part in ("", ".", "..", ".git") for part in path.split("/"))):
                raise ValueError("deliverable_relpath must be a normalized repository-relative file path")
            digest = _text(self.deliverable_sha256, "deliverable_sha256")
            if not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ValueError("deliverable_sha256 must be 64 lowercase hexadecimal characters")

    @classmethod
    def from_registry(cls, entry: Mapping[str, object]) -> ArtifactLocator:
        """Read only locator fields; other registry metadata is not schema input."""
        if not isinstance(entry, Mapping):
            raise ValueError("registry entry must be a mapping")
        return cls(**{field.name: entry.get(field.name) for field in fields(cls)})

    def to_dict(self) -> dict[str, str | None]:
        """Return the five flat registry fields, with explicit JSON-ready nulls."""
        return asdict(self)

    def require_deliverable(self, task_id: str) -> ArtifactLocator:
        """Reject pending/legacy state or a locator belonging to another task."""
        if self.board_ref is None:
            raise ValueError("a complete external deliverable locator is required")
        if self.board_ref != f"board/{task_id}":
            raise ValueError("board_ref does not match the task id")
        return self

    def publish_over(self, existing: ArtifactLocator) -> ArtifactLocator:
        """Reject replacement of a published locator; allow identical retries."""
        if existing.board_ref is not None and existing.to_dict() != self.to_dict():
            raise ValueError("a published deliverable locator cannot be replaced")
        return self
