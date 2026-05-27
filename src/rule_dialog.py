"""In-app rules editor.

Lets the user pick mods from the catalog, set min-tier / min-value, combine
with AND/OR, and save back to config.json. Designed to be unobtrusive: one
dialog, native widgets, live preview of the compiled regex.
"""

from __future__ import annotations

from typing import Callable, List, Optional

from PyQt6.QtCore import QSortFilterProxyModel, Qt
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QCompleter, QDialog, QDialogButtonBox, QFormLayout,
    QGroupBox, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QPushButton,
    QRadioButton, QSpinBox, QVBoxLayout, QWidget,
)


def _make_searchable(combo: QComboBox) -> None:
    """Turn a QComboBox into a typeable, case-insensitive substring-filtered
    picker. The line edit accepts free text; the completer popup shows all
    entries whose display string *contains* the typed substring."""
    combo.setEditable(True)
    combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
    combo.lineEdit().setPlaceholderText("type to filter…")

    proxy = QSortFilterProxyModel(combo)
    proxy.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
    proxy.setSourceModel(combo.model())

    completer = QCompleter(proxy, combo)
    completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
    completer.setFilterMode(Qt.MatchFlag.MatchContains)
    completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
    combo.setCompleter(completer)

    combo.lineEdit().textEdited.connect(proxy.setFilterFixedString)

    def _select_by_text(text: str) -> None:
        idx = combo.findText(text, Qt.MatchFlag.MatchFixedString)
        if idx >= 0:
            combo.setCurrentIndex(idx)

    completer.activated.connect(_select_by_text)

from .mod_db import Mod, ModDB


DIALOG_CSS = """
QDialog { background-color: #17191f; color: #d7dae0; }
QLabel, QCheckBox, QRadioButton { color: #d7dae0; }
QGroupBox { color: #e5c07b; border: 1px solid #2a2f3a; border-radius: 6px;
             margin-top: 10px; padding-top: 10px; }
QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; }
QComboBox, QSpinBox, QListWidget {
    background-color: #23262d; color: #d7dae0; border: 1px solid #2f3340;
    border-radius: 4px; padding: 3px; selection-background-color: #3b4252;
}
QSpinBox { padding: 3px 6px; min-width: 72px; font-size: 13px; }
QSpinBox::up-button, QSpinBox::down-button { width: 16px; }
QSpinBox:disabled { color: #555b66; background-color: #1b1d22; }
QListWidget::item { padding: 4px 2px; }
QListWidget::item:selected { background-color: #2c313c; color: #e5c07b; }
QComboBox QAbstractItemView {
    background-color: #17191f; color: #d7dae0; border: 1px solid #2f3340;
    selection-background-color: #2c313c; selection-color: #e5c07b;
    outline: 0;
}
QComboBox QLineEdit { background: transparent; color: #d7dae0; border: none; }
QPushButton {
    background-color: #23262d; color: #d7dae0; border: 1px solid #2f3340;
    border-radius: 6px; padding: 5px 12px;
}
QPushButton:hover { background-color: #2c313c; }
QPushButton[role="primary"] { background-color: #2a3a2a; color: #98c379; border-color: #3a5a3a; }
QPushButton[role="danger"]  { background-color: #3b2d2d; color: #ff5f56; border-color: #5a3838; }
"""


