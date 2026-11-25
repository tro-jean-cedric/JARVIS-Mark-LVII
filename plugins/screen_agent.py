"""
plugins/screen_agent.py — does a task on the screen with the mouse and keyboard.

It looks (core/screenview.py), decides one step with the model, acts
(core/screenact.py), measures what changed and repeats until the screen shows
the task done, a step needs the user, or the step limit is reached. The loop
lives in plugins/_screen_loop.py. Risky steps — paying, sending, deleting,
submitting, closing — wait for the user's CONFIRM on the HUD (core.confirm)
unless the task's own words asked for exactly that step; paying always waits.

run() returns at once (NON_BLOCKING); progress goes to the activity log and the
outcome is spoken when it is known. Pushing the mouse into a screen corner
stops it (pyautogui's fail-safe), as does action='stop'.
"""
from __future__ import annotations

import threading
import time

from plugins import _announce

PLUGIN = {
    "name": "screen_agent",
    "description": (
        "Does a task ON THE SCREEN the way a person at the computer would: looks at "
        "the screen, decides one action, clicks or types, checks it worked, and "
        "repeats until the task is visibly done. Use it for any task that needs "
        "several actions in a program or website: filling in a form, finding and "
        "opening something, changing a setting, working inside a desktop app. Give "
        "the WHOLE task in one call as 'goal'. It opens and focuses programs "
        "itself. Do NOT use it for: a question about what is on screen (use "
        "screen_process), a single keystroke or click (computer_control), only "
        "launching a program (open_app), only opening one web address "
        "(browser_control), files and folders (file_controller), or apps connected "
        "through connected_apps. Not for bulk posting, liking or following. "
        "action='stop' aborts, 'status' reports progress, 'decline' the moment the "
        "user refuses a step it asked about. A risky step is approved ONLY by the "
        "user pressing the confirmation button on screen - never by you."
    ),
    "brief": "Multi-step tasks on screen (any program or website) by looking, acting and checking each step.",
    "tier": "core",
    "behavior": "NON_BLOCKING",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING",
                       "description": "'start' (default), 'stop', 'status', or 'decline' when the "
                                      "user refuses a step."},
            "goal": {"type": "STRING",
                     "description": "The whole task in ENGLISH, concretely: the program or website "
                                    "named, every value the user gave VERBATIM in its own language "
                                    "(text to type, names, numbers), and what should be true when "
                                    "it is finished. If the user said to go ahead without asking "
                                    "(to send, delete), keep that in their words; never add it."},
            "quick": {"type": "BOOLEAN",
                      "description": "true for a task of one or two actions (press a button, flip a "
                                     "switch, type into one field, choose one option)."},
            "app": {"type": "STRING", "description": "quick: the app, as the user named it."},
            "control": {"type": "STRING",
                        "description": "quick: the button, switch, field, tab or option, as the user said it."},
            "do": {"type": "STRING",
                   "description": "quick: what to do with that control: press, toggle, type, add, choose, set."},
            "text": {"type": "STRING", "description": "quick: the text to type, option to choose or number to set."},
            "show": {"type": "BOOLEAN",
                     "description": "true only when the user asked to be SHOWN or TAUGHT how: each "
                                    "step is then said out loud."},
        },
        "required": [],
    },
}


def _check_view(values: dict):
    """Settings button: wait 5 s for the user to switch windows, then report
    what this machine can read there. Clicks nothing."""
    try:
        from core import screenview
        time.sleep(5)
        v = screenview.look()
        caps = screenview.capabilities()
        named = sum(1 for e in v.elements if e.interactable)
        msg = (f"{v.app or '?'}: {named} controls named by the window itself via {v.source}"
               + (f" ({v.reason})" if v.degraded and v.reason else "")
               + ("" if caps.get("capture_available") else "; screenshots NOT available (pip install mss pillow)"))
        return (not v.degraded or named > 0), msg
    except Exception as e:
        return False, f"could not read the screen: {e}"


