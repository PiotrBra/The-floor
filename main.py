"""The Floor - a two-player desktop quiz game inspired by the TV show.

Players take turns identifying images from a category. The host watches the
screen and presses G (good / correct) or B (bad / wrong). Each player has
their own countdown; whichever runs out of time first loses.

Run with:
    poetry install
    poetry run python main.py --images ./images --time 60
"""

from __future__ import annotations

import argparse
import csv
import os
import random
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from PyQt5.QtCore import Qt, QTimer, QElapsedTimer
from PyQt5.QtGui import QPixmap, QImage, QFont, QKeyEvent

# Pillow handles formats Qt doesn't ship a plugin for (notably AVIF).
# Importing pillow_avif registers the AVIF codec on Pillow as a side effect.
try:
    from PIL import Image  # type: ignore
    try:
        import pillow_avif  # type: ignore  # noqa: F401
    except ImportError:
        pass
except ImportError:
    Image = None  # type: ignore
from PyQt5.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)


# Supported image extensions when scanning category folders.
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".jfif", ".png", ".bmp", ".gif", ".webp", ".avif"}


def load_pixmap(path: Path) -> QPixmap:
    """Load any supported image into a QPixmap.

    Qt handles JPG/PNG/WebP via built-in plugins. AVIF (and any other format
    Qt can't decode) falls back to Pillow, which is then converted to a
    QImage and finally a QPixmap.
    """
    pix = QPixmap(str(path))
    if not pix.isNull():
        return pix
    if Image is None:
        return pix
    try:
        with Image.open(path) as img:
            rgba = img.convert("RGBA")
            data = rgba.tobytes("raw", "RGBA")
            qimg = QImage(data, rgba.width, rgba.height, QImage.Format_RGBA8888)
            # `data` is owned by Python; copy detaches the QImage from it.
            return QPixmap.fromImage(qimg.copy())
    except Exception:
        return QPixmap()

# Fixed size for the image display area. All images are scaled to fit this
# exact box so every question appears at the same on-screen resolution.
IMAGE_FRAME_WIDTH = 900
IMAGE_FRAME_HEIGHT = 600

# Fixed window sizes so every launch / new round shows the same layout.
GAME_WINDOW_WIDTH = 1000
GAME_WINDOW_HEIGHT = 900
SETUP_DIALOG_WIDTH = 460
SETUP_DIALOG_HEIGHT = 620


# ---------------------------------------------------------------------------
# Domain model
# ---------------------------------------------------------------------------


@dataclass
class Player:
    """A single player with a countdown of remaining seconds."""

    name: str
    time_left: float

    def decrement(self, seconds: float) -> None:
        """Subtract elapsed seconds; clamps to zero so we never go negative."""
        self.time_left = max(0.0, self.time_left - seconds)

    @property
    def is_out_of_time(self) -> bool:
        return self.time_left <= 0.0


@dataclass
class ImageQuestion:
    """One prompt shown to the player. Label comes from the CSV."""

    category: str
    image_path: Path
    label: str

    @property
    def answer(self) -> str:
        return self.label


def load_categories_from_csv(
    csv_path: Path, images_root: Path
) -> dict[str, list[ImageQuestion]]:
    """Parse the game's label CSV.

    Layout (semicolon-separated):
      - Header row:      ``;Nazwa pliku;Etykieta``
      - Category marker: ``Kategoria:;<CategoryName>;``
      - Image row:       ``<n>;<filename.jpg>;<label>``

    Column B (index 1) holds the filename; column C (index 2) holds the label
    shown to players. Rows whose file is missing from
    ``images_root/<category>/<filename>`` are skipped so the game only offers
    categories that actually have images on disk.
    """
    result: dict[str, list[ImageQuestion]] = {}
    if not csv_path.exists():
        return result
    with open(csv_path, encoding="utf-8", newline="") as f:
        reader = csv.reader(f, delimiter=";")
        current_category: Optional[str] = None
        for row in reader:
            # Pad short rows so indexing never raises.
            row = row + [""] * (3 - len(row))
            col_a, col_b, col_c = row[0].strip(), row[1].strip(), row[2].strip()

            if col_a.lower().startswith("kategoria"):
                current_category = col_b or None
                continue

            if not current_category or not col_b or not col_c:
                continue

            file_path = images_root / current_category / col_b
            if not file_path.exists():
                continue
            result.setdefault(current_category, []).append(
                ImageQuestion(
                    category=current_category,
                    image_path=file_path,
                    label=col_c,
                )
            )
    return {k: v for k, v in result.items() if v}


