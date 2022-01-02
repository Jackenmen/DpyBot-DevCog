"""
The original implementation of this cog was heavily based on
RoboDanny's REPL cog which can be found here:
https://github.com/Rapptz/RoboDanny/blob/f13e1c9a6a7205e50de6f91fa5326fc7113332d3/cogs/repl.py

The original copy was distributed under MIT License and this derivative work
is distributed under GNU GPL Version 3.

Red - A fully customizable Discord bot
Copyright (C) 2017-2021  Cog Creators
Copyright (C) 2015-2017  Twentysix

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""

import ast
import asyncio
import aiohttp
import contextlib
import inspect
import io
import re
import textwrap
import traceback
import types
from contextlib import redirect_stdout
from copy import copy
from typing import Generator, Iterable, List

import discord
from discord.ext import commands

START_CODE_BLOCK_RE = re.compile(r"^((```py(thon)?)(?=\s)|(```))")


def pagify(
    text: str,
    delims=["\n"],
    *,
    priority: bool = False,
    shorten_by: int = 12,
    page_length: int = 2000,
) -> Generator[str, None, None]:
    in_text = text
    page_length -= shorten_by
    while len(in_text) > page_length:
        this_page_len = page_length
        closest_delim = (in_text.rfind(d, 1, this_page_len) for d in delims)
        if priority:
            closest_delim = next((x for x in closest_delim if x > 0), -1)
        else:
            closest_delim = max(closest_delim)
        closest_delim = closest_delim if closest_delim != -1 else this_page_len
        to_send = in_text[:closest_delim]
        if len(to_send.strip()) > 0:
            yield to_send
        in_text = in_text[closest_delim:]

    if len(in_text.strip()) > 0:
        yield in_text


async def send_interactive(
    ctx: commands.Context, messages: Iterable[str], box_lang: str = None, timeout: int = 15
) -> List[discord.Message]:
    messages = tuple(messages)
    ret = []

    for idx, page in enumerate(messages, 1):
        if box_lang is None:
            msg = await ctx.send(page)
        else:
            msg = await ctx.send(f"```{box_lang}\n{page}\n```")
        ret.append(msg)
        n_remaining = len(messages) - idx
        if n_remaining > 0:
            if n_remaining == 1:
                plural = ""
                is_are = "is"
            else:
                plural = "s"
                is_are = "are"
            query = await ctx.send(
                f"There {is_are} still {n_remaining} message{plural} remaining. "
                "Type `more` to continue."
            )

            def predicate(message: discord.Message) -> bool:
                return (
                    ctx.author.id == message.author.id
                    and ctx.channel.id == message.channel.id
                    and message.content.lower() == "more"
                )

            try:
                resp = await ctx.bot.wait_for(
                    "message", check=predicate, timeout=timeout
                )
            except asyncio.TimeoutError:
                with contextlib.suppress(discord.HTTPException):
                    await query.delete()
                break
            else:
                try:
                    await ctx.channel.delete_messages((query, resp))
                except (discord.HTTPException, AttributeError):
                    # In case the bot can't delete other users' messages,
                    # or is not a bot account
                    # or channel is a DM
                    with contextlib.suppress(discord.HTTPException):
                        await query.delete()
    return ret


async def tick(ctx: commands.Context) -> bool:
    try:
        if not ctx.channel.permissions_for(ctx.me).add_reactions:
            raise RuntimeError
        await ctx.message.add_reaction("\N{WHITE HEAVY CHECK MARK}")
    except (RuntimeError, discord.HTTPException):
        return False
    else:
        return True


class Dev(commands.Cog):
    """Various development focused utilities."""

    def __init__(self) -> None:
        super().__init__()
        self._last_result = None
        self.sessions = {}
        self.env_extensions = {}

    @staticmethod
    def async_compile(source, filename, mode):
        return compile(source, filename, mode, flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT, optimize=0)

    @staticmethod
    async def maybe_await(coro):
        for i in range(2):
            if inspect.isawaitable(coro):
                coro = await coro
            else:
                return coro
        return coro

    @staticmethod
    def cleanup_code(content):
        """Automatically removes code blocks from the code."""
        # remove ```py\n```
        if content.startswith("```") and content.endswith("```"):
            return START_CODE_BLOCK_RE.sub("", content)[:-3]

        # remove `foo`
        return content.strip("` \n")

    @classmethod
    def get_syntax_error(cls, e):
        """Format a syntax error to send to the user.

        Returns a string representation of the error formatted as a codeblock.
        """
        if e.text is None:
            return cls.get_pages("{0.__class__.__name__}: {0}".format(e))
        return cls.get_pages(
            "{0.text}\n{1:>{0.offset}}\n{2}: {0}".format(e, "^", type(e).__name__)
        )

    @staticmethod
    def get_pages(msg: str):
        """Pagify the given message for output to the user."""
        return pagify(msg, delims=["\n", " "], priority=True, shorten_by=10)

    @staticmethod
    def sanitize_output(ctx: commands.Context, input_: str) -> str:
        """Hides the bot's token from a string."""
        token = ctx.bot.http.token
        return re.sub(re.escape(token), "[EXPUNGED]", input_, re.I)

    def get_environment(self, ctx: commands.Context) -> dict:
        env = {
            "bot": ctx.bot,
            "ctx": ctx,
            "channel": ctx.channel,
            "author": ctx.author,
            "guild": ctx.guild,
            "message": ctx.message,
            "asyncio": asyncio,
            "aiohttp": aiohttp,
            "discord": discord,
            "commands": commands,
            "_": self._last_result,
            "__name__": "__main__",
        }
        for name, value in self.env_extensions.items():
            try:
                env[name] = value(ctx)
            except Exception as e:
                traceback.clear_frames(e.__traceback__)
                env[name] = e
        return env

    @commands.is_owner()
    @commands.command()
    async def debug(self, ctx, *, code):
        """Evaluate a statement of python code.

        The bot will always respond with the return value of the code.
        If the return value of the code is a coroutine, it will be awaited,
        and the result of that will be the bot's response.

        Note: Only one statement may be evaluated. Using certain restricted
        keywords, e.g. yield, will result in a syntax error. For multiple
        lines or asynchronous code, see [p]repl or [p]eval.

        Environment Variables:
            ctx      - command invocation context
            bot      - bot object
            channel  - the current channel object
            author   - command author's member object
            message  - the command's message object
            discord  - discord.py library
            commands - redbot.core.commands
            _        - The result of the last dev command.
        """
        env = self.get_environment(ctx)
        code = self.cleanup_code(code)

        try:
            compiled = self.async_compile(code, "<string>", "eval")
            result = await self.maybe_await(eval(compiled, env))
        except SyntaxError as e:
            await send_interactive(ctx, self.get_syntax_error(e), box_lang="py")
            return
        except Exception as e:
            await send_interactive(
                ctx, self.get_pages("{}: {!s}".format(type(e).__name__, e)), box_lang="py"
            )
            return

        self._last_result = result
        result = self.sanitize_output(ctx, str(result))

        await tick(ctx)
        await send_interactive(ctx, self.get_pages(result), box_lang="py")

    @commands.is_owner()
    @commands.command(name="eval")
    async def _eval(self, ctx, *, body: str):
        """Execute asynchronous code.

        This command wraps code into the body of an async function and then
        calls and awaits it. The bot will respond with anything printed to
        stdout, as well as the return value of the function.

        The code can be within a codeblock, inline code or neither, as long
        as they are not mixed and they are formatted correctly.

        Environment Variables:
            ctx      - command invocation context
            bot      - bot object
            channel  - the current channel object
            author   - command author's member object
            message  - the command's message object
            discord  - discord.py library
            commands - redbot.core.commands
            _        - The result of the last dev command.
        """
        env = self.get_environment(ctx)
        body = self.cleanup_code(body)
        stdout = io.StringIO()

        to_compile = "async def func():\n%s" % textwrap.indent(body, "  ")

        try:
            compiled = self.async_compile(to_compile, "<string>", "exec")
            exec(compiled, env)
        except SyntaxError as e:
            return await send_interactive(ctx, self.get_syntax_error(e), box_lang="py")

        func = env["func"]
        result = None
        try:
            with redirect_stdout(stdout):
                result = await func()
        except:
            printed = f"{stdout.getvalue()}{traceback.format_exc()}"
        else:
            printed = stdout.getvalue()
            await tick(ctx)

        if result is not None:
            self._last_result = result
            msg = f"{printed}{result}"
        else:
            msg = printed
        msg = self.sanitize_output(ctx, msg)

        await send_interactive(ctx, self.get_pages(msg), box_lang="py")

    @commands.is_owner()
    @commands.group(invoke_without_command=True)
    async def repl(self, ctx: commands.Context) -> None:
        """Open an interactive REPL.

        The REPL will only recognise code as messages which start with a
        backtick. This includes codeblocks, and as such multiple lines can be
        evaluated.
        """
        if ctx.channel.id in self.sessions:
            if self.sessions[ctx.channel.id]:
                await ctx.send(
                    "Already running a REPL session in this channel. Exit it with `quit`."
                )
            else:
                await ctx.send(
                    "Already running a REPL session in this channel."
                    f" Resume the REPL with `{ctx.prefix}repl resume`."
                )
            return

        env = self.get_environment(ctx)
        env["__builtins__"] = __builtins__
        env["_"] = None
        self.sessions[ctx.channel.id] = True
        await ctx.send(
            "Enter code to execute or evaluate. `exit()` or `quit` to exit."
            f" `{ctx.prefix}repl pause` to pause."
        )

        while True:
            def predicate(message: discord.Message) -> bool:
                return (
                    ctx.author.id == message.author.id
                    and ctx.channel.id == message.channel.id
                    and message.content.startswith("`")
                )

            response = await ctx.bot.wait_for("message", check=predicate)

            if not self.sessions[ctx.channel.id]:
                continue

            cleaned = self.cleanup_code(response.content)

            if cleaned in ("quit", "exit", "exit()"):
                await ctx.send("Exiting.")
                del self.sessions[ctx.channel.id]
                return

            executor = None
            if cleaned.count("\n") == 0:
                # single statement, potentially 'eval'
                try:
                    code = self.async_compile(cleaned, "<repl session>", "eval")
                except SyntaxError:
                    pass
                else:
                    executor = eval

            if executor is None:
                try:
                    code = self.async_compile(cleaned, "<repl session>", "exec")
                except SyntaxError as e:
                    await send_interactive(ctx, self.get_syntax_error(e), box_lang="py")
                    continue

            env["message"] = response
            stdout = io.StringIO()

            msg = ""

            try:
                with redirect_stdout(stdout):
                    if executor is None:
                        result = types.FunctionType(code, env)()
                    else:
                        result = executor(code, env)
                    result = await self.maybe_await(result)
            except:
                value = stdout.getvalue()
                msg = f"{value}{traceback.format_exc()}"
            else:
                value = stdout.getvalue()
                if result is not None:
                    msg = f"{value}{result}"
                    env["_"] = result
                elif value:
                    msg = f"{value}"

            msg = self.sanitize_output(ctx, msg)

            try:
                await send_interactive(ctx, self.get_pages(msg), box_lang="py")
            except discord.Forbidden:
                pass
            except discord.HTTPException as e:
                await ctx.send(f"Unexpected error: `{e}`")

    @repl.command(aliases=["resume"])
    async def pause(self, ctx: commands.Context, toggle: bool = None) -> None:
        """Pauses/resumes the REPL running in the current channel"""
        if ctx.channel.id not in self.sessions:
            await ctx.send("There is no currently running REPL session in this channel.")
            return

        if toggle is None:
            toggle = not self.sessions[ctx.channel.id]
        self.sessions[ctx.channel.id] = toggle

        if toggle:
            await ctx.send("The REPL session in this channel has been resumed.")
        else:
            await ctx.send("The REPL session in this channel is now paused.")

    @commands.is_owner()
    @commands.command()
    async def mock(self, ctx: commands.Context, user: discord.Member, *, command: str) -> None:
        """
        Mock another user invoking a command.

        The prefix must not be entered.
        """
        msg = copy(ctx.message)
        msg.author = user
        msg.content = ctx.prefix + command

        ctx.bot.dispatch("message", msg)

    @commands.is_owner()
    @commands.command(name="mockmsg")
    async def mock_msg(self, ctx, user: discord.Member, *, content: str = ""):
        """
        Dispatch a message event as if it were sent by a different user.

        Current message is used as a base (including attachments, embeds, etc.),
        the content and author of the message are replaced with the given arguments.

        Note: If `content` isn't passed, the message needs to contain embeds, attachments,
        or anything else that makes the message non-empty.
        """
        msg = ctx.message
        if not content and not msg.embeds and not msg.attachments and not msg.stickers:
            await ctx.send_help(ctx.command)
            return
        msg = copy(msg)
        msg.author = user
        msg.content = content

        ctx.bot.dispatch("message", msg)


def setup(bot: commands) -> None:
    bot.add_cog(Dev())
