import pyautogui
import re
import time
import os
import subprocess
import threading
import json
import platform
import shutil
import pywhatkit
import csv
import importlib
import random
import io
import contextlib
import webbrowser
from io import StringIO
from urllib.parse import quote_plus
from openpyxl import Workbook, load_workbook
from openpyxl.chart import LineChart, Reference
from openpyxl.utils import get_column_letter
from aiassistant.infra.config.app_config import CONFIG

try:
    Document = importlib.import_module("docx").Document
    DOCX_AVAILABLE = True
except ImportError:
    Document = None
    DOCX_AVAILABLE = False

try:
    requests = importlib.import_module("requests")
    REQUESTS_AVAILABLE = True
except ImportError:
    requests = None
    REQUESTS_AVAILABLE = False

try:
    BeautifulSoup = importlib.import_module("bs4").BeautifulSoup
    BS4_AVAILABLE = True
except ImportError:
    BeautifulSoup = None
    BS4_AVAILABLE = False

try:
    pyperclip = importlib.import_module("pyperclip")
    CLIPBOARD_AVAILABLE = True
except ImportError:
    pyperclip = None
    CLIPBOARD_AVAILABLE = False

try:
    Presentation = importlib.import_module("pptx").Presentation
    POWERPOINT_AVAILABLE = True
except ImportError:
    Presentation = None
    POWERPOINT_AVAILABLE = False

SYSTEM_NAME = platform.system().lower()
IS_WINDOWS = SYSTEM_NAME.startswith("win")

GENERAL_TASK_COMMANDS = [
    "open <app>",
    "close <app>",
    "play <topic>",
    "write <text>",
    "note <text>",
    "type <text>",
    "take a note <text>",
    "volume up",
    "volume down",
    "mute",
    "unmute",
    "search web <query>",
    "open website <url>",
    "browse <url>",
    "research <query>",
    "web research <query>",
    "research web <query>",
]

CLIPBOARD_COMMANDS = [
    "copy selected text",
    "copy now",
    "paste clipboard",
    "paste now",
    "save clipboard to rad as <key>",
]

SYSTEM_COMMANDS = [
    "system check",
    "check system",
    "malware scan",
    "scan for malware",
    "run malware scan",
    "security quick scan",
    "scan apps",
    "update apps",
]

OFFICE_HELP_COMMANDS = [
    "office help",
    "excel help",
    "word help",
    "powerpoint help",
]

EXCEL_HELP_COMMANDS = [
    "create an excel workbook with <rows>x<cols> table with random data and then create the graph from it",
    "excel random table <rows>x<cols> with graph [in <file>]",
    "excel demo table graph [in <file>]",
    "excel create <file>",
    "excel create sheet <sheet_name> in <file>",
    "excel list sheets in <file>",
    "excel set <cell> to <value> in <file> [sheet <sheet_name>]",
    "excel get <cell> in <file> [sheet <sheet_name>]",
    "excel add row <comma-separated values> in <file> [sheet <sheet_name>]",
    "excel delete row <number> in <file> [sheet <sheet_name>]",
    "excel delete column <A..Z> in <file> [sheet <sheet_name>]",
    "excel sum <A1:B10> in <file> to <cell> [sheet <sheet_name>]",
    "excel formula <cell> = <formula> in <file> [sheet <sheet_name>]",
]

WORD_HELP_COMMANDS = [
    "word create <file>",
    "word add heading <text> [level 1-6] in <file>",
    "word add paragraph <text> in <file>",
    "word read <file>",
]

POWERPOINT_HELP_COMMANDS = [
    "powerpoint create <file>",
    "powerpoint add slide title <title> content <content> in <file>",
    "powerpoint launch <file>",
]

ASSISTANT_JSON_ACTIONS = [
    '{"action":"open","target":"chrome"}',
    '{"action":"close","target":"notepad"}',
    '{"action":"search_web","target":"latest ai news"}',
    '{"action":"open_website","target":"github.com"}',
    '{"action":"volume","target":"up"}',
    '{"action":"write_note","target":"meeting summary"}',
    '{"action":"play","target":"lofi coding music"}',
]


def _open_with_default_app(path):
    try:
        if IS_WINDOWS and hasattr(os, "startfile"):
            os.startfile(path)
        elif SYSTEM_NAME == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
        return True
    except Exception as e:
        print(f"[ACTION] Could not open file with default app: {e}")
        return False


try:
    # Import AppOpener functions (best on Windows).
    from AppOpener import open as open_app
    from AppOpener import close as close_app
    from AppOpener import give_appnames
    APPOPENER_AVAILABLE = True
except Exception:
    open_app = None
    close_app = None
    give_appnames = None
    APPOPENER_AVAILABLE = False

