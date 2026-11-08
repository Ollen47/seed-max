"""
Anti-Klepto Check: the seed typed word by word (BIP39 English), for users who keep their
backup as words and not as a SeedQR.

- 12 or 24 words.
- One screen per word: a QWERTY keyboard where only the letters that can still lead to a
  BIP39 word are active, and the matching words above it to tap (four letters always
  identify a BIP39 word). "Back" goes back to the previous word.
- A review of all the words at the end, every time, with the checksum: "Confirm" works
  only with a valid checksum; tapping a word re-types it.

Same rules as the rest of the verifier: its own code (no SeedSigner GUI), only the
wordlist of nonceverify (checked against its SHA-256 at load), nothing logged, nothing
written anywhere; the words live in one list that is cleared as soon as the mnemonic is
handed over.
"""
from nonceverify import seedqr, ui
from nonceverify.hw import HEIGHT, WIDTH, Event

WORD_COUNTS = (12, 24)


def _wordlist():
    return seedqr.wordlist()


def matches(prefix: str) -> list:
    """ BIP39 words starting with `prefix`, in wordlist (alphabetical) order """
    return [w for w in _wordlist() if w.startswith(prefix)] if prefix else []


def next_letters(prefix: str) -> set:
    """ Letters that, typed after `prefix`, still lead to at least one BIP39 word """
    n = len(prefix)
    return {w[n] for w in _wordlist() if len(w) > n and w.startswith(prefix)}


class WordCountScreen(ui.Screen):
    step = 0

    def __init__(self):
        super().__init__()
        self.buttons = ui.button_row(["12 words", "24 words"], 150, height=60, values=[12, 24])
        self.buttons += ui.bottom_row(["Back"], values=["back"], icons=["back"])

    def draw(self, d, image):
        ui.title(d, "Type the seed word by word")
        ui.paragraph(d, "How many words does your seed have?", ui.TOP + 44, size=19, color=ui.DIM)
        ui.paragraph(d, "They stay in RAM only; at the end you review them all, with the checksum.",
                     ui.TOP + 186, size=15, color=ui.DIM)