class RuleDialog(QDialog):
    """Edits ``cfg['targets']`` in place and returns the new dict via ``result_targets``."""

    def __init__(
        self,
        mod_db: ModDB,
        current_targets: dict,
        on_save: Callable[[dict], None],
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Target rules")
        self.setMinimumSize(560, 480)
        self.setStyleSheet(DIALOG_CSS)
        self.mod_db = mod_db
        self.on_save = on_save
        self.result_targets = dict(current_targets)
        self.rules: List[dict] = list(current_targets.get("rules") or [])

        self._build()
        self._populate_items()
        self._refresh_rules_list()

    # --- layout --------------------------------------------------------

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(10)

        header = QLabel(
            f"Catalog: <b>{self.mod_db.catalog_version}</b> · "
            f"{sum(len(i.mods) for i in self.mod_db.items.values())} mods loaded"
        )
        header.setStyleSheet("color: #7a808c; font-size: 11px;")
        root.addWidget(header)

        # Mode selector
        mode_box = QGroupBox("Match mode")
        mode_layout = QHBoxLayout(mode_box)
        self.mode_any = QRadioButton("ANY rule matches (OR)")
        self.mode_all = QRadioButton("ALL rules match (AND)")
        if (self.result_targets.get("mode") or "any_of") == "all_of":
            self.mode_all.setChecked(True)
        else:
            self.mode_any.setChecked(True)
        mode_layout.addWidget(self.mode_any)
        mode_layout.addWidget(self.mode_all)
        mode_layout.addStretch()
        root.addWidget(mode_box)

        # Existing rules list
        rules_box = QGroupBox("Active rules")
        rl = QVBoxLayout(rules_box)
        hint = QLabel(
            "Check a rule to mark it <b>Required</b> (must-have). Unchecked rules "
            "are scored by match mode above. Example: one required + two unchecked "
            "with mode=ANY means <i>\"must have the required mod, AND any of the rest\"</i>."
        )
        hint.setStyleSheet("color: #7a808c; font-size: 10px;")
        hint.setWordWrap(True)
        rl.addWidget(hint)
        self.rules_list = QListWidget()
        self.rules_list.setAlternatingRowColors(True)
        self.rules_list.itemChanged.connect(self._on_rule_toggled)
        rl.addWidget(self.rules_list)
        btns = QHBoxLayout()
        self.remove_btn = QPushButton("Remove selected")
        self.remove_btn.setProperty("role", "danger")
        self.remove_btn.clicked.connect(self._remove_selected)
        btns.addWidget(self.remove_btn)
        btns.addStretch()
        rl.addLayout(btns)
        root.addWidget(rules_box, 1)

        # Add new rule
        add_box = QGroupBox("Add rule")
        form = QFormLayout(add_box)
        form.setContentsMargins(10, 10, 10, 10)

        self.item_combo = QComboBox()
        self.item_combo.currentIndexChanged.connect(self._on_item_changed)
        form.addRow("Item type:", self.item_combo)

        self.mod_combo = QComboBox()
        self.mod_combo.setMinimumContentsLength(40)
        self.mod_combo.currentIndexChanged.connect(self._on_mod_changed)
        _make_searchable(self.mod_combo)
        form.addRow("Mod:", self.mod_combo)

        mode_row = QHBoxLayout()
        self.mode_any   = QRadioButton("Any roll")
        self.mode_tier  = QRadioButton("Min tier T")
        self.mode_value = QRadioButton("Min value ≥")
        self.mode_any.setChecked(True)

        self.tier_spin = QSpinBox()
        self.tier_spin.setMinimum(1)
        self.tier_spin.setMaximum(20)
        self.tier_spin.setValue(1)
        self.tier_spin.setEnabled(False)

        self.value_spin = QSpinBox()
        self.value_spin.setMinimum(0)
        self.value_spin.setMaximum(100000)
        self.value_spin.setEnabled(False)

        mode_row.addWidget(self.mode_any)
        mode_row.addSpacing(6)
        mode_row.addWidget(self.mode_tier)
        mode_row.addWidget(self.tier_spin)
        mode_row.addSpacing(6)
        mode_row.addWidget(self.mode_value)
        mode_row.addWidget(self.value_spin)
        mode_row.addStretch()
        form.addRow("Match when:", mode_row)

        self.mode_any.toggled.connect(self._on_mode_changed)
        self.mode_tier.toggled.connect(self._on_mode_changed)
        self.mode_value.toggled.connect(self._on_mode_changed)
        self.tier_spin.valueChanged.connect(self._refresh_preview)
        self.value_spin.valueChanged.connect(self._refresh_preview)

        self.preview = QLabel("—")
        self.preview.setWordWrap(True)
        self.preview.setStyleSheet("color: #61afef; font-family: Consolas, monospace; font-size: 10px;")
        form.addRow("Resolves to:", self.preview)

        add_btn = QPushButton("+ Add rule")
        add_btn.setProperty("role", "primary")
        add_btn.clicked.connect(self._add_rule)
        form.addRow(add_btn)

        root.addWidget(add_box)

        # OK / cancel
        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        bb.accepted.connect(self._on_save)
        bb.rejected.connect(self.reject)
        root.addWidget(bb)

    # --- combos --------------------------------------------------------

    def _populate_items(self) -> None:
        self.item_combo.clear()
        for item_id, display in self.mod_db.item_types():
            self.item_combo.addItem(display, userData=item_id)
        if self.item_combo.count() == 0:
            self.item_combo.addItem("(no catalog loaded)", userData=None)

    def _on_item_changed(self) -> None:
        item_id = self.item_combo.currentData()
        self.mod_combo.clear()
        le = self.mod_combo.lineEdit()
        if le is not None:
            le.clear()
        if not item_id:
            return
        for mod in self.mod_db.mods_for(item_id):
            prefix = "★ " if mod.god_mod else ""
            observed = f" · n={mod.n_observed}" if mod.n_observed else ""
            label = f"{prefix}{mod.display_name}{observed}"
            self.mod_combo.addItem(label, userData=mod.id)
        self._on_mod_changed()

    def _current_mod(self) -> Optional[Mod]:
        mod_id = self.mod_combo.currentData()
        if not mod_id:
            return None
        return self.mod_db.get_mod(mod_id)

    def _on_mod_changed(self) -> None:
        mod = self._current_mod()
        if not mod:
            self.tier_spin.setMaximum(1)
            self.value_spin.setValue(0)
            self._refresh_preview()
            return
        tiers = mod.tiers_sorted()
        if tiers:
            self.tier_spin.setMaximum(max(t.t for t in tiers))
            self.tier_spin.setValue(min(t.t for t in tiers))
            self.value_spin.setValue(int(tiers[0].min_value))
        else:
            suggested = mod.suggested_min_value()
            if suggested is not None:
                self.value_spin.setValue(int(suggested))
        # Disable tier mode when the catalog has no tier breakpoints;
        # fall back to "Any roll" in that case so the user isn't locked
        # into a disabled control.
        if not tiers:
            self.mode_tier.setEnabled(False)
            if self.mode_tier.isChecked():
                self.mode_any.setChecked(True)
        else:
            self.mode_tier.setEnabled(True)
        self._refresh_preview()

    def _on_mode_changed(self) -> None:
        self.tier_spin.setEnabled(self.mode_tier.isChecked())
        self.value_spin.setEnabled(self.mode_value.isChecked())
        self._refresh_preview()

    def _refresh_preview(self) -> None:
        mod = self._current_mod()
        if not mod:
            self.preview.setText("—")
            return
        if self.mode_any.isChecked():
            threshold = "any roll matches"
            if mod.roll_p10 is not None and mod.roll_p90 is not None:
                threshold += f"  (observed p10-p90: {int(mod.roll_p10)}-{int(mod.roll_p90)})"
        elif self.mode_value.isChecked():
            threshold = f"value ≥ {self.value_spin.value()}"
        elif self.mode_tier.isChecked() and mod.tiers:
            t = self.tier_spin.value()
            min_val = mod.min_value_for_tier(t)
            if min_val is None:
                threshold = f"T{t} (no catalog entry)"
            else:
                threshold = f"T{t}+ → value ≥ {int(min_val)}"
        else:
            threshold = "any roll matches"
        god = "  ★ GOD MOD" if mod.god_mod else ""
        price = ""
        if mod.price_p50_div is not None:
            price = f"\nprice p50: {mod.price_p50_div:.1f}d"
            if mod.price_p90_div:
                price += f" · p90: {mod.price_p90_div:.1f}d"
        bases = ""
        if mod.bases_seen:
            bases = f"\nseen on: {', '.join(mod.bases_seen[:4])}" + ("…" if len(mod.bases_seen) > 4 else "")
        self.preview.setText(
            f"{mod.display_name}{god}  ·  {threshold}\n{mod.regex}{price}{bases}"
        )

    # --- rule operations ----------------------------------------------

    def _add_rule(self) -> None:
        mod = self._current_mod()
        if not mod:
            return
        rule: dict = {"mod_id": mod.id}
        if self.mode_value.isChecked():
            rule["min_value"] = self.value_spin.value()
        elif self.mode_tier.isChecked():
            rule["min_tier"] = self.tier_spin.value()
        # else: mode_any — no threshold, matches any roll
        self.rules.append(rule)
        self._refresh_rules_list()

    def _remove_selected(self) -> None:
        idx = self.rules_list.currentRow()
        if 0 <= idx < len(self.rules):
            del self.rules[idx]
            self._refresh_rules_list()

    def _refresh_rules_list(self) -> None:
        # Block itemChanged while we rebuild rows programmatically.
        self.rules_list.blockSignals(True)
        self.rules_list.clear()
        for rule in self.rules:
            if "mod_id" in rule:
                mod = self.mod_db.get_mod(rule["mod_id"])
                disp = mod.display_name if mod else rule["mod_id"]
                if "min_tier" in rule:
                    threshold = f"T{rule['min_tier']}+"
                elif "min_value" in rule:
                    threshold = f"value ≥ {rule['min_value']}"
                else:
                    threshold = "any roll"
                star = "★ " if (mod and mod.god_mod) else ""
                tag = "[REQ] " if rule.get("required") else ""
                label = f"{tag}{star}{disp}  ·  {threshold}"
            else:
                tag = "[REQ] " if rule.get("required") else ""
                label = f"{tag}[regex] {rule.get('name', rule.get('regex', '?'))}"
            item = QListWidgetItem(label)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                Qt.CheckState.Checked if rule.get("required") else Qt.CheckState.Unchecked
            )
            item.setToolTip("Checked = Required (must hit). Unchecked = counts toward the match-mode group.")
            self.rules_list.addItem(item)
        self.rules_list.blockSignals(False)

    def _on_rule_toggled(self, item: QListWidgetItem) -> None:
        idx = self.rules_list.row(item)
        if 0 <= idx < len(self.rules):
            self.rules[idx]["required"] = (item.checkState() == Qt.CheckState.Checked)
            # Rebuild to refresh the [REQ] prefix.
            self._refresh_rules_list()

    # --- save ----------------------------------------------------------

    def _on_save(self) -> None:
        self.result_targets = {
            "mode": "all_of" if self.mode_all.isChecked() else "any_of",
            "rules": self.rules,
        }
        self.on_save(self.result_targets)
        self.accept()
