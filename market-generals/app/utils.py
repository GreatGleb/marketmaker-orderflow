import asyncio
import inspect

from abc import abstractmethod

from contextlib import AsyncExitStack

from typing import Any

from fastapi import Request
from fastapi.dependencies.models import Dependant
from fastapi.dependencies.utils import get_dependant, solve_dependencies

from pydantic import ValidationError


class CommandResult:
    def __init__(
        self,
        success: bool,
        data: Any = None,
        need_retry: bool = False,
        retry_after: int = 5,
    ):
        self.success = success
        self.data = data
        self.need_retry = need_retry
        self.retry_after = retry_after


class Command:
    @abstractmethod
    async def command(self) -> CommandResult:
        pass

    @staticmethod
    async def _execute_command(request: Request, dependant: Dependant):
        async with AsyncExitStack() as async_exit_stack:
            solved = await solve_dependencies(
                request=request,
                dependant=dependant,
                async_exit_stack=async_exit_stack,
                embed_body_fields=False,
            )
            values = solved.values
            errors = solved.errors

            if errors:
                raise ValidationError(errors, None)

            if inspect.iscoroutinefunction(dependant.call):
                result = await dependant.call(**values)
            else:
                result = dependant.call(**values)

            return result

    async def run_async(self):
        async with AsyncExitStack() as cm:
            request = Request(
                {
                    "type": "http",
                    "headers": [],
                    "query_string": "",
                    "fastapi_astack": cm,
                }
            )
            dependant = get_dependant(
                path=f"command:{self.__class__.__name__}", call=self.command
            )
            return await self._execute_command(request, dependant)

    def run(self) -> CommandResult:
        return asyncio.get_event_loop().run_until_complete(self.run_async())
