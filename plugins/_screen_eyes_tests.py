"""
Self-test for the screen agent's eyes (plugins/_screen_eyes.py) and how the
loop uses them (plugins/_screen_loop.py).

    python -m plugins._screen_eyes_tests [--save DIR]

Drawn pictures only: no screen, mouse, camera, model or network. `--save`
writes the tagged test picture to DIR so it can be looked at.
"""
from __future__ import annotations

import random
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PIL import Image, ImageDraw  # noqa: E402

from plugins import _screen_eyes as eyes  # noqa: E402

_failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        _failures.append(name)


BUTTONS = [(40, 30, 140, 62), (160, 30, 260, 62), (280, 30, 380, 62),   # toolbar
           (60, 380, 200, 412)]                                         # lone button
PANEL = (420, 200, 700, 420)
PANEL_BUTTONS = [(440, 230, 540, 262), (560, 230, 680, 262), (440, 300, 680, 332)]
PHOTO = (40, 120, 360, 340)


def ui_picture() -> Image.Image:
    img = Image.new("RGB", (720, 450), (245, 245, 245))
    d = ImageDraw.Draw(img)
    for b in BUTTONS:
        d.rectangle(b, fill=(225, 228, 232), outline=(90, 90, 100), width=2)
        d.text((b[0] + 12, b[1] + 10), "Button", fill=(20, 20, 20))
    d.rectangle(PANEL, fill=(250, 250, 252), outline=(150, 150, 160), width=2)
    for b in PANEL_BUTTONS:
        d.rectangle(b, fill=(210, 225, 245), outline=(40, 90, 160), width=2)
        d.text((b[0] + 10, b[1] + 10), "Choice", fill=(20, 20, 60))
    rnd = random.Random(7)
    px = img.load()
    for y in range(PHOTO[1], PHOTO[3]):
        for x in range(PHOTO[0], PHOTO[2]):
            px[x, y] = (rnd.randrange(256), rnd.randrange(256), rnd.randrange(256))
    return img


def _covers(box, target, slack=6) -> bool:
    return all(abs(a - b) <= slack for a, b in zip(box, target))


@dataclass
class FakeEl:
    eid: int
    role: str
    name: str
    left: int
    top: int
    width: int
    height: int
    enabled: bool = True
    protected: bool = False

    @property
    def rect(self):
        return self.left, self.top, self.width, self.height

    @property
    def center(self):
        return self.left + self.width // 2, self.top + self.height // 2

    @property
    def interactable(self):
        return self.enabled and self.role in ("button", "edit", "link")

    @property
    def label(self):
        return self.name


MON = {"left": 100, "top": 50, "width": 1440, "height": 900}   # picture is half size


def test_controls() -> None:
    els = [FakeEl(1, "button", "Save", 100 + 80, 50 + 60, 200, 64),
           FakeEl(2, "button", "Off screen", 100 + 3000, 50 + 60, 200, 64),
           FakeEl(3, "button", "Greyed", 100 + 600, 50 + 60, 200, 64, enabled=False),
           FakeEl(4, "edit", "Huge pane", 100, 50, 1440, 800),
           FakeEl(5, "text", "Just a label", 100 + 900, 50 + 60, 200, 64),
           FakeEl(6, "edit", "Password", 100 + 80, 50 + 700, 400, 50, protected=True)]
    boxes = eyes.control_boxes(els, MON, (720, 450))
    names = [el.name for _b, el in boxes]
    check("controls: only enabled pressable controls on this picture, not panes",
          names == ["Save", "Password"], str(names))
    check("controls: mouse units mapped onto the half-size picture",
          boxes and boxes[0][0] == (40, 30, 140, 62), str(boxes[:1]))