class ExcelCommandHandler:
    def __init__(self):
        self.default_extension = ".xlsx"

    def _normalize_path(self, file_name):
        clean_name = file_name.strip().strip('"').strip("'")
        if not clean_name.lower().endswith(self.default_extension):
            clean_name += self.default_extension

        is_windows_absolute = re.match(r"^[a-zA-Z]:\\", clean_name) is not None
        if not os.path.isabs(clean_name) and not is_windows_absolute:
            clean_name = os.path.abspath(clean_name)

        parent = os.path.dirname(clean_name)
        if parent:
            os.makedirs(parent, exist_ok=True)
        return clean_name

    def _load_or_create_workbook(self, path):
        if os.path.exists(path):
            return load_workbook(path)
        return Workbook()

    def _pick_sheet(self, workbook, sheet_name):
        if not sheet_name:
            return workbook.active
        target_sheet = sheet_name.strip().strip('"').strip("'")
        if target_sheet in workbook.sheetnames:
            return workbook[target_sheet]
        return workbook.create_sheet(target_sheet)

    def _to_value(self, raw_value):
        value = raw_value.strip()
        if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
            return value[1:-1]
        if value.lower() == "true":
            return True
        if value.lower() == "false":
            return False
        if re.fullmatch(r"-?\d+", value):
            return int(value)
        try:
            return float(value)
        except ValueError:
            pass
        return value

    def _ensure_formula_prefix(self, formula_text):
        cleaned_formula = formula_text.strip()
        return cleaned_formula if cleaned_formula.startswith("=") else "=" + cleaned_formula

    def _create_random_table_with_chart(self, file_name, rows=10, cols=10):
        file_path = self._normalize_path(file_name)
        workbook = self._load_or_create_workbook(file_path)
        sheet = self._pick_sheet(workbook, "RandomData")

        # Reset sheet contents when regenerating demo data.
        if sheet.max_row > 0:
            sheet.delete_rows(1, sheet.max_row)
        if sheet.max_column > 0:
            sheet.delete_cols(1, sheet.max_column)
        sheet._charts = []

        for row in range(1, rows + 1):
            for col in range(1, cols + 1):
                sheet.cell(row=row, column=col, value=random.randint(10, 99))

        chart = LineChart()
        chart.title = f"Random Data {rows}x{cols}"
        chart.style = 10
        chart.y_axis.title = "Value"
        chart.x_axis.title = "Row"

        data_ref = Reference(sheet, min_col=1, min_row=1, max_col=cols, max_row=rows)
        category_ref = Reference(sheet, min_col=1, min_row=1, max_row=rows)
        chart.add_data(data_ref, titles_from_data=False)
        chart.set_categories(category_ref)

        chart_anchor = f"{get_column_letter(cols + 2)}2"
        sheet.add_chart(chart, chart_anchor)

        workbook.save(file_path)
        print(f"[ACTION][EXCEL] Random {rows}x{cols} table + chart created in {file_path}")

        try:
            if _open_with_default_app(file_path):
                print("[ACTION][EXCEL] Opened workbook in available spreadsheet software.")
            else:
                print("[ACTION][EXCEL] Workbook created, but opening the file failed.")
        except Exception as e:
            print(f"[ACTION][EXCEL] Workbook created but auto-open failed: {e}")

    def handle(self, text):
        random_nl_match = re.fullmatch(
            r"(?:create|make)\s+an?\s+excel\s+workbook\s+with\s+(\d+)x(\d+)\s+table\s+with\s+random\s+data\s+and\s+then\s+create\s+(?:the\s+)?graph\s+from\s+it(?:\s+in\s+available\s+software)?(?:\s+in\s+(.+?))?\s*$",
            text,
            flags=re.IGNORECASE,
        )
        if random_nl_match:
            rows, cols, file_name = random_nl_match.groups()
            rows = int(rows)
            cols = int(cols)
            if rows > 100 or cols > 50 or rows <= 0 or cols <= 0:
                print("[ACTION][EXCEL] Table size out of supported range. Use 1..100 rows and 1..50 columns.")
                return True
            target_file = file_name or "random_chart_demo.xlsx"
            self._create_random_table_with_chart(target_file, rows=rows, cols=cols)
            return True

        random_cmd_match = re.fullmatch(
            r"excel\s+random\s+table\s+(\d+)x(\d+)\s+with\s+graph(?:\s+in\s+(.+?))?\s*$",
            text,
            flags=re.IGNORECASE,
        )
        if random_cmd_match:
            rows, cols, file_name = random_cmd_match.groups()
            rows = int(rows)
            cols = int(cols)
            if rows > 100 or cols > 50 or rows <= 0 or cols <= 0:
                print("[ACTION][EXCEL] Table size out of supported range. Use 1..100 rows and 1..50 columns.")
                return True
            target_file = file_name or "random_chart_demo.xlsx"
            self._create_random_table_with_chart(target_file, rows=rows, cols=cols)
            return True

        if re.fullmatch(r"excel\s+demo\s+table\s+graph(?:\s+in\s+(.+?))?\s*$", text, flags=re.IGNORECASE):
            demo_file_match = re.fullmatch(r"excel\s+demo\s+table\s+graph(?:\s+in\s+(.+?))?\s*$", text, flags=re.IGNORECASE)
            target_file = demo_file_match.group(1) if demo_file_match else "random_chart_demo.xlsx"
            self._create_random_table_with_chart(target_file or "random_chart_demo.xlsx", rows=10, cols=10)
            return True

        open_match = re.fullmatch(r"excel\s+(?:create|open)\s+(.+?)\s*$", text, flags=re.IGNORECASE)
        if open_match:
            file_path = self._normalize_path(open_match.group(1))
            workbook = self._load_or_create_workbook(file_path)
            workbook.save(file_path)
            print(f"[ACTION][EXCEL] Ready: {file_path}")
            return True

        create_sheet_match = re.fullmatch(
            r"excel\s+create\s+sheet\s+(.+?)\s+in\s+(.+?)\s*$",
            text,
            flags=re.IGNORECASE,
        )
        if create_sheet_match:
            sheet_name, file_name = create_sheet_match.groups()
            file_path = self._normalize_path(file_name)
            workbook = self._load_or_create_workbook(file_path)
            target_sheet = sheet_name.strip().strip('"').strip("'")
            if target_sheet not in workbook.sheetnames:
                workbook.create_sheet(target_sheet)
                workbook.save(file_path)
                print(f"[ACTION][EXCEL] Sheet '{target_sheet}' created in {file_path}")
            else:
                print(f"[ACTION][EXCEL] Sheet '{target_sheet}' already exists in {file_path}")
            return True

        list_sheets_match = re.fullmatch(
            r"excel\s+list\s+sheets\s+in\s+(.+?)\s*$",
            text,
            flags=re.IGNORECASE,
        )
        if list_sheets_match:
            file_name = list_sheets_match.group(1)
            file_path = self._normalize_path(file_name)
            workbook = self._load_or_create_workbook(file_path)
            print(f"[ACTION][EXCEL] Sheets in {file_path}: {', '.join(workbook.sheetnames)}")
            return True

        set_match = re.fullmatch(
            r"excel\s+set\s+([a-zA-Z]+\d+)\s+to\s+(.+?)\s+in\s+(.+?)(?:\s+sheet\s+(.+))?",
            text,
            flags=re.IGNORECASE,
        )
        if set_match:
            cell, value_text, file_name, sheet_name = set_match.groups()
            file_path = self._normalize_path(file_name)
            workbook = self._load_or_create_workbook(file_path)
            sheet = self._pick_sheet(workbook, sheet_name)
            sheet[cell.upper()] = self._to_value(value_text)
            workbook.save(file_path)
            print(f"[ACTION][EXCEL] {cell.upper()} updated in {file_path}")
            return True

        get_match = re.fullmatch(
            r"excel\s+get\s+([a-zA-Z]+\d+)\s+in\s+(.+?)(?:\s+sheet\s+(.+))?",
            text,
            flags=re.IGNORECASE,
        )
        if get_match:
            cell, file_name, sheet_name = get_match.groups()
            file_path = self._normalize_path(file_name)
            workbook = self._load_or_create_workbook(file_path)
            sheet = self._pick_sheet(workbook, sheet_name)
            value = sheet[cell.upper()].value
            print(f"[ACTION][EXCEL] {cell.upper()} = {value}")
            return True

        row_match = re.fullmatch(
            r"excel\s+add\s+row\s+(.+?)\s+in\s+(.+?)(?:\s+sheet\s+(.+))?",
            text,
            flags=re.IGNORECASE,
        )
        if row_match:
            row_values_text, file_name, sheet_name = row_match.groups()
            file_path = self._normalize_path(file_name)
            workbook = self._load_or_create_workbook(file_path)
            sheet = self._pick_sheet(workbook, sheet_name)
            try:
                parsed_row = next(csv.reader(StringIO(row_values_text)), [])
            except csv.Error:
                print("[ACTION][EXCEL] Row input is malformed. Use comma-separated values, e.g. apple,10,done.")
                return True
            if not parsed_row or all(not item.strip() for item in parsed_row):
                print("[ACTION][EXCEL] No valid row values provided. Use comma-separated values.")
                return True
            sheet.append([self._to_value(item.strip()) for item in parsed_row])
            workbook.save(file_path)
            print(f"[ACTION][EXCEL] Row added in {file_path}")
            return True

        delete_row_match = re.fullmatch(
            r"excel\s+delete\s+row\s+(\d+)\s+in\s+(.+?)(?:\s+sheet\s+(.+))?",
            text,
            flags=re.IGNORECASE,
        )
        if delete_row_match:
            row_number, file_name, sheet_name = delete_row_match.groups()
            row_number = int(row_number)
            if row_number <= 0:
                print("[ACTION][EXCEL] Row number must be 1 or greater.")
                return True
            file_path = self._normalize_path(file_name)
            workbook = self._load_or_create_workbook(file_path)
            sheet = self._pick_sheet(workbook, sheet_name)
            sheet.delete_rows(row_number, 1)
            workbook.save(file_path)
            print(f"[ACTION][EXCEL] Row {row_number} deleted in {file_path}")
            return True

        delete_col_match = re.fullmatch(
            r"excel\s+delete\s+column\s+([a-zA-Z]+)\s+in\s+(.+?)(?:\s+sheet\s+(.+))?",
            text,
            flags=re.IGNORECASE,
        )
        if delete_col_match:
            col_letters, file_name, sheet_name = delete_col_match.groups()
            file_path = self._normalize_path(file_name)
            workbook = self._load_or_create_workbook(file_path)
            sheet = self._pick_sheet(workbook, sheet_name)
            col_letters = col_letters.upper()
            col_index = 0
            for ch in col_letters:
                col_index = col_index * 26 + (ord(ch) - ord("A") + 1)
            sheet.delete_cols(col_index, 1)
            workbook.save(file_path)
            print(f"[ACTION][EXCEL] Column {col_letters} deleted in {file_path}")
            return True

        sum_match = re.fullmatch(
            r"excel\s+sum\s+([a-zA-Z]+\d+:[a-zA-Z]+\d+)\s+in\s+(.+?)\s+to\s+([a-zA-Z]+\d+)(?:\s+sheet\s+(.+))?",
            text,
            flags=re.IGNORECASE,
        )
        if sum_match:
            source_range, file_name, target_cell, sheet_name = sum_match.groups()
            file_path = self._normalize_path(file_name)
            workbook = self._load_or_create_workbook(file_path)
            sheet = self._pick_sheet(workbook, sheet_name)
            sheet[target_cell.upper()] = self._ensure_formula_prefix(f"SUM({source_range.upper()})")
            workbook.save(file_path)
            print(f"[ACTION][EXCEL] Sum {source_range.upper()} -> {target_cell.upper()} in {file_path}")
            return True

        formula_match = re.fullmatch(
            r"excel\s+formula\s+([a-zA-Z]+\d+)\s*=\s*(.+?)\s+in\s+(.+?)(?:\s+sheet\s+(.+))?",
            text,
            flags=re.IGNORECASE,
        )
        if formula_match:
            target_cell, formula_text, file_name, sheet_name = formula_match.groups()
            file_path = self._normalize_path(file_name)
            workbook = self._load_or_create_workbook(file_path)
            sheet = self._pick_sheet(workbook, sheet_name)
            sheet[target_cell.upper()] = self._ensure_formula_prefix(formula_text)
            workbook.save(file_path)
            print(f"[ACTION][EXCEL] Formula set in {target_cell.upper()} for {file_path}")
            return True

        if re.fullmatch(r"excel\s+help", text, flags=re.IGNORECASE):
            print("[ACTION][EXCEL] Commands:")
            for command in EXCEL_HELP_COMMANDS:
                print(f"- {command}")
            return True

        return False


