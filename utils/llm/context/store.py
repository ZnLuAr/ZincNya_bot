"""保存工作上下文、任务与操作记录，并在启动时标记上次未完成的工作。"""

import asyncio
import json
import time
from uuid import uuid4

from utils.core.crypto import encryptText, decryptText
from utils.core.database import Database
from utils.core.logger import logSystemEvent, LogLevel
from utils.core.schema import loadSchema




_TASK_FINAL_STATUSES = frozenset({
    "completed", "limitReached", "stopped", "failed", "interrupted",
})
_OPERATION_FINAL_STATUSES = frozenset({
    "done", "submitted", "failed", "uncertain", "skipped",
})




class ContextStorageError(RuntimeError):
    """工作上下文的存储失败、数据损坏或状态转换被拒绝。"""


class _StateConflict(ContextStorageError):
    """业务状态冲突；原事务回滚，存储仍可接收后续工作。"""




class ContextStore:
    """以实例锁和 SQLite 事务串行保存记录，实例由应用状态管理器持有。"""


    def __init__(self, db: Database):
        self._db = db
        self._lock = asyncio.Lock()
        self._initialized = False
        self._paused = False


    def requireWritable(self) -> None:
        """同步检查初始化和暂停状态，供模型调用前确认存储可用。"""
        if not self._initialized:
            raise ContextStorageError("工作上下文存储尚未初始化")
        if self._paused:
            raise ContextStorageError("工作上下文存储已暂停，需要健康试写恢复")


    async def _run(self, callback, *, write=False, allowPaused=False):
        async with self._lock:
            if not self._initialized:
                raise ContextStorageError("工作上下文存储尚未初始化")
            if write and not allowPaused:
                self.requireWritable()
            return await self._waitForWrite(self._guard(callback, write=write))


    async def _guard(self, callback, *, write):
        try:
            return await callback()
        except _StateConflict:
            raise
        except Exception as error:
            if write:
                self._paused = True
                # 异常类型足以定位故障层，正文和参数留在加密记录中。
                try:
                    await logSystemEvent(
                        "工作上下文存储写入失败", type(error).__name__, LogLevel.ERROR,
                    )
                except Exception:
                    pass
            raise ContextStorageError("工作上下文存储写入失败" if write else "工作上下文读取失败") from error


    async def _waitForWrite(self, awaitable):
        """取消调用时等候后台事务完成，使锁覆盖提交、回滚和暂停状态更新。"""
        worker = asyncio.create_task(awaitable)
        cancelled = False
        while True:
            try:
                result = await asyncio.shield(worker)
                break
            except asyncio.CancelledError:
                cancelled = True
                if worker.done():
                    # 取走后台异常，调用者仍收到原来的取消信号。
                    if not worker.cancelled():
                        worker.exception()
                    raise
            except Exception:
                if cancelled:
                    raise asyncio.CancelledError() from None
                raise
        if cancelled:
            raise asyncio.CancelledError()
        return result


    def _transaction(self, conn):
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")


    def _context(self, conn, contextID):
        row = conn.execute("SELECT * FROM contexts WHERE contextID=?", (contextID,)).fetchone()
        if row is None:
            raise _StateConflict("工作上下文不存在")
        return row


    def _task(self, conn, contextID, taskID):
        self._context(conn, contextID)
        row = conn.execute("SELECT * FROM tasks WHERE taskID=?", (taskID,)).fetchone()
        if row is None or row["contextID"] != contextID:
            raise _StateConflict("任务与工作上下文不匹配")
        if row["status"] != "running":
            raise _StateConflict("任务已经结束")
        return row


    def _encode(self, content):
        if not isinstance(content, dict):
            raise _StateConflict("工作上下文内容必须是对象")
        return encryptText(json.dumps(content, ensure_ascii=False, allow_nan=False))


    def _decode(self, content):
        decoded = json.loads(decryptText(content))
        if not isinstance(decoded, dict):
            raise ContextStorageError("工作上下文内容损坏")
        return decoded


    def _touch(self, conn, contextID):
        conn.execute("UPDATE contexts SET lastActivity=? WHERE contextID=?", (time.time(), contextID))




    async def initialize(self) -> None:
        """初始化表结构，每个实例启动时恢复一次上次未完成的状态。"""
        async with self._lock:
            if self._initialized:
                return

            async def initializeStore():
                schema = loadSchema("llmContext")
                await asyncio.to_thread(self._db.initSchema, lambda conn: conn.executescript(schema))

                def recover(conn):
                    self._transaction(conn)
                    conn.execute("UPDATE tasks SET status='interrupted' WHERE status='running'")
                    conn.execute("UPDATE records SET status='uncertain' WHERE status='executing'")
                    conn.execute("UPDATE records SET status='expired' WHERE status='submitted'")

                await self._db.run(recover)
                self._initialized = True
                self._paused = False

            await self._waitForWrite(self._guard(initializeStore, write=True))


    async def checkWritable(self) -> None:
        """暂停时提交独立健康试写，成功后恢复新任务和新操作。"""
        async def probe():
            if not self._paused:
                return

            def writeProbe(conn):
                self._transaction(conn)
                cursor = conn.execute("UPDATE storageHealth SET probe=1-probe WHERE id=1")
                if cursor.rowcount != 1:
                    raise ContextStorageError("工作上下文健康记录缺失")

            await self._db.run(writeProbe)
            self._paused = False

        await self._run(probe, write=True, allowPaused=True)




    async def createContext(self, chatID, *, shared=False) -> str:
        """创建独立上下文；shared 模式原子获取或创建聊天的当前上下文。"""
        def create(conn):
            self._transaction(conn)
            if shared:
                current = conn.execute(
                    "SELECT contextID FROM currentContexts WHERE chatID=?", (str(chatID),),
                ).fetchone()
                if current is not None:
                    self._context(conn, current["contextID"])
                    return current["contextID"]
            contextID = str(uuid4())
            conn.execute(
                "INSERT INTO contexts(contextID, chatID, lastActivity) VALUES (?, ?, ?)",
                (contextID, str(chatID), time.time()),
            )
            if shared:
                conn.execute(
                    "INSERT INTO currentContexts(chatID, contextID) VALUES (?, ?)",
                    (str(chatID), contextID),
                )
            return contextID

        return await self._run(lambda: self._db.run(create), write=True)


    async def createTask(self, contextID, taskID) -> None:
        """绑定任务到上下文，并保存 running 初始状态。"""
        def create(conn):
            self._transaction(conn)
            self._context(conn, contextID)
            if not isinstance(taskID, str) or not taskID:
                raise _StateConflict("任务 ID 必须是非空字符串")
            if conn.execute("SELECT 1 FROM tasks WHERE taskID=?", (taskID,)).fetchone():
                raise _StateConflict("任务已经存在")
            conn.execute(
                "INSERT INTO tasks(taskID, contextID, status, metadata) VALUES (?, ?, 'running', ?)",
                (taskID, contextID, self._encode({})),
            )
            self._touch(conn, contextID)

        await self._run(lambda: self._db.run(create), write=True)


    async def beginOperation(self, contextID, taskID, operationIndex, name, args, *, status="executing") -> None:
        """在副作用之前提交操作参数；skipped 用于记录同批尚未派发的操作。"""
        def begin(conn):
            self._transaction(conn)
            self._task(conn, contextID, taskID)
            if status not in {"executing", "skipped"}:
                raise _StateConflict("无效的操作开始状态")
            if type(operationIndex) is not int or operationIndex < 1:
                raise _StateConflict("操作序号必须是正整数")
            if not isinstance(name, str) or not name or not isinstance(args, dict):
                raise _StateConflict("操作名称或参数无效")
            if conn.execute(
                "SELECT 1 FROM records WHERE contextID=? AND taskID=? AND operationIndex=?",
                (contextID, taskID, operationIndex),
            ).fetchone():
                raise _StateConflict("操作已经存在")
            position = conn.execute(
                "SELECT COALESCE(MAX(position), 0)+1 FROM records WHERE contextID=?", (contextID,),
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO records VALUES (?, ?, ?, ?, 'operation', ?, ?, ?)",
                (contextID, position, taskID, operationIndex, status,
                 self._encode({"name": name, "args": args}), time.time()),
            )
            self._touch(conn, contextID)

        await self._run(lambda: self._db.run(begin), write=True)


    async def finishOperation(self, contextID, taskID, operationIndex, status, content: dict) -> None:
        """把执行中的记录更新为实际结果；暂停期间仍接收在途结果。"""
        def finish(conn):
            self._transaction(conn)
            self._task(conn, contextID, taskID)
            if status not in _OPERATION_FINAL_STATUSES or not isinstance(content, dict):
                raise _StateConflict("无效的操作结果")
            row = conn.execute(
                "SELECT * FROM records WHERE contextID=? AND taskID=? AND operationIndex=?",
                (contextID, taskID, operationIndex),
            ).fetchone()
            if row is None or row["status"] != "executing":
                raise _StateConflict("操作不存在或已经完成")
            savedContent = self._decode(row["content"])
            savedContent["result"] = content
            conn.execute(
                "UPDATE records SET status=?, content=? WHERE contextID=? AND taskID=? AND operationIndex=?",
                (status, self._encode(savedContent), contextID, taskID, operationIndex),
            )
            self._touch(conn, contextID)

        await self._run(lambda: self._db.run(finish), write=True, allowPaused=True)


    async def finishTask(self, taskID, status, modelCalls) -> None:
        """保存任务终态；中断任务的在途操作标为 uncertain，已送审记录保留。"""
        def finish(conn):
            self._transaction(conn)
            if status not in _TASK_FINAL_STATUSES or type(modelCalls) is not int or modelCalls < 0:
                raise _StateConflict("无效的任务结束状态或模型调用次数")
            task = conn.execute("SELECT * FROM tasks WHERE taskID=?", (taskID,)).fetchone()
            if task is None:
                raise _StateConflict("任务不存在")
            self._task(conn, task["contextID"], taskID)
            conn.execute("UPDATE tasks SET status=?, modelCalls=? WHERE taskID=?", (status, modelCalls, taskID))
            if status == "interrupted":
                conn.execute(
                    "UPDATE records SET status='uncertain' WHERE contextID=? AND taskID=? AND status='executing'",
                    (task["contextID"], taskID),
                )
            self._touch(conn, task["contextID"])

        await self._run(lambda: self._db.run(finish), write=True, allowPaused=True)




    async def readRecords(self, contextID) -> list[dict]:
        """按位置读取记录并解密内容，缺失上下文和损坏内容明确报告。"""
        def read(conn):
            conn.execute("BEGIN")
            self._context(conn, contextID)
            records = []
            for row in conn.execute("SELECT * FROM records WHERE contextID=? ORDER BY position", (contextID,)):
                record = dict(row)
                record["content"] = self._decode(row["content"])
                records.append(record)
            return records

        return await self._run(lambda: self._db.run(read))


    async def forkContext(self, contextID, position) -> str:
        """复制指定位置及之前的完整记录；位置 0 表示从空上下文分叉。"""
        def fork(conn):
            self._transaction(conn)
            parent = self._context(conn, contextID)
            if type(position) is not int or position < 0:
                raise _StateConflict("分叉位置必须是非负整数")
            if position and conn.execute(
                "SELECT 1 FROM records WHERE contextID=? AND position=?", (contextID, position),
            ).fetchone() is None:
                raise _StateConflict("分叉位置不存在")
            rows = conn.execute(
                "SELECT * FROM records WHERE contextID=? AND position<=? ORDER BY position",
                (contextID, position),
            ).fetchall()
            if len(rows) != position:
                raise ContextStorageError("工作上下文记录位置不连续")
            # 校验快照内容，数据损坏时回滚整个分叉。
            for row in rows:
                self._decode(row["content"])
            childID = str(uuid4())
            conn.execute(
                "INSERT INTO contexts VALUES (?, ?, ?, ?, ?)",
                (childID, parent["chatID"], contextID, position, time.time()),
            )
            conn.execute(
                "INSERT INTO records SELECT ?, position, taskID, operationIndex, kind, status, content, createdAt "
                "FROM records WHERE contextID=? AND position<=?",
                (childID, contextID, position),
            )
            return childID

        return await self._run(lambda: self._db.run(fork), write=True)


    async def deleteContext(self, contextID) -> None:
        """删除上下文、所属任务和记录，并清理聊天当前指向。"""
        def delete(conn):
            self._transaction(conn)
            self._context(conn, contextID)
            if conn.execute(
                "SELECT 1 FROM tasks WHERE contextID=? AND status='running'", (contextID,),
            ).fetchone():
                raise _StateConflict("上下文仍有运行中的任务")
            conn.execute("DELETE FROM contexts WHERE contextID=?", (contextID,))

        await self._run(lambda: self._db.run(delete), write=True)
