"""工作区文件服务 — Diff 卡片后端支持。

管理文件编辑的完整生命周期，为插件的 AIDevFilePanel 提供：
1. 工作区文件状态跟踪（GENERATING → APPLIED → ACCEPTED/REJECTED）
2. 文件内容存储（编辑前后的完整内容，供 getLastStableContent/getFullContent 使用）
3. DiffInfo 计算（基于 difflib 的行级/字符级变更统计）
4. 快照管理（每个 chat request 一个 snapshot，关联所有文件变更）

数据结构严格匹配插件模型：
- WorkingSpaceFileInfo.java: id, sessionId, snapshotId, fileId, mode, version, diffInfo, ...
- DiffInfo.java: add, delete, addChars, delChars
- SnapshotInfo.java: id, sessionId, requestId, name, description, status
"""

from __future__ import annotations

import difflib
import threading
import uuid
from dataclasses import dataclass, field

from loguru import logger


@dataclass
class DiffInfo:
    """行级 + 字符级变更统计，匹配插件 DiffInfo.java。"""
    add: int = 0
    delete: int = 0
    addChars: int = 0
    delChars: int = 0

    def to_dict(self) -> dict:
        return {"add": self.add, "delete": self.delete,
                "addChars": self.addChars, "delChars": self.delChars}


@dataclass
class WorkspaceFile:
    """单个工作区文件的完整状态。"""
    id: str
    session_id: str
    snapshot_id: str
    file_id: str          # = 文件绝对路径，用于 UI 匹配
    mode: str             # ADD / MODIFIED / DELETE
    version: str = "1"
    status: str = "GENERATING"
    message: str = ""
    content: str = ""               # 编辑后内容（fullContent）
    last_stable_content: str = ""   # 编辑前内容（lastStableContent）
    diff_info: DiffInfo = field(default_factory=DiffInfo)
    last_diff_info: DiffInfo | None = None
    version_count: str = "1"
    tool_call_id: str = ""

    def to_wire_format(self) -> dict:
        """转换为插件 WorkingSpaceFileInfo 的 JSON 格式。"""
        return {
            "id": self.id,
            "sessionId": self.session_id,
            "snapshotId": self.snapshot_id,
            "fileId": self.file_id,
            "mode": self.mode,
            "version": self.version,
            "diffInfo": self.diff_info.to_dict(),
            "lastDiffInfo": self.last_diff_info.to_dict() if self.last_diff_info else None,
            "versionCount": self.version_count,
            "status": self.status,
            "message": self.message,
            "content": self.content,
        }


@dataclass
class SnapshotInfo:
    """快照信息，匹配插件 SnapshotInfo.java。"""
    id: str
    session_id: str
    request_id: str
    name: str = "Snapshot"
    description: str = ""
    status: str = "ACTIVE"

    def to_dict(self) -> dict:
        # 插件端状态枚举不包含 ACTIVE，统一映射到可识别状态，避免按钮面板失效。
        status_map = {
            "ACTIVE": "ACTIVE_INIT",
        }
        wire_status = status_map.get((self.status or "").upper(), self.status)
        return {
            "id": self.id,
            "sessionId": self.session_id,
            "requestId": self.request_id,
            "name": self.name,
            "description": self.description,
            "status": wire_status,
        }