class WordCommandHandler:
    def __init__(self):
        self.default_extension = ".docx"

    def _normalize_path(self, file_name):
        clean_name = file_name.strip().strip('"').strip("'")
        if not clean_name.lower().endswith(self.default_extension):
            clean_name += self.default_extension

        is_windows_absolute = re.match(r"^[a-zA-Z]:\\", clean_name) is not None
        if not os.path.isabs(clean_name) and not is_windows_absolute:
            clean_name = os.path.abspath(clean_name)

        parent = os.path.dirname(clean_name)
        if parent:
            os.makedirs(parent, exist_ok=True)
        return clean_name

    def _load_or_create_doc(self, path):
        if not DOCX_AVAILABLE:
            return None
        if os.path.exists(path):
            return Document(path)
        return Document()

    def handle(self, text):
        if not DOCX_AVAILABLE:
            if text.lower().startswith("word "):
                print("[ACTION][WORD] python-docx is not installed. Run: pip install python-docx")
                return True
            return False

        create_match = re.fullmatch(r"word\s+(?:create|open)\s+(.+?)\s*$", text, flags=re.IGNORECASE)
        if create_match:
            file_path = self._normalize_path(create_match.group(1))
            doc = self._load_or_create_doc(file_path)
            doc.save(file_path)
            print(f"[ACTION][WORD] Ready: {file_path}")
            return True

        paragraph_match = re.fullmatch(
            r"word\s+add\s+paragraph\s+(.+?)\s+in\s+(.+?)\s*$",
            text,
            flags=re.IGNORECASE,
        )
        if paragraph_match:
            paragraph_text, file_name = paragraph_match.groups()
            file_path = self._normalize_path(file_name)
            doc = self._load_or_create_doc(file_path)
            content = paragraph_text.strip().strip('"').strip("'")
            doc.add_paragraph(content)
            doc.save(file_path)
            print(f"[ACTION][WORD] Paragraph added in {file_path}")
            return True

        heading_match = re.fullmatch(
            r"word\s+add\s+heading\s+(.+?)(?:\s+level\s+([1-6]))?\s+in\s+(.+?)\s*$",
            text,
            flags=re.IGNORECASE,
        )
        if heading_match:
            heading_text, level_text, file_name = heading_match.groups()
            level = int(level_text) if level_text else 1
            file_path = self._normalize_path(file_name)
            doc = self._load_or_create_doc(file_path)
            content = heading_text.strip().strip('"').strip("'")
            doc.add_heading(content, level=level)
            doc.save(file_path)
            print(f"[ACTION][WORD] Heading (level {level}) added in {file_path}")
            return True

        read_match = re.fullmatch(r"word\s+read\s+(.+?)\s*$", text, flags=re.IGNORECASE)
        if read_match:
            file_path = self._normalize_path(read_match.group(1))
            if not os.path.exists(file_path):
                print(f"[ACTION][WORD] File not found: {file_path}")
                return True
            doc = self._load_or_create_doc(file_path)
            paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
            if not paragraphs:
                print("[ACTION][WORD] Document is empty.")
            else:
                preview = " | ".join(paragraphs[:8])
                print(f"[ACTION][WORD] Preview: {preview}")
            return True

        if re.fullmatch(r"word\s+help", text, flags=re.IGNORECASE):
            print("[ACTION][WORD] Commands:")
            for command in WORD_HELP_COMMANDS:
                print(f"- {command}")
            return True

        return False