def scan_categories_from_disk(root: Path) -> dict[str, list[ImageQuestion]]:
    """Fallback when no CSV is available: use filename stems as labels."""
    result: dict[str, list[ImageQuestion]] = {}
    if not root.exists():
        return result
    for category_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        questions = [
            ImageQuestion(
                category=category_dir.name,
                image_path=p,
                label=p.stem.replace("_", " ").replace("-", " ").title(),
            )
            for p in sorted(category_dir.iterdir())
            if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS
        ]
        if questions:
            result[category_dir.name] = questions
    return result


class ImageBank:
    """Serves random questions from a pre-loaded set of categories."""

    def __init__(
        self,
        all_categories: dict[str, list[ImageQuestion]],
        selected: Optional[list[str]] = None,
    ) -> None:
        if selected is None:
            selected = list(all_categories.keys())
        self.categories: dict[str, list[ImageQuestion]] = {
            name: all_categories[name] for name in selected if name in all_categories
        }
        if not self.categories:
            raise ValueError("No categories selected.")
        # Avoid repeats until the pool is exhausted.
        self._seen: set[Path] = set()

    def next_question(self) -> ImageQuestion:
        """Pick a random category, then a random unseen question from it."""
        total = sum(len(qs) for qs in self.categories.values())
        if len(self._seen) >= total:
            self._seen.clear()

        category = random.choice(list(self.categories.keys()))
        candidates = [q for q in self.categories[category] if q.image_path not in self._seen]
        if not candidates:
            # Current category exhausted — pick any available globally.
            candidates = [
                q
                for qs in self.categories.values()
                for q in qs
                if q.image_path not in self._seen
            ]
            category = candidates[0].category
        choice = random.choice(candidates)
        self._seen.add(choice.image_path)
        return choice


# ---------------------------------------------------------------------------
# Setup dialog
# ---------------------------------------------------------------------------


@dataclass
class GameConfig:
    player_names: list[str] = field(default_factory=lambda: ["Player 1", "Player 2"])
    starting_time: int = 60
    images_root: Path = Path("images")
    csv_path: Optional[Path] = None
    selected_categories: list[str] = field(default_factory=list)


