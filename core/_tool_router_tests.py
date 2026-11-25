"""
Self-test for the tool router (core/tool_router.py) and the argument checker
(core/tool_args.py).

    python -m core._tool_router_tests

No network, no key, no window. The model is replaced by a function returning
canned JSON, so every path is driven: good answer, answer that needs one
repair, no answer, a crash. The last two sections use the real registries
(how much the router takes off the live list) and main.py's real
_execute_tool on a bare JarvisLive (skipped if main.py's dependencies are
missing).
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import tool_args, tool_router  # noqa: E402

_failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        _failures.append(name)


SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "file_name": {"type": "STRING"},
        "count": {"type": "INTEGER"},
        "volume": {"type": "NUMBER"},
        "loud": {"type": "BOOLEAN"},
        "repeat": {"type": "STRING", "enum": ["daily", "weekly", "monthly"]},
        "tags": {"type": "ARRAY", "items": {"type": "STRING"}},
        "place": {"type": "OBJECT", "properties": {"city": {"type": "STRING"},
                                                   "floor": {"type": "INTEGER"}},
                  "required": ["city"]},
    },
    "required": ["file_name"],
}


def test_args() -> None:
    args, problems = tool_args.check(SCHEMA, {
        "File-Name": "notes.txt", "count": "3", "volume": "0,5", "loud": "yes",
        "repeat": "Weekly", "tags": "work", "place": '{"city": "Lyon", "floor": "2"}'})
    check("args: names, numbers, booleans, enum case, list, object all repaired",
          problems == [] and args == {"file_name": "notes.txt", "count": 3, "volume": 0.5,
                                      "loud": True, "repeat": "weekly", "tags": ["work"],
                                      "place": {"city": "Lyon", "floor": 2}},
          f"{args} {problems}")

    args, problems = tool_args.check(SCHEMA, {"file_name": "a", "repeat": "weeklyy"})
    check("args: a near spelling of one choice is taken", args["repeat"] == "weekly"
          and not problems, f"{args} {problems}")

    args, problems = tool_args.check(SCHEMA, {"file_name": "a", "repeat": "hourly"})
    check("args: a value off the list is reported, not replaced",
          args["repeat"] == "hourly" and any("allowed values" in p for p in problems), str(problems))

    args, problems = tool_args.check(SCHEMA, {"count": "many", "loud": "maybe",
                                              "colour": "red", "place": {"floor": 1}})
    joined = " | ".join(problems)
    check("args: wrong kinds, unknown key, missing required (top and nested) reported",
          "'count' should be a number" in joined and "'loud' should be true or false" in joined
          and "'colour' is unknown here" in joined and "'file_name' is required" in joined
          and "'place.city' is required" in joined and "colour" not in args, joined)

    args, problems = tool_args.check(SCHEMA, {"file_name": "a", "count": 2.5})
    check("args: a fraction where a whole number is wanted is reported",
          any("whole number" in p for p in problems), str(problems))

    args, problems = tool_args.check(SCHEMA, ["not", "an", "object"])
    check("args: a non-object is one problem, empty args", args == {} and len(problems) == 1)

    args, problems = tool_args.check({"type": "OBJECT", "properties": {"m": {
        "type": "STRING", "enum": ["colour", "colours"]}}}, {"m": "colourz"})
    check("args: an ambiguous spelling is not guessed", args["m"] == "colourz" and problems,
          f"{args} {problems}")

    args, problems = tool_args.check(SCHEMA, {"file_name": "a", "x": 1}, keep_unknown=True)
    check("args: keep_unknown keeps the key but still reports it",
          args.get("x") == 1 and len(problems) == 1)


def _decl(name, tier=None, props=None, **extra):
    d = {"name": name, "description": f"{name.title()} does a thing. More detail follows here.",
         "parameters": {"type": "OBJECT", "properties": props or {}}}
    if tier:
        d["tier"] = tier
    d.update(extra)
    return d


def _decls():
    return [
        _decl("open_app", "core", {"app": {"type": "STRING"}}),
        _decl("quiz", "extra", {"topic": {"type": "STRING"}, "count": {"type": "INTEGER"}},
              brief="Quiz the user on a topic."),
        _decl("pomodoro", "extra", {"action": {"type": "STRING",
                                               "enum": ["start", "stop", "status"]},
                                    "minutes": {"type": "INTEGER"}}),
        _decl("water_reminder", "extra"),
        _decl("whatsapp_call", "extra", behavior="NON_BLOCKING"),
        _decl("my_plugin"),                      # no tier: user-added, stays direct
        _decl("shopping_list", "extra", {"item": {"type": "STRING"}}),
    ]


def test_build() -> None:
    few = [_decl("a", "extra"), _decl("b", "extra"), _decl("c")]
    out, menu = tool_router.build(few)
    check("build: too few extras leaves the very same list", out is few and menu is None)

    decls = _decls()
    out, menu = tool_router.build(decls, [("printer_control", "Print a file."),
                                          ("quiz", "duplicate of a live one")])
    names = [d["name"] for d in out]
    check("build: core, untiered and NON_BLOCKING stay direct, in order, router last",
          names == ["open_app", "whatsapp_call", "my_plugin", "use_feature"], str(names))
    check("build: extras are behind the router",
          sorted(menu.features) == ["pomodoro", "quiz", "shopping_list", "water_reminder"])
    router = out[-1]
    enum = router["parameters"]["properties"]["feature"]["enum"]
    check("build: switched-off plugins are listed (but not one that is live)",
          "printer_control" in enum and enum.count("quiz") == 1
          and "printer_control (off)" in router["description"], str(enum))
    check("build: menu line uses the brief when there is one",
          "- quiz: Quiz the user on a topic." in router["description"])
    check("build: menu line is the first sentence otherwise",
          "- pomodoro: Pomodoro does a thing.\n" in router["description"])
    check("build: input declarations untouched", decls == _decls())

    from providers.schema import strip_meta
    check("build: router declaration has no project-only keys",
          strip_meta([router])[0] == router)


class _Ask:
    """Stands in for the model: returns the queued answers in turn."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.prompts: list[str] = []

    def __call__(self, prompt):
        self.prompts.append(prompt)
        a = self.answers.pop(0) if self.answers else None
        if isinstance(a, Exception):
            raise a
        return a