class PowerPointCommandHandler:
    def __init__(self):
        self.default_extension = ".pptx"

    def _normalize_path(self, file_name):
        clean_name = file_name.strip().strip('"').strip("'")
        if not clean_name.lower().endswith(self.default_extension):
            clean_name += self.default_extension

        is_windows_absolute = re.match(r"^[a-zA-Z]:\\", clean_name) is not None
        if not os.path.isabs(clean_name) and not is_windows_absolute:
            clean_name = os.path.abspath(clean_name)

        parent = os.path.dirname(clean_name)
        if parent:
            os.makedirs(parent, exist_ok=True)
        return clean_name

    def _load_or_create_presentation(self, path):
        if not POWERPOINT_AVAILABLE:
            return None
        if os.path.exists(path):
            return Presentation(path)
        return Presentation()

    def handle(self, text):
        if not POWERPOINT_AVAILABLE:
            if text.lower().startswith("powerpoint "):
                print("[ACTION][PPT] python-pptx is not installed. Run: pip install python-pptx")
                return True
            return False

        create_match = re.fullmatch(r"powerpoint\s+(?:create|open)\s+(.+?)\s*$", text, flags=re.IGNORECASE)
        if create_match:
            file_path = self._normalize_path(create_match.group(1))
            prs = self._load_or_create_presentation(file_path)
            prs.save(file_path)
            print(f"[ACTION][PPT] Ready: {file_path}")
            return True

        slide_match = re.fullmatch(
            r"powerpoint\s+add\s+slide\s+title\s+(.+?)\s+content\s+(.+?)\s+in\s+(.+?)\s*$",
            text,
            flags=re.IGNORECASE,
        )
        if slide_match:
            title_text, content_text, file_name = slide_match.groups()
            file_path = self._normalize_path(file_name)
            prs = self._load_or_create_presentation(file_path)
            layout = prs.slide_layouts[1] if len(prs.slide_layouts) > 1 else prs.slide_layouts[0]
            slide = prs.slides.add_slide(layout)

            title_box = getattr(slide.shapes, "title", None)
            if title_box:
                title_box.text = title_text.strip().strip('"').strip("'")

            if len(slide.placeholders) > 1:
                slide.placeholders[1].text = content_text.strip().strip('"').strip("'")

            prs.save(file_path)
            print(f"[ACTION][PPT] Slide added in {file_path}")
            return True

        launch_match = re.fullmatch(r"powerpoint\s+launch\s+(.+?)\s*$", text, flags=re.IGNORECASE)
        if launch_match:
            file_path = self._normalize_path(launch_match.group(1))
            if not os.path.exists(file_path):
                print(f"[ACTION][PPT] File not found: {file_path}")
                return True
            try:
                if _open_with_default_app(file_path):
                    print(f"[ACTION][PPT] Opened {file_path}")
                else:
                    print(f"[ACTION][PPT] Failed to open {file_path}")
            except Exception as e:
                print(f"[ACTION][PPT] Failed to open: {e}")
            return True

        if re.fullmatch(r"powerpoint\s+help", text, flags=re.IGNORECASE):
            print("[ACTION][PPT] Commands:")
            for command in POWERPOINT_HELP_COMMANDS:
                print(f"- {command}")
            return True

        return False