class WorkspaceFileService:
    """工作区文件服务（per-router 实例，与 LSP4JRouter 生命周期一致）。"""

    def __init__(self) -> None:
        # 与 jsonrpc_router 的 create_task 并发配合：保护 _files / snapshot 等可变状态
        self._lock = threading.RLock()
        # file_id(=file_path) → WorkspaceFile
        self._files: dict[str, WorkspaceFile] = {}
        # workspace_file_id → WorkspaceFile（按 UUID 索引，用于 getLastStableContent 等）
        self._files_by_id: dict[str, WorkspaceFile] = {}
        # request_id → snapshot_id
        self._snapshots: dict[str, SnapshotInfo] = {}
        # 当前活跃 snapshot_id（全局，兼容旧逻辑；新代码优先用 _current_snapshot_by_session）
        self._current_snapshot_id: str | None = None
        # session_id → 该会话最近一次使用的 snapshot_id（避免多会话共用全局 current 时 syncAll 文件列表为空）
        self._current_snapshot_by_session: dict[str, str] = {}
        # 文件内容缓存：file_path → content（由 read_file 结果填充）
        self._file_content_cache: dict[str, str] = {}
        # snapshot_id → request_id（反向索引）
        self._snapshot_to_request: dict[str, str] = {}

    def cache_file_content(self, file_path: str, content: str) -> None:
        """缓存 read_file 结果，供后续编辑工具使用。"""
        with self._lock:
            if file_path and content is not None:
                self._file_content_cache[file_path] = content
                logger.debug("[WS-FILE] 缓存文件内容: path={} len={}", file_path, len(content))

    def get_cached_content(self, file_path: str) -> str | None:
        """获取缓存的文件内容。"""
        with self._lock:
            return self._file_content_cache.get(file_path)

    def get_or_create_snapshot(self, session_id: str, request_id: str) -> SnapshotInfo:
        """获取或创建与 request 关联的 snapshot。"""
        with self._lock:
            if request_id in self._snapshots:
                snap = self._snapshots[request_id]
                self._current_snapshot_by_session[session_id] = snap.id
                self._current_snapshot_id = snap.id
                return snap

            snapshot_id = str(uuid.uuid4())
            idx = len(self._snapshots) + 1
            snap = SnapshotInfo(
                id=snapshot_id,
                session_id=session_id,
                request_id=request_id,
                name=f"Snapshot {idx}",
            )
            self._snapshots[request_id] = snap
            self._snapshot_to_request[snapshot_id] = request_id
            self._current_snapshot_id = snapshot_id
            self._current_snapshot_by_session[session_id] = snapshot_id
            logger.info("[WS-FILE] 创建 snapshot: id={} requestId={} idx={}",
                        snapshot_id[:8], request_id[:8], idx)
            return snap

    def create_or_update_file(
        self,
        session_id: str,
        snapshot_id: str,
        file_id: str,
        mode: str,
        tool_call_id: str = "",
    ) -> WorkspaceFile:
        """创建或更新工作区文件记录。"""
        with self._lock:
            existing = self._files.get(file_id)
            if existing and existing.snapshot_id == snapshot_id:
                # 同一 snapshot 内多次编辑同一文件：更新版本
                old_version = int(existing.version)
                existing.version = str(old_version + 1)
                existing.version_count = existing.version
                existing.last_diff_info = DiffInfo(
                    add=existing.diff_info.add,
                    delete=existing.diff_info.delete,
                    addChars=existing.diff_info.addChars,
                    delChars=existing.diff_info.delChars,
                )
                existing.tool_call_id = tool_call_id
                logger.info("[WS-FILE] 更新文件版本: fileId={} version={}", file_id, existing.version)
                return existing

            ws_file = WorkspaceFile(
                id=str(uuid.uuid4()),
                session_id=session_id,
                snapshot_id=snapshot_id,
                file_id=file_id,
                mode=mode,
                tool_call_id=tool_call_id,
            )
            self._files[file_id] = ws_file
            self._files_by_id[ws_file.id] = ws_file
            logger.info("[WS-FILE] 创建工作区文件: id={} fileId={} mode={} snapshot={}",
                        ws_file.id[:8], file_id, mode, snapshot_id[:8])
            return ws_file

    def set_content(
        self,
        file_id: str,
        last_stable_content: str,
        full_content: str,
    ) -> DiffInfo:
        """设置文件的编辑前后内容，并计算 DiffInfo。"""
        with self._lock:
            ws_file = self._files.get(file_id)
            if not ws_file:
                logger.warning("[WS-FILE] set_content: 文件不存在 fileId={}", file_id)
                return DiffInfo()

            ws_file.last_stable_content = last_stable_content
            ws_file.content = full_content
            ws_file.diff_info = self.compute_diff_info(last_stable_content, full_content)
            logger.info("[WS-FILE] 设置内容: fileId={} old_len={} new_len={} +{} -{}",
                        file_id, len(last_stable_content), len(full_content),
                        ws_file.diff_info.add, ws_file.diff_info.delete)
            return ws_file.diff_info

    def update_status(self, file_id: str, status: str, message: str = "") -> WorkspaceFile | None:
        """更新文件状态。"""
        with self._lock:
            ws_file = self._files.get(file_id)
            if not ws_file:
                ws_file_by_uuid = self._files_by_id.get(file_id)
                if ws_file_by_uuid:
                    ws_file = ws_file_by_uuid
                else:
                    logger.warning("[WS-FILE] update_status: 文件不存在 id={}", file_id)
                    return None
            ws_file.status = status
            if message:
                ws_file.message = message
            logger.info("[WS-FILE] 状态更新: fileId={} status={}", ws_file.file_id, status)
            return ws_file

    def get_file(self, file_id: str) -> WorkspaceFile | None:
        """按 file_id（文件路径）查找。"""
        with self._lock:
            return self._files.get(file_id)

    def get_file_by_id(self, ws_id: str) -> WorkspaceFile | None:
        """按工作区文件 UUID 查找。"""
        with self._lock:
            return self._files_by_id.get(ws_id)

    def list_by_snapshot(self, snapshot_id: str) -> list[WorkspaceFile]:
        """列出某个 snapshot 下的所有文件。"""
        with self._lock:
            return [f for f in self._files.values() if f.snapshot_id == snapshot_id]

    def operate(self, ws_id: str, op_type: str, content: str | None = None) -> bool:
        """执行 accept/reject 操作。"""
        with self._lock:
            ws_file = self._files_by_id.get(ws_id)
            if not ws_file:
                logger.warning("[WS-FILE] operate: 文件不存在 id={}", ws_id)
                return False

            if op_type.upper() in ("ACCEPT", "ACCEPTED"):
                ws_file.status = "ACCEPTED"
                if content is not None:
                    ws_file.content = content
            elif op_type.upper() in ("REJECT", "REJECTED"):
                ws_file.status = "REJECTED"
            else:
                logger.warning("[WS-FILE] operate: 未知操作类型 op={}", op_type)
                return False

            logger.info("[WS-FILE] 操作完成: id={} op={} status={}",
                        ws_id[:8], op_type, ws_file.status)
            return True

    def update_content(self, ws_id: str, content: str | None, local_content: str | None) -> bool:
        """更新文件内容（UpdateWorkingSpaceFileContentRequest）。"""
        with self._lock:
            ws_file = self._files_by_id.get(ws_id)
            if not ws_file:
                logger.warning("[WS-FILE] update_content: 文件不存在 id={}", ws_id)
                return False

            if content is not None:
                ws_file.content = content
            if local_content is not None:
                ws_file.last_stable_content = local_content
            # 重新计算 DiffInfo
            ws_file.diff_info = self.compute_diff_info(
                ws_file.last_stable_content, ws_file.content)
            logger.info("[WS-FILE] 更新内容: id={} +{} -{}", ws_id[:8],
                        ws_file.diff_info.add, ws_file.diff_info.delete)
            return True

    def get_all_snapshots(self) -> list[SnapshotInfo]:
        """获取所有 snapshot。"""
        with self._lock:
            return list(self._snapshots.values())

    def list_snapshots_for_session(self, session_id: str) -> list[SnapshotInfo]:
        """按会话 ID 列出快照（snapshot/listBySession）。"""
        with self._lock:
            if not session_id:
                return []
            return [s for s in self._snapshots.values() if s.session_id == session_id]

    def find_snapshot_by_id(self, snapshot_id: str) -> SnapshotInfo | None:
        """按快照 UUID 查找。"""
        with self._lock:
            if not snapshot_id:
                return None
            for s in self._snapshots.values():
                if s.id == snapshot_id:
                    return s
            return None

    def set_current_snapshot(self, snapshot_id: str) -> bool:
        """将给定快照设为当前会话的快照（SWITCH / ACTIVATE）。"""
        with self._lock:
            if not snapshot_id:
                return False
            for s in self._snapshots.values():
                if s.id == snapshot_id:
                    self._current_snapshot_by_session[s.session_id] = snapshot_id
                    self._current_snapshot_id = snapshot_id
                    return True
            return False

    def apply_snapshot_accept_all(self, snapshot_id: str) -> int:
        """快照下全部工作区文件标为已接受。"""
        with self._lock:
            n = 0
            for f in self._files.values():
                if f.snapshot_id != snapshot_id:
                    continue
                f.status = "ACCEPTED"
                logger.info("[WS-FILE] 状态更新: fileId={} status={}", f.file_id, "ACCEPTED")
                n += 1
            return n

    def apply_snapshot_reject_all(self, snapshot_id: str) -> int:
        """快照下全部工作区文件标为已拒绝。"""
        with self._lock:
            n = 0
            for f in self._files.values():
                if f.snapshot_id != snapshot_id:
                    continue
                f.status = "REJECTED"
                logger.info("[WS-FILE] 状态更新: fileId={} status={}", f.file_id, "REJECTED")
                n += 1
            return n

    def build_sync_result(
        self,
        ws_file: WorkspaceFile,
        project_path: str,
        sync_type: str = "MODIFIED",
    ) -> dict:
        """构建 WorkspaceFileSyncResult 格式（匹配插件模型）。"""
        with self._lock:
            return {
                "type": sync_type,
                "projectPath": project_path,
                "isStream": False,
                "workingSpaceFile": ws_file.to_wire_format(),
            }

    def build_snapshot_sync_all(
        self,
        session_id: str,
        project_path: str,
        sync_type: str = "ADD",
    ) -> dict:
        """构建 SnapshotSyncAllResult 格式。"""
        with self._lock:
            snapshots = [s for s in self._snapshots.values() if s.session_id == session_id]
            current = self._current_snapshot_by_session.get(session_id) or ""
            if current:
                all_files = [
                    f for f in self._files.values()
                    if f.session_id == session_id and f.snapshot_id == current
                ]
            else:
                all_files = [f for f in self._files.values() if f.session_id == session_id]
            return {
                "snapshots": [s.to_dict() for s in snapshots],
                "workingSpaceFiles": [f.to_wire_format() for f in all_files],
                "currentSnapshotId": current,
                "currentSessionId": session_id,
                "type": sync_type,
                "projectPath": project_path,
            }

    @staticmethod
    def compute_diff_info(old_content: str, new_content: str) -> DiffInfo:
        """计算两段内容之间的 DiffInfo（行级 + 字符级）。"""
        old_lines = old_content.splitlines(keepends=True) if old_content else []
        new_lines = new_content.splitlines(keepends=True) if new_content else []

        added_lines = 0
        deleted_lines = 0
        for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(
            None, old_lines, new_lines
        ).get_opcodes():
            if tag == "insert":
                added_lines += j2 - j1
            elif tag == "delete":
                deleted_lines += i2 - i1
            elif tag == "replace":
                added_lines += j2 - j1
                deleted_lines += i2 - i1

        added_chars = max(0, len(new_content) - len(old_content))
        deleted_chars = max(0, len(old_content) - len(new_content))

        return DiffInfo(
            add=added_lines,
            delete=deleted_lines,
            addChars=added_chars,
            delChars=deleted_chars,
        )