def test_resolve() -> None:
    _, menu = tool_router.build(_decls(), [("printer_control", "Print a file.")])
    quiet = lambda _m: None

    ask = _Ask({"args": {"action": "Start", "minutes": "25"}})
    name, args, answer = menu.resolve("pomodoro", "start a 25 minute focus timer",
                                      "User: I need to focus", ask=ask, log=quiet)
    check("resolve: good answer runs the feature with repaired args",
          (name, args, answer) == ("pomodoro", {"action": "start", "minutes": 25}, None),
          f"{name} {args} {answer}")
    p = ask.prompts[0]
    check("resolve: prompt carries the schema, request, conversation and time",
          '"minutes"' in p and "25 minute focus" in p and "I need to focus" in p
          and "CURRENT LOCAL TIME" in p and '"tier"' not in p)

    ask = _Ask({"args": {"actoin": "begin"}}, {"args": {"action": "start"}})
    name, args, _ = menu.resolve("Pomodoro", "go", ask=ask, log=quiet)
    check("resolve: one repair round, with the problems shown to the model",
          args == {"action": "start"} and len(ask.prompts) == 2
          and "'actoin' is unknown here" in ask.prompts[1], str(args))

    ask = _Ask({"args": {"action": "dance"}}, {"args": {"action": "dance"}})
    name, args, answer = menu.resolve("pomodoro", "x", ask=ask, log=quiet)
    check("resolve: still wrong after repair → the feature decides",
          name == "pomodoro" and args == {"action": "dance"} and answer is None)

    ask = _Ask()
    name, args, answer = menu.resolve("water_reminder", "remind me to drink", ask=ask, log=quiet)
    check("resolve: a feature without parameters needs no model call",
          (name, args, answer) == ("water_reminder", {}, None) and not ask.prompts)

    name, _, answer = menu.resolve("shoping list", "milk", ask=_Ask({"item": "milk"}), log=quiet)
    check("resolve: near name and a bare args object are accepted",
          name == "shopping_list", str(answer))

    name, _, answer = menu.resolve("teleport", "x", ask=_Ask(), log=quiet)
    check("resolve: an unknown feature is a failed outcome listing what exists",
          name is None and "status=failed" in answer and "pomodoro" in answer, str(answer))

    name, _, answer = menu.resolve("printer_control", "print it", ask=_Ask(), log=quiet)
    check("resolve: a switched-off feature says how to switch it on",
          name is None and "switched off" in answer and "Plugin Manager" in answer, str(answer))

    name, _, answer = menu.resolve("quiz", "quiz me", ask=_Ask(None), log=quiet)
    check("resolve: no model answer is a failed outcome, not a guess",
          name is None and "status=failed" in answer, str(answer))

    name, _, answer = menu.resolve("quiz", "quiz me", ask=_Ask(RuntimeError("boom")), log=quiet)
    check("resolve: a crash is a failed outcome, never raised",
          name is None and "status=failed" in answer, str(answer))