class WordEntryScreen(ui.Screen):
    """ Typing word `index` (0-based) of `count`. Returns ("word", w), ("prev",) or ("cancel",) """
    step = 0
    ROWS = ("qwertyuiop", "asdfghjkl", "zxcvbnm")
    KEY_W = 46
    KEY_H = 44
    KEYS_TOP = 124
    CAND_TOP = 76
    CAND_H = 42
    CAND_SLOTS = 4
    CONTROLS_TOP = 270

    def __init__(self, index: int, count: int, text: str = "", back_label: str = "Back"):
        super().__init__()
        self.index, self.count = index, count
        self.text = text
        self.back_label = back_label

    @classmethod
    def key_rect(cls, letter: str):
        for row, chars in enumerate(cls.ROWS):
            if letter in chars:
                total = len(chars) * cls.KEY_W + (len(chars) - 1) * 2
                x = (WIDTH - total) / 2 + chars.index(letter) * (cls.KEY_W + 2)
                y = cls.KEYS_TOP + row * (cls.KEY_H + 4)
                return (int(x), int(y), int(x + cls.KEY_W), int(y + cls.KEY_H))
        raise KeyError(letter)

    @classmethod
    def candidate_rect(cls, slot: int):
        width = (WIDTH - ui.PAD * (cls.CAND_SLOTS + 1)) / cls.CAND_SLOTS
        x = ui.PAD + slot * (width + ui.PAD)
        return (int(x), cls.CAND_TOP, int(x + width), cls.CAND_TOP + cls.CAND_H)

    @classmethod
    def control_rects(cls):
        y1, y2 = cls.CONTROLS_TOP, HEIGHT - ui.PAD + 4
        return {"prev": (ui.PAD, y1, 160, y2), "erase": (170, y1, 310, y2), "cancel": (320, y1, WIDTH - ui.PAD, y2)}

    def candidates(self) -> list:
        return matches(self.text)[:self.CAND_SLOTS]

    def draw(self, d, image):
        ui.spaced(d, (ui.PAD, ui.TOP + 2), f"WORD {self.index + 1} OF {self.count}",
                  ui.font(12, bold=True, mono=True), ui.DIM, 1, anchor="lt")
        found = matches(self.text)
        if self.text:
            note = f"{len(found)} words" if len(found) > self.CAND_SLOTS else ""
            d.text((WIDTH - ui.PAD, ui.TOP + 2), note, fill=ui.DIM, font=ui.font(12, mono=True), anchor="rt")
        d.rounded_rectangle((ui.PAD, ui.TOP + 17, WIDTH - ui.PAD, ui.TOP + 36), radius=4, fill=ui.PANEL,
                            outline=ui.ACCENT, width=1)
        d.text((2 * ui.PAD, ui.TOP + 27), self.text + "_", fill=ui.TEXT, font=ui.font(18, bold=True, mono=True),
               anchor="lm")
        for slot, word in enumerate(found[:self.CAND_SLOTS]):
            ui.Button(self.candidate_rect(slot), word, value=("word", word), primary=(len(found) == 1),
                      size=17).draw(d)
        if not self.text:
            d.text((WIDTH / 2, self.CAND_TOP + self.CAND_H / 2), "type the first letters",
                   fill=ui.DIM, font=ui.font(15), anchor="mm")
        allowed = next_letters(self.text)
        for chars in self.ROWS:
            for c in chars:
                rect = self.key_rect(c)
                if c in allowed:
                    ui.Button(rect, c, size=21).draw(d)
                else:
                    d.rounded_rectangle(rect, radius=5, fill=ui.BACKGROUND, outline=(24, 32, 42), width=1)
                    d.text(((rect[0] + rect[2]) / 2, (rect[1] + rect[3]) / 2), c, fill=(52, 62, 74),
                           font=ui.font(21, bold=True), anchor="mm")
        rects = self.control_rects()
        ui.Button(rects["prev"], self.back_label, size=17, icon_name="back").draw(d)
        ui.Button(rects["erase"], "Delete", size=17, icon_name="erase").draw(d)
        ui.Button(rects["cancel"], "Cancel", size=17).draw(d)

    def on_event(self, event):
        if event.kind != Event.TAP:
            return None
        x, y = event.x, event.y

        def inside(rect, margin=1):
            return rect[0] - margin <= x <= rect[2] + margin and rect[1] - margin <= y <= rect[3] + margin

        for slot, word in enumerate(self.candidates()):
            if inside(self.candidate_rect(slot), 2):
                return ("word", word)
        allowed = next_letters(self.text)
        for chars in self.ROWS:
            for c in chars:
                if inside(self.key_rect(c)):
                    if c not in allowed:
                        return None             # dimmed: no BIP39 word goes that way
                    self.text += c
                    return "__redraw__"
        rects = self.control_rects()
        if inside(rects["erase"], 2):
            if self.text:
                self.text = self.text[:-1]
                return "__redraw__"
            return None
        if inside(rects["prev"], 2):
            return ("prev",)
        if inside(rects["cancel"], 2):
            return ("cancel",)
        return None


