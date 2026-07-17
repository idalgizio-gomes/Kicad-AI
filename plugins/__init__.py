from __future__ import annotations

import logging
import sys
from pathlib import Path

import pcbnew

try:
    import wx
except ImportError:
    print("Error: wx not available - plugin cannot run without wxPython")
    sys.exit(1)

plugin_dir = Path(__file__).resolve().parent
log_file = plugin_dir / "chat_assistant.log"


def _setup_logging() -> None:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s:%(filename)s:%(lineno)d]: %(message)s",
        filename=str(log_file),
        filemode="a",
    )


def _setup_submodule_paths() -> None:
    plugin_dir_str = str(plugin_dir)
    if plugin_dir_str not in sys.path:
        sys.path.insert(0, plugin_dir_str)


class ActionKiCadChatAssistant(pcbnew.ActionPlugin):
    """KiCad Action Plugin: AI chat assistant (Claude/ChatGPT/Gemini)."""

    def defaults(self) -> None:
        self.name = "KiCad Chat Assistant (fallback pcbnew)"
        self.category = "AI Assistant"
        self.description = "Chat com IA (Claude/ChatGPT/Gemini) com contexto do projeto e ações aprovadas"
        self.show_toolbar_button = True

        icon_path = plugin_dir / "icon.png"
        self.icon_file_name = str(icon_path)
        self.dark_icon_file_name = str(icon_path)

    def Run(self) -> None:
        _setup_logging()
        logging.info("KiCad Chat Assistant starting (fallback pcbnew)")

        try:
            _setup_submodule_paths()
            from .chat_action import run_chat

            run_chat()
        except Exception as e:
            logging.exception("Chat Assistant failed to start")
            try:
                app = wx.App() if not wx.GetApp() else None
                wx.MessageBox(
                    f"KiCad Chat Assistant error:\n\n{e}\n\nCheck log file: {log_file}",
                    "Error",
                    wx.OK | wx.ICON_ERROR,
                )
                if app:
                    app.Destroy()
            except Exception:
                print(f"Error: {e}")


ActionKiCadChatAssistant().register()