def test_real_registries() -> None:
    """How much the router takes off the live list with this checkout's tools."""
    try:
        from core.action_loader import discover_actions
        from core.plugin_loader import discover_plugins
        from providers.schema import payload_size
        actions = discover_actions(ROOT / "actions", logger=lambda m: None)
        inline = {"save_memory", "recall_memory", "undo", "screen_process", "close_camera",
                  "system_status", "manage_monitor", "shutdown_jarvis"}
        action_names = {d["name"] for d in actions.get_tool_declarations()}
        plugins = discover_plugins(ROOT / "plugins", inline | action_names,
                                   logger=lambda m: None)
        decls = actions.get_tool_declarations() + plugins.get_tool_declarations()
    except Exception as e:
        print(f"SKIP  real registries ({type(e).__name__}: {e})")
        return
    out, menu = tool_router.build(decls, plugins.switched_off())
    if menu is None:
        print("SKIP  real registries (fewer than 3 extras loaded here)")
        return
    before, after = payload_size(decls), payload_size(out)
    print(f"INFO  real tools: {len(decls)} declarations, {before:,} chars  →  "
          f"{len(out)} declarations, {after:,} chars "
          f"({len(menu.features)} behind use_feature, router itself "
          f"{payload_size([out[-1]]):,} chars)")
    check("real: the router saves characters", after < before)
    check("real: every deferred feature is in the router's list",
          set(menu.features) <= set(out[-1]["parameters"]["properties"]["feature"]["enum"]))


def test_execute_tool() -> None:
    """main.py's real _execute_tool, on a bare JarvisLive with fake registries."""
    try:
        import main
        from google.genai import types
    except Exception as e:
        print(f"SKIP  main.py integration ({type(e).__name__}: {e})")
        return

    calls = []

    class _UI:
        muted = False

        def set_state(self, *_a):
            pass

    class _Actions:
        def has(self, name):
            return False

        def scheduling(self, name):
            return None

    class _Plugins:
        def has(self, name):
            return name in ("pomodoro", "quiz")

        def run(self, name, args, player=None, session_memory=None):
            calls.append((name, args))
            return f"status=done action={name}"

        def scheduling(self, name):
            return "INTERRUPT"

    _, menu = tool_router.build(_decls(), [("printer_control", "Print a file.")])
    j = main.JarvisLive.__new__(main.JarvisLive)
    j.ui, j._action_registry, j._plugin_registry = _UI(), _Actions(), _Plugins()
    j._briefing_until, j._last_user_input_at, j._briefing_started = 0.0, 0.0, 0.0
    j._session_log = ["User: focus time please"]
    j._router_menu = menu

    seen = []
    menu_resolve = menu.resolve
    menu.resolve = lambda f, r, c, **kw: (seen.append(c), menu_resolve(
        f, r, c, ask=_Ask({"args": {"action": "start", "minutes": 25}}), log=lambda m: None))[1]

    import actions.action_history as ah
    recorded = []
    ah_record, ah.record = ah.record, lambda name, *a, **k: recorded.append(name)
    try:
        fc = types.FunctionCall(id="1", name="use_feature",
                                args={"feature": "pomodoro", "request": "25 minutes"})
        fr = asyncio.run(j._execute_tool(fc))
        check("main: use_feature runs the real plugin with resolved args",
              calls == [("pomodoro", {"action": "start", "minutes": 25})], str(calls))
        check("main: the answer goes back under use_feature, without scheduling",
              fr.name == "use_feature" and fr.scheduling is None
              and "status=done" in fr.response["result"], str(fr))
        check("main: conversation reaches the resolver", seen and "focus time" in seen[0])
        check("main: action history records the real feature", recorded == ["pomodoro"],
              str(recorded))

        fc = types.FunctionCall(id="2", name="use_feature",
                                args={"feature": "printer_control", "request": "print"})
        fr = asyncio.run(j._execute_tool(fc))
        check("main: a switched-off feature is answered without running anything",
              len(calls) == 1 and "switched off" in fr.response["result"])

        fc = types.FunctionCall(id="3", name="quiz", args={"topic": "x"})
        fr = asyncio.run(j._execute_tool(fc))
        check("main: a direct call is unchanged (own name, own scheduling)",
              fr.name == "quiz" and fr.scheduling is not None and calls[-1][0] == "quiz")
    finally:
        ah.record = ah_record


def main() -> int:
    test_args()
    test_build()
    test_resolve()
    test_real_registries()
    test_execute_tool()
    print(f"\n{'ALL PASSED' if not _failures else f'{len(_failures)} FAILED'}")
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main())