class ActionHandler:
    @staticmethod
    def get_supported_command_sections():
        return {
            "General task commands": list(GENERAL_TASK_COMMANDS),
            "Clipboard commands": list(CLIPBOARD_COMMANDS),
            "System commands": list(SYSTEM_COMMANDS),
            "Office quick help": list(OFFICE_HELP_COMMANDS),
            "Excel commands": list(EXCEL_HELP_COMMANDS),
            "Word commands": list(WORD_HELP_COMMANDS),
            "PowerPoint commands": list(POWERPOINT_HELP_COMMANDS),
            "Assistant JSON command format": list(ASSISTANT_JSON_ACTIONS),
        }

    def __init__(self, db=None, context_provider=None):
        # 1. CUSTOM APPS / GAMES
        # Add games or portable apps here that the scanner misses.
        # Use double backslashes \\ for paths.
        self.custom_apps = {
            "genshin": r"C:\Program Files\Genshin Impact\Genshin Impact Game\GenshinImpact.exe",
            "minecraft": r"C:\XboxGames\Minecraft Launcher\Content\Minecraft.exe",
            "steam": r"C:\Program Files (x86)\Steam\steam.exe",
            "obs": r"C:\Program Files\obs-studio\bin\64bit\obs64.exe"
        }
        self.db = db
        self.context_provider = context_provider
        self.excel = ExcelCommandHandler()
        self.word = WordCommandHandler()
        self.powerpoint = PowerPointCommandHandler()
        action_cfg = CONFIG.get("actions", {})
        self.safe_mode = bool(action_cfg.get("safe_mode", True))
        self.allow_legacy_text_commands = bool(action_cfg.get("allow_legacy_text_commands", True))

    def execute_and_collect(self, text):
        if not text:
            return ""

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            try:
                self.execute(text)
            except Exception as e:
                print(f"[ACTION ERROR] {e}")
        return buffer.getvalue().strip()

    def _looks_like_action_command(self, text):
        if not text:
            return False
        lowered = text.strip().lower()
        prefixes = (
            "open ",
            "close ",
            "play ",
            "write ",
            "note ",
            "type ",
            "take a note ",
            "search web ",
            "open website ",
            "browse ",
            "excel ",
            "word ",
            "powerpoint ",
            "research ",
            "web research ",
            "system check",
            "check system",
            "scan apps",
            "update apps",
            "copy selected text",
            "copy now",
            "paste clipboard",
            "paste now",
            "save clipboard to rad as ",
            "malware scan",
            "scan for malware",
            "run malware scan",
            "security quick scan",
        )
        if lowered.startswith(prefixes):
            return True
        return any(token in lowered for token in ("volume up", "volume down", "mute", "unmute"))

    def _print_system_check(self):
        checks = {
            "Python": True,
            "Reasoning Server Module": importlib.util.find_spec("aiassistant.backend.server_reasoning") is not None,
            "Voice Server Module": importlib.util.find_spec("aiassistant.backend.server_voice") is not None,
            "Piper Folder": os.path.isdir("piper"),
            "Models Folder": os.path.isdir("models"),
            "RVC Models Folder": os.path.isdir("rvc_models"),
            "Internet Library (requests)": REQUESTS_AVAILABLE,
            "Clipboard Library (pyperclip)": CLIPBOARD_AVAILABLE,
        }

        print("[ACTION][SYSTEM] Quick Check:")
        for name, is_ok in checks.items():
            state = "OK" if is_ok else "MISSING"
            print(f"- {name}: {state}")

    def _run_quick_malware_scan(self):
        print("[ACTION][SECURITY] Starting Windows Defender quick scan...")
        try:
            subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    "Start-MpScan -ScanType QuickScan",
                ],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            print("[ACTION][SECURITY] Quick scan request sent to Windows Defender.")
        except Exception as e:
            print(f"[ACTION][SECURITY] Could not start Defender scan: {e}")

    def _open_website(self, target):
        url = target.strip().strip('"').strip("'")
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        webbrowser.open(url)
        print(f"[ACTION][WEB] Opened {url}")

    def _extract_key_points(self, text, max_points=5):
        if not text:
            return []

        chunks = re.split(r"(?<=[.!?])\s+", text)
        points = []
        for chunk in chunks:
            cleaned = re.sub(r"\s+", " ", chunk).strip(" -\n\t")
            if len(cleaned) < 30:
                continue
            if cleaned in points:
                continue
            points.append(cleaned)
            if len(points) >= max_points:
                break
        return points

    def _store_web_points_to_rad(self, query, points, source_url=""):
        if not self.db or not hasattr(self.db, "add_rad_data_if_new"):
            return 0

        slug = re.sub(r"[^a-z0-9]+", "_", query.lower()).strip("_")[:32] or "query"
        stored_count = 0

        for idx, point in enumerate(points, start=1):
            key = f"web_{slug}_{idx}"
            if self.db.add_rad_data_if_new("web_fact", key, point, 0.78):
                stored_count += 1

        if source_url:
            if self.db.add_rad_data_if_new("web_source", f"source_{slug}", source_url, 0.95):
                stored_count += 1

        return stored_count

    def _fetch_web_text(self, url):
        if not REQUESTS_AVAILABLE:
            return ""

        response = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; MARIE/1.0)"},
            timeout=12,
        )
        response.raise_for_status()

        html_text = response.text
        if BS4_AVAILABLE:
            soup = BeautifulSoup(html_text, "html.parser")
            paragraphs = [p.get_text(" ", strip=True) for p in soup.find_all("p")]
            joined = " ".join(paragraphs)
            return re.sub(r"\s+", " ", joined).strip()

        no_scripts = re.sub(r"<script[\s\S]*?</script>", " ", html_text, flags=re.IGNORECASE)
        no_styles = re.sub(r"<style[\s\S]*?</style>", " ", no_scripts, flags=re.IGNORECASE)
        plain = re.sub(r"<[^>]+>", " ", no_styles)
        return re.sub(r"\s+", " ", plain).strip()

    def _research_and_store_web_data(self, query):
        if not REQUESTS_AVAILABLE:
            print("[ACTION][WEB] requests is not installed. Run: pip install requests")
            return

        query = query.strip()
        if not query:
            print("[ACTION][WEB] Missing query. Example: web research latest AI trends")
            return

        source_url = ""
        combined_text = ""

        try:
            if query.startswith(("http://", "https://")):
                source_url = query
                combined_text = self._fetch_web_text(source_url)
            else:
                ddg_url = "https://api.duckduckgo.com/"
                response = requests.get(
                    ddg_url,
                    params={
                        "q": query,
                        "format": "json",
                        "no_html": 1,
                        "skip_disambig": 1,
                    },
                    timeout=10,
                )
                response.raise_for_status()
                data = response.json()

                abstract = data.get("AbstractText", "")
                source_url = data.get("AbstractURL", "")
                related_lines = []
                for item in data.get("RelatedTopics", [])[:10]:
                    if isinstance(item, dict) and item.get("Text"):
                        related_lines.append(item["Text"])
                    elif isinstance(item, dict) and item.get("Topics"):
                        for sub in item.get("Topics", [])[:5]:
                            if isinstance(sub, dict) and sub.get("Text"):
                                related_lines.append(sub["Text"])

                combined_text = " ".join([abstract] + related_lines)

                if source_url:
                    fetched_page_text = self._fetch_web_text(source_url)
                    if fetched_page_text:
                        combined_text = f"{combined_text} {fetched_page_text}"

            points = self._extract_key_points(combined_text, max_points=6)
            if not points:
                print("[ACTION][WEB] Could not extract useful points from the web result.")
                return

            print(f"[ACTION][WEB] Key points for '{query}':")
            for idx, point in enumerate(points, start=1):
                print(f"{idx}. {point}")

            stored_count = self._store_web_points_to_rad(query, points, source_url)
            if stored_count > 0:
                print(f"[ACTION][RAD] Stored {stored_count} web-derived memory items.")

            if CLIPBOARD_AVAILABLE:
                pyperclip.copy("\n".join(points))
                print("[ACTION][WEB] Copied key points to clipboard.")
        except Exception as e:
            print(f"[ACTION][WEB] Research failed: {e}")

    def _open_text_editor(self):
        try:
            if IS_WINDOWS:
                os.system("start notepad")
                return
            if SYSTEM_NAME == "darwin":
                subprocess.Popen(["open", "-a", "TextEdit"])
                return

            for editor in ("gedit", "kate", "mousepad", "xed"):
                if shutil.which(editor):
                    subprocess.Popen([editor])
                    return
        except Exception as e:
            print(f"[ACTION] Could not open text editor: {e}")

    def _open_app_name(self, app_name):
        print(f"[ACTION] Opening: '{app_name}'")

        for key, path in self.custom_apps.items():
            if key in app_name:
                print(f"[ACTION] Found custom path for {key}")
                try:
                    if _open_with_default_app(path):
                        return True
                except Exception as e:
                    print(f"[ERROR] Custom path failed: {e}")

        if APPOPENER_AVAILABLE and IS_WINDOWS:
            try:
                open_app(app_name, match_closest=True, output=False, throw_error=True)
                return True
            except Exception:
                pass

        try:
            if IS_WINDOWS:
                os.system(f"start {app_name}")
            elif SYSTEM_NAME == "darwin":
                subprocess.Popen(["open", "-a", app_name])
            else:
                subprocess.Popen([app_name])
            return True
        except Exception:
            print(f"[ERROR] Could not open '{app_name}'")
            return False

    def _close_app_name(self, app_name):
        if APPOPENER_AVAILABLE and IS_WINDOWS:
            try:
                close_app(app_name, match_closest=True, output=False, throw_error=True)
                return True
            except Exception:
                pass

        try:
            if IS_WINDOWS:
                subprocess.run(["taskkill", "/f", "/im", f"{app_name}.exe"], check=False)
            else:
                subprocess.run(["pkill", "-f", app_name], check=False)
            return True
        except Exception:
            print(f"[ERROR] Could not close '{app_name}'")
            return False

    def _extract_json_action(self, text):
        if not text:
            return None

        candidates = []
        stripped = text.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            candidates.append(stripped)

        fenced = re.findall(r"```json\s*(\{[\s\S]*?\})\s*```", text, flags=re.IGNORECASE)
        candidates.extend(fenced)

        loose = re.findall(r"(\{\s*\"action\"[\s\S]*?\})", text)
        candidates.extend(loose)

        for candidate in candidates:
            try:
                obj = json.loads(candidate)
            except Exception:
                continue

            if not isinstance(obj, dict):
                continue

            action = str(obj.get("action", "")).strip().lower()
            target = str(obj.get("target", "")).strip()
            value = obj.get("value", "")
            if action in {"open", "close", "search_web", "open_website", "volume", "write_note", "play"}:
                return {"action": action, "target": target, "value": value}

        return None

    def _execute_json_action(self, action_obj):
        action = action_obj.get("action", "")
        target = (action_obj.get("target") or "").strip()
        value = action_obj.get("value", "")

        if action == "open":
            return self._open_app_name(re.sub(r"[^\w\s]", "", target.lower()))

        if action == "close":
            return self._close_app_name(re.sub(r"[^\w\s]", "", target.lower()))

        if action == "search_web":
            if not target:
                return False
            search_url = f"https://duckduckgo.com/?q={quote_plus(target)}"
            webbrowser.open(search_url)
            print(f"[ACTION][WEB] Searching web for: {target}")
            return True

        if action == "open_website":
            if not target:
                return False
            self._open_website(target)
            return True

        if action == "volume":
            value_text = f"{target} {value}".lower()
            if "up" in value_text:
                pyautogui.press("volumeup")
                return True
            if "down" in value_text:
                pyautogui.press("volumedown")
                return True
            if "mute" in value_text or "unmute" in value_text:
                pyautogui.press("volumemute")
                return True
            return False

        if action == "write_note":
            content = target or str(value)
            if not content:
                return False
            self._open_text_editor()
            time.sleep(1.0)
            pyautogui.write(content, interval=0.05)
            print(f"[ACTION] Writing to editor: {content}")
            return True

        if action == "play":
            if not target:
                return False
            pywhatkit.playonyt(target)
            print(f"[ACTION] Playing on YouTube: {target}")
            return True

        return False

    def execute_from_assistant(self, text):
        action_obj = self._extract_json_action(text)
        if not action_obj:
            print("[ACTION][SAFE] Assistant output has no valid JSON action. Ignored.")
            return False
        return self._execute_json_action(action_obj)

    def execute(self, text):
        if not text: return
        raw_text = text.strip()
        text = raw_text.lower()

        structured_action = self._extract_json_action(raw_text)
        if structured_action:
            self._execute_json_action(structured_action)
            return

        # =========================================================
        # EXCEL COMMANDS
        # =========================================================
        if self.excel.handle(raw_text):
            return

        # =========================================================
        # WORD / POWERPOINT COMMANDS
        # =========================================================
        if text.startswith("word ") and self.word.handle(raw_text):
            return

        if text.startswith("powerpoint ") and self.powerpoint.handle(raw_text):
            return

        if text == "office help":
            self.excel.handle("excel help")
            self.word.handle("word help")
            self.powerpoint.handle("powerpoint help")
            return

        # =========================================================
        # WEB + STUDY + CLIPBOARD
        # =========================================================
        web_research_match = re.fullmatch(r"(?:web\s+research|research\s+web|research)\s+(.+)", raw_text, flags=re.IGNORECASE)
        if web_research_match:
            query = web_research_match.group(1)
            self._research_and_store_web_data(query)
            return

        open_web_match = re.fullmatch(r"(?:open\s+website|browse)\s+(.+)", raw_text, flags=re.IGNORECASE)
        if open_web_match:
            self._open_website(open_web_match.group(1))
            return

        search_web_match = re.fullmatch(r"search\s+web\s+(.+)", raw_text, flags=re.IGNORECASE)
        if search_web_match:
            query = search_web_match.group(1).strip()
            search_url = f"https://duckduckgo.com/?q={quote_plus(query)}"
            webbrowser.open(search_url)
            print(f"[ACTION][WEB] Searching web for: {query}")
            return

        if re.fullmatch(r"(?:copy\s+selected\s+text|copy\s+now)", text):
            pyautogui.hotkey("ctrl", "c")
            time.sleep(0.2)
            if CLIPBOARD_AVAILABLE:
                snippet = pyperclip.paste().strip()
                preview = snippet[:120] + ("..." if len(snippet) > 120 else "")
                print(f"[ACTION][CLIPBOARD] Copied: {preview}")
            else:
                print("[ACTION][CLIPBOARD] Copied selection (clipboard preview unavailable).")
            return

        if re.fullmatch(r"(?:paste\s+clipboard|paste\s+now)", text):
            pyautogui.hotkey("ctrl", "v")
            print("[ACTION][CLIPBOARD] Pasted clipboard into active software.")
            return

        save_clip_match = re.fullmatch(r"save\s+clipboard\s+to\s+rad\s+as\s+(.+)", raw_text, flags=re.IGNORECASE)
        if save_clip_match:
            if not CLIPBOARD_AVAILABLE:
                print("[ACTION][RAD] pyperclip not installed. Run: pip install pyperclip")
                return
            if not self.db or not hasattr(self.db, "add_rad_data_if_new"):
                print("[ACTION][RAD] Database connection unavailable for clipboard save.")
                return

            key_name = save_clip_match.group(1).strip().strip('"').strip("'")
            clip_text = pyperclip.paste().strip()
            if not clip_text:
                print("[ACTION][RAD] Clipboard is empty.")
                return

            stored = self.db.add_rad_data_if_new("clipboard_note", key_name, clip_text, 0.84)
            if stored:
                print(f"[ACTION][RAD] Clipboard saved under key '{key_name}'.")
            else:
                print(f"[ACTION][RAD] Duplicate clipboard note ignored for key '{key_name}'.")
            return

        # =========================================================
        # SYSTEM CHECKING / OPTIONAL SECURITY
        # =========================================================
        if re.fullmatch(r"(?:system check|check system)", text):
            self._print_system_check()
            return

        if re.fullmatch(r"(?:malware scan|scan for malware|run malware scan|security quick scan)", text):
            threading.Thread(target=self._run_quick_malware_scan, daemon=True).start()
            return

        # =========================================================
        # 0. SPECIAL COMMAND: UPDATE APP LIST
        # =========================================================
        if "scan apps" in text or "update apps" in text:
            print("[ACTION] Scanning for new apps...")
            if APPOPENER_AVAILABLE and give_appnames:
                # Run this in a thread so it doesn't freeze MARIE
                threading.Thread(target=give_appnames, daemon=True).start()
            else:
                print("[ACTION] App scanning is only available when AppOpener is installed.")
            return

        # =========================================================
        # 1. YOUTUBE (Play Video)
        # =========================================================
        if text.startswith("play "):
            video_topic = text.replace("play ", "").replace("please", "").strip()
            print(f"[ACTION] Playing on YouTube: {video_topic}")
            try:
                pywhatkit.playonyt(video_topic)
            except Exception as e:
                print(f"[ERROR] YouTube failed: {e}")
            return

        # =========================================================
        # 2. NOTEPAD (Write text)
        # =========================================================
        write_triggers = ["write ", "note ", "type ", "take a note "]
        triggered_word = next((w for w in write_triggers if text.startswith(w)), None)

        if triggered_word:
            content = text.replace(triggered_word, "").strip()
            print(f"[ACTION] Writing to editor: {content}")
            self._open_text_editor()
            time.sleep(1.0)
            pyautogui.write(content, interval=0.05)
            return

        # =========================================================
        # 3. SYSTEM CONTROLS
        # =========================================================
        if "volume up" in text:
            pyautogui.press('volumeup')
            return
        elif "volume down" in text:
            pyautogui.press('volumedown')
            return
        elif "mute" in text or "unmute" in text:
            pyautogui.press('volumemute')
            return

        # =========================================================
        # 4. OPEN APPS (Custom + General)
        # =========================================================
        if text.startswith("open "):
            raw_name = text.replace("open ", "").strip()
            app_name = raw_name.replace("please", "").replace("now", "").strip()
            app_name = re.sub(r'[^\w\s]', '', app_name)

            self._open_app_name(app_name)
            return

        # =========================================================
        # 5. CLOSE APPS
        # =========================================================
        if text.startswith("close "):
            app_name = text.replace("close ", "").replace("please", "").strip()
            app_name = re.sub(r'[^\w\s]', '', app_name)

            self._close_app_name(app_name)
            return

        if self._looks_like_action_command(raw_text):
            print("[ACTION] No command matched. Run 'office help' for office commands.")