PLUGIN_SETTINGS = {
    "namespace": "screen_agent",
    "title": "SCREEN AGENT (LOOK, ACT, CHECK)",
    "fields": [
        {"key": "approval", "label": "Ask me before", "type": "choice",
         "options": ["risky steps only", "every step"], "default": "risky steps only"},
        {"key": "max_steps", "label": "Step limit", "type": "text", "default": "25"},
        {"key": "look_closer", "label": "Look closer before every click (more accurate, a little slower)",
         "type": "toggle", "default": True},
        {"key": "marks", "label": "Put numbered tags on what can be pressed before each look",
         "type": "toggle", "default": True},
        {"key": "model", "label": "Model (empty: automatic)", "type": "text",
         "placeholder": "e.g. gemini-2.5-pro"},
    ],
    "action": {"label": "CHECK WHAT I CAN SEE (switch to a window within 5 s; clicks nothing)",
               "run": _check_view},
}

_agent = None
_lock = threading.Lock()


def _setting(key: str, default):
    try:
        from memory.config_manager import get_plugin_setting
        v = get_plugin_setting("screen_agent", key, default)
        return default if v in (None, "") else v
    except Exception:
        return default


def _quick_goal(p: dict) -> str:
    app = str(p.get("app") or "").strip()
    control = str(p.get("control") or "").strip()
    do = str(p.get("do") or "press").strip().lower()
    text = str(p.get("text") or "").strip()
    if not control:
        return ""
    where = f" in {app}" if app else ""
    if do in ("type", "write", "enter"):
        return f"In the field '{control}'{where}, make the field hold exactly this text: {text!r}."
    if do in ("add", "append"):
        return f"Add this text after what the field '{control}'{where} already holds: {text!r}."
    if do in ("choose", "select", "pick"):
        return f"In '{control}'{where}, choose the option {text!r}."
    if do == "set":
        return f"Set '{control}'{where} to {text}."
    if do in ("toggle", "flip", "switch"):
        return f"Flip the switch or checkbox '{control}'{where}."
    return f"Press '{control}'{where}."


def run(parameters: dict, player=None, session_memory=None) -> str:
    global _agent
    _announce.remember(player)
    p = parameters or {}
    action = str(p.get("action") or "start").strip().lower()

    with _lock:
        agent = _agent
        busy = agent is not None and agent.running()

        if action == "stop":
            if not busy:
                return "The screen agent is not doing anything right now."
            agent.stop()
            return "Stopping. Confirm to the user that you have stopped working on the screen."
        if action == "status":
            if not busy:
                last = f" The last task ended: {agent.finished}." if agent and agent.finished else ""
                return "The screen agent is not doing anything right now." + last
            return agent.status()
        if action == "decline":
            if busy and agent.decline():
                return ("Declined - that step will not be done and the screen task stops there. "
                        "Acknowledge in one short sentence; I report the rest myself.")
            return ("There is no step waiting for approval. Say nothing about approval and continue "
                    "the conversation normally.")
        if action in ("approve", "confirm", "yes"):
            return ("A step is approved only by the user pressing the confirmation button on screen. "
                    "Tell them that in one short sentence. Do not call this tool to approve it.")

        goal = str(p.get("goal") or "").strip()
        quick = bool(p.get("quick"))
        if not goal and (quick or p.get("control")):
            goal = _quick_goal(p)
        if not goal:
            return ("Ask the user what exactly they want done on screen; I need a concrete goal "
                    "before I can start.")
        if busy:
            return (f"I am already working on the screen: \"{agent.goal}\". Ask the user whether to "
                    "stop that first.")

        try:
            from plugins import _screen_loop as loop
        except Exception as e:
            return f"I cannot work on the screen: {e}"
        ok, why = loop.can_run()
        if not ok:
            return f"I cannot drive this screen: {why}"

        try:
            limit = int(float(_setting("max_steps", 25)))
        except (TypeError, ValueError):
            limit = 25
        if quick:
            limit = min(limit, 6)
        every = str(_setting("approval", "risky steps only")).lower().startswith("every")
        _agent = loop.ScreenAgent(goal, player=player, max_steps=limit, teach=bool(p.get("show")),
                                  ask_every_step=every)
        _agent.start()

    try:
        if player is not None:
            player.write_log(f"Screen agent: starting - {goal}")
    except Exception:
        pass
    return (f"[SCREEN_AGENT_STARTED] I am now working on the screen: \"{goal}\". Tell the user you "
            "are starting, in one short sentence, then stop and say nothing more; I narrate the "
            "result myself. Do not call this tool again unless the user asks to stop, or declines "
            "a step.")