def test_shapes(save_dir: Path | None) -> None:
    img = ui_picture()
    photos = eyes.photo_cells(img)
    in_photo = {(c, r) for c in range(PHOTO[0] // 24 + 1, PHOTO[2] // 24 - 1)
                for r in range(PHOTO[1] // 24 + 1, PHOTO[3] // 24 - 1)}
    check("photo cells: the noisy area is a photo, the buttons are not",
          in_photo <= photos and (2, 1) not in photos and (20, 10) not in photos,
          f"{len(photos)} cells")
    shapes = eyes.shape_boxes(img, photos)
    if not shapes and not _cv2():
        print("SKIP  shapes (OpenCV not installed)")
        return
    for b in BUTTONS + PANEL_BUTTONS:
        check(f"shapes: button at {b} found", any(_covers(s, b) for s in shapes),
              str(sorted(shapes)[:12]))
    check("shapes: the panel holding buttons is not a mark",
          not any(_covers(s, PANEL) for s in shapes))
    inside = [s for s in shapes if s[0] >= PHOTO[0] + 8 and s[2] <= PHOTO[2] - 8
              and s[1] >= PHOTO[1] + 8 and s[3] <= PHOTO[3] - 8]
    check("shapes: nothing inside the photo", not inside, str(inside[:5]))

    els = [FakeEl(1, "button", "Save", 100 + 80, 50 + 60, 200, 64)]   # = BUTTONS[0]
    marks = eyes.find_marks(img, MON, els)
    check("marks: the control's tag is its element, no duplicate shape on it",
          sum(1 for m in marks if _covers(m.box, BUTTONS[0])) == 1
          and any(m.element is els[0] for m in marks))
    order = [(m.box[1] // (450 // 60), m.box[0]) for m in marks]
    check("marks: numbered 1..n in reading order",
          [m.n for m in marks] == list(range(1, len(marks) + 1)) and order == sorted(order))
    check("marks: capped", len(eyes.find_marks(img, MON, els, limit=3)) == 3)
    check("marks: by_number", eyes.by_number(marks, "2") is marks[1]
          and eyes.by_number(marks, 999) is None and eyes.by_number(marks, "x") is None)

    tagged = eyes.draw_marks(img, marks)
    check("draw: a tagged copy, original untouched",
          tagged is not img and img.getpixel((BUTTONS[0][0], BUTTONS[0][1] - 4)) == (245, 245, 245)
          and eyes.TAG_COLOUR in [c for _n, c in tagged.getcolors(1 << 20)])
    if save_dir:
        save_dir.mkdir(parents=True, exist_ok=True)
        tagged.save(save_dir / "screen_eyes_marks.png")
        print(f"INFO  saved {save_dir / 'screen_eyes_marks.png'}")


def _cv2() -> bool:
    try:
        import cv2  # noqa: F401
        return True
    except Exception:
        return False


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def _frames(kind: str):
    base = ui_picture()
    n = [0]

    def grab():
        n[0] += 1
        img = base.copy()
        d = ImageDraw.Draw(img)
        if kind in ("spinner", "loading-then-spinner"):
            # a small spinner on the lone button's right that flips every frame
            d.rectangle((230, 380, 262, 412), fill=(0, 0, 0) if n[0] % 2 else (255, 255, 255))
        if kind == "loading-then-spinner" and n[0] < 6:
            d.rectangle((420, 200, 700, 420), fill=(n[0] * 40 % 256,) * 3)
        if kind == "page-loading":
            d.rectangle((0, 0, 720, 450), fill=((n[0] * 60) % 256,) * 3)
        return img
    return grab


def test_settle() -> None:
    c = Clock()
    _img, s = eyes.settle(_frames("static"), clock=c, sleep=c.sleep)
    check("settle: a still screen settles after the quiet time",
          s.still and s.seconds < 1.0 and not s.moving, f"{s}")

    c = Clock()
    _img, s = eyes.settle(_frames("spinner"), clock=c, sleep=c.sleep)
    regions = s.moving_regions()
    check("settle: a spinner does not hold the wait to its limit",
          s.still and s.seconds < eyes.SETTLE_MAX_S, f"{s.seconds:.2f}s still={s.still}")
    check("settle: the spinner is reported as moving, where it is",
          len(regions) == 1 and regions[0][0] <= 320 and regions[0][2] >= 360
          and regions[0][1] <= 845 and regions[0][3] >= 915, str(regions))
    check("settle: the model is told about it", "STILL MOVING" in s.describe())

    c = Clock()
    _img, s = eyes.settle(_frames("loading-then-spinner"), clock=c, sleep=c.sleep)
    check("settle: waits for a panel that is still drawing, then settles",
          s.still and s.seconds >= 0.8, f"{s.seconds:.2f}s")

    c = Clock()
    _img, s = eyes.settle(_frames("page-loading"), clock=c, sleep=c.sleep)
    check("settle: a whole page changing is loading, not animation; the wait runs out",
          not s.still and not s.moving and abs(s.seconds - eyes.SETTLE_MAX_S) < 0.3
          and "still changing" in s.describe(), f"{s}")

    stop = threading.Event()
    stop.set()
    c = Clock()
    _img, s = eyes.settle(_frames("page-loading"), clock=c, sleep=c.sleep, stop=stop)
    check("settle: stops at once when asked", s.seconds == 0.0 and not s.still)


def test_spot_and_regions() -> None:
    a = ui_picture()
    b = a.copy()
    ImageDraw.Draw(b).rectangle((60, 380, 200, 412), fill=(30, 120, 30))   # lone button lit up
    check("spot: change under the acted point is seen", eyes.spot_change(a, b, 130, 396) > 0.3)
    check("spot: nothing changed far from it", eyes.spot_change(a, b, 600, 100) == 0.0)
    check("spot: different sizes compare as no change",
          eyes.spot_change(a, a.resize((100, 100)), 10, 10) == 0.0)
    cells = {0, 1, eyes.GRID_COLS, 40 * 1 + 20 * eyes.GRID_COLS}
    r = eyes.regions(cells)
    check("regions: touching cells merge, biggest first", len(r) == 2 and r[0][0] == 0, str(r))


def test_loop_uses_them() -> None:
    """The loop's own helpers: a tag on a control clicks the control, a tag
    on a shape clicks its middle, moving cells do not count as change."""
    try:
        from core import screenact
        from plugins._screen_loop import ScreenAgent, Shot
    except Exception as e:
        print(f"SKIP  loop integration ({type(e).__name__}: {e})")
        return

    @dataclass
    class FakeView:
        elements: list

        def by_id(self, eid):
            return next((e for e in self.elements if e.eid == eid), None)

    el = FakeEl(1, "button", "Save", 100 + 80, 50 + 60, 200, 64)
    img = ui_picture()
    marks = eyes.find_marks(img, MON, [el], shapes=False)
    marks.append(eyes.Mark(len(marks) + 1, (60, 380, 200, 412), None, "shape"))
    shot = Shot(image=img, monitor=MON, view=FakeView([el]),
                small=img.convert("L").resize((64, 36)), marks=marks)
    agent = ScreenAgent("test")
    tgt, label, vision = agent._target(shot, {"mark": 1, "what": "save"})
    check("loop: a tag on a control targets the control itself",
          tgt is el and label == "Save" and not vision)
    tgt, _label, vision = agent._target(shot, {"mark": 2, "what": "lone"})
    check("loop: a tag on a shape targets its middle in mouse units",
          isinstance(tgt, screenact.Point) and vision
          and (tgt.x, tgt.y) == (100 + 260, 50 + 792), f"{tgt}")
    tgt, why, _v = agent._target(shot, {"mark": 9})
    check("loop: an unknown tag is refused with a reason", tgt is None and "9" in why)

    at = agent._acted_at(shot, {"mark": 1})
    check("loop: acted-at point of a tag", at is not None and abs(at[0] - 125) < 1
          and abs(at[1] - 102.2) < 1, str(at))
    check("loop: acted-at point of an element id and of x/y",
          agent._acted_at(shot, {"id": 1}) is not None
          and agent._acted_at(shot, {"x": 10, "y": 20}) == (10.0, 20.0)
          and agent._acted_at(shot, None) is None)

    after_img = img.copy()
    ImageDraw.Draw(after_img).rectangle((230, 380, 262, 412), fill=(0, 0, 0))   # spinner moved
    moving = eyes.Settled(moving={i for i in range(64 * 36)
                                  if 20 <= i % 64 <= 23 and 30 <= i // 64 <= 33})
    before = Shot(image=img, monitor=MON, view=FakeView([el]),
                  small=img.convert("L").resize((64, 36)), settled=moving)
    after = Shot(image=after_img, monitor=MON, view=FakeView([el]),
                 small=after_img.convert("L").resize((64, 36)), settled=moving)
    check("loop: a moving spinner alone is not a change", agent._change(before, after)[0] == 0.0)
    ImageDraw.Draw(after_img).rectangle((60, 380, 200, 412), fill=(30, 120, 30))
    after.small = after_img.convert("L").resize((64, 36))
    m = agent._measure(before, after, (180.0, 880.0))
    check("loop: measure says something changed right where it acted",
          "right where it acted something changed" in m, m)
    m = agent._measure(before, after, (900.0, 100.0))
    check("loop: and says so when the acted point itself did not change",
          "right where it acted nothing changed" in m, m)


def main() -> int:
    save = None
    if "--save" in sys.argv:
        save = Path(sys.argv[sys.argv.index("--save") + 1])
    test_controls()
    test_shapes(save)
    test_settle()
    test_spot_and_regions()
    test_loop_uses_them()
    print(f"\n{len(_failures)} failed" if _failures else "\nALL PASSED")
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main())
