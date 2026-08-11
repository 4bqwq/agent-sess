from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from sess.errors import MetadataError, ProviderError, TargetError
from sess.metadata import MetadataStore, validate_name
from sess.models import (
    Agent,
    Catalog,
    DeletionPreview,
    DeletionResult,
    Session,
    SessionTrace,
)
from sess.paths import same_directory
from sess.process import NativeProcessRunner
from sess.providers import ClaudeProvider, CodexProvider
from sess.providers.base import SessionProvider


MIN_SESSION_ID_PREFIX_LENGTH = 8


class SessionService:
    def __init__(
        self,
        project_dir: Path,
        providers: Sequence[SessionProvider] | None = None,
        metadata: MetadataStore | None = None,
        runner: NativeProcessRunner | None = None,
    ) -> None:
        self.project_dir = project_dir.resolve(strict=False)
        selected_providers = (
            tuple(providers)
            if providers is not None
            else (CodexProvider(), ClaudeProvider())
        )
        self.providers = {provider.agent: provider for provider in selected_providers}
        self.metadata = metadata or MetadataStore(self.project_dir)
        self.runner = runner or NativeProcessRunner()

    def catalog(self) -> Catalog:
        sessions: list[Session] = []
        warnings: list[str] = []
        for provider in self.providers.values():
            try:
                result = provider.discover(self.project_dir)
            except (OSError, ProviderError) as exc:
                warnings.append(f"{provider.agent.value}: {exc}")
                continue
            sessions.extend(
                session
                for session in result.sessions
                if same_directory(session.cwd, self.project_dir)
            )
            warnings.extend(result.warnings)

        names = self.metadata.read_names()
        named_sessions = [session.with_name(names.get(session.metadata_key)) for session in sessions]
        named_sessions.sort(
            key=lambda session: (session.updated_at, session.created_at, session.session_id),
            reverse=True,
        )
        return Catalog(tuple(named_sessions), tuple(warnings))

    def resolve(self, target: str, catalog: Catalog | None = None) -> Session:
        selected_catalog = catalog or self.catalog()
        id_matches = [
            session for session in selected_catalog.sessions if session.session_id == target
        ]
        if id_matches:
            return self._one_match(target, id_matches)

        name_matches = [
            session for session in selected_catalog.sessions if session.name == target
        ]
        if name_matches:
            return self._one_match(target, name_matches)

        normalized_target = target.lower()
        prefix_matches = [
            session
            for session in selected_catalog.sessions
            if session.session_id.lower().startswith(normalized_target)
        ]
        if prefix_matches:
            if len(target) < MIN_SESSION_ID_PREFIX_LENGTH:
                raise TargetError(
                    "session ID 前缀至少需要 "
                    f"{MIN_SESSION_ID_PREFIX_LENGTH} 个字符: {target!r}"
                )
            return self._one_match(target, prefix_matches)
        raise TargetError(
            "当前目录中找不到完整 session ID、完全匹配的名称或唯一 ID 前缀: "
            f"{target!r}"
        )

    @staticmethod
    def _one_match(target: str, matches: Sequence[Session]) -> Session:
        if len(matches) == 1:
            return matches[0]
        rendered = ", ".join(
            f"{session.agent.value}:{session.session_id}" for session in matches
        )
        raise TargetError(f"目标 {target!r} 对应多个会话: {rendered}")

    def rename(self, target: str, new_name: str) -> Session:
        validate_name(new_name)
        session = self.resolve(target)
        self.metadata.set_name(session.metadata_key, new_name)
        return session.with_name(new_name)

    def resume(self, target: str, native_args: Sequence[str] = ()) -> int:
        session = self.resolve(target)
        provider = self._provider_for(session.agent)
        return self.runner.run(provider.resume_argv(session.session_id, native_args))

    def resume_session(self, session: Session, native_args: Sequence[str] = ()) -> int:
        provider = self._provider_for(session.agent)
        return self.runner.run(provider.resume_argv(session.session_id, native_args))

    def fork(self, target: str, native_args: Sequence[str] = ()) -> int:
        session = self.resolve(target)
        provider = self._provider_for(session.agent)
        return self.runner.run(provider.fork_argv(session.session_id, native_args))

    def read_trace(self, target: str, all_turns: bool = False) -> SessionTrace:
        session = self.resolve(target)
        provider = self._provider_for(session.agent)
        return provider.read_trace(session, all_turns)

    def preview_delete(self, session: Session) -> DeletionPreview:
        return self._provider_for(session.agent).preview_delete(session)

    def delete(self, session: Session) -> DeletionResult:
        provider = self._provider_for(session.agent)
        result = provider.delete(session, self.runner)
        if not result.success:
            return result
        try:
            removed_name = self.metadata.remove(session.metadata_key)
        except MetadataError as exc:
            return DeletionResult(
                deleted=result.deleted,
                remaining=(f"sess 名称元数据 {self.metadata.path}: {exc}",),
            )
        if removed_name:
            return DeletionResult(
                deleted=(*result.deleted, f"sess 名称 {session.name!r}"),
                remaining=(),
            )
        return result

    def _provider_for(self, agent: Agent) -> SessionProvider:
        try:
            return self.providers[agent]
        except KeyError as exc:
            raise ProviderError(f"没有为 {agent.value} 配置 Provider") from exc