class SeedReviewScreen(ui.Screen):
    """
    All the words, numbered, with the checksum. Returns "ok" (valid checksum only),
    ("edit", index) or "cancel".
    """
    step = 0
    GRID_TOP = ui.TOP + 30

    def __init__(self, words: list):
        super().__init__()
        self.words = words
        self.valid = seedqr.mnemonic_is_valid(" ".join(words))
        self.buttons = ui.bottom_row(["Cancel", "Confirm" if self.valid else "Checksum wrong"],
                                     primary_index=1 if self.valid else None, values=["cancel", "ok"])
        if not self.valid:
            self.buttons[1].color = ui.RED

    def layout(self):
        n = len(self.words)
        cols = 3 if n <= 12 else 4
        rows = (n + cols - 1) // cols
        bottom = HEIGHT - ui.BUTTON_H - 2 * ui.PAD
        cell_w = (WIDTH - 2 * ui.PAD) / cols
        cell_h = (bottom - self.GRID_TOP) / rows
        cells = []
        for i in range(n):
            col, row = i // rows, i % rows          # numbered down each column, as on paper
            x = ui.PAD + col * cell_w
            y = self.GRID_TOP + row * cell_h
            cells.append((int(x), int(y), int(x + cell_w - 4), int(y + cell_h - 3)))
        return cells

    def draw(self, d, image):
        if self.valid:
            ui.icon(d, "ok_circle", (ui.PAD + 11, ui.TOP + 13), 20, ui.GREEN)
            text, color = "Checksum OK: compare the words with your backup", ui.TEXT
        else:
            ui.icon(d, "x_circle", (ui.PAD + 11, ui.TOP + 13), 20, ui.RED)
            text, color = "Checksum wrong: tap the wrong word", ui.RED
        d.text((ui.PAD + 28, ui.TOP + 13), text, fill=color, font=ui.font(16, bold=True), anchor="lm")
        f_num, f_word = ui.font(13, mono=True), ui.font(17 if len(self.words) <= 12 else 15, bold=True)
        for i, rect in enumerate(self.layout()):
            d.rounded_rectangle(rect, radius=4, fill=ui.PANEL, outline=ui.LINE, width=1)
            cy = (rect[1] + rect[3]) / 2
            d.text((rect[0] + 22, cy), f"{i + 1}", fill=ui.DIM, font=f_num, anchor="rm")
            d.text((rect[0] + 28, cy), self.words[i], fill=ui.TEXT, font=f_word, anchor="lm")

    def on_event(self, event):
        if event.kind != Event.TAP:
            return None
        for i, (x1, y1, x2, y2) in enumerate(self.layout()):
            if x1 <= event.x <= x2 and y1 <= event.y <= y2:
                return ("edit", i)
        for b in self.buttons:
            if b.contains(event.x, event.y):
                if b.value == "ok" and not self.valid:
                    return None                 # only a valid checksum goes on
                return b.value
        return None


def confirm_discard(hw, typed: int) -> bool:
    """ Leaving the word entry throws the typed words away: ask first (Cancel sits next to Delete) """
    if typed == 0:
        return True
    choice = ui.MessageScreen("Cancel the seed entry?",
                              f"The {typed} words typed so far will be erased and you will start again.",
                              buttons=("Continue", "Cancel all"), primary_index=0, values=["keep", "drop"],
                              heading_color=ui.YELLOW, icon_name="warning").run(hw)
    return choice == "drop"


def enter_mnemonic(hw):
    """ The whole word-entry flow. Returns the mnemonic (valid checksum) or None if cancelled """
    count = WordCountScreen().run(hw)
    if count not in WORD_COUNTS:
        return None
    words = [""] * count
    try:
        index, reviewing = 0, False
        while True:
            if index >= count:
                choice = SeedReviewScreen(words).run(hw)
                if choice == "ok":
                    return " ".join(words)
                if choice == "cancel":
                    if confirm_discard(hw, sum(1 for w in words if w)):
                        return None
                    continue
                if isinstance(choice, tuple) and choice[0] == "edit":
                    index, reviewing = choice[1], True
                continue
            # From the review the word is re-typed from scratch (it is the wrong one); going
            # back with "Indietro" keeps it, to fix just its last letters
            screen = WordEntryScreen(index, count, "" if reviewing else words[index],
                                     back_label="Review" if reviewing else "Back")
            result = screen.run(hw)
            screen.text = ""
            typed = sum(1 for w in words if w)
            if result[0] == "cancel":
                if confirm_discard(hw, typed):
                    return None
                continue
            if result[0] == "word":
                words[index] = result[1]
                index = count if reviewing else index + 1
            elif result[0] == "prev":
                if reviewing:
                    index = count
                elif index > 0:
                    index -= 1
                elif confirm_discard(hw, typed):
                    return None
    finally:
        for i in range(len(words)):
            words[i] = ""