class SetupDialog(QDialog):
    """Prompt for player names, starting time, and categories to play."""

    def __init__(
        self,
        defaults: GameConfig,
        available_categories: list[str],
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("The Floor — New Game")
        # Fixed size so the setup screen looks identical every launch.
        self.setFixedSize(SETUP_DIALOG_WIDTH, SETUP_DIALOG_HEIGHT)

        self._name1 = QLineEdit(defaults.player_names[0])
        self._name2 = QLineEdit(defaults.player_names[1])
        self._time = QSpinBox()
        self._time.setRange(10, 600)
        self._time.setSuffix(" s")
        self._time.setValue(defaults.starting_time)

        form = QFormLayout()
        form.addRow("Player 1 name", self._name1)
        form.addRow("Player 2 name", self._name2)
        form.addRow("Starting time", self._time)

        # Build category checkboxes. Default to just the first category so a
        # single-category game is the starting point; the user can still tick
        # more via checkboxes or the "Select all" button.
        self._category_boxes: list[QCheckBox] = []
        if defaults.selected_categories:
            preselect = set(defaults.selected_categories)
        elif available_categories:
            preselect = {available_categories[0]}
        else:
            preselect = set()
        category_body = QWidget()
        category_layout = QVBoxLayout(category_body)
        category_layout.setContentsMargins(6, 6, 6, 6)
        for name in available_categories:
            cb = QCheckBox(name)
            cb.setChecked(name in preselect)
            category_layout.addWidget(cb)
            self._category_boxes.append(cb)
        category_layout.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(category_body)
        scroll.setMinimumHeight(160)

        # All / None helper buttons.
        all_btn = QPushButton("Select all")
        none_btn = QPushButton("Select none")
        all_btn.clicked.connect(lambda: self._set_all(True))
        none_btn.clicked.connect(lambda: self._set_all(False))
        helper_row = QHBoxLayout()
        helper_row.addWidget(all_btn)
        helper_row.addWidget(none_btn)
        helper_row.addStretch(1)

        # Quick single-category picker. Selecting a category here unchecks
        # everything else so only that one is in play. Defaults to the first
        # category — use the checkboxes below to play multiple.
        self._single_combo = QComboBox()
        for name in available_categories:
            self._single_combo.addItem(name)
        self._single_combo.currentIndexChanged.connect(self._on_single_pick)
        single_row = QHBoxLayout()
        single_row.addWidget(QLabel("Play only:"))
        single_row.addWidget(self._single_combo, 1)

        cat_group = QGroupBox("Categories")
        cat_layout = QVBoxLayout(cat_group)
        cat_layout.addLayout(single_row)
        cat_layout.addWidget(scroll)
        cat_layout.addLayout(helper_row)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(cat_group)
        layout.addWidget(buttons)

    def _set_all(self, checked: bool) -> None:
        for cb in self._category_boxes:
            cb.setChecked(checked)

    def _on_single_pick(self, index: int) -> None:
        """When a single category is chosen, uncheck all others."""
        if index < 0:
            return
        target = self._single_combo.itemText(index)
        for cb in self._category_boxes:
            cb.setChecked(cb.text() == target)

    def _on_accept(self) -> None:
        # Force the spinbox to commit whatever the user just typed — without
        # this, clicking OK before focus leaves the field returns the previous
        # value instead of the typed one.
        self._time.interpretText()
        if not any(cb.isChecked() for cb in self._category_boxes):
            QMessageBox.warning(
                self, "Pick at least one category", "Select at least one category to play."
            )
            return
        self.accept()

    def result_config(self, base: GameConfig) -> GameConfig:
        """Build a GameConfig from the dialog inputs."""
        n1 = self._name1.text().strip() or "Player 1"
        n2 = self._name2.text().strip() or "Player 2"
        selected = [cb.text() for cb in self._category_boxes if cb.isChecked()]
        return GameConfig(
            player_names=[n1, n2],
            starting_time=self._time.value(),
            images_root=base.images_root,
            selected_categories=selected,
        )


# ---------------------------------------------------------------------------
# Main game window
# ---------------------------------------------------------------------------


class GameWindow(QMainWindow):
    """Top-level window that owns the whole session.

    Runs one match at a time; on game over stays open, shows a big winner
    banner inside the image area, and offers "New Game" / "Quit" buttons.
    Clicking "New Game" reopens the setup dialog and restarts play in-place.
    """

    # Game phases
    PHASE_COUNTDOWN = "countdown"
    PHASE_PLAYING = "playing"
    PHASE_CORRECT_REVEAL = "correct_reveal"
    PHASE_WRONG_REVEAL = "wrong_reveal"
    PHASE_GAME_OVER = "game_over"

    TICK_MS = 50  # timer resolution
    COUNTDOWN_SECONDS = 3
    CORRECT_REVEAL_SECONDS = 1
    WRONG_REVEAL_SECONDS = 3

    def __init__(
        self,
        all_categories: dict[str, list[ImageQuestion]],
        initial_config: GameConfig,
    ) -> None:
        super().__init__()
        self.setWindowTitle("The Floor")
        self.all_categories = all_categories
        self.config = initial_config

        # Live match state; populated per round by `_start_match`.
        self.players: list[Player] = []
        self.bank: Optional[ImageBank] = None
        self.active_index = 0
        self.phase = self.PHASE_COUNTDOWN
        self.current_question: Optional[ImageQuestion] = None
        self.winner: Optional[Player] = None
        self._phase_seconds_left: float = 0.0

        self._build_ui()

        # Single real-time timer driving all phases.
        self._elapsed = QElapsedTimer()
        self._tick_timer = QTimer(self)
        self._tick_timer.setInterval(self.TICK_MS)
        self._tick_timer.timeout.connect(self._on_tick)

        # Fixed size so every round / new game opens at the exact same resolution.
        self.setFixedSize(GAME_WINDOW_WIDTH, GAME_WINDOW_HEIGHT)

        self._start_match(initial_config)

    # ----- UI construction -------------------------------------------------

    def _build_ui(self) -> None:
        central = QWidget()
        central.setStyleSheet("background-color: #0a1226;")
        self.setCentralWidget(central)

        # Two player panels — created once, names updated per match.
        self.player_panels: list[_PlayerPanel] = [
            _PlayerPanel("Player 1"),
            _PlayerPanel("Player 2"),
        ]
        players_row = QHBoxLayout()
        for panel in self.player_panels:
            players_row.addWidget(panel, 1)

        # Category label and status (countdown / reveal hint).
        self.category_label = QLabel("—")
        self.category_label.setAlignment(Qt.AlignCenter)
        self.category_label.setFont(QFont("Helvetica", 22, QFont.Bold))
        self.category_label.setStyleSheet("color: #e2e8f0;")

        self.status_label = QLabel("")
        self.status_label.setAlignment(Qt.AlignCenter)
        self.status_label.setFont(QFont("Helvetica", 28, QFont.Bold))
        self.status_label.setStyleSheet("color: #fbbf24;")

        # Image area. A QStackedWidget lets us swap between the quiz image
        # and a big "winner" banner at game over — both within the same
        # fixed frame, so the layout never shifts.
        self.image_label = QLabel()
        self.image_label.setAlignment(Qt.AlignCenter)
        self.image_label.setStyleSheet(
            "background-color: #0f1d3d; border: 2px solid #1e3a8a; border-radius: 8px;"
        )

        self.winner_label = QLabel()
        self.winner_label.setAlignment(Qt.AlignCenter)
        self.winner_label.setWordWrap(True)
        self.winner_label.setFont(QFont("Helvetica", 64, QFont.Bold))
        self.winner_label.setStyleSheet(
            "background-color: #0f1d3d; color: #fbbf24;"
            " border: 2px solid #1e3a8a; border-radius: 8px;"
        )

        self.countdown_label = QLabel()
        self.countdown_label.setAlignment(Qt.AlignCenter)
        self.countdown_label.setFont(QFont("Helvetica", 280, QFont.Bold))
        self.countdown_label.setStyleSheet(
            "background-color: #0f1d3d; color: #fbbf24;"
            " border: 2px solid #1e3a8a; border-radius: 8px;"
        )

        self.display_stack = QStackedWidget()
        self.display_stack.setFixedSize(IMAGE_FRAME_WIDTH, IMAGE_FRAME_HEIGHT)
        self.display_stack.addWidget(self.image_label)      # index 0: quiz image
        self.display_stack.addWidget(self.winner_label)     # index 1: game over
        self.display_stack.addWidget(self.countdown_label)  # index 2: 3-2-1 intro

        image_row = QHBoxLayout()
        image_row.addStretch(1)
        image_row.addWidget(self.display_stack)
        image_row.addStretch(1)

        # Bottom: during play show the key-hint; at game over swap it for
        # the New Game / Quit buttons.
        self.hint_label = QLabel(
            "Host controls:  G = correct   •   P = pass   •   Esc = quit"
        )
        self.hint_label.setAlignment(Qt.AlignCenter)
        self.hint_label.setStyleSheet("color: #94a3b8;")

        self.new_game_btn = QPushButton("New Game")
        self.quit_btn = QPushButton("Quit")
        button_qss = (
            "QPushButton {"
            " background-color: #1e3a8a; color: #ffffff;"
            " border: 2px solid #2a4080; border-radius: 6px;"
            " padding: 6px 16px;"
            "}"
            "QPushButton:hover { background-color: #274aa3; }"
            "QPushButton:pressed { background-color: #16306f; }"
        )
        for btn in (self.new_game_btn, self.quit_btn):
            btn.setMinimumHeight(48)
            btn.setMinimumWidth(160)
            btn.setFont(QFont("Helvetica", 16, QFont.Bold))
            btn.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
            btn.setStyleSheet(button_qss)
        self.new_game_btn.clicked.connect(self._on_new_game)
        self.quit_btn.clicked.connect(self._on_quit)

        self.button_row_widget = QWidget()
        btn_row = QHBoxLayout(self.button_row_widget)
        btn_row.setContentsMargins(0, 0, 0, 0)
        btn_row.addStretch(1)
        btn_row.addWidget(self.new_game_btn)
        btn_row.addSpacing(20)
        btn_row.addWidget(self.quit_btn)
        btn_row.addStretch(1)
        self.button_row_widget.setVisible(False)

        layout = QVBoxLayout(central)
        layout.addLayout(players_row)
        layout.addWidget(self.category_label)
        layout.addWidget(self.status_label)
        layout.addLayout(image_row, 1)
        layout.addWidget(self.hint_label)
        layout.addWidget(self.button_row_widget)

    # ----- Match lifecycle -------------------------------------------------

    def _start_match(self, config: GameConfig) -> None:
        """(Re)initialize all match state and start the countdown."""
        self.config = config
        self.players = [
            Player(name=name, time_left=float(config.starting_time))
            for name in config.player_names
        ]
        self.bank = ImageBank(self.all_categories, config.selected_categories)
        self.active_index = 0
        # Show a 3-2-1 intro before the first image so players have time to focus.
        self.phase = self.PHASE_COUNTDOWN
        self.winner = None
        self._phase_seconds_left = float(self.COUNTDOWN_SECONDS)

        # Update per-match UI: player names, show image view + hint, hide buttons.
        for panel, player in zip(self.player_panels, self.players):
            panel.set_name(player.name)
        self.button_row_widget.setVisible(False)
        self.hint_label.setVisible(True)
        self.category_label.setVisible(True)

        # First image is pre-loaded so it appears the instant the countdown ends.
        self.current_question = self.bank.next_question()
        self._render_question()
        self.display_stack.setCurrentIndex(2)  # show countdown over the image
        self._render_players()
        self._render_status()

        self._elapsed.restart()
        self._tick_timer.start()

    # ----- Rendering -------------------------------------------------------

    def _render_players(self) -> None:
        for idx, (panel, player) in enumerate(zip(self.player_panels, self.players)):
            panel.set_time(player.time_left)
            active = (
                idx == self.active_index
                and self.phase
                in (
                    self.PHASE_PLAYING,
                    self.PHASE_WRONG_REVEAL,
                    self.PHASE_CORRECT_REVEAL,
                )
            )
            panel.set_active(active)

    def _render_question(self) -> None:
        if self.current_question is None:
            self.category_label.setText("—")
            self.image_label.clear()
            return
        self.category_label.setText(f"Category: {self.current_question.category}")
        pix = load_pixmap(self.current_question.image_path)
        if pix.isNull():
            self.image_label.setText(
                f"[could not load image]\n{self.current_question.image_path}"
            )
            self.image_label.setStyleSheet(
                "color: #f87171; background-color: #0f1d3d;"
                " border: 2px solid #1e3a8a; border-radius: 8px;"
            )
        else:
            # Image area is a fixed box, so every image renders at the same
            # on-screen size (KeepAspectRatio letterboxes non-matching ratios).
            scaled = pix.scaled(
                IMAGE_FRAME_WIDTH,
                IMAGE_FRAME_HEIGHT,
                Qt.KeepAspectRatio,
                Qt.SmoothTransformation,
            )
            self.image_label.setPixmap(scaled)

    def _render_status(self) -> None:
        if self.phase == self.PHASE_COUNTDOWN:
            seconds = max(1, int(self._phase_seconds_left) + 1)
            self.countdown_label.setText(str(seconds))
            self.status_label.setStyleSheet("color: #fbbf24;")
            self.status_label.setText("Get ready…")
        elif self.phase == self.PHASE_PLAYING:
            self.status_label.setStyleSheet("color: #10b981;")
            self.status_label.setText(
                f"{self.players[self.active_index].name}, twoja kolej"
            )
        elif self.phase == self.PHASE_CORRECT_REVEAL:
            # Brief label flash after a correct answer; clock is frozen.
            self.status_label.setStyleSheet("color: #10b981;")
            answer = self.current_question.answer if self.current_question else ""
            self.status_label.setText(answer)
        elif self.phase == self.PHASE_WRONG_REVEAL:
            # Just the label — no "Wrong!" prefix per design.
            self.status_label.setStyleSheet("color: #ef4444;")
            answer = self.current_question.answer if self.current_question else ""
            self.status_label.setText(answer)
        elif self.phase == self.PHASE_GAME_OVER:
            self.status_label.setStyleSheet("color: #3b82f6;")

    # ----- Core tick / state machine --------------------------------------

    def _on_tick(self) -> None:
        """Called every TICK_MS. Advances whichever clock is running."""
        delta = self._elapsed.restart() / 1000.0

        if self.phase == self.PHASE_COUNTDOWN:
            self._phase_seconds_left -= delta
            if self._phase_seconds_left <= 0:
                self.phase = self.PHASE_PLAYING
                self.display_stack.setCurrentIndex(0)  # reveal the first image
                self._render_status()
                self._render_players()
            else:
                self._render_status()
            return

        if self.phase == self.PHASE_CORRECT_REVEAL:
            # Clock is frozen — we just flash the label, then hand off.
            self._phase_seconds_left -= delta
            if self._phase_seconds_left <= 0:
                self.active_index = (self.active_index + 1) % len(self.players)
                self.current_question = self.bank.next_question()
                self.phase = self.PHASE_PLAYING
                self._render_question()
                self._render_status()
                self._render_players()
            return

        if self.phase in (self.PHASE_PLAYING, self.PHASE_WRONG_REVEAL):
            active = self.players[self.active_index]
            active.decrement(delta)
            self._render_players()

            if active.is_out_of_time:
                self._end_game(loser_index=self.active_index)
                return

            if self.phase == self.PHASE_WRONG_REVEAL:
                self._phase_seconds_left -= delta
                if self._phase_seconds_left <= 0:
                    # Same player continues with a fresh image.
                    self.current_question = self.bank.next_question()
                    self.phase = self.PHASE_PLAYING
                    self._render_question()
                    self._render_status()
                    self._render_players()

    # ----- Keyboard handling ----------------------------------------------

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        key = event.key()
        if key == Qt.Key_Escape:
            self.close()
            return

        if self.phase != self.PHASE_PLAYING:
            return

        if key == Qt.Key_G:
            self._handle_correct()
        elif key == Qt.Key_P:
            self._handle_wrong()
        else:
            super().keyPressEvent(event)

    def _handle_correct(self) -> None:
        # Enter a brief label-reveal during which the clock is frozen; the
        # tick loop switches to the next player after CORRECT_REVEAL_SECONDS.
        self.phase = self.PHASE_CORRECT_REVEAL
        self._phase_seconds_left = float(self.CORRECT_REVEAL_SECONDS)
        self._render_status()
        self._render_players()

    def _handle_wrong(self) -> None:
        self.phase = self.PHASE_WRONG_REVEAL
        self._phase_seconds_left = float(self.WRONG_REVEAL_SECONDS)
        self._render_status()
        self._render_players()

    # ----- End of game -----------------------------------------------------

    def _end_game(self, loser_index: int) -> None:
        """Freeze the match and show the winner banner + action buttons."""
        self.phase = self.PHASE_GAME_OVER
        self._tick_timer.stop()
        self.winner = (
            self.players[1 - loser_index] if len(self.players) == 2 else None
        )
        self._render_players()

        # Swap the image frame for a big winner banner; no window close.
        if self.winner is not None:
            self.winner_label.setText(f"🏆\n{self.winner.name}\nwins!")
        else:
            self.winner_label.setText("Game over")
        self.display_stack.setCurrentIndex(1)

        # Clear the play-time chrome and surface the action buttons.
        self.category_label.setText("")
        self.status_label.setText("Play again?")
        self.status_label.setStyleSheet("color: #3b82f6;")
        self.hint_label.setVisible(False)
        self.button_row_widget.setVisible(True)

    # ----- New game / quit -------------------------------------------------

    def _on_new_game(self) -> None:
        """Open the setup dialog and, on OK, restart in the same window."""
        # Each new round should prompt a fresh category pick.
        self.config.selected_categories = []
        setup = SetupDialog(
            self.config,
            available_categories=list(self.all_categories.keys()),
            parent=self,
        )
        if setup.exec_() != QDialog.Accepted:
            return
        new_config = setup.result_config(self.config)
        try:
            self._start_match(new_config)
        except ValueError as exc:
            QMessageBox.critical(self, "Cannot start game", str(exc))

    def _on_quit(self) -> None:
        self.close()


class _PlayerPanel(QWidget):
    """Small widget showing a single player's name and remaining time."""

    def __init__(self, name: str) -> None:
        super().__init__()
        self.name_label = QLabel(name)
        self.name_label.setAlignment(Qt.AlignCenter)
        self.name_label.setFont(QFont("Helvetica", 20, QFont.Bold))

        self.time_label = QLabel("--")
        self.time_label.setAlignment(Qt.AlignCenter)
        self.time_label.setFont(QFont("Helvetica", 48, QFont.Bold))

        layout = QVBoxLayout(self)
        layout.addWidget(self.name_label)
        layout.addWidget(self.time_label)
        self.setStyleSheet(
            "QWidget { background-color: #15224a; border-radius: 10px; color: #e2e8f0; }"
        )

    def set_name(self, name: str) -> None:
        self.name_label.setText(name)

    def set_time(self, seconds: float) -> None:
        whole = int(seconds + 0.999)
        self.time_label.setText(f"{whole:02d}")
        color = "#f87171" if seconds <= 10 else "#e2e8f0"
        self.time_label.setStyleSheet(f"color: {color};")

    def set_active(self, active: bool) -> None:
        if active:
            self.setStyleSheet(
                "QWidget { background-color: #1e3a8a; border: 3px solid #60a5fa;"
                " border-radius: 10px; color: #ffffff; }"
            )
        else:
            self.setStyleSheet(
                "QWidget { background-color: #15224a; border: 3px solid transparent;"
                " border-radius: 10px; color: #e2e8f0; }"
            )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _bundled_path(relative: str) -> Path:
    """Locate an asset shipped with the app.

    When running as a PyInstaller binary, data files are unpacked under
    ``sys._MEIPASS`` (onefile) or sit next to the executable (onedir).
    From source, assets live next to ``main.py``.
    """
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    else:
        base = Path(__file__).resolve().parent
    return base / relative


def _parse_args(argv: list[str]) -> GameConfig:
    parser = argparse.ArgumentParser(description="The Floor quiz game")
    parser.add_argument(
        "--images",
        type=Path,
        default=Path(os.environ.get("THEFLOOR_IMAGES") or _bundled_path("images")),
        help="Root folder containing category subfolders of images",
    )
    parser.add_argument(
        "--csv",
        type=Path,
        default=Path(os.environ.get("THEFLOOR_CSV") or _bundled_path("excel.csv")),
        help="CSV with labels (column B = filename, column C = label)",
    )
    parser.add_argument("--time", type=int, default=60)
    parser.add_argument("--p1", type=str, default="Player 1")
    parser.add_argument("--p2", type=str, default="Player 2")
    args = parser.parse_args(argv)
    return GameConfig(
        player_names=[args.p1, args.p2],
        starting_time=args.time,
        images_root=args.images,
        csv_path=args.csv,
    )


def main(argv: Optional[list[str]] = None) -> int:
    config = _parse_args(argv if argv is not None else sys.argv[1:])

    app = QApplication(sys.argv)
    app.setStyleSheet(
        """
        QDialog, QMessageBox { background-color: #0a1226; color: #e2e8f0; }
        QLabel { color: #e2e8f0; }
        QGroupBox {
            color: #e2e8f0;
            border: 1px solid #1e3a8a;
            border-radius: 6px;
            margin-top: 14px;
            padding-top: 10px;
        }
        QGroupBox::title {
            subcontrol-origin: margin;
            left: 10px;
            padding: 0 4px;
        }
        QLineEdit, QSpinBox, QComboBox {
            background-color: #15224a;
            color: #e2e8f0;
            border: 1px solid #2a4080;
            border-radius: 4px;
            padding: 4px 8px;
            selection-background-color: #1e3a8a;
        }
        QComboBox QAbstractItemView {
            background-color: #15224a;
            color: #e2e8f0;
            selection-background-color: #1e3a8a;
        }
        QCheckBox { color: #e2e8f0; spacing: 6px; }
        QScrollArea {
            background-color: #0f1d3d;
            border: 1px solid #1e3a8a;
            border-radius: 6px;
        }
        QScrollArea > QWidget > QWidget { background-color: #0f1d3d; }
        QDialog QPushButton, QMessageBox QPushButton {
            background-color: #1e3a8a; color: #ffffff;
            border: 1px solid #2a4080; border-radius: 4px;
            padding: 6px 14px;
            min-width: 80px;
        }
        QDialog QPushButton:hover, QMessageBox QPushButton:hover {
            background-color: #274aa3;
        }
        QDialog QPushButton:pressed, QMessageBox QPushButton:pressed {
            background-color: #16306f;
        }
        """
    )

    # Load categories + labels. If a CSV is configured and exists, use it as
    # the authoritative source (column B → filename, column C → label).
    # Otherwise fall back to scanning the image folder.
    root = config.images_root.expanduser().resolve()
    csv_path = config.csv_path.expanduser().resolve() if config.csv_path else None
    if csv_path and csv_path.exists():
        all_categories = load_categories_from_csv(csv_path, root)
        source_msg = f"CSV: {csv_path}\nImages: {root}"
    else:
        all_categories = scan_categories_from_disk(root)
        source_msg = f"Images: {root}"

    if not all_categories:
        QMessageBox.critical(
            None,
            "No images found",
            f"No playable images found.\n\n{source_msg}",
        )
        return 1

    # Initial setup dialog; subsequent rounds are launched from inside the
    # game window via the "New Game" button.
    setup = SetupDialog(config, available_categories=list(all_categories.keys()))
    if setup.exec_() != QDialog.Accepted:
        return 0
    config = setup.result_config(config)

    try:
        window = GameWindow(all_categories, config)
    except ValueError as exc:
        QMessageBox.critical(None, "Cannot start game", str(exc))
        return 1
    window.show()
    return app.exec_()


if __name__ == "__main__":
    sys.exit(main())
