# -*- coding: utf-8 -*-

# Copyright 2023-2025 Mike Fährmann
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 2 as
# published by the Free Software Foundation.

""" """

import time
import logging
import operator
import functools
from . import util, exception


def parse_logging(actionspec):
    if isinstance(actionspec, dict):
        actionspec = actionspec.items()

    actions = {}
    actions[-logging.DEBUG] = actions_bd = []
    actions[-logging.INFO] = actions_bi = []
    actions[-logging.WARNING] = actions_bw = []
    actions[-logging.ERROR] = actions_be = []
    actions[logging.DEBUG] = actions_ad = []
    actions[logging.INFO] = actions_ai = []
    actions[logging.WARNING] = actions_aw = []
    actions[logging.ERROR] = actions_ae = []

    for event, spec in actionspec:
        level, _, pattern = event.partition(":")
        search = util.re(pattern).search if pattern else util.true

        if isinstance(spec, str):
            type, _, args = spec.partition(" ")
            before, after = ACTIONS[type](args)
        else:
            actions_before = []
            actions_after = []
            for s in spec:
                type, _, args = s.partition(" ")
                before, after = ACTIONS[type](args)
                if before:
                    actions_before.append(before)
                if after:
                    actions_after.append(after)
            before = _chain_actions(actions_before)
            after = _chain_actions(actions_after)

        level = level.strip()
        if not level or level == "*":
            if before:
                action = (search, before)
                actions_bd.append(action)
                actions_bi.append(action)
                actions_bw.append(action)
                actions_be.append(action)
            if after:
                action = (search, after)
                actions_ad.append(action)
                actions_ai.append(action)
                actions_aw.append(action)
                actions_ae.append(action)
        else:
            level = _level_to_int(level)
            if before:
                actions[-level].append((search, before))
            if after:
                actions[level].append((search, after))

    return actions


def parse_signals(actionspec):
    import signal

    if isinstance(actionspec, dict):
        actionspec = actionspec.items()

    for signal_name, spec in actionspec:
        signal_num = getattr(signal, signal_name, None)
        if signal_num is None:
            log = logging.getLogger("gallery-dl")
            log.warning("signal '%s' is not defined", signal_name)
            continue

        if isinstance(spec, str):
            type, _, args = spec.partition(" ")
            before, after = ACTIONS[type](args)
            action = before if after is None else after
        else:
            actions_before = []
            actions_after = []
            for s in spec:
                type, _, args = s.partition(" ")
                before, after = ACTIONS[type](args)
                if before is not None:
                    actions_before.append(before)
                if after is not None:
                    actions_after.append(after)

            actions = actions_before
            actions.extend(actions_after)
            action = _chain_actions(actions)

        signal.signal(signal_num, signals_handler(action))


class LoggerAdapter():

    def __init__(self, logger, job):
        self.logger = logger
        self.extra = job._logger_extra
        self.actions = job._logger_actions

        self.debug = functools.partial(self.log, logging.DEBUG)
        self.info = functools.partial(self.log, logging.INFO)
        self.warning = functools.partial(self.log, logging.WARNING)
        self.error = functools.partial(self.log, logging.ERROR)

    def log(self, level, msg, *args, **kwargs):
        msg = str(msg)
        if args:
            msg = msg % args

        before = self.actions[-level]
        after = self.actions[level]

        if before:
            args = self.extra.copy()
            args["level"] = level

            for cond, action in before:
                if cond(msg):
                    action(args)

            level = args["level"]

        if self.logger.isEnabledFor(level):
            kwargs["extra"] = self.extra
            self.logger._log(level, msg, (), **kwargs)

        if after:
            args = self.extra.copy()
            for cond, action in after:
                if cond(msg):
                    action(args)


def _level_to_int(level):
    try:
        return logging._nameToLevel[level]
    except KeyError:
        return int(level)


def _chain_actions(actions):
    def _chain(args):
        for action in actions:
            action(args)
    return _chain


def signals_handler(action, args={}):
    def handler(signal_num, frame):
        action(args)
    return handler


# --------------------------------------------------------------------

def action_print(opts):
    def _print(_):
        print(opts)
    return None, _print


def action_status(opts):
    op, value = util.re(r"\s*([&|^=])=?\s*(\d+)").match(opts).groups()

    op = {
        "&": operator.and_,
        "|": operator.or_,
        "^": operator.xor,
        "=": lambda x, y: y,
    }[op]

    value = int(value)

    def _status(args):
        args["job"].status = op(args["job"].status, value)
    return _status, None


def action_level(opts):
    level = _level_to_int(opts.lstrip(" ~="))

    def _level(args):
        args["level"] = level
    return _level, None


def action_exec(opts):
    def _exec(_):
        util.Popen(opts, shell=True).wait()
    return None, _exec


def action_wait(opts):
    if opts:
        seconds = util.build_duration_func(opts)

        def _wait(args):
            time.sleep(seconds())
    else:
        def _wait(args):
            input("Press Enter to continue")

    return None, _wait


def action_flag(opts):
    flag, value = util.re(
        r"(?i)(file|post|child|download)(?:\s*[= ]\s*(.+))?"
    ).match(opts).groups()
    flag = flag.upper()
    value = "stop" if value is None else value.lower()

    def _flag(args):
        util.FLAGS.__dict__[flag] = value
    return _flag, None


def action_raise(opts):
    name, _, arg = opts.partition(" ")

    exc = getattr(exception, name, None)
    if exc is None:
        import builtins
        exc = getattr(builtins, name, Exception)

    if arg:
        def _raise(args):
            raise exc(arg)
    else:
        def _raise(args):
            raise exc()

    return None, _raise


def action_abort(opts):
    return None, util.raises(exception.StopExtraction)


def action_terminate(opts):
    return None, util.raises(exception.TerminateExtraction)


def action_restart(opts):
    return None, util.raises(exception.RestartExtraction)


def action_exit(opts):
    try:
        opts = int(opts)
    except ValueError:
        pass

    def _exit(args):
        raise SystemExit(opts)
    return None, _exit


ACTIONS = {
    "abort"    : action_abort,
    "exec"     : action_exec,
    "exit"     : action_exit,
    "flag"     : action_flag,
    "level"    : action_level,
    "print"    : action_print,
    "raise"    : action_raise,
    "restart"  : action_restart,
    "status"   : action_status,
    "terminate": action_terminate,
    "wait"     : action_wait,
}
